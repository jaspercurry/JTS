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
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from jasper.atomic_io import (
    ENV_FILE_LOCK_TIMEOUT_SECONDS,
    EnvKeyAction as EnvAction,
    advisory_file_lock,
    env_key_action,
    env_lock_path,
    locked_upsert_env_file,
)
from jasper.audio_hardware.config_txt import boot_config_path
from jasper.audio_hardware.reconcile_inputs import publish_reconcile_inputs
from jasper.audio_hardware.usb_port_role import DEFAULT_MODEL_PATH
from jasper.usbgadget import DEFAULT_UDC_CLASS_DIR
from jasper.env_file import read_env_file
from jasper.env_load import BASE_ENV_PATH, FANIN_ENV_PATH, OUTPUTD_ENV_PATH
from jasper.log_event import log_event
from jasper.logging_setup import configure_logging
from jasper.output_hardware import (
    DEFAULT_PROC_ASOUND_PATH,
    DEFAULT_TOPOLOGY_PATH,
    ObservedOutput,
    observe,
    observed_output,
    degraded_marker_path,
    state_path,
)
from jasper.service_units import SYSTEMCTL_TIMEOUT_SEC, run_systemctl
from jasper.shell_env import render_shell_assignments

logger = logging.getLogger(__name__)

EVENT = "audio_hardware_reconcile"

DAC_INIT_UNIT = "jasper-dac-init.service"
HEADPHONE_MONITOR_UNIT = "jasper-headphone-monitor.service"
OUTPUTD_UNIT = "jasper-outputd.service"
VOICE_UNIT = "jasper-voice.service"
AEC_RECONCILE_UNIT = "jasper-aec-reconcile.service"
FANIN_UNIT = "jasper-fanin.service"
COUPLING_AUTO_UNIT = "jasper-fanin-coupling-auto.service"

# The ACTIVE RING's playback PCM — the ONE legal active endpoint. This module
# never CHOOSES it; the active-lane decision reports which endpoint the live
# graph targets, and this literal is only how the answer is recognized. Mirrors
# jasper.fanin_coupling.RING_ACTIVE_PLAYBACK_DEVICE and the conf.d block name;
# pinned equal by tests/test_ring_active_endpoint.py.
RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE = "jts_ring_active_playback"

ENV_FILE_MODE = 0o640
ENV_DIR_MODE = 0o750

_SIGNAL_EXITS = {signal.SIGTERM: (143, "TERM"), signal.SIGHUP: (129, "HUP"),
                 signal.SIGINT: (130, "INT")}

#: ``systemctl_call(timeout=...)``'s "use SYSTEMCTL_TIMEOUT_SEC" default, read
#: at CALL time rather than bound at import. Not a legal timeout itself.
_MANAGER_BOUND = -1.0

_LOG_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9_.:,-]")


class _Abort(Exception):
    """Stop the pass and exit with ``status``."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _log_token(value: str) -> str:
    """``jasper_asound_log_token``: a value reduced to one grep-safe token."""
    if not value:
        return "direct"
    return _LOG_TOKEN_UNSAFE.sub("_", value)


def _ensure_dir(path: Path, mode: int) -> None:
    """Create an absent directory at ``mode``; never re-mode an existing one.

    The installer owns each env directory's mode/group, and a blanket re-mode
    on every boot/udev reconcile re-strips them (#827).
    """
    if not path.is_dir():
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)


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


#: ALSA card ids are not stable across a re-enumeration, so the Apple mixer
#: units bake in "resolve it yourself" rather than a card this pass observed.
APPLE_SERVICE_CARD_AUTO = "auto"


class Pass:
    """One reconcile pass over the box's owned output-hardware state."""

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
        self._stage_hold = ExitStack()
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
        self.i2s_hat_intent_file = (
            env.get("JASPER_I2S_HAT_INTENT_FILE") or "/var/lib/jasper/i2s_hat.env"
        )
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
        self.camilla_statefile = (
            env.get("JASPER_CAMILLA_STATEFILE")
            or "/var/lib/camilladsp/outputd-statefile.yml"
        )
        self.camilla2_statefile = (
            env.get("JASPER_CAMILLA2_STATEFILE")
            or "/var/lib/camilladsp/crossover-statefile.yml"
        )
        self.camilla_conf_dir = env.get("JASPER_CAMILLA_CONF_DIR") or "/etc/camilladsp"
        self.ring_conf_d = env.get("JASPER_RING_CONF_D") or ""

        self.apple_dongle_present = False
        self.apple_dongle_service_card = APPLE_SERVICE_CARD_AUTO
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

    # -- systemd ------------------------------------------------------------

    def systemctl_call(
        self, *args: str, quiet: bool = False, timeout: float | None = _MANAGER_BOUND
    ) -> int:
        """Run one systemctl verb. Returns its exit code, never raises.

        ``timeout`` bounds only how long an UNRESPONSIVE MANAGER may stall the
        pass; the default is :data:`SYSTEMCTL_TIMEOUT_SEC`, read at call time.
        A BLOCKING lifecycle verb passes ``None`` and is bounded instead by
        this unit's own ``TimeoutStartSec=50s``
        (``deploy/systemd/jasper-audio-hardware-reconcile.service``), the way
        the shell reconciler ran them: capping ``stop jasper-voice.service``
        below that unit's ``TimeoutStopSec=14s`` would report a failure for a
        stop that is merely finishing its mic-loss cue (ADR-0239), and capping
        an ``enable`` would abort the pass mid-restart on a slow daemon-reload.
        """
        bound = SYSTEMCTL_TIMEOUT_SEC if timeout == _MANAGER_BOUND else timeout
        try:
            return run_systemctl(
                args, executable=self.systemctl, capture_output=False, quiet=quiet,
                timeout=bound,
            ).returncode
        except OSError:
            return 127
        except subprocess.TimeoutExpired:
            # An unresponsive manager is a FAILED call, not a reason to hang a
            # pass that runs from udev. 124 is timeout(1)'s own code.
            self.log(
                "systemctl_timeout",
                command=" ".join(args),
                timeout_sec=bound,
            )
            return 124

    def systemctl_required(
        self, *args: str, timeout: float | None = _MANAGER_BOUND
    ) -> None:
        rc = self.systemctl_call(*args, timeout=timeout)
        if rc != 0:
            raise _Abort(rc)

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

    def repair_generated_env_permissions(self) -> None:
        for path in (self.outputd_env_file, self.fanin_env_file):
            target = Path(path)
            if not target.is_file():
                continue
            # A content-current but root:root file is unreadable to the
            # non-root status daemons, and /state then drifts from root doctor.
            try:
                os.chown(path, -1, target.parent.stat().st_gid)
            except OSError:
                pass
            os.chmod(path, ENV_FILE_MODE)

    # -- the observed record ------------------------------------------------

    def observe_output_hardware_state(self, *, write: bool) -> None:
        action = "written" if write else "observed"
        try:
            state, cards, record_changed = observe(write=write)
            observed = observed_output(state, cards, record_changed=record_changed)
        # noqa reason: the classifier walks sysfs, /proc and an `aplay` spawn; a
        # failure of ANY shape must still leave the DAC-role policy below to run.
        except Exception:  # noqa: BLE001
            self.mark_degraded()
            self.log(f"state_{action}_failed", path=self.state_path)
            return
        # A record missing either of the two facts the whole thing hangs off is
        # not one anything below may read.
        if not observed.valid:
            self.observed = ObservedOutput()
            self.log(
                f"state_{action}_failed",
                reason="invalid_payload",
                path=self.state_path,
            )
            return
        self.observed = observed
        if observed.record_changed:
            self.record_changed = True
        if observed.apple_card_ids:
            self.dongle_card = observed.apple_card_ids[0]
            self.apple_dongle_present = True
        if write:
            # Never fatal: a full /run must not skip the DAC-role policy the
            # caller runs next. --print-env promises no mutations, which is why
            # this is gated on the write.
            try:
                self.publish_management_transport_marker(
                    observed.management_transport_available
                )
            except OSError:
                pass
        self.log(
            f"state_{action}",
            path=self.state_path,
            profile_id=observed.profile_id or "unknown",
            status=observed.status or "unknown",
            blockers=_log_token(",".join(observed.blocker_codes) or "none"),
        )

    def publish_management_transport_marker(self, available: bool | None) -> None:
        """Read by jasper-usbgadget's composition with ``test -e``. In /run so
        a reboot clears it before the boot config it describes can change.
        Truncated in place rather than replaced: an unlink would leave a window
        where a true->true republish reads as false."""
        marker = self.management_transport_marker
        if not available:
            marker.unlink(missing_ok=True)
            return
        _ensure_dir(marker.parent, 0o755)
        marker.open("w").close()

    # -- I2S HAT boot intent ------------------------------------------------

    def reconcile_i2s_hat_boot(self) -> None:
        # lazy: patch target — the tests replace it on the source module, which
        # only a per-call import sees.
        from jasper.audio_hardware.usb_port_role import (
            reconcile_boot_config, boot_role_events,
        )

        try:
            (
                state,
                boot_changed,
                hat_changed,
                desired_profile,
                durability_failed,
                hat_collision,
            ) = reconcile_boot_config(
                model_path=self.model_path,
                boot_config_path=self.boot_config_path,
                udc_class_dir=self.udc_class_dir,
                i2s_hat_intent_path=self.i2s_hat_intent_file,
            )
        # noqa reason: any failure here means the boot config was NOT applied, and
        # 66 (rather than a traceback) is what says the config was preserved.
        except Exception:  # noqa: BLE001
            self.log("i2s_hat_apply", result="error", action="preserve_boot_config")
            raise _Abort(66) from None
        for name, fields in boot_role_events(
            state,
            boot_config_changed=boot_changed,
            hat_profile=desired_profile or "",
            hat_changed=hat_changed,
            hat_collision=hat_collision,
        ):
            log_event(logger, name, fields=fields)
        if durability_failed:
            self.i2s_hat_apply_error = True
        self.i2s_hat_desired_profile = desired_profile or ""
        if state.board_topology == "unsupported":
            self.log(
                "i2s_hat_apply", result="unavailable", board_topology="unsupported"
            )
            return
        self.i2s_hat_boot_changed = bool(hat_changed)
        if self.i2s_hat_apply_error:
            self.log(
                "i2s_hat_apply",
                result="error",
                error="boot_config_published_not_durable",
            )
            return
        self.log(
            "i2s_hat_apply",
            result=self.i2s_hat_boot_changed,
            profile=self.i2s_hat_desired_profile or "none",
        )

    def sync_i2s_hat_reboot_marker(self) -> None:
        """State, not edge: an install-time pass can write the detected HAT's
        boot line before this service ever runs, so "changed this pass" is
        false while the kernel still runs the old overlay. Any registered I2S
        profile, not just InnoMaker: a HAT can be the composite's child device
        rather than the top-level profile_id."""
        desired = self.i2s_hat_desired_profile
        if self.i2s_hat_boot_changed is None or not self.observed.valid:
            return
        observed = self.observed.profile_id
        children = self.observed.child_device_ids
        if desired and desired in children:
            observed = desired
        marker = Path(self.i2s_hat_reboot_required_path)
        if observed == desired:
            marker.unlink(missing_ok=True)
            return
        # No HAT desired: whatever DAC is attached is not a pending boot
        # change, so only this pass having cleared the managed block pends one.
        if not desired and not self.i2s_hat_boot_changed:
            return
        _ensure_dir(marker.parent, 0o755)
        marker.write_text("", encoding="utf-8")
        os.chmod(marker, 0o644)

    # -- outputd.env staging ------------------------------------------------

    @property
    def outputd_env_target(self) -> str:
        """Where this pass's outputd.env writes LAND: the staged candidate
        while one is open, the live file otherwise."""
        return self.outputd_env_stage or self.outputd_env_file

    def stage_outputd_env(self) -> None:
        directory = Path(self.outputd_env_file).parent
        _ensure_dir(directory, ENV_DIR_MODE)
        # See ADR-0235 G8. The hold spans snapshot -> rename, so a second
        # whole-file publisher cannot discard this candidate's base.
        held = True
        try:
            self._stage_hold.enter_context(
                advisory_file_lock(
                    env_lock_path(self.outputd_env_file),
                    timeout_sec=ENV_FILE_LOCK_TIMEOUT_SECONDS,
                )
            )
        except (OSError, TimeoutError):
            held = False
            self.log(
                "outputd_env_stage_unlocked",
                outputd_env=self.outputd_env_file,
                reason="stage_lock_unheld",
            )
        if held:
            # Debris an earlier pass could not clean (a SIGKILL mid-stage).
            # Gated on the hold: a refused hold means a live holder mid-stage,
            # whose in-flight candidate this must not delete.
            for stale in directory.glob(".outputd.env.candidate.*"):
                stale.unlink(missing_ok=True)
                Path(env_lock_path(str(stale))).unlink(missing_ok=True)
        handle, stage = tempfile.mkstemp(
            prefix=".outputd.env.candidate.", dir=directory
        )
        os.close(handle)
        self.outputd_env_stage = stage
        if Path(self.outputd_env_file).is_file():
            shutil.copy2(self.outputd_env_file, stage)
        else:
            try:
                os.chown(stage, -1, directory.stat().st_gid)
            except OSError:
                pass
            os.chmod(stage, ENV_FILE_MODE)

    def cleanup_outputd_env_stage(self) -> None:
        """End the stage: drop the candidate, its own lock, and the live hold."""
        stage = self.outputd_env_stage
        if stage:
            Path(stage).unlink(missing_ok=True)
            Path(env_lock_path(stage)).unlink(missing_ok=True)
        self._stage_hold.close()

    def finish_outputd_env_stage(self) -> None:
        self.cleanup_outputd_env_stage()
        self.outputd_env_stage = None

    def validate_outputd_env_stage(self) -> bool:
        # lazy: patch target — the tests replace it on the source module,
        # which only a per-call import sees.
        from jasper.audio_runtime_plan import validate_outputd_env

        stage = self.outputd_env_stage
        if stage is None:
            return True
        try:
            ok, lines = validate_outputd_env(
                base_env=self.env_file,
                outputd_env=stage,
                outputd_label=self.outputd_env_file,
                camilla_statefile=self.camilla_statefile,
                camilla2_statefile=self.camilla2_statefile,
                output_topology=self.output_topology_path,
                topology=self.saved_topology(),
            )
        # noqa reason: a validator that cannot answer must REJECT the candidate,
        # never abort the pass — the refusal is what preserves the running env.
        except Exception as exc:  # noqa: BLE001
            self.mark_degraded()
            ok, lines = False, (f"{type(exc).__name__}: {exc}",)
        detail = "; ".join(lines)
        if ok:
            # The validator accepted the candidate, and MAY have reported a
            # coherent-but-transient state (`ok note=...` — today the
            # ACTIVE-ring arm waypoint). Log it so the journal carries the
            # whole reason a mid-ladder box goes silent at its next Camilla
            # load; the reconcile proceeds either way.
            note = next((line for line in lines if line.startswith("ok note=")), "")
            if note:
                self.log(
                    "outputd_env_note",
                    outputd_env=self.outputd_env_file,
                    detail=_log_token(note[len("ok note=") :]),
                )
            return True
        self.log(
            "outputd_env_invalid",
            outputd_env=self.outputd_env_file,
            preserved=1,
            detail=_log_token(detail),
        )
        return False

    def commit_outputd_env_stage(self) -> bool:
        stage = self.outputd_env_stage
        if stage is None:
            return False
        if not self.validate_outputd_env_stage():
            self.outputd_env_stage_rejected = True
            self.finish_outputd_env_stage()
            return False
        live = Path(self.outputd_env_file)
        if live.is_file() and live.read_bytes() == Path(stage).read_bytes():
            self.finish_outputd_env_stage()
            return False
        try:
            if live.exists():
                info = live.stat()
                os.chown(stage, info.st_uid, info.st_gid)
            else:
                os.chown(stage, -1, live.parent.stat().st_gid)
        except OSError:
            pass
        os.chmod(stage, ENV_FILE_MODE)
        os.replace(stage, live)
        self.finish_outputd_env_stage()
        return True

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
                from jasper.output_topology import load_output_topology_strict

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

    def active_lane_channels_for_dac(self, dac_id: str) -> tuple[int | None, bool]:
        """A recognized single DAC's active-lane channel CAP, and whether the
        registry ITSELF answered "this DAC declares no active lane".

        THREE-VALUED: ``(n, False)`` the cap, ``(None, True)`` the registry's
        own "no active lane", and ``(None, False)`` no answer at all — the probe
        itself failed. The last two need different remedies, so they must not
        collapse."""
        if not dac_id:
            return None, False
        try:
            # lazy: patch target — the tests replace it on the source
            # module, which only a per-call import sees.
            from jasper.audio_hardware.dac import (
                active_outputd_lane_channels_for,
                is_known_profile_id,
            )

            width = active_outputd_lane_channels_for(dac_id)
            known = is_known_profile_id(dac_id)
        # noqa reason: three-valued by design — a probe that failed for any reason
        # answers "no answer", which the caller reports as the transient it is.
        except Exception:  # noqa: BLE001
            self.mark_degraded()
            return None, False
        if width:
            return int(width), False
        return None, bool(known)

    def final_edge_format_for_dac(self, dac_id: str) -> tuple[str, str]:
        """A recognized DAC's declared final-edge ALSA format AND its outputd
        sink kind, from ONE profile lookup (ADR-0235 R1).

        BOTH or NEITHER: outputd's ``env_str`` defaults only on an UNSET key,
        so an empty JASPER_OUTPUTD_SINK fails its config parse and parks it at
        exit 78. Resolved BY ID off whichever profile the caller armed, never
        through a composite's children — outputd's paired composite sink has no
        packed-24 child write path, which is why the dual-Apple composite
        declares S16_LE though both its children declare S24_3LE.
        """
        if not dac_id:
            return "", ""
        try:
            # lazy: patch target — the tests replace it on the source
            # module, which only a per-call import sees.
            from jasper.audio_hardware.dac import by_id, final_edge_format_for

            fmt = final_edge_format_for(dac_id)
            profile = by_id(dac_id)
        # noqa reason: any failure preserves the previous edge format; writing a
        # guess would silently narrow a wide DAC edge.
        except Exception:  # noqa: BLE001
            self.mark_degraded()
            return "", ""
        if fmt and profile is not None:
            return fmt, profile.outputd_sink
        return "", ""

    def dac_format_actions_for_recognized(
        self, dac_id: str
    ) -> tuple[list[EnvAction], str]:
        """A recognized DAC's declared edge format AND sink as env actions, plus
        the format the file will state — or NO actions when the registry probe
        is unavailable, since one lookup answers both and they degrade together.

        Empty is a MEANINGFUL value on the format key (outputd reads it as
        S16_LE), so writing it on a lost probe would silently NARROW a wide
        edge with no error anywhere. Preserving the previous value is the loud
        option: on a same-pass id change the stale value parks outputd at exit
        78 rather than converting audio wrongly.
        """
        dac_format, dac_sink = self.final_edge_format_for_dac(dac_id)
        if not dac_format:
            preserved = read_env_file(self.outputd_env_target).get(
                "JASPER_OUTPUTD_DAC_FORMAT", ""
            )
            self.log(
                "dac_format_skip",
                reason="registry_probe_unavailable",
                dac_id=dac_id,
                preserved=preserved or "absent",
                outputd_env=self.outputd_env_file,
            )
            return [], preserved
        return [
            ("JASPER_OUTPUTD_DAC_FORMAT", dac_format),
            ("JASPER_OUTPUTD_SINK", dac_sink),
        ], dac_format

    # -- route and latency-floor env ----------------------------------------

    def apply_route_env(self) -> bool:
        """Apply the route-owned fan-in env actions. Returns whether it moved."""
        # lazy: import cost — the route plan is a policy layer the --print-env
        # path never reaches (ADR-0226).
        from jasper.audio_runtime_plan import (
            resolve_audio_route_profile,
            route_owned_env_actions,
        )
        from jasper.env_load import read_env_file_state  # lazy: with the plan

        self.route_fanin_changed = False
        try:
            base = read_env_file_state(self.env_file)
            actions = route_owned_env_actions(
                resolve_audio_route_profile(base.values)
            )
        # noqa reason: a route plan that cannot be built leaves fanin.env alone;
        # the pass still reconciles the DAC.
        except Exception:  # noqa: BLE001
            self.mark_degraded()
            self.log("route_env_skip", reason="audio_config_unavailable")
            return False
        changed = self.set_env_file_var(
            self.fanin_env_file, [env_key_action(action) for action in actions]
        )
        self.route_fanin_changed = changed
        self.log(
            "route_env",
            fanin_env=self.fanin_env_file,
            changed=int(changed),
            fanin_changed=int(self.route_fanin_changed),
        )
        return changed

    def apply_latency_floor_env(self, dac_id: str) -> None:
        """Apply the active DAC's codified latency floor into outputd.env.

        The decisions come from jasper.audio_runtime_plan (operator env >
        profile floor > packaged default, in one policy layer); this only
        performs the requested mutations and reports whether the file moved.

        A probe that cannot answer leaves the four keys ALONE, the same way
        the DAC-format and content-format probes do: clearing them would
        silently drop a tuned box to packaged defaults with no error anywhere,
        while a stale floor is the loud option.
        """
        # lazy: import cost — --print-env never reaches the floor policy (ADR-0226).
        from jasper.audio_runtime_plan import outputd_floor_plan

        try:
            summary, actions = outputd_floor_plan(
                profile_id=dac_id,
                base_env=self.env_file,
                outputd_env=self.outputd_env_target,
            )
        # noqa reason: any failure preserves the previous floor keys; the pass
        # is marked degraded so the shim leaves no stamp to skip against.
        except Exception:  # noqa: BLE001
            self.mark_degraded()
            self.latency_floor_changed = False
            self.log(
                "latency_floor_skip",
                reason="probe_unavailable",
                output_dac_id=dac_id,
                outputd_env=self.outputd_env_file,
            )
            return
        self.latency_floor_changed = self.set_env_file_var(
            self.outputd_env_target, [env_key_action(action) for action in actions]
        )
        self.log(
            "latency_floor",
            output_dac_id=dac_id,
            camilla_chunksize=summary.get("JASPER_CAMILLA_CHUNKSIZE") or "default",
            camilla_target_level=(
                summary.get("JASPER_CAMILLA_TARGET_LEVEL") or "default"
            ),
            outputd_period_frames=(
                summary.get("JASPER_OUTPUTD_PERIOD_FRAMES") or "default"
            ),
            outputd_dac_buffer_frames=(
                summary.get("JASPER_OUTPUTD_DAC_BUFFER_FRAMES") or "default"
            ),
            changed=int(self.latency_floor_changed),
        )

    # -- role policy --------------------------------------------------------

    def apply_observed_single_policy(self) -> None:
        """Consume the classifier's verdict for ordinary single devices, so a
        newly registered DAC needs no second hardware rule here."""
        if self.observed.status != "ready":
            return
        if not self.observed.selected_card_id:
            return
        self.output_dac_card = self.observed.selected_card_id
        self.output_dac_id = self.observed.profile_id
        self.output_dac_recognized = True

    def apply_observed_composite_policy(self) -> None:
        if self.observed.kind != "composite":
            return
        # The parked shape, up front: a composite is NAMED whatever its status,
        # so every branch leaves these exactly here except the one that arms.
        self.output_dac_id = self.observed.profile_id
        self.output_dac_card = ""
        self.output_dac_recognized = False
        self.apple_dongle_present = True
        self.apple_dongle_service_card = APPLE_SERVICE_CARD_AUTO
        if self.observed.status != "ready":
            self.log(
                "dual_apple_detected",
                status=self.observed.status or "unknown",
                action="park_until_ready",
            )
            return
        if not self.observed.dual_mapping_ok:
            self.log(
                "dual_apple_detected",
                status="ready",
                action="park_unstable_child_order",
                topology_path=self.output_topology_path,
                reason=self.observed.dual_mapping_reason
                or "unknown",
            )
            return
        self.dual_apple_order_source = self.observed.dual_order_source
        self.dual_apple_dac_a_pcm = self.observed.dual_dac_a_pcm
        self.dual_apple_dac_b_pcm = self.observed.dual_dac_b_pcm
        # The composite sink is rigidly 4-channel in outputd, so the dual
        # branch needs the gate's pass/fail and its ENDPOINT DEVICE but not the
        # returned width. The endpoint field must not be discarded: the marker
        # `active_ring_endpoint_proof` demands is derived from exactly it.
        ok, payload = self.active_graph_status(4)
        if not ok:
            self.dual_apple_dac_a_pcm = ""
            self.dual_apple_dac_b_pcm = ""
            self.dual_apple_active_endpoint_device = ""
            self.log(
                "dual_apple_detected",
                status="ready",
                action="park_until_active_graph",
                reason=payload,
            )
            return
        self.output_dac_card = self.dongle_card
        self.output_dac_recognized = True
        self.dual_apple_active_endpoint_device = payload[1]
        self.log(
            "dual_apple_detected",
            status="ready",
            action="outputd_dual_sink",
            order_source=self.dual_apple_order_source,
            dac_a_pcm=_log_token(self.dual_apple_dac_a_pcm),
            dac_b_pcm=_log_token(self.dual_apple_dac_b_pcm),
            active_endpoint=self.dual_apple_active_endpoint_device or "none",
        )

    # -- outputd runtime env ------------------------------------------------

    def set_outputd_active_lane_pair(self, lane: str, endpoint_device: str) -> bool:
        """THE SINGLE WRITER of the active-lane PAIR.

        JASPER_OUTPUTD_ACTIVE_LANE and JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT are
        ONE FACT with two consumers: outputd bails at startup on the incoherent
        pair (marker set, lane clear), because that can only mean this writer
        is broken. So every path that states one states the other, here, from
        one decision. Positive equality against the named device, never "not
        the ALSA lane": an unrecognized endpoint must resolve to NO marker,
        which a negative test would invert into a spurious arm. Returns whether
        either key changed.
        """
        ring_endpoint = (
            "1"
            if lane == "1" and endpoint_device == RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE
            else ""
        )
        return self.set_env_file_var(
            self.outputd_env_target,
            [
                ("JASPER_OUTPUTD_ACTIVE_LANE", lane),
                ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", ring_endpoint),
            ],
        )

    def apply_audio_runtime_env(self) -> bool:
        target = self.outputd_env_target
        # The RETIRED content lane's capture PCM, HEALED rather than restated.
        # outputd stopped reading this key with the lane (ADR-0100), so this
        # reconciler no longer states it — but a box that reconciled before that
        # carries the old line, and a per-key upsert never touches a key nobody
        # writes. Present-but-empty defeats the absent-key default in
        # jasper.audio_runtime_plan's retired-route describer, and on an
        # ACTIVE -> PASSIVE move the leftover can fail the staged validator
        # outright. REMOVED, never written empty.
        # REMOVAL CONDITION: dies with that describer's read.
        prelude: list[EnvAction] = [("JASPER_OUTPUTD_CONTENT_PCM", None)]
        # The CONTENT lane's width is a function of the fan-in coupling, never
        # of the DAC, so unlike the edge format it is emitted once ahead of the
        # per-hardware branches and is always definitive. An empty answer means
        # leave the key alone rather than write a guess — a fallback here would
        # be a second spelling of DEFAULT_PLAYBACK_FORMAT.
        try:
            # lazy: patch target — the tests replace it on the source
            # module, which only a per-call import sees.
            from jasper.fanin_coupling import content_lane_format_for_coupling

            content_format = content_lane_format_for_coupling()
        # noqa reason: any failure leaves the key alone rather than narrowing the
        # content lane; the pass is marked degraded below.
        except Exception:  # noqa: BLE001
            content_format = ""
        if content_format:
            prelude.append(("JASPER_OUTPUTD_CONTENT_FORMAT", content_format))
        else:
            self.mark_degraded()
            self.log(
                "content_format_skip",
                reason="coupling_probe_unavailable",
                outputd_env=self.outputd_env_file,
            )
        changed = self.set_env_file_var(target, prelude)
        composite = self.observed.kind == "composite"
        if composite and self.output_dac_recognized:
            changed = self._apply_composite_runtime_env(content_format) or changed
        elif self.output_dac_recognized:
            changed = self._apply_single_runtime_env(content_format) or changed
        else:
            changed = self._apply_parked_runtime_env(content_format) or changed
        return changed

    def _apply_composite_runtime_env(self, content_format: str) -> bool:
        target = self.outputd_env_target
        self.outputd_active_mode = False
        self.outputd_active_channels = ""
        actions: list[EnvAction] = [
            ("JASPER_OUTPUTD_BACKEND", "alsa"),
            ("JASPER_OUTPUTD_DAC_PCM", self.output_dac_id),
            ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", self.dual_apple_dac_a_pcm),
            ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", self.dual_apple_dac_b_pcm),
        ]
        format_actions, dac_format = self.dac_format_actions_for_recognized(
            self.output_dac_id
        )
        actions += format_actions
        # Composite width is fixed at 4 (two stereo children); clear the
        # single-sink width knob so a stale value cannot reach outputd, which
        # rejects != 4 on this sink.
        actions.append(("JASPER_OUTPUTD_ACTIVE_CHANNELS", ""))
        changed = self.set_env_file_var(target, actions)
        # Deliberately narrower than the single-DAC branch: an ALOOP composite
        # keeps its unconditional clear. `active_lane` is inert on a composite
        # at runtime, so writing =1 there would change no behaviour but WOULD
        # churn outputd.env and /state on boxes this has no business touching.
        dual_apple_endpoint = self.dual_apple_active_endpoint_device
        if dual_apple_endpoint == RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE:
            changed = self.set_outputd_active_lane_pair("1", dual_apple_endpoint) or changed
        else:
            changed = self.set_outputd_active_lane_pair("", "") or changed
        self.log(
            "runtime_env",
            mode="dual_apple",
            content_format=content_format or "unset",
            dac_format=dac_format or "unset",
            outputd_env=self.outputd_env_file,
            changed=int(changed),
        )
        return changed

    def _apply_single_runtime_env(self, content_format: str) -> bool:
        """A coherent single DAC runs the active lane ONLY when it declares one
        AND a legal active-speaker graph whose playback width fits within that
        cap is the live CamillaDSP config. We DRIVE WHAT WE USE: the gate
        returns the config's ACTUAL width W, emitted as
        JASPER_OUTPUTD_ACTIVE_CHANNELS so outputd opens the DAC at exactly W.
        Fail-closed: without a confirmed in-cap active graph the DAC stays
        ordinary stereo."""
        target = self.outputd_env_target
        changed = False
        active_mode = False
        active_channels = ""
        active_endpoint_device = ""
        graph_status = ""
        active_lane_cap, declares_no_lane = self.active_lane_channels_for_dac(
            self.output_dac_id
        )
        # Three outcomes, three remedies. All resolve passive (fail-closed);
        # they differ only in what an operator reading the journal should do.
        if declares_no_lane:
            # The registry answered: this DAC declares no active outputd lane,
            # so the width gate never ran. Fixed only by choosing a different
            # layout at /sound/speaker/. Same literal as that save-guard's
            # refusal reason.
            graph_status = "dac_no_active_lane"
        elif active_lane_cap is not None:
            ok, payload = self.active_graph_status(active_lane_cap)
            if ok:
                active_mode = True
                active_channels, active_endpoint_device = payload
            else:
                graph_status = payload
        else:
            # No answer at all on a RECOGNIZED DAC: the lane-cap probe itself
            # failed. Transient — the next pass converges — so this must NOT be
            # reported as the permanent dac_no_active_lane.
            graph_status = "lane_probe_failed"
        actions: list[EnvAction] = [
            ("JASPER_OUTPUTD_BACKEND", "alsa"),
            ("JASPER_OUTPUTD_DAC_PCM", "outputd_dac"),
            ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", ""),
            ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", ""),
        ]
        format_actions, dac_format = self.dac_format_actions_for_recognized(
            self.output_dac_id
        )
        actions += format_actions
        if active_mode:
            self.outputd_active_mode = True
            self.outputd_active_channels = active_channels
            actions.append(("JASPER_OUTPUTD_ACTIVE_CHANNELS", active_channels))
            changed = self.set_env_file_var(target, actions)
            # An active 2-way speaker is ALSO 2-channel, so outputd's bare
            # content_channels==2 check would wrongly permit its post-crossover
            # TTS mixer / content bridge here. Mark the lane explicitly so
            # those stereo-only features fail closed (full-range-to-tweeter
            # safety). The endpoint travels with it, from the same decision.
            changed = (
                self.set_outputd_active_lane_pair("1", active_endpoint_device)
                or changed
            )
            self.log(
                "runtime_env",
                mode="single_alsa_active",
                active_channels=active_channels,
                active_lane_cap=active_lane_cap,
                active_endpoint=active_endpoint_device or "unset",
                content_format=content_format or "unset",
                dac_format=dac_format or "unset",
                outputd_env=self.outputd_env_file,
                changed=int(changed),
            )
            return changed
        self.outputd_active_mode = False
        self.outputd_active_channels = ""
        # Clear the width knob so outputd defaults to stereo, and the lane PAIR
        # so a stale =1 cannot keep the stereo-only features fenced off on an
        # ordinary passive DAC.
        actions.append(("JASPER_OUTPUTD_ACTIVE_CHANNELS", ""))
        changed = self.set_env_file_var(target, actions)
        changed = self.set_outputd_active_lane_pair("", "") or changed
        self.log(
            "runtime_env",
            mode="single_alsa",
            content_format=content_format or "unset",
            dac_format=dac_format or "unset",
            outputd_env=self.outputd_env_file,
            changed=int(changed),
            active_graph=graph_status or "none",
        )
        return changed

    def _apply_parked_runtime_env(self, content_format: str) -> bool:
        self.outputd_active_mode = False
        self.outputd_active_channels = ""
        changed = self.set_env_file_var(
            self.outputd_env_target,
            [
                ("JASPER_OUTPUTD_BACKEND", "fake"),
                ("JASPER_OUTPUTD_SINK", "single_alsa"),
                ("JASPER_OUTPUTD_DAC_PCM", "outputd_dac"),
                ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", ""),
                ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", ""),
                # Unrecognized/parked: no profile to query, so clear rather than
                # query. Explicit empty, not omitted: this reconciler-owned file
                # always states a definitive value for every conditional key, so
                # a hot-swap to an unrecognized card cannot leave a stale format.
                ("JASPER_OUTPUTD_DAC_FORMAT", ""),
                ("JASPER_OUTPUTD_ACTIVE_CHANNELS", ""),
            ],
        )
        changed = self.set_outputd_active_lane_pair("", "") or changed
        self.log(
            "runtime_env",
            mode="parked",
            content_format=content_format or "unset",
            dac_format="unset",
            outputd_env=self.outputd_env_file,
            changed=int(changed),
        )
        return changed

    # -- rendered artifacts -------------------------------------------------

    def render_asound_if_needed(self) -> bool:
        source = Path(self.asound_source_template)
        if not source.is_file():
            self.log(
                "asound_skip", source_template=self.asound_source_template, missing=1
            )
            return False
        destination = Path(self.asound_template)
        _ensure_dir(destination.parent, 0o755)
        handle, tmp = tempfile.mkstemp(
            prefix=destination.name + ".", dir=destination.parent
        )
        os.close(handle)
        self.asound_template_temp = tmp
        # Render and validate BEFORE replacing the live template: a card-less
        # recognized DAC makes the shared renderer fail closed, and an ignored
        # failure would clobber the working source with an empty file.
        rendered = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; jasper_asound_render_template "$2" "$3"',
                "jasper-asound-render",
                self.asound_render_lib,
                str(source),
                tmp,
            ],
            check=False,
            env={
                **os.environ,
                "OUTPUT_DAC_CARD": self.output_dac_card,
                "OUTPUT_DAC_ID": self.output_dac_id,
                "OUTPUT_DAC_RECOGNIZED": "1" if self.output_dac_recognized else "0",
            },
        )
        if rendered.returncode != 0 or os.path.getsize(tmp) == 0:
            os.unlink(tmp)
            self.log(
                "asound_render_failed",
                stage="source_template",
                source_template=self.asound_source_template,
                output_dac_id=self.output_dac_id,
                output_dac_card=_log_token(self.output_dac_card),
                preserved_existing=1,
            )
            return False
        # BEFORE the byte-compare, or a narrow-wire box never converges: the
        # source ships the wide aloop aliases, so a candidate compared wide
        # would differ from the narrowed live template on EVERY pass and stop
        # jasper-voice with it (#3580).
        aloop_lane_render, narrow_wire = self.render_aloop_lane_wire_candidate(tmp)
        if narrow_wire and aloop_lane_render not in ("rendered", "unchanged"):
            os.unlink(tmp)
            # Publishing here would write the WIDE aliases over a narrowed live
            # template — a renderer that cannot open its lane, plus a restart of
            # jasper-voice on every pass. Same posture as a rejected candidate:
            # keep what the box runs. Scoped to a box that positively declared
            # the narrow wire, so an unresolvable wire still publishes rather
            # than leaving a fresh box with no asound.conf at all.
            self.log(
                "asound_render_failed",
                stage="aloop_lane_wire",
                result=aloop_lane_render,
                sample_format=narrow_wire,
                preserved_existing=1,
            )
            return False
        os.chmod(tmp, 0o644)
        if destination.is_file() and destination.read_bytes() == Path(tmp).read_bytes():
            os.unlink(tmp)
            return False
        try:
            rc = subprocess.run(
                [self.render_asound_conf],
                check=False,
                env={**os.environ, "JASPER_ASOUND_TEMPLATE": tmp},
            ).returncode
        except OSError:
            # An absent or non-executable renderer is the shell's own 127, and
            # it refuses for the same reason a nonzero one does. Uncaught it
            # escaped main() (which handles only _Abort/SystemExit), skipping
            # the mixer-pin restart and leaking this template.
            rc = 127
        if rc != 0:
            os.unlink(tmp)
            # Fails the unit for the same reason as a rejected candidate:
            # nothing has been stopped or restarted yet, while continuing would
            # restart outputd against an asound.conf naming a different DAC
            # than the outputd.env this pass already committed.
            raise _Abort(
                self.rejected_stage_exit(
                    "asound_render_failed",
                    stage="asound_conf",
                    rc=rc,
                    output_dac_id=self.output_dac_id,
                    output_dac_card=_log_token(self.output_dac_card),
                    preserved_existing=1,
                )
            )
        os.replace(tmp, destination)
        self.log(
            "asound_rendered",
            output_dac_id=self.output_dac_id,
            output_dac_card=self.output_dac_card,
            outputd_active_mode=int(self.outputd_active_mode),
            outputd_active_channels=_log_token(self.outputd_active_channels),
            aloop_lane_wire=aloop_lane_render,
        )
        return True

    def render_aloop_lane_wire_candidate(self, candidate: str) -> tuple[str, str]:
        """Narrow the candidate template's snd-aloop lane aliases to this box's
        resolved ring wire (#3580).

        Returns the render verdict and the NARROW wire this box declared —
        empty when it resolves the shipped wide wire, and empty when the wire
        did not resolve at all. The caller refuses to publish a candidate that
        carries neither, so "" is what keeps an unresolvable wire on the old
        best-effort path instead of leaving a fresh box with no asound.conf.
        """
        from jasper.fanin_coupling import (  # lazy: ADR-0226
            RING_WIRE_FORMAT_WIDE,
            resolve_ring_wire,
        )
        from jasper.ring_assets import render_aloop_lane_wire  # lazy: ADR-0226

        narrow = ""
        try:
            wire = resolve_ring_wire(self.saved_topology()).sample_format
            narrow = "" if wire == RING_WIRE_FORMAT_WIDE else wire
            return render_aloop_lane_wire(candidate, wire), narrow
        # noqa reason: same posture as the ring conf.d render — a failure leaves
        # the shipped wire in place and must not abort a hardware reconcile.
        except Exception as exc:  # noqa: BLE001
            self.log(
                "aloop_lane_wire_failed",
                detail=_log_token(f"{type(exc).__name__}: {exc}"),
            )
            return "failed", narrow

    def render_ring_conf_if_needed(self) -> None:
        """Render the shm-ring conf.d slot period from the ACTIVE DAC's
        DECLARED latency floor. Narrow on purpose: an unrecognized DAC, a DAC
        with no declared floor, and a floor whose period is not fan-in's
        compile-time RING_SLOT_FRAMES all leave the shipped conf.d untouched.

        The report owns those gates — including this pass's own DAC recognition
        — because the renderer-lane conf.d beside the ring's narrows on every
        call the WIRE resolves, which none of the three gates speaks to (#3580).

        Triggers NO restart and feeds no restart flag: ALSA reads the conf.d at
        the next PCM open, and arming is owned by the coupling reconciler.
        """
        from jasper.ring_assets import ring_conf_wire_report  # lazy: --print-env skips it (ADR-0226)

        try:
            report = ring_conf_wire_report(
                profile_id=self.output_dac_id,
                conf_d=self.ring_conf_d,
                output_topology=self.output_topology_path,
                topology=self.saved_topology(),
                dac_recognized=self.output_dac_recognized,
            )
        # noqa reason: the conf.d render is best-effort — a failure leaves the
        # shipped wire in place and must not abort a hardware reconcile.
        except Exception as exc:  # noqa: BLE001
            self.log(
                "ring_conf",
                result="failed",
                output_dac_id=self.output_dac_id,
                detail=_log_token(f"{type(exc).__name__}: {exc}"),
            )
            return
        self.log(
            "ring_conf",
            result=report.get("result") or "unknown",
            output_dac_id=self.output_dac_id,
            period_frames=report.get("period_frames") or "none",
            previous_period_frames=report.get("previous_period_frames") or "none",
            sample_format=report.get("sample_format") or "none",
            ring_a_channels=report.get("ring_a_channels") or "none",
            ring_b_channels=report.get("ring_b_channels") or "none",
            ring_active_channels=report.get("ring_active_channels") or "none",
            topology=report.get("topology") or "none",
            lane_result=report.get("lane_result") or "none",
            reason=report.get("reason") or "none",
            ring_conf=_log_token(report.get("conf") or ""),
            lane_conf=_log_token(report.get("lane_conf") or ""),
        )

    def render_flat_cutover_if_needed(self) -> None:
        """Render the flat cutover startup graph for the saved topology.

        The RUNTIME sibling of install's render, through the same single writer
        so there is no second spelling. It belongs here because the graph is
        width-matched to the saved topology and goes stale whenever that
        changes — and both paths that change it run inside jasper-web's
        sandbox, which has no /etc/camilladsp write path (WS1-deliberate).
        Write-on-change; a failed render leaves the previous bytes.
        """
        # lazy: import cost — the YAML emitters are the pass's second heaviest
        # import and the --print-env path never reaches them (ADR-0226).
        from jasper.output_topology import OutputTopologyError
        from jasper.sound.camilla_yaml import render_flat_cutover_configs

        try:
            result = render_flat_cutover_configs(
                config_dir=self.camilla_conf_dir, topology=self.saved_topology()
            )
        except (OutputTopologyError, OSError, ValueError) as exc:
            self.log(
                "flat_cutover",
                result="failed",
                detail=_log_token(f"{type(exc).__name__}: {exc}"),
            )
            return
        self.log(
            "flat_cutover",
            result="ok",
            changed="yes" if result.changed else "no",
            config_dir=self.camilla_conf_dir,
            topology=self.output_topology_path,
        )

    def converge_runtime_graph(self) -> bool:
        """Seed the proved boot statefile for the saved topology.

        The graph selector is the single owner of topology -> CamillaDSP
        safety. This hardware owner never mutates live CamillaDSP: web
        save/reset owns immediate locked convergence and coupling
        reconciliation owns ordered live route transitions.
        """
        # lazy: the contract behind this is the pass's heaviest import and the
        # --print-env path never reaches it (ADR-0226).
        from jasper.active_speaker.runtime_convergence import converge_boot_statefile

        try:
            result = converge_boot_statefile(
                topology_path=self.output_topology_path,
                topology=self.saved_topology(),
                statefile_path=self.camilla_statefile,
                flat_config_path=os.path.join(
                    self.camilla_conf_dir, "outputd-cutover.yml"
                ),
                write_statefile=True,
            )
        # noqa reason: a convergence that cannot decide fails the pass through its
        # own return, which is what blocks CamillaDSP at boot.
        except Exception as exc:  # noqa: BLE001
            self.mark_degraded()
            self.log(
                "runtime_graph",
                result="failed",
                detail=_log_token(f"{type(exc).__name__}: {exc}"),
            )
            return False
        if not result.ok:
            self.log(
                "runtime_graph",
                result="failed",
                detail=_log_token(
                    result.error or f"{result.decision.status}:{result.decision.reason}"
                ),
            )
            return False
        self.log(
            "runtime_graph",
            result="ok",
            apply_mode="write-statefile",
            selected=result.decision.selected_config_path,
            detail=_log_token(
                f"{result.decision.status}:"
                f"{'wrote' if result.statefile_written else 'unchanged'}"
            ),
        )
        return True

    # -- unit gating and restarts -------------------------------------------

    def bounce(self, unit: str, verb: str, *, quiet: bool = True) -> None:
        """Clear a parked unit's failure state, then ask systemd for the
        transition WITHOUT waiting on it.

        --no-block throughout: this runs from udev and from install, where a
        blocking transition can deadlock against the jobs it waits on. The two
        BLOCKING starts in :meth:`gate_role_services` are deliberately not this
        (see their own note) and stay written out.

        The direct-systemctl spelling of the broker's ``reset_then_manage``:
        this pass is a ROOT oneshot, which is outside the client set
        :mod:`jasper.control.restart_broker` exists for (see its docstring's
        "NOT brokered, by design").
        """
        self.systemctl_call("reset-failed", unit, quiet=True)
        self.systemctl_call("--no-block", verb, unit, quiet=quiet)

    def restart_dac_init_for_record_change(self) -> None:
        """Restart the mixer pin only when the record it reads CHANGED.

        RemainAfterExit makes a plain `start` a no-op otherwise, and a restart
        per pass would spawn an interpreter on every udev sound event
        (ADR-0226). --no-block: the unit is ordered Before= camilla and the
        renderers. Called from gate_role_services AND from every early exit
        between the record write and it, so a pass that aborts there still
        restarts the pin its own changed record earned.
        """
        if not self.record_changed:
            return
        self.bounce(DAC_INIT_UNIT, "restart", quiet=False)
        self.log("dac_init_restarted", output_dac_id=self.output_dac_id or "unknown")

    def gate_role_services(self) -> None:
        # The monitor exists to re-pin ONE mixer control, so the DAC declaring
        # that control is the whole condition for running it — the classifier
        # answers it off the registry (ADR-0235 R2).
        apple_output = bool(self.observed.headphone_control)
        # The pin is enabled on every box: which controls a DAC pins is the
        # registry's answer, and jasper-dac-init is where it is asked.
        self.systemctl_required("enable", DAC_INIT_UNIT, timeout=None)
        if self.record_changed:
            self.restart_dac_init_for_record_change()
        else:
            self.systemctl_call("reset-failed", DAC_INIT_UNIT, quiet=True)
            self.systemctl_call("start", DAC_INIT_UNIT, timeout=None)
        if apple_output:
            self.systemctl_required("enable", HEADPHONE_MONITOR_UNIT, timeout=None)
            # Idempotent start, never a restart: this gate runs on every
            # udev/reconcile pass and a deploy's core-audio bounce fires it
            # several times inside StartLimitIntervalSec. reset-failed clears a
            # parked state; start is a no-op when it is already running, and
            # the monitor re-resolves the card in its own poll loop.
            self.systemctl_call("reset-failed", HEADPHONE_MONITOR_UNIT, quiet=True)
            self.systemctl_call("start", HEADPHONE_MONITOR_UNIT, timeout=None)
            self.log(
                "apple_services", state="enabled", output_dac_id=self.output_dac_id
            )
        else:
            self.systemctl_call(
                "disable", "--now", HEADPHONE_MONITOR_UNIT, quiet=True, timeout=None
            )
            self.systemctl_call("reset-failed", HEADPHONE_MONITOR_UNIT, quiet=True)
            self.log(
                "apple_services", state="disabled", output_dac_id=self.output_dac_id
            )

    def park_output_audio(self) -> None:
        if self.no_restart:
            self.log(
                "output_park_skip",
                output_dac_id=self.output_dac_id,
                recognized=int(self.output_dac_recognized),
                no_restart=1,
            )
            return
        self.systemctl_call("--no-block", "stop", VOICE_UNIT, OUTPUTD_UNIT, quiet=True)
        self.systemctl_call("reset-failed", VOICE_UNIT, OUTPUTD_UNIT, quiet=True)
        self.log(
            "output_parked",
            output_dac_id=self.output_dac_id,
            output_dac_card=self.output_dac_card,
            recognized=int(self.output_dac_recognized),
            observed_blockers=_log_token(
                ",".join(self.observed.blocker_codes) or "none"
            ),
        )

    def install_profile(self) -> str:
        """The box's install profile; only a genuinely absent marker gets the
        historical full-brain default."""
        marker = Path(self.install_profile_file)
        if not marker.exists() and not marker.is_symlink():
            return "full"
        try:
            first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError, UnicodeDecodeError):
            return "unknown"
        return first or "unknown"

    def restart_audio_if_needed(self) -> None:
        if self.no_restart:
            return
        profile = self.install_profile()
        if profile == "full":
            self.systemctl_call("stop", VOICE_UNIT, quiet=True, timeout=None)
        # These are SEPARATE transactions, deliberately unordered. Correctness
        # does not depend on winning the race with jasper-aec-init: it refuses
        # to certify a STATUS older than outputd.env and the AEC reconciler
        # drops to software AEC3, keeping hearing until a later pass re-arms the
        # chip (ADR-0101).
        self.bounce(OUTPUTD_UNIT, "restart")
        if profile == "full":
            # Not `bounce`: this oneshot declares no start-rate limit, so it has
            # no parked state a reset-failed would have to clear first.
            self.systemctl_call(
                "--no-block", "restart", AEC_RECONCILE_UNIT, quiet=True
            )
        self.log(
            "audio_restarted",
            output_dac_id=self.output_dac_id,
            output_dac_card=self.output_dac_card,
            brain_restarted=1 if profile == "full" else 0,
            install_profile=_log_token(profile[:32]),
        )

    def restart_outputd_only(self) -> None:
        """Bounce ONLY jasper-outputd for an outputd.env-only change.

        None of those can move the mic/input profile, so unlike the full path
        this must NOT stop jasper-voice or kick the AEC reconciler: wake
        detection stays up across the restart instead of being deafened for
        ~10-15 s on a routine /sources/ toggle (#1257).
        """
        if self.no_restart:
            self.log(
                "outputd_only_restart_skip",
                output_dac_id=self.output_dac_id,
                output_dac_card=self.output_dac_card,
                no_restart=1,
            )
            return
        self.bounce(OUTPUTD_UNIT, "restart")
        self.log(
            "outputd_only_restarted",
            output_dac_id=self.output_dac_id,
            output_dac_card=self.output_dac_card,
        )

    def restart_route_runtime_if_needed(self) -> None:
        if self.no_restart:
            return
        if self.route_fanin_changed:
            self.bounce(FANIN_UNIT, "restart")
        self.log(
            "route_runtime_restarted",
            fanin_env=self.fanin_env_file,
            fanin_restarted=int(self.route_fanin_changed),
        )

    def kick_fanin_coupling_auto(self, dac_changed: int, render_moved: int) -> None:
        """Ask the coupling reconciler to converge after a successful runtime
        graph convergence — the DAC-swap edge (#2285 P7).

        --no-block IS LOAD-BEARING: the coupling pass kicks this unit back
        synchronously during its arm, so a blocking start would wait on a pass
        waiting on this one. systemd also coalesces starts of an already-active
        oneshot, so each external event causes at most one follow-up pass and
        the pair cannot ping-pong.
        """
        if self.no_restart:
            result = "skipped_no_restart"
        else:
            self.systemctl_call("--no-block", "start", COUPLING_AUTO_UNIT, quiet=True)
            result = "started"
        self.log(
            "coupling_kick",
            result=result,
            dac_env_changed=dac_changed,
            render_changed=render_moved,
        )

    def start_outputd_if_recognized(self) -> None:
        if self.no_restart:
            self.log(
                "outputd_start_skip",
                output_dac_id=self.output_dac_id,
                output_dac_card=self.output_dac_card,
                recognized=1,
                no_restart=1,
            )
            return
        self.bounce(OUTPUTD_UNIT, "start")
        self.log(
            "outputd_start_requested",
            output_dac_id=self.output_dac_id,
            output_dac_card=self.output_dac_card,
            recognized=1,
        )

    # -- the pass -----------------------------------------------------------

    def rejected_stage_exit(self, name: str, **fields: Any) -> int:
        """The one way a REFUSED candidate ends a pass: name the refusal, then
        restart the mixer pin this pass's own changed record earned before
        failing the unit at 78. The exit precedes every render and every stop,
        so the box keeps running the configuration an earlier pass of this same
        validator accepted."""
        self.log(name, **fields)
        self.restart_dac_init_for_record_change()
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
            self.observe_output_hardware_state(write=False)
            self.apply_observed_single_policy()
            self.apply_observed_composite_policy()
            self.print_role_env()
            return 0
        self.reconcile_i2s_hat_boot()
        self.observe_output_hardware_state(write=True)
        self.sync_i2s_hat_reboot_marker()
        if self.i2s_hat_apply_error:
            self.restart_dac_init_for_record_change()
            return 74
        self.apply_observed_single_policy()
        self.apply_observed_composite_policy()

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
        self.stage_outputd_env()
        if self.set_env_file_var(
            self.env_file,
            [
                ("JASPER_AUDIO_DAC_ID", self.output_dac_id),
                ("JASPER_AUDIO_DAC_CARD", self.output_dac_card),
            ],
        ):
            env_changed = dac_env_changed = 1
        if self.apply_audio_runtime_env():
            outputd_env_changed = 1
        # A route change also counts toward env_changed so the outputd/audio
        # restart still fires when appropriate.
        if self.apply_route_env():
            env_changed = 1
        # For a recognized DAC this is its profile floor (or cleared when the
        # profile declares none); for a parked one an empty id clears any stale
        # floor (#27).
        self.apply_latency_floor_env(
            self.output_dac_id if self.output_dac_recognized else ""
        )
        if self.latency_floor_changed:
            outputd_env_changed = 1
        if outputd_env_changed:
            if self.commit_outputd_env_stage():
                env_changed = 1
                outputd_committed = 1
        else:
            self.finish_outputd_env_stage()
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
        self.repair_generated_env_permissions()

        render_changed = 1 if self.render_asound_if_needed() else 0
        self.render_ring_conf_if_needed()
        # The first candidate publishes DAC and latency facts for graph
        # rendering. It is deliberately not the final lane decision: after
        # convergence, a second validated candidate is the only one that may
        # enable final output.
        self.render_flat_cutover_if_needed()
        runtime_converge_failed = 0
        if not self.converge_runtime_graph():
            # A live topology-replacement caller already parked before saving,
            # so keep the preliminary non-active candidate rather than deriving
            # an active lane from an old graph. At boot the statefile may still
            # be stale; jasper-camilla Requires this oneshot and therefore
            # cannot start after this nonzero result.
            runtime_converge_failed = 1
        else:
            self.stage_outputd_env()
            self.apply_audio_runtime_env()
            if self.commit_outputd_env_stage():
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
        self.gate_role_services()
        if self.output_dac_recognized:
            # A DAC-identity or asound change can move the mic/input profile,
            # so it takes the full path. A committed change that touches ONLY
            # outputd.env cannot, so it bounces outputd alone and leaves wake
            # detection up. Fail-safe: skipping the voice stop requires BOTH
            # flags clear (#1257).
            if dac_env_changed or render_changed:
                self.restart_audio_if_needed()
            elif outputd_committed:
                self.restart_outputd_only()
            else:
                self.start_outputd_if_recognized()
            self.restart_route_runtime_if_needed()
            # Recognized DACs only: an unrecognized one has just parked, and
            # the park is the loud end state (#2261), not a state to reconcile
            # out of here.
            if not runtime_converge_failed:
                self.kick_fanin_coupling_auto(dac_env_changed, render_changed)
        else:
            self.restart_route_runtime_if_needed()
            self.park_output_audio()
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
        run.cleanup_outputd_env_stage()
        if run.asound_template_temp:
            Path(run.asound_template_temp).unlink(missing_ok=True)
        run.log("exit", signal=run.signalled or "none", status=status)
        for signum, disposition in previous.items():
            signal.signal(signum, disposition)
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
