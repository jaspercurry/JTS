# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reconcile the JTS final-output DAC role with the ALSA hardware present.

The output-hardware counterpart to ``jasper-aec-reconcile``: one pass at
install/boot, and again from udev on sound-card add/remove/change. Idempotent.

``deploy/bin/jasper-audio-hardware-reconcile`` is the shim in front of this
module. It keeps ``--changed`` (and the stamp that answers it) interpreter-free
because that predicate runs as the unit's ``ExecCondition=`` (ADR-0226 rule 2);
every other verb is this one process.

Roles, and what each one owns:

- registered single DAC present -> ``outputd_dac`` targets it, Apple mixer
  helpers disabled
- Apple USB-C dongle present -> ``outputd_dac`` targets Apple, Apple helpers
  enabled
- a declared dual-Apple composite -> the paired sink, once its child order and
  the live graph both prove out
- no recognized output DAC -> Apple helpers disabled, an unknown/fallback id
  recorded, and the output units parked

Fail-closed everywhere it decides: a probe that cannot answer preserves what
the box already runs rather than committing a guess.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from jasper.atomic_io import EnvKeyAction as EnvAction, locked_upsert_env_file
from jasper.audio_hardware import reconcile_boot as boot
from jasper.audio_hardware import reconcile_env_files as env_files
from jasper.audio_hardware import reconcile_hardware as hardware
from jasper.audio_hardware import reconcile_outputd_lane as outputd_lane
from jasper.audio_hardware import reconcile_render as render
from jasper.audio_hardware import reconcile_units as units
from jasper.audio_hardware.config_txt import boot_config_path
from jasper.audio_hardware.i2s_hat import i2s_hat_intent_path
from jasper.audio_hardware.output_probe import DEFAULT_PROC_ASOUND_PATH
from jasper.audio_hardware.reconcile_common import (
    ENV_DIR_MODE,
    ENV_FILE_MODE,
    _Abort,
    _log_token,
)
from jasper.audio_hardware.reconcile_inputs import publish_reconcile_inputs
from jasper.audio_hardware.usb_port_role import DEFAULT_MODEL_PATH
from jasper.usbgadget import DEFAULT_UDC_CLASS_DIR
from jasper.env_load import BASE_ENV_PATH, FANIN_ENV_PATH, OUTPUTD_ENV_PATH
from jasper.log_event import log_event
from jasper.logging_setup import configure_logging
from jasper.paths import (
    OUTPUT_TOPOLOGY_PATH as DEFAULT_TOPOLOGY_PATH,
    camilla_statefile,
    crossover_statefile,
)
from jasper.output_hardware import (
    ObservedOutput,
    degraded_marker_path,
    state_path,
)
from jasper.shell_env import render_shell_assignments

logger = logging.getLogger(__name__)

EVENT = "audio_hardware_reconcile"

_SIGNAL_EXITS = {signal.SIGTERM: (143, "TERM"), signal.SIGHUP: (129, "HUP"),
                 signal.SIGINT: (130, "INT")}


def _resolve_asound_render_lib() -> str:
    """The shared ALSA template renderer, checkout sibling first.

    install.sh runs a ``--print-env`` pass from the rsynced checkout BEFORE
    ``/usr/local/lib`` is refreshed, so installed-first could pair new code
    with a stale library; the installed tree has no such sibling and falls
    through.
    """
    override = os.environ.get("JASPER_ASOUND_RENDER_LIB")
    if override:
        return override
    sibling = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "lib"
        / "jasper-asound-render.sh"
    )
    if os.access(sibling, os.R_OK):
        return str(sibling)
    return "/usr/local/lib/jasper/jasper-asound-render.sh"


class Pass:
    """One reconcile pass over the box's owned output-hardware state.

    Holds the pass's state, the plumbing its steps share and the order they
    run in; each step lives in the ``reconcile_*`` module of its concern.
    """

    def __init__(self, *, reason: str, print_env: bool, no_restart: bool) -> None:
        env = os.environ
        self.reason = reason
        self.print_env = print_env
        self.no_restart = no_restart
        self.signalled = ""
        self.proc_asound = env.get("JASPER_PROC_ASOUND", DEFAULT_PROC_ASOUND_PATH)
        self.model_path = env.get("JASPER_PI_MODEL_FILE", DEFAULT_MODEL_PATH)
        self.boot_config_path = boot_config_path()
        self.udc_class_dir = env.get("JASPER_UDC_CLASS_DIR", DEFAULT_UDC_CLASS_DIR)

        self.env_file = env.get("JASPER_ENV_FILE") or BASE_ENV_PATH
        self.outputd_env_file = env.get("JASPER_OUTPUTD_ENV_FILE") or OUTPUTD_ENV_PATH
        self.outputd_env_stage: str | None = None
        # The asound-template render candidate, from mkstemp until os.replace
        # consumes it. Recorded so main()'s cleanup sweeps one a signal left.
        self.asound_template_temp: str | None = None
        self.outputd_env_stage_rejected = False
        self.outputd_env_stage_hold = ExitStack()
        self.fanin_env_file = env.get("JASPER_FANIN_ENV_FILE") or FANIN_ENV_PATH
        self.asound_source_template = (
            env.get("JASPER_ASOUND_SOURCE_TEMPLATE")
            or "/etc/jasper/asoundrc.jasper.source"
        )
        self.asound_template = (
            env.get("JASPER_ASOUND_TEMPLATE") or "/etc/jasper/asoundrc.jasper.template"
        )
        self.render_asound_conf = (
            env.get("JASPER_RENDER_ASOUND_CONF")
            or "/usr/local/sbin/jasper-render-asound-conf"
        )
        self.asound_render_lib = _resolve_asound_render_lib()
        self.systemctl = env.get("JASPER_SYSTEMCTL") or "systemctl"
        self.state_path = str(state_path())
        self.degraded_marker = degraded_marker_path()
        self.management_transport_marker = (
            self.degraded_marker.parent / "management-transport.ok"
        )
        self.i2s_hat_intent_file = str(i2s_hat_intent_path())
        self.i2s_hat_reboot_required_path = (
            env.get("JASPER_I2S_HAT_REBOOT_REQUIRED_PATH")
            or "/run/jasper-output-hardware/i2s-hat-reboot-required"
        )
        self.install_profile_file = (
            env.get("JASPER_INSTALL_PROFILE_FILE") or "/var/lib/jasper/install_profile"
        )
        self.output_topology_path = (
            env.get("JASPER_OUTPUT_TOPOLOGY_PATH") or DEFAULT_TOPOLOGY_PATH
        )
        self._topology: Any | None = None
        self._topology_read = False
        self.camilla_statefile = str(camilla_statefile())
        self.camilla2_statefile = str(crossover_statefile())
        self.camilla_conf_dir = env.get("JASPER_CAMILLA_CONF_DIR") or "/etc/camilladsp"
        self.ring_conf_d = env.get("JASPER_RING_CONF_D") or ""

        self.apple_dongle_present = False
        self.apple_dongle_service_card = hardware.APPLE_SERVICE_CARD_AUTO
        self.dongle_card = "A"
        self.output_dac_card = "A"
        self.output_dac_id = "unknown"
        self.output_dac_recognized = False
        self.outputd_active_mode = False
        self.outputd_active_channels = ""
        # Set when this pass WROTE a record naming a different profile or card
        # than the one it replaced. The mixer pin reads that record, so it is
        # the one thing that has to re-run it.
        self.record_changed = False
        self.observed = ObservedOutput()
        self.dual_apple_dac_a_pcm = ""
        self.dual_apple_dac_b_pcm = ""
        self.dual_apple_order_source = ""
        # The endpoint device an accepted composite graph reports. Empty means
        # NO ring marker (the pair below fails closed by positive equality).
        self.dual_apple_active_endpoint_device = ""
        self.i2s_hat_desired_profile = ""
        # None until reconcile_i2s_hat_boot decides; then whether THIS pass
        # moved the managed boot block.
        self.i2s_hat_boot_changed: bool | None = None
        self.i2s_hat_apply_error = False
        self.latency_floor_changed = False
        self.route_fanin_changed = False

    # -- logging ------------------------------------------------------------

    def log(self, name: str, **fields: Any) -> None:
        # `pass_reason=` (this run's own --reason, why the PASS happened) is a
        # different key from a caller's own `reason=` (why THIS event
        # happened), so both can appear on one line without colliding.
        log_event(
            logger,
            f"{EVENT}.{name}",
            fields={"pass_reason": self.reason, **fields},
        )

    def mark_degraded(self) -> None:
        """A probe this pass could not run left an owned value unwritten, so
        the state it produced is not one a later ``--changed`` may skip
        against. A marker file, read by the shim's stamp writer."""
        if self.print_env:
            # --print-env promises no mutations (install.sh evals it
            # mid-install) and writes no stamp, so there is nothing here for a
            # marker to invalidate.
            return
        try:
            self.degraded_marker.parent.mkdir(parents=True, exist_ok=True)
            self.degraded_marker.touch()
        except OSError:
            pass

    # -- env files ----------------------------------------------------------

    def set_env_file_var(self, path: str, actions: Sequence[EnvAction]) -> bool:
        """Apply one stage's whole intent for one file, or fail the pass.

        A refused lock writes NOTHING, and the callers' ``changed`` idiom would
        read that as "changed" and restart jasper-outputd onto the old
        lane/PCM/format. A write that did not happen fails the pass instead.
        """
        changed = self.try_set_env_file_var(path, actions)
        if changed is None:
            raise _Abort(1)
        return changed

    def try_set_env_file_var(
        self, path: str, actions: Sequence[EnvAction]
    ) -> bool | None:
        """The same write, reported rather than fatal: ``None`` when it did not
        land, so a caller can keep its journal line honest."""
        try:
            # An emptied file is published as ZERO BYTES, never unlinked: that
            # is what the bash `jasper_env_file_unset` this replaced did, and
            # jasper.env's own header comments make the case unreachable today.
            _, changed = locked_upsert_env_file(
                path,
                lambda _text: actions,
                mode=ENV_FILE_MODE,
                dir_mode=ENV_DIR_MODE,
            )
            return changed
        except OSError:
            self.log(
                "env_write_failed", file=path, key=",".join(k for k, _ in actions)
            )
            return None

    @property
    def outputd_env_target(self) -> str:
        """Where this pass's outputd.env writes LAND: the staged candidate
        while one is open, the live file otherwise."""
        return self.outputd_env_stage or self.outputd_env_file

    # -- registry and graph probes ------------------------------------------

    def saved_topology(self) -> Any | None:
        """The saved output topology, parsed at most ONCE per pass and handed to
        every consumer that takes the object rather than the path.

        ``None`` when the strict load raised: the consumer then loads the path
        itself and produces its own fail-closed reason, exactly as it did before
        anything was shared. ``OutputTopology`` is frozen, so one object is safe
        to hand to several consumers.
        """
        if not self._topology_read:
            self._topology_read = True
            try:
                # lazy: import cost — 2k lines a single-DAC install pass
                # never needs (ADR-0226).
                from jasper.output_topology_store import load_output_topology_strict  # lazy: topology parse cost on composite paths

                self._topology = load_output_topology_strict(
                    self.output_topology_path
                )
            # noqa reason: a topology this pass cannot read is the consumer's own
            # fail-closed case to report, not a reason to abort here.
            except Exception:  # noqa: BLE001
                self._topology = None
        return self._topology

    def active_graph_status(self, cap_channels: int) -> tuple[bool, Any]:
        """The active-graph cutover gate. DRIVE WHAT WE USE, not the DAC's
        full channel count.

        ``cap_channels`` is the resolved profile's active-lane cap. On success
        returns ``(True, (width, endpoint_device))`` from ONE decision — the
        ACTUAL driven width the config loads (2..cap) and the accepted endpoint
        — so the width the DAC is opened at and the endpoint the marker names
        always describe the same classification of the same graph. Otherwise
        ``(False, reason)`` and every caller fails closed.
        """
        try:
            # lazy: import cost — 5k lines of contract a single-DAC install
            # pass never needs (a declared composite does reach it here).
            from jasper.active_speaker.runtime_contract import (
                outputd_active_lane_decision,
            )

            decision = outputd_active_lane_decision(
                cap_channels,
                statefile_path=self.camilla_statefile,
                crossover_statefile_path=self.camilla2_statefile,
                topology=self.saved_topology(),
                topology_path=self.output_topology_path,
            )
        # noqa reason: named in the reason token, so an operator reads WHICH
        # contract failure kept the box passive rather than an unhelpful `unknown`.
        except Exception as exc:  # noqa: BLE001
            self.mark_degraded()
            detail = getattr(exc, "name", None) or type(exc).__name__
            return (
                False,
                f"active_graph_contract_unavailable:{type(exc).__name__}:{detail}",
            )
        if not decision.ok:
            return False, decision.reason
        return True, (str(decision.width), decision.endpoint_device or "")

    # -- the pass -----------------------------------------------------------

    def rejected_stage_exit(self, name: str, **fields: Any) -> int:
        """The one way a REFUSED candidate ends a pass: name the refusal, then
        restart the mixer pin this pass's own changed record earned before
        failing the unit at 78. The exit precedes every render and every stop,
        so the box keeps running the configuration an earlier pass of this same
        validator accepted."""
        self.log(name, **fields)
        units.restart_dac_init_for_record_change(self)
        return 78

    def print_role_env(self) -> None:
        print(render_shell_assignments({
            "DONGLE_CARD": self.dongle_card,
            "APPLE_DONGLE_PRESENT": "1" if self.apple_dongle_present else "0",
            "APPLE_DONGLE_SERVICE_CARD": self.apple_dongle_service_card,
            "OUTPUT_DAC_CARD": self.output_dac_card,
            "OUTPUT_DAC_ID": self.output_dac_id,
            "OUTPUT_DAC_RECOGNIZED": "1" if self.output_dac_recognized else "0",
        }), end="")

    def execute(self) -> int:
        if not os.access(self.asound_render_lib, os.R_OK):
            # LOUD and before any mutation, and ahead of --print-env because
            # install.sh runs that verb from the same tree it is about to
            # install from: a broken library has to fail the install, not
            # answer it. Unreadable, the `bash -c source` in
            # render_asound_if_needed exits 127 — which preserves the template
            # but lets the pass go on to restart jasper-outputd against an
            # asound.conf naming a different DAC than the outputd.env it just
            # committed. The shell reconciler exited 66 here for the same reason.
            self.log(
                "asound_render_lib_missing",
                lib=_log_token(self.asound_render_lib),
            )
            raise _Abort(66)
        if self.print_env:
            hardware.observe_output_hardware_state(self, write=False)
            hardware.apply_observed_single_policy(self)
            hardware.apply_observed_composite_policy(self)
            self.print_role_env()
            return 0
        render.open_runtime_graph_attempt(self)
        boot.reconcile_i2s_hat_boot(self, logger)
        hardware.observe_output_hardware_state(self, write=True)
        boot.sync_i2s_hat_reboot_marker(self)
        if self.i2s_hat_apply_error:
            units.restart_dac_init_for_record_change(self)
            return 74
        hardware.apply_observed_single_policy(self)
        hardware.apply_observed_composite_policy(self)

        env_changed = 0
        # dac_env_changed tracks ONLY a DAC-identity/card move — the class of
        # change that can shift the mic/input profile and therefore requires
        # stopping voice. env_changed stays the coarse "anything moved" flag.
        dac_env_changed = 0
        outputd_env_changed = 0
        # Set only when the staged outputd.env is actually written; NOT
        # outputd_env_changed, which is set pre-commit and can be cleared when
        # validation rejects the stage.
        outputd_committed = 0
        env_files.stage_outputd_env(self)
        if self.set_env_file_var(
            self.env_file,
            [
                ("JASPER_AUDIO_DAC_ID", self.output_dac_id),
                ("JASPER_AUDIO_DAC_CARD", self.output_dac_card),
            ],
        ):
            env_changed = dac_env_changed = 1
        if outputd_lane.apply_audio_runtime_env(self):
            outputd_env_changed = 1
        # A route change also counts toward env_changed so the outputd/audio
        # restart still fires when appropriate.
        if env_files.apply_route_env(self):
            env_changed = 1
        # For a recognized DAC this is its profile floor (or cleared when the
        # profile declares none); for a parked one an empty id clears any stale
        # floor (#27).
        env_files.apply_latency_floor_env(
            self, self.output_dac_id if self.output_dac_recognized else ""
        )
        if self.latency_floor_changed:
            outputd_env_changed = 1
        if outputd_env_changed:
            if env_files.commit_outputd_env_stage(self):
                env_changed = 1
                outputd_committed = 1
        else:
            env_files.finish_outputd_env_stage(self)
        if self.outputd_env_stage_rejected:
            # Stopping anything here would convert a healthy box into a silent
            # one on behalf of a change that never landed (jts3 2026-08-11 lost
            # the assistant that way, with no cue and no journal line saying so).
            return self.rejected_stage_exit(
                "outputd_candidate_rejected",
                action="preserve_runtime_env",
                services="unchanged",
                output_dac_id=self.output_dac_id,
                output_dac_card=self.output_dac_card,
            )
        env_files.repair_generated_env_permissions(self)

        render_changed = 1 if render.render_asound_if_needed(self) else 0
        render.render_ring_conf_if_needed(self)
        # The first candidate publishes DAC and latency facts for graph
        # rendering. It is deliberately not the final lane decision: after
        # convergence, a second validated candidate is the only one that may
        # enable final output.
        render.render_flat_cutover_if_needed(self)
        runtime_converge_failed = 0
        if not render.converge_runtime_graph(self):
            # A live topology-replacement caller already parked before saving,
            # so keep the preliminary non-active candidate rather than deriving
            # an active lane from an old graph. At boot the statefile may still
            # be stale; jasper-camilla only Wants= this oneshot, so it does
            # start after a nonzero result — this exit status is the signal,
            # and jasper-camilla-topology-gate is what refuses a graph proved
            # against a different topology (#4416 R8).
            runtime_converge_failed = 1
        else:
            env_files.stage_outputd_env(self)
            outputd_lane.apply_audio_runtime_env(self)
            if env_files.commit_outputd_env_stage(self):
                env_changed = 1
                outputd_committed = 1
            if self.outputd_env_stage_rejected:
                # Keep the previously validated preliminary candidate. Do not
                # restart any service against a lane the final graph failed to
                # validate.
                return self.rejected_stage_exit(
                    "runtime_graph",
                    result="failed",
                    reason="post_convergence_outputd_env_rejected",
                    action="preserve_preliminary_env",
                )
        units.gate_role_services(self)
        if self.output_dac_recognized:
            # A DAC-identity or asound change can move the mic/input profile,
            # so it takes the full path. A committed change that touches ONLY
            # outputd.env cannot, so it bounces outputd alone and leaves wake
            # detection up. Fail-safe: skipping the voice stop requires BOTH
            # flags clear (#1257).
            if dac_env_changed or render_changed:
                units.restart_audio_if_needed(self)
            elif outputd_committed:
                units.restart_outputd_only(self)
            else:
                units.start_outputd_if_recognized(self)
            units.restart_route_runtime_if_needed(self)
            # Recognized DACs only: an unrecognized one has just parked, and
            # the park is the loud end state (#2261), not a state to reconcile
            # out of here.
            if not runtime_converge_failed:
                units.kick_fanin_coupling_auto(self, dac_env_changed, render_changed)
        else:
            units.restart_route_runtime_if_needed(self)
            units.park_output_audio(self)
        self.log(
            "complete",
            output_dac_id=self.output_dac_id,
            output_dac_card=self.output_dac_card,
            outputd_active_mode=int(self.outputd_active_mode),
            outputd_active_channels=_log_token(self.outputd_active_channels),
            recognized=int(self.output_dac_recognized),
            env_changed=env_changed,
            render_changed=render_changed,
            dac_env_changed=dac_env_changed,
            outputd_committed=outputd_committed,
            runtime_converge_failed=runtime_converge_failed,
        )
        return 1 if runtime_converge_failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-audio-hardware-reconcile",
        description=(
            "Detect the current final-output DAC role and reconcile owned JTS "
            "state."
        ),
    )
    parser.add_argument("--reason", default="manual")
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="print shell-quoted role variables for install.sh; mutates nothing",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="update files and service enablement but restart nothing",
    )
    return parser


def _install_signal_traps(run: Pass) -> dict[int, Any]:
    """Exit at the next boundary with the code systemd expects for the signal,
    rather than letting the pass run on. The terminal ``exit`` event names the
    signal, which is the only place the cause is recorded."""

    def handler(signum: int, _frame: Any) -> None:
        status, name = _SIGNAL_EXITS[signal.Signals(signum)]
        run.signalled = name
        raise SystemExit(status)

    previous: dict[int, Any] = {}
    for signum in _SIGNAL_EXITS:
        try:
            previous[signum] = signal.signal(signum, handler)
        except ValueError:
            # Not the main thread; the caller keeps its own disposition.
            pass
    return previous


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    run = Pass(
        reason=args.reason,
        print_env=args.print_env,
        no_restart=args.no_restart,
    )
    previous = _install_signal_traps(run)
    status = 1
    try:
        status = run.execute()
        if status == 0 and not (run.print_env or run.no_restart):
            try:
                publish_reconcile_inputs(run)
            except (OSError, ValueError):
                run.mark_degraded()
                run.log("stamp_skipped", reason="inputs_unavailable")
    except _Abort as abort:
        status = abort.status
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else 1
    finally:
        env_files.cleanup_outputd_env_stage(run)
        if run.asound_template_temp:
            Path(run.asound_template_temp).unlink(missing_ok=True)
        run.log("exit", signal=run.signalled or "none", status=status)
        for signum, disposition in previous.items():
            signal.signal(signum, disposition)
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
