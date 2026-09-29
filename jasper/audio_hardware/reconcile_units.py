# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The reconcile pass's systemd steps: role-unit gating, the output park,
the restarts and the coupling kick.

Each step reads the pass's state and names what it did on the pass's own
journal line; ``Pass.execute`` decides which ones run, and in what order.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from jasper.audio_hardware.reconcile_common import _Abort, _log_token
from jasper.service_units import (
    AEC_RECONCILE_SERVICE,
    FANIN_SERVICE,
    JASPER_VOICE_SERVICE,
    OUTPUTD_SERVICE,
    SYSTEMCTL_TIMEOUT_SEC,
    run_systemctl,
)

if TYPE_CHECKING:
    from jasper.audio_hardware.reconcile import Pass

DAC_INIT_UNIT = "jasper-dac-init.service"
HEADPHONE_MONITOR_UNIT = "jasper-headphone-monitor.service"
OUTPUTD_UNIT = OUTPUTD_SERVICE
VOICE_UNIT = JASPER_VOICE_SERVICE
AEC_RECONCILE_UNIT = AEC_RECONCILE_SERVICE
FANIN_UNIT = FANIN_SERVICE
COUPLING_AUTO_UNIT = "jasper-fanin-coupling-auto.service"

#: ``systemctl_call(timeout=...)``'s "use SYSTEMCTL_TIMEOUT_SEC" default, read
#: at CALL time rather than bound at import. Not a legal timeout itself.
_MANAGER_BOUND = -1.0


def systemctl_call(
    run: Pass, *args: str, quiet: bool = False, timeout: float | None = _MANAGER_BOUND
) -> int:
    """Run one systemctl verb. Returns its exit code, never raises.

    ``timeout`` bounds only how long an UNRESPONSIVE MANAGER may stall the
    pass; the default is :data:`SYSTEMCTL_TIMEOUT_SEC`, read at call time.
    A BLOCKING lifecycle verb passes ``None`` and is bounded instead by
    this unit's own ``TimeoutStartSec=50s``
    (``deploy/systemd/jasper-audio-hardware-reconcile.service``), the way
    the shell reconciler ran them: capping ``stop jasper-voice.service``
    below that unit's ``TimeoutStopSec=14s`` would report a failure for a
    stop that is still within its shutdown budget, and capping
    an ``enable`` would abort the pass mid-restart on a slow daemon-reload.
    """
    bound = SYSTEMCTL_TIMEOUT_SEC if timeout == _MANAGER_BOUND else timeout
    try:
        return run_systemctl(
            args, executable=run.systemctl, capture_output=False, quiet=quiet,
            timeout=bound,
        ).returncode
    except OSError:
        return 127
    except subprocess.TimeoutExpired:
        # An unresponsive manager is a FAILED call, not a reason to hang a
        # pass that runs from udev. 124 is timeout(1)'s own code.
        run.log(
            "systemctl_timeout",
            command=" ".join(args),
            timeout_sec=bound,
        )
        return 124


def systemctl_required(
    run: Pass, *args: str, timeout: float | None = _MANAGER_BOUND
) -> None:
    rc = systemctl_call(run, *args, timeout=timeout)
    if rc != 0:
        raise _Abort(rc)


def bounce(run: Pass, unit: str, verb: str, *, quiet: bool = True) -> None:
    """Clear a parked unit's failure state, then ask systemd for the
    transition WITHOUT waiting on it.

    --no-block throughout: this runs from udev and from install, where a
    blocking transition can deadlock against the jobs it waits on. The two
    BLOCKING starts in :func:`gate_role_services` are deliberately not this
    (see their own note) and stay written out.

    The direct-systemctl spelling of the broker's ``reset_then_manage``:
    this pass is a ROOT oneshot, which is outside the client set
    :mod:`jasper.control.restart_broker` exists for (see its docstring's
    "NOT brokered, by design").
    """
    systemctl_call(run, "reset-failed", unit, quiet=True)
    systemctl_call(run, "--no-block", verb, unit, quiet=quiet)


def restart_dac_init_for_record_change(run: Pass) -> None:
    """Restart the mixer pin only when the record it reads CHANGED.

    RemainAfterExit makes a plain `start` a no-op otherwise, and a restart
    per pass would spawn an interpreter on every udev sound event
    (ADR-0226). --no-block: the unit is ordered Before= camilla and the
    renderers. Called from gate_role_services AND from every early exit
    between the record write and it, so a pass that aborts there still
    restarts the pin its own changed record earned.
    """
    if not run.record_changed:
        return
    bounce(run, DAC_INIT_UNIT, "restart", quiet=False)
    run.log("dac_init_restarted", output_dac_id=run.output_dac_id or "unknown")


def gate_role_services(run: Pass) -> None:
    # The monitor exists to re-pin ONE mixer control, so the DAC declaring
    # that control is the whole condition for running it — the classifier
    # answers it off the registry (ADR-0235 R2).
    apple_output = bool(run.observed.headphone_control)
    # The pin is enabled on every box: which controls a DAC pins is the
    # registry's answer, and jasper-dac-init is where it is asked.
    # --no-reload: neither unit's file is written by this pass, so the
    # implicit reload `enable` would otherwise trigger buys nothing and
    # only costs time under memory pressure (#3639, the same fix
    # jasper/audio_routes/source_intent.py's _UNIT_ENABLEMENT_VERBS already made).
    systemctl_required(run, "enable", "--no-reload", DAC_INIT_UNIT, timeout=None)
    if run.record_changed:
        restart_dac_init_for_record_change(run)
    else:
        systemctl_call(run, "reset-failed", DAC_INIT_UNIT, quiet=True)
        systemctl_call(run, "start", DAC_INIT_UNIT, timeout=None)
    if apple_output:
        systemctl_required(
            run, "enable", "--no-reload", HEADPHONE_MONITOR_UNIT, timeout=None
        )
        # Idempotent start, never a restart: this gate runs on every
        # udev/reconcile pass and a deploy's core-audio bounce fires it
        # several times inside StartLimitIntervalSec. reset-failed clears a
        # parked state; start is a no-op when it is already running, and
        # the monitor re-resolves the card in its own poll loop.
        systemctl_call(run, "reset-failed", HEADPHONE_MONITOR_UNIT, quiet=True)
        systemctl_call(run, "start", HEADPHONE_MONITOR_UNIT, timeout=None)
        run.log(
            "apple_services", state="enabled", output_dac_id=run.output_dac_id
        )
    else:
        systemctl_call(
            run, "disable", "--now", HEADPHONE_MONITOR_UNIT, quiet=True, timeout=None
        )
        systemctl_call(run, "reset-failed", HEADPHONE_MONITOR_UNIT, quiet=True)
        run.log(
            "apple_services", state="disabled", output_dac_id=run.output_dac_id
        )


def park_output_audio(run: Pass) -> None:
    if run.no_restart:
        run.log(
            "output_park_skip",
            output_dac_id=run.output_dac_id,
            recognized=int(run.output_dac_recognized),
            no_restart=1,
        )
        return
    systemctl_call(run, "--no-block", "stop", VOICE_UNIT, OUTPUTD_UNIT, quiet=True)
    systemctl_call(run, "reset-failed", VOICE_UNIT, OUTPUTD_UNIT, quiet=True)
    run.log(
        "output_parked",
        output_dac_id=run.output_dac_id,
        output_dac_card=run.output_dac_card,
        recognized=int(run.output_dac_recognized),
        observed_blockers=_log_token(
            ",".join(run.observed.blocker_codes) or "none"
        ),
    )


def install_profile(path: str) -> str:
    """The box's install profile; only a genuinely absent marker gets the
    historical full-brain default."""
    marker = Path(path)
    if not marker.exists() and not marker.is_symlink():
        return "full"
    try:
        first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return "unknown"
    return first or "unknown"


def restart_audio_if_needed(run: Pass) -> None:
    if run.no_restart:
        return
    profile = install_profile(run.install_profile_file)
    if profile == "full":
        systemctl_call(run, "stop", VOICE_UNIT, quiet=True, timeout=None)
    # These are SEPARATE transactions, deliberately unordered. Correctness
    # does not depend on winning the race with jasper-aec-init: it refuses
    # to certify a STATUS older than outputd.env and the AEC reconciler
    # drops to software AEC3, keeping hearing until a later pass re-arms the
    # chip (ADR-0101).
    bounce(run, OUTPUTD_UNIT, "restart")
    if profile == "full":
        # Not `bounce`: this oneshot declares no start-rate limit, so it has
        # no parked state a reset-failed would have to clear first.
        systemctl_call(
            run, "--no-block", "restart", AEC_RECONCILE_UNIT, quiet=True
        )
    run.log(
        "audio_restarted",
        output_dac_id=run.output_dac_id,
        output_dac_card=run.output_dac_card,
        brain_restarted=1 if profile == "full" else 0,
        install_profile=_log_token(profile[:32]),
    )


def restart_outputd_only(run: Pass) -> None:
    """Bounce ONLY jasper-outputd for an outputd.env-only change.

    None of those can move the mic/input profile, so unlike the full path
    this must NOT stop jasper-voice or kick the AEC reconciler: wake
    detection stays up across the restart instead of being deafened for
    ~10-15 s on a routine /sources/ toggle (#1257).
    """
    if run.no_restart:
        run.log(
            "outputd_only_restart_skip",
            output_dac_id=run.output_dac_id,
            output_dac_card=run.output_dac_card,
            no_restart=1,
        )
        return
    bounce(run, OUTPUTD_UNIT, "restart")
    run.log(
        "outputd_only_restarted",
        output_dac_id=run.output_dac_id,
        output_dac_card=run.output_dac_card,
    )


def restart_route_runtime_if_needed(run: Pass) -> None:
    if run.no_restart:
        return
    if run.route_fanin_changed:
        bounce(run, FANIN_UNIT, "restart")
    run.log(
        "route_runtime_restarted",
        fanin_env=run.fanin_env_file,
        fanin_restarted=int(run.route_fanin_changed),
    )


def kick_fanin_coupling_auto(run: Pass, dac_changed: int, render_moved: int) -> None:
    """Ask the coupling reconciler to converge after a successful runtime
    graph convergence — the DAC-swap edge (#2285 P7).

    --no-block IS LOAD-BEARING: the coupling pass kicks this unit back
    synchronously during its arm, so a blocking start would wait on a pass
    waiting on this one. systemd also coalesces starts of an already-active
    oneshot, so each external event causes at most one follow-up pass and
    the pair cannot ping-pong.
    """
    if run.no_restart:
        result = "skipped_no_restart"
    else:
        # Refused while an install owns the core-graph window (#5470).
        rc = systemctl_call(run, "--no-block", "start", COUPLING_AUTO_UNIT, quiet=True)
        result = "started" if rc == 0 else "start_failed"
    run.log(
        "coupling_kick",
        result=result,
        dac_env_changed=dac_changed,
        render_changed=render_moved,
    )


def start_outputd_if_recognized(run: Pass) -> None:
    if run.no_restart:
        run.log(
            "outputd_start_skip",
            output_dac_id=run.output_dac_id,
            output_dac_card=run.output_dac_card,
            recognized=1,
            no_restart=1,
        )
        return
    bounce(run, OUTPUTD_UNIT, "start")
    run.log(
        "outputd_start_requested",
        output_dac_id=run.output_dac_id,
        output_dac_card=run.output_dac_card,
        recognized=1,
    )
