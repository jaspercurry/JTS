# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered arm/disarm of the fan-in -> CamillaDSP coupling.

WHY THIS EXISTS — the two daemons must transition in a specific order.
:mod:`jasper.fanin_coupling` owns the *vocabulary* (the flag, the pipe path, the
emit kwargs); this module owns the *transition* across all three audio daemons.
The ``transport_pipe`` coupling is an end-to-end DAC-paced path:

    fan-in -> RawFile pipe -> CamillaDSP -> File pipe -> outputd -> DAC

Both pipe boundaries matter. CamillaDSP's ``RawFile`` capture reads the pipe
``jasper-fanin`` writes, and its ``File`` playback writes the pipe outputd reads.
CamillaDSP **crash-loops on its statefile RawFile config whenever the capture
pipe has no writer** (verified on jts5 / CamillaDSP 4.1.3, 2026-06-27), and a
``File`` playback can block until outputd opens the read side. So the order is
not optional:

- **ARM** (loopback -> transport_pipe): outputd MUST read the local content pipe
  first, fan-in MUST write the capture pipe second, and only then may CamillaDSP
  load the RawFile/File config. Order: write fanin.env + outputd.env -> restart
  outputd -> restart fan-in -> reconcile CamillaDSP. On clean boot, systemd
  gives the same reader/writer rendezvous.

- **DISARM** (transport_pipe -> loopback): CamillaDSP must leave the RawFile/File
  config before either endpoint is moved back to ALSA. Order: write loopback env
  -> reconcile CamillaDSP -> restart fan-in -> restart outputd. A sub-second
  silence spans the transition; it is acceptable on a deliberate operator
  change and it never strands Camilla on a config it cannot open.

SINGLE WRITER. This module is the sole writer of the topology keys it owns:
``JASPER_FANIN_CAMILLA_COUPLING`` in ``/var/lib/jasper/fanin.env`` and
``JASPER_OUTPUTD_LOCAL_CONTENT_PIPE`` in ``/var/lib/jasper/outputd.env``. The
order-preserving single-key helpers (:mod:`jasper.env_file`) leave neighboring
operator/reconciler lines intact.

FAIL-SAFE DIRECTION = loopback (the byte-identical-to-today path). Any failure
during ARM rolls the whole transition back to loopback (env + camilla + fan-in)
so a half-applied coupling never strands the realtime path. ``reconcile_camilla``
itself fail-closes on an invalid config (CamillaDSP ``--check`` rejects it; the
apply never loads it), so the worst case is "stayed on / reverted to loopback",
never a bricked DSP. The result carries ``ok`` so a caller's own ladder can
react; daemon-op failures are reported, not raised.

NOT a per-tick hot path. This runs on a deliberate coupling change (a CLI / the
deploy), not in the mux loop — a real transition bounces the SHARED fan-in
daemon (a brief all-source glitch), which is why it is change-gated, not polled.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jasper.atomic_io import atomic_write_text
from jasper.audio_runtime_plan import RouteMode, RuntimeEnvAction, fanin_coupling_action
from jasper.camilla_config_contract import DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT
from jasper.env_file import read_value, remove, upsert
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_TRANSPORT_PIPE,
    PIPE_PATH_ENV_VAR,
    OUTPUTD_PIPE_PATH_ENV_VAR,
    resolve_coupling,
    resolve_pipe_path,
    resolve_outputd_pipe_path,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"
FANIN_UNIT = "jasper-fanin.service"
OUTPUTD_UNIT = "jasper-outputd.service"
ACTIVE_LEADER_TRANSPORT_BLOCK_REASON = "active_leader_transport_pipe_coupling_unsupported"
FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"
OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"

# Activation gate for the experimental end-to-end pipe topology. The defaults
# are intentionally short enough for an operator CLI, but long enough to catch
# the failure observed on JTS: pipe occupancy and fan-in catchup counters ran
# away within tens of seconds.
TRANSPORT_PIPE_GATE_WARMUP_SECONDS = 2.0
TRANSPORT_PIPE_GATE_WINDOW_SECONDS = 12.0
TRANSPORT_PIPE_MAX_FANIN_OUTPUT_DROP_DELTA = 0
TRANSPORT_PIPE_MAX_FANIN_INPUT_XRUN_DELTA = 2
TRANSPORT_PIPE_MAX_FANIN_INPUT_CATCHUP_DELTA = 8
TRANSPORT_PIPE_MAX_OUTPUTD_EMPTY_DELTA = 4
TRANSPORT_PIPE_MAX_OUTPUTD_PARTIAL_DELTA = 2
TRANSPORT_PIPE_USB_INPUT_LABEL = "usbsink"
TRANSPORT_PIPE_USB_RESAMPLER_FILL_TOLERANCE_PERIODS = 4
TRANSPORT_PIPE_USB_RESAMPLER_MIN_FILL_TOLERANCE_FRAMES = 512

# A daemon op (fan-in restart or camilla reconcile) returns (ok, detail).
DaemonOp = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class CouplingResult:
    """Outcome of a coupling reconcile.

    ``ok`` is True only when the env write AND every daemon op the chosen
    direction needs succeeded (or there was nothing to do). ``changed`` is True
    when the persisted env value actually moved. ``direction`` is ``arm`` /
    ``disarm`` / ``confirm`` (env already at desired — camilla re-confirmed, no
    fan-in bounce). ``recovered`` is True when an ARM failure rolled the box back
    to loopback. ``detail`` carries the first failure's reason for the log/CLI.
    """

    ok: bool
    desired: str
    changed: bool
    direction: str
    restarted_fanin: bool = False
    restarted_outputd: bool = False
    reconciled_camilla: bool = False
    validated_transport_pipe: bool = False
    recovered: bool = False
    detail: str = ""


@dataclass(frozen=True)
class _EnvSnapshot:
    path: Path
    text: str
    existed: bool


def _restart_unit(unit: str, *, reason: str, timeout: float) -> tuple[bool, str]:
    """Restart a systemd unit through the broker. (ok, detail).

    Guarded lazy import (mirrors buffer_reconcile SF-2): a missing/broken
    control package degrades to a reported failure, never an exception out of
    the reconcile that would defeat the fail-safe ladder.
    """
    try:
        from jasper.control import restart_broker
    except ImportError as e:  # pragma: no cover - control pkg always present in prod
        return False, f"restart_broker unavailable: {e}"
    resp = restart_broker.manage_units(
        unit, verb="restart", reason=reason, no_block=False, timeout=timeout,
    )
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("error") or f"rc={resp.get('rc')}")


def _restart_fanin(reason: str) -> tuple[bool, str]:
    """Restart jasper-fanin through the broker. (ok, detail)."""
    return _restart_unit(FANIN_UNIT, reason=reason, timeout=8.0)


def _restart_outputd(reason: str) -> tuple[bool, str]:
    """Restart jasper-outputd through the broker. (ok, detail)."""
    return _restart_unit(OUTPUTD_UNIT, reason=reason, timeout=8.0)


def _reconcile_camilla(coupling: str, *, reason: str) -> tuple[bool, str]:
    """Re-emit + load the CamillaDSP config for ``coupling``. (ok, detail).

    Forces a full reconcile (``force=True``) so the capture flips even on a flat
    profile (the coupling IS the change), and passes ``coupling`` explicitly so
    the emit does not depend on this process's stale ``os.environ`` (the env file
    was just rewritten under us). reconcile_current_dsp validates with
    ``camilladsp --check`` before loading and fail-closes on an invalid config,
    so a failure here leaves the previously-loaded config running.
    """
    import asyncio

    from jasper.sound.runtime import reconcile_current_dsp

    try:
        payload = asyncio.run(reconcile_current_dsp(force=True, coupling=coupling))
    except Exception as e:  # noqa: BLE001 - report, never raise out of the reconcile
        return False, f"camilla reconcile raised: {e}"
    status = payload.get("status")
    if status in ("reconciled", "unchanged"):
        return True, str(status)
    if status == "skipped" and coupling != COUPLING_TRANSPORT_PIPE:
        return True, str(status)
    return False, str(payload.get("reason") or status or "unknown")


def reconcile_coupling(
    desired_raw: str | None,
    *,
    reason: str,
    env_path: str | Path = FANIN_ENV_PATH,
    outputd_env_path: str | Path = OUTPUTD_ENV_PATH,
    apply: bool = True,
    restart_fanin: "DaemonOp | None" = None,
    restart_outputd: "DaemonOp | None" = None,
    reconcile_camilla=None,
    validate_transport_pipe: "DaemonOp | None" = None,
    active_leader_check: "Callable[[], bool] | None" = None,
) -> CouplingResult:
    """Make the live fan-in->Camilla coupling match ``desired_raw``, in order.

    ``desired_raw`` is normalized by :func:`resolve_coupling` (unknown/typo ->
    loopback, fail-safe). Writes the persisted env, then runs the direction's
    ordered daemon ops:

    - ARM (-> transport_pipe): restart outputd, restart fan-in, then reconcile
      camilla. On any failure, roll the whole box back to loopback
      (``recovered=True``) and report ``ok=False``.
    - DISARM (-> loopback): reconcile camilla, restart fan-in, then restart
      outputd. A camilla failure still proceeds to both restarts and reports
      ``ok=False``.
    - CONFIRM (env already at desired): re-run only the camilla reconcile to
      self-heal a drifted loaded config, WITHOUT bouncing fan-in.

    ``apply=False`` writes the env only (no daemon ops) — for staging/migration.
    ``restart_fanin`` / ``restart_outputd`` / ``reconcile_camilla`` /
    ``active_leader_check`` are injectable for tests (default to the real broker
    + reconcile_current_dsp + grouping-state reader); the camilla hook takes the
    resolved coupling string.
    """
    do_restart = restart_fanin or (lambda: _restart_fanin(reason=reason))
    do_restart_outputd = restart_outputd or (lambda: _restart_outputd(reason=reason))

    def do_reconcile(coupling: str) -> tuple[bool, str]:
        if reconcile_camilla is not None:
            return reconcile_camilla(coupling)
        return _reconcile_camilla(coupling, reason=reason)

    do_validate_transport_pipe = (
        validate_transport_pipe or _validate_transport_pipe_activation
    )

    fanin_snapshot = _read_snapshot(env_path)
    outputd_snapshot = _read_snapshot(outputd_env_path)
    current = resolve_coupling(read_value(fanin_snapshot.text, COUPLING_ENV_VAR))

    route_mode = _route_mode_for_reconcile(active_leader_check)
    action, support = fanin_coupling_action(desired_raw, route_mode)
    desired = support.coupling
    if not support.supported:
        return _block_transport_for_active_leader(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot,
            outputd_snapshot,
            current,
            reason,
            block_detail=support.detail,
            apply=apply,
        )

    if action is None:
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="error",
            detail=support.detail or "unsupported coupling action",
        )
    fanin_new_text, fanin_changed = _apply_action(fanin_snapshot.text, action)
    outputd_action = _outputd_local_pipe_action(desired, outputd_snapshot.text)
    outputd_new_text, outputd_changed = _apply_action(
        outputd_snapshot.text, outputd_action
    )
    changed = fanin_changed or outputd_changed

    # Persist the desired value first (single source of truth for the daemons'
    # next start). A write failure aborts BEFORE any daemon op so we never bounce
    # a daemon into a value the file doesn't carry.
    if changed:
        try:
            if fanin_changed:
                _write_env_text(fanin_snapshot.path, fanin_new_text)
            if outputd_changed:
                _write_env_text(outputd_snapshot.path, outputd_new_text)
        except OSError as e:
            _restore_snapshot(fanin_snapshot)
            _restore_snapshot(outputd_snapshot)
            log_event(
                logger, "fanin.coupling_reconcile", result="write_failed",
                desired=desired, reason=reason, error=e, level=logging.ERROR,
            )
            return CouplingResult(
                ok=False, desired=desired, changed=False, direction="error",
                detail=str(e),
            )

    _sync_process_env_for_emit(desired, fanin_new_text, outputd_new_text)

    if not apply:
        log_event(
            logger, "fanin.coupling_reconcile", result="written",
            desired=desired, changed=changed, reason=reason,
        )
        return CouplingResult(
            ok=True, desired=desired, changed=changed,
            direction="arm" if desired == COUPLING_TRANSPORT_PIPE else "disarm",
        )

    if not changed:
        # Env already at desired: re-confirm camilla only (self-heal a drifted
        # loaded config) — no fan-in bounce on a no-op tick.
        ok, detail = do_reconcile(desired)
        validated = False
        if ok and desired == COUPLING_TRANSPORT_PIPE:
            ok, detail, validated, recovered = _run_transport_pipe_gate(
                do_restart,
                do_restart_outputd,
                do_reconcile,
                do_validate_transport_pipe,
                fanin_snapshot.path,
                outputd_snapshot.path,
                reason,
            )
            if not ok:
                log_event(
                    logger,
                    "fanin.coupling_reconcile",
                    result="confirm_transport_pipe_gate_failed",
                    desired=desired,
                    reason=reason,
                    detail=detail or None,
                    recovered=recovered,
                    level=logging.WARNING,
                )
                return CouplingResult(
                    ok=False,
                    desired=desired,
                    changed=True,
                    direction="confirm",
                    restarted_fanin=recovered,
                    restarted_outputd=recovered,
                    reconciled_camilla=True,
                    validated_transport_pipe=False,
                    recovered=recovered,
                    detail=detail,
                )
        log_event(
            logger, "fanin.coupling_reconcile",
            result="confirmed" if ok else "confirm_failed",
            desired=desired, reason=reason, detail=detail or None,
            level=logging.INFO if ok else logging.WARNING,
        )
        return CouplingResult(
            ok=ok, desired=desired, changed=False, direction="confirm",
            reconciled_camilla=ok, validated_transport_pipe=validated,
            detail="" if ok else detail,
        )

    if desired == COUPLING_TRANSPORT_PIPE:
        return _arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            do_validate_transport_pipe,
        )
    return _disarm(do_restart, do_restart_outputd, do_reconcile, desired, reason)


def _route_mode_for_reconcile(check: "Callable[[], bool] | None") -> RouteMode:
    """Return the route shape for the coupling support matrix."""
    if check is not None:
        try:
            return "active_leader" if bool(check()) else "solo"
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="active_leader_check_failed",
                detail=e,
                level=logging.WARNING,
            )
            return "unknown"
    try:
        from jasper.audio_runtime_plan import route_mode_from_grouping_config
        from jasper.multiroom.config import load_config

        return route_mode_from_grouping_config(load_config())
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as e:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="active_leader_check_failed",
            detail=e,
            level=logging.DEBUG,
        )
        return "unknown"


def _block_transport_for_active_leader(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    fanin_snapshot: _EnvSnapshot,
    outputd_snapshot: _EnvSnapshot,
    current: str,
    reason: str,
    *,
    block_detail: str | None = None,
    apply: bool,
) -> CouplingResult:
    detail = block_detail or (
        "JASPER_FANIN_CAMILLA_COUPLING=transport_pipe is not supported while this box is "
        "an active multiroom leader; keep the fan-in coupling on loopback until "
        "the grouped active-leader transport-pipe topology exists"
    )
    fanin_action = RuntimeEnvAction("set", COUPLING_ENV_VAR, COUPLING_LOOPBACK)
    outputd_action = RuntimeEnvAction("unset", OUTPUTD_PIPE_PATH_ENV_VAR)
    fanin_new_text, fanin_changed = _apply_action(fanin_snapshot.text, fanin_action)
    outputd_new_text, outputd_changed = _apply_action(
        outputd_snapshot.text, outputd_action
    )
    stale_transport = current == COUPLING_TRANSPORT_PIPE or outputd_changed
    if stale_transport:
        try:
            if fanin_changed:
                _write_env_text(fanin_snapshot.path, fanin_new_text)
            if outputd_changed:
                _write_env_text(outputd_snapshot.path, outputd_new_text)
        except OSError as e:
            _restore_snapshot(fanin_snapshot)
            _restore_snapshot(outputd_snapshot)
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result=ACTIVE_LEADER_TRANSPORT_BLOCK_REASON,
                action="loopback_write_failed",
                reason=reason,
                detail=e,
                level=logging.ERROR,
            )
            return CouplingResult(
                ok=False,
                desired=COUPLING_TRANSPORT_PIPE,
                changed=False,
                direction="blocked",
                detail=f"{detail}; failed to write loopback fallback: {e}",
            )
        _sync_process_env_for_emit(COUPLING_LOOPBACK, fanin_new_text, outputd_new_text)
        if apply:
            disarm = _disarm(
                do_restart,
                do_restart_outputd,
                do_reconcile,
                COUPLING_LOOPBACK,
                reason,
            )
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result=ACTIVE_LEADER_TRANSPORT_BLOCK_REASON,
                action="recovered_to_loopback",
                reason=reason,
                recovered=disarm.ok,
                detail=disarm.detail or None,
                level=logging.WARNING,
            )
            return CouplingResult(
                ok=False,
                desired=COUPLING_TRANSPORT_PIPE,
                changed=True,
                direction="blocked",
                restarted_fanin=disarm.restarted_fanin,
                restarted_outputd=disarm.restarted_outputd,
                reconciled_camilla=disarm.reconciled_camilla,
                recovered=disarm.ok,
                detail=detail if disarm.ok else f"{detail}; {disarm.detail}",
            )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result=ACTIVE_LEADER_TRANSPORT_BLOCK_REASON,
            action="wrote_loopback_no_apply",
            reason=reason,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=COUPLING_TRANSPORT_PIPE,
            changed=True,
            direction="blocked",
            detail=detail,
        )

    log_event(
        logger,
        "fanin.coupling_reconcile",
        result=ACTIVE_LEADER_TRANSPORT_BLOCK_REASON,
        action="kept_loopback",
        reason=reason,
        level=logging.WARNING,
    )
    return CouplingResult(
        ok=False,
        desired=COUPLING_TRANSPORT_PIPE,
        changed=False,
        direction="blocked",
        detail=detail,
    )


def _arm(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    desired,
    reason,
    fanin_snapshot,
    outputd_snapshot,
    do_validate_transport_pipe,
) -> CouplingResult:
    """outputd first, fan-in second, Camilla last. Roll back to loopback on any
    failure so we never leave a half-piped chain."""
    out_ok, out_detail = do_restart_outputd()
    if not out_ok:
        recovered = _recover_to_loopback(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_outputd_failed",
            desired=desired, reason=reason, detail=out_detail or None,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            restarted_outputd=False, detail=out_detail, recovered=recovered,
        )

    fan_ok, fan_detail = do_restart()
    if not fan_ok:
        recovered = _recover_to_loopback(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_fanin_failed",
            desired=desired, reason=reason, detail=fan_detail or None,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            restarted_outputd=True, detail=fan_detail, recovered=recovered,
        )

    cam_ok, cam_detail = do_reconcile(COUPLING_TRANSPORT_PIPE)
    if not cam_ok:
        recovered = _recover_to_loopback(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_camilla_failed",
            desired=desired, reason=reason, detail=cam_detail or None,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            restarted_fanin=True, restarted_outputd=True,
            detail=cam_detail, recovered=recovered,
        )

    gate_ok, gate_detail, validated, recovered = _run_transport_pipe_gate(
        do_restart,
        do_restart_outputd,
        do_reconcile,
        do_validate_transport_pipe,
        fanin_snapshot.path,
        outputd_snapshot.path,
        reason,
    )
    if not gate_ok:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="arm_transport_pipe_gate_failed",
            desired=desired,
            reason=reason,
            detail=gate_detail or None,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=True,
            direction="arm",
            restarted_fanin=True,
            restarted_outputd=True,
            reconciled_camilla=True,
            validated_transport_pipe=False,
            recovered=recovered,
            detail=gate_detail,
        )

    log_event(
        logger, "fanin.coupling_reconcile", result="armed",
        desired=desired, reason=reason, detail=gate_detail or cam_detail or None,
    )
    return CouplingResult(
        ok=True, desired=desired, changed=True, direction="arm",
        restarted_fanin=True, restarted_outputd=True, reconciled_camilla=True,
        validated_transport_pipe=validated,
    )


def _run_transport_pipe_gate(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    do_validate_transport_pipe,
    fanin_path,
    outputd_path,
    reason,
) -> tuple[bool, str, bool, bool]:
    """Run the transport-pipe activation gate and recover on failure.

    Returns ``(ok, detail, validated, recovered)``. The gate is part of the
    transition, not a passive doctor warning: if the new topology immediately
    shows the queue/counter drift that produced rough audio on JTS, force the
    same fail-safe loopback recovery as an ordinary arm failure.
    """
    try:
        gate_ok, gate_detail = do_validate_transport_pipe()
    except Exception as e:  # noqa: BLE001 - gate failures must fail safe
        gate_ok = False
        gate_detail = f"transport_pipe activation gate raised: {e}"
    if gate_ok:
        return True, gate_detail, True, False
    recovered = _recover_to_loopback(
        do_restart,
        do_restart_outputd,
        do_reconcile,
        fanin_path,
        outputd_path,
        reason,
    )
    return False, gate_detail, False, recovered


def _validate_transport_pipe_activation() -> tuple[bool, str]:
    """Short live-health gate for the experimental dual-pipe topology.

    A successful config load only proves that every endpoint opened. It does not
    prove the topology is behaving as a low-latency transport. This gate samples
    the live fan-in and outputd STATUS surfaces across a short window and fails
    on the signatures observed during the JTS transport-pipe test: growing fan-in
    catchup/xrun counters, fan-in pipe drops, DAC/content xruns, outputd local
    pipe starvation, or hidden queued pipe latency.
    """
    return validate_transport_pipe_status_window()


def validate_transport_pipe_status_window(
    *,
    warmup_seconds: float = TRANSPORT_PIPE_GATE_WARMUP_SECONDS,
    window_seconds: float = TRANSPORT_PIPE_GATE_WINDOW_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    read_fanin_status: Callable[[], tuple[dict[str, object] | None, str]]
    | None = None,
    read_outputd_status: Callable[[], tuple[dict[str, object] | None, str]]
    | None = None,
) -> tuple[bool, str]:
    """Validate that transport_pipe stays stable across one short window.

    Kept public-ish for unit tests and future soak tooling; the CLI uses it
    through :func:`_validate_transport_pipe_activation`.
    """
    fanin_reader = read_fanin_status or (
        lambda: _read_status_socket(FANIN_STATUS_SOCKET)
    )
    outputd_reader = read_outputd_status or (
        lambda: _read_status_socket(OUTPUTD_STATUS_SOCKET)
    )
    if warmup_seconds > 0:
        sleep(warmup_seconds)

    start_fanin, err = fanin_reader()
    if start_fanin is None:
        return False, f"fan-in STATUS unavailable before gate: {err}"
    start_outputd, err = outputd_reader()
    if start_outputd is None:
        return False, f"outputd STATUS unavailable before gate: {err}"
    ok, detail = _transport_pipe_shape_ok(start_fanin, start_outputd)
    if not ok:
        return False, detail

    if window_seconds > 0:
        sleep(window_seconds)

    end_fanin, err = fanin_reader()
    if end_fanin is None:
        return False, f"fan-in STATUS unavailable after gate: {err}"
    end_outputd, err = outputd_reader()
    if end_outputd is None:
        return False, f"outputd STATUS unavailable after gate: {err}"
    ok, detail = _transport_pipe_shape_ok(end_fanin, end_outputd)
    if not ok:
        return False, detail

    issues = _transport_pipe_delta_issues(
        start_fanin,
        end_fanin,
        start_outputd,
        end_outputd,
    )
    if issues:
        return False, "; ".join(issues)

    available_frames = _outputd_local_pipe_available_frames(end_outputd)
    return (
        True,
        "transport_pipe activation gate ok "
        f"window_seconds={window_seconds:g} "
        f"outputd_local_pipe_available_frames={available_frames}",
    )


def _read_status_socket(
    path: str,
    *,
    timeout: float = 1.5,
) -> tuple[dict[str, object] | None, str]:
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, str(e)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not isinstance(payload, dict):
        return None, f"STATUS payload is {type(payload).__name__}, not object"
    return payload, ""


def _transport_pipe_shape_ok(
    fanin: dict[str, object],
    outputd: dict[str, object],
) -> tuple[bool, str]:
    fanin_output = fanin.get("output")
    if not isinstance(fanin_output, dict):
        return False, "fan-in STATUS missing output{}"
    if fanin_output.get("transport") != COUPLING_TRANSPORT_PIPE:
        return (
            False,
            f"fan-in transport={fanin_output.get('transport')!r}, "
            "expected transport_pipe",
        )
    fanin_pipe = fanin_output.get("pipe")
    if not isinstance(fanin_pipe, dict):
        return False, "fan-in transport_pipe STATUS missing output.pipe"
    fanin_actual = _int_value(fanin_pipe.get("actual_pipe_bytes"))
    if fanin_actual <= 0:
        return False, "fan-in capture pipe is not open"
    fanin_period = _int_value(fanin_output.get("period_frames"))
    if fanin_period > 0:
        # S32_LE stereo = 8 bytes/frame. Allow sixteen periods because Linux may
        # round a small FIFO request up to the system page-size floor.
        fanin_budget = fanin_period * 8 * 16
        if fanin_actual > fanin_budget:
            return (
                False,
                "fan-in capture pipe is too large for low-latency activation: "
                f"actual_bytes={fanin_actual} budget_bytes={fanin_budget}",
            )

    content = outputd.get("content")
    dac = outputd.get("dac")
    if not isinstance(content, dict):
        return False, "outputd STATUS missing content{}"
    if not isinstance(dac, dict):
        return False, "outputd STATUS missing dac{}"
    if content.get("source") != "local_pipe":
        return False, f"outputd content.source={content.get('source')!r}"
    local_pipe = content.get("local_pipe")
    if not isinstance(local_pipe, dict):
        return False, "outputd local_pipe source missing content.local_pipe"
    if not bool(local_pipe.get("open", False)):
        return False, "outputd local content pipe is not open"
    if local_pipe.get("format") != DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT:
        return (
            False,
            "outputd local content pipe format mismatch: "
            f"format={local_pipe.get('format')!r}, "
            f"expected {DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT}",
        )
    local_frame_bytes = _outputd_local_pipe_frame_bytes(outputd)
    local_actual = _int_value(local_pipe.get("actual_pipe_bytes"))
    if local_actual <= 0:
        return False, "outputd local content pipe has no actual capacity"
    period_frames = _int_value(dac.get("period_frames")) or _int_value(
        content.get("period_frames")
    )
    if period_frames <= 0:
        return False, "outputd STATUS missing period_frames"
    # Same budget as doctor: sixteen periods for kernel/page-size rounding, but
    # no giant hidden FIFO.
    local_budget = period_frames * local_frame_bytes * 16
    if local_actual > local_budget:
        return (
            False,
            "outputd local pipe is too large for low-latency activation: "
            f"actual_bytes={local_actual} budget_bytes={local_budget}",
        )
    available_frames = _outputd_local_pipe_available_frames(outputd)
    max_available = period_frames * 8
    if available_frames > max_available:
        return (
            False,
            "outputd local pipe is carrying hidden queued latency: "
            f"available_frames={available_frames} budget_frames={max_available}",
        )
    return True, ""


def _transport_pipe_delta_issues(
    start_fanin: dict[str, object],
    end_fanin: dict[str, object],
    start_outputd: dict[str, object],
    end_outputd: dict[str, object],
) -> list[str]:
    issues: list[str] = []

    start_output = _dict_value(start_fanin.get("output"))
    end_output = _dict_value(end_fanin.get("output"))
    output_drop_delta = _counter_delta(
        start_output.get("pipe"),
        end_output.get("pipe"),
        "dropped_periods",
    )
    if output_drop_delta > TRANSPORT_PIPE_MAX_FANIN_OUTPUT_DROP_DELTA:
        issues.append(f"fan-in pipe dropped_periods delta={output_drop_delta}")

    output_xrun_delta = _counter_delta(start_output, end_output, "xrun_count")
    if output_xrun_delta > 0:
        issues.append(f"fan-in output xrun_count delta={output_xrun_delta}")

    for label, deltas in _fanin_input_counter_deltas(start_fanin, end_fanin).items():
        xrun_delta = deltas.get("xrun_count", 0)
        if xrun_delta > TRANSPORT_PIPE_MAX_FANIN_INPUT_XRUN_DELTA:
            issues.append(f"fan-in input {label} xrun_count delta={xrun_delta}")
        catchup_delta = deltas.get("catchup_events", 0)
        if catchup_delta > TRANSPORT_PIPE_MAX_FANIN_INPUT_CATCHUP_DELTA:
            issues.append(
                f"fan-in input {label} catchup_events delta={catchup_delta}"
            )
    issues.extend(_usb_resampler_delta_issues(start_fanin, end_fanin))

    start_content = _dict_value(start_outputd.get("content"))
    end_content = _dict_value(end_outputd.get("content"))
    empty_delta = _counter_delta(start_content, end_content, "empty_periods")
    if empty_delta > TRANSPORT_PIPE_MAX_OUTPUTD_EMPTY_DELTA:
        issues.append(f"outputd local pipe empty_periods delta={empty_delta}")
    partial_delta = _counter_delta(start_content, end_content, "partial_periods")
    if partial_delta > TRANSPORT_PIPE_MAX_OUTPUTD_PARTIAL_DELTA:
        issues.append(f"outputd local pipe partial_periods delta={partial_delta}")
    content_xrun_delta = _counter_delta(start_content, end_content, "xrun_count")
    if content_xrun_delta > 0:
        issues.append(f"outputd content xrun_count delta={content_xrun_delta}")

    start_dac = _dict_value(start_outputd.get("dac"))
    end_dac = _dict_value(end_outputd.get("dac"))
    dac_xrun_delta = _counter_delta(start_dac, end_dac, "xrun_count")
    if dac_xrun_delta > 0:
        issues.append(f"outputd dac xrun_count delta={dac_xrun_delta}")

    return issues


def _usb_resampler_delta_issues(
    start_fanin: dict[str, object],
    end_fanin: dict[str, object],
) -> list[str]:
    """Require the USB clock crossing to be engaged only when USB is active."""
    start_input = _fanin_input_by_label(start_fanin, TRANSPORT_PIPE_USB_INPUT_LABEL)
    end_input = _fanin_input_by_label(end_fanin, TRANSPORT_PIPE_USB_INPUT_LABEL)
    if not end_input:
        return []
    start_resampler = _dict_value(start_input.get("resampler"))
    end_resampler = _dict_value(end_input.get("resampler"))
    frames_read_delta = _counter_delta(start_input, end_input, "frames_read")
    resampler_input_delta = _counter_delta(
        start_resampler,
        end_resampler,
        "input_frames",
    )
    active_delta = max(frames_read_delta, resampler_input_delta)
    if active_delta <= 0:
        return []

    prefix = f"fan-in input {TRANSPORT_PIPE_USB_INPUT_LABEL} resampler"
    if not end_resampler:
        return [f"{prefix} missing while USB frames flowed delta={active_delta}"]

    issues: list[str] = []
    if end_resampler.get("armed") is not True:
        issues.append(
            f"{prefix} not armed while USB frames flowed delta={active_delta}"
        )
    if end_resampler.get("locked") is not True:
        issues.append(
            f"{prefix} not locked while USB frames flowed delta={active_delta}"
        )

    unlock_delta = _counter_delta(start_resampler, end_resampler, "unlock_count")
    if unlock_delta > 0:
        issues.append(f"{prefix} unlock_count delta={unlock_delta}")
    overrun_delta = _counter_delta(start_resampler, end_resampler, "overrun_frames")
    if overrun_delta > 0:
        issues.append(f"{prefix} overrun_frames delta={overrun_delta}")

    target = _int_value(end_resampler.get("target_fill_frames"))
    fill = _int_value(end_resampler.get("fill_frames"))
    if target > 0:
        output = _dict_value(end_fanin.get("output"))
        period = _int_value(output.get("period_frames")) or 256
        tolerance = max(
            period * TRANSPORT_PIPE_USB_RESAMPLER_FILL_TOLERANCE_PERIODS,
            TRANSPORT_PIPE_USB_RESAMPLER_MIN_FILL_TOLERANCE_FRAMES,
        )
        fill_error = abs(fill - target)
        if fill_error > tolerance:
            issues.append(
                f"{prefix} fill_frames={fill} target_fill_frames={target} "
                f"tolerance_frames={tolerance}"
            )
    return issues


def _fanin_input_by_label(
    status: dict[str, object],
    label: str,
) -> dict[str, object]:
    inputs = status.get("inputs")
    if not isinstance(inputs, list):
        return {}
    for value in inputs:
        if isinstance(value, dict) and value.get("label") == label:
            return value
    return {}


def _fanin_input_counter_deltas(
    start: dict[str, object],
    end: dict[str, object],
) -> dict[str, dict[str, int]]:
    start_inputs = {
        str(inp.get("label") or idx): inp
        for idx, inp in enumerate(_dict_list_value(start.get("inputs")))
    }
    end_inputs = {
        str(inp.get("label") or idx): inp
        for idx, inp in enumerate(_dict_list_value(end.get("inputs")))
    }
    deltas: dict[str, dict[str, int]] = {}
    for label, end_input in end_inputs.items():
        start_input = start_inputs.get(label, {})
        if not isinstance(start_input, dict):
            start_input = {}
        deltas[label] = {
            "xrun_count": _counter_delta(start_input, end_input, "xrun_count"),
            "catchup_events": _counter_delta(
                start_input,
                end_input,
                "catchup_events",
            ),
        }
    return deltas


def _outputd_local_pipe_available_frames(outputd: dict[str, object]) -> int:
    content = _dict_value(outputd.get("content"))
    local_pipe = _dict_value(content.get("local_pipe"))
    return _int_value(local_pipe.get("available_bytes")) // _outputd_local_pipe_frame_bytes(
        outputd
    )


def _outputd_local_pipe_frame_bytes(outputd: dict[str, object]) -> int:
    content = _dict_value(outputd.get("content"))
    local_pipe = _dict_value(content.get("local_pipe"))
    channels = _int_value(content.get("channels")) or 2
    pipe_format = str(local_pipe.get("format") or "")
    bytes_per_sample = {
        "S16_LE": 2,
        "S32_LE": 4,
    }.get(pipe_format, 4)
    return max(1, channels * bytes_per_sample)


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _dict_list_value(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, object], item) for item in value if isinstance(item, dict)]


def _counter_delta(
    start: object,
    end: object,
    key: str,
) -> int:
    return max(
        0,
        _int_value(_dict_value(end).get(key))
        - _int_value(_dict_value(start).get(key)),
    )


def _int_value(value: object) -> int:
    if isinstance(value, int | float | str | bytes | bytearray):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _disarm(do_restart, do_restart_outputd, do_reconcile, desired, reason) -> CouplingResult:
    """Camilla first (off RawFile/File -> Alsa), then fan-in and outputd. Even
    if the camilla reconcile fails, still restart both endpoints to loopback."""
    cam_ok, cam_detail = do_reconcile(COUPLING_LOOPBACK)
    fan_ok, fan_detail = do_restart()
    out_ok, out_detail = do_restart_outputd()
    ok = cam_ok and fan_ok and out_ok
    detail = "; ".join(d for d in (cam_detail if not cam_ok else "",
                                   fan_detail if not fan_ok else "",
                                   out_detail if not out_ok else "") if d)
    log_event(
        logger, "fanin.coupling_reconcile",
        result="disarmed" if ok else "disarm_partial",
        desired=desired, reason=reason, detail=detail or None,
        level=logging.INFO if ok else logging.WARNING,
    )
    return CouplingResult(
        ok=ok, desired=desired, changed=True, direction="disarm",
        restarted_fanin=fan_ok, restarted_outputd=out_ok,
        reconciled_camilla=cam_ok, detail=detail,
    )


def _recover_to_loopback(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    fanin_path,
    outputd_path,
    reason,
) -> bool:
    """ARM-failure recovery: force the whole box back to loopback (env + camilla
    Alsa + fan-in loopback + outputd ALSA). Returns True iff the recovery fully
    succeeded."""
    del reason
    try:
        existing = Path(fanin_path).read_text(encoding="utf-8")
    except OSError:
        existing = ""
    new_text, _ = upsert(existing, COUPLING_ENV_VAR, COUPLING_LOOPBACK)
    try:
        _write_env_text(Path(fanin_path), new_text)
    except OSError:
        return False
    try:
        existing_outputd = Path(outputd_path).read_text(encoding="utf-8")
    except OSError:
        existing_outputd = ""
    new_outputd, _ = remove(existing_outputd, OUTPUTD_PIPE_PATH_ENV_VAR)
    try:
        _write_env_text(Path(outputd_path), new_outputd)
    except OSError:
        return False
    _sync_process_env_for_emit(COUPLING_LOOPBACK, new_text, new_outputd)
    cam_ok, _ = do_reconcile(COUPLING_LOOPBACK)
    fan_ok, _ = do_restart()
    out_ok, _ = do_restart_outputd()
    return cam_ok and fan_ok and out_ok


def _read_snapshot(path: str | Path) -> _EnvSnapshot:
    env_path = Path(path)
    try:
        return _EnvSnapshot(env_path, env_path.read_text(encoding="utf-8"), True)
    except OSError:
        return _EnvSnapshot(env_path, "", False)


def _restore_snapshot(snapshot: _EnvSnapshot) -> None:
    """Restore the env file to its pre-write contents. Best-effort."""
    try:
        if snapshot.existed:
            atomic_write_text(snapshot.path, snapshot.text)
        elif snapshot.path.exists():
            snapshot.path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_env_text(path: Path, text: str) -> None:
    if text:
        atomic_write_text(path, text)
    elif path.exists():
        path.unlink(missing_ok=True)


def _apply_action(text: str, action: RuntimeEnvAction) -> tuple[str, bool]:
    if action.action == "set":
        return upsert(text, action.key, action.value)
    return remove(text, action.key)


def _outputd_local_pipe_action(coupling: str, outputd_text: str) -> RuntimeEnvAction:
    if coupling == COUPLING_TRANSPORT_PIPE:
        # Preserve an existing custom pipe path in the reconciler-owned outputd
        # env; otherwise write the canonical default.
        return RuntimeEnvAction(
            "set",
            OUTPUTD_PIPE_PATH_ENV_VAR,
            resolve_outputd_pipe_path(
                read_value(outputd_text, OUTPUTD_PIPE_PATH_ENV_VAR)
            ),
        )
    return RuntimeEnvAction("unset", OUTPUTD_PIPE_PATH_ENV_VAR)


def _sync_process_env_for_emit(
    coupling: str,
    fanin_text: str,
    outputd_text: str,
) -> None:
    """Make the in-process Camilla re-emit see the env we just persisted."""
    os.environ[COUPLING_ENV_VAR] = coupling
    os.environ[PIPE_PATH_ENV_VAR] = resolve_pipe_path(
        read_value(fanin_text, PIPE_PATH_ENV_VAR)
    )
    if coupling == COUPLING_TRANSPORT_PIPE:
        os.environ[OUTPUTD_PIPE_PATH_ENV_VAR] = resolve_outputd_pipe_path(
            read_value(outputd_text, OUTPUTD_PIPE_PATH_ENV_VAR)
        )
    else:
        os.environ.pop(OUTPUTD_PIPE_PATH_ENV_VAR, None)


def read_persisted_coupling(env_path: str | Path = FANIN_ENV_PATH) -> str:
    """The coupling the daemons will read on their next start (resolved,
    fail-safe to loopback). Doctor + observability use this to compare the
    persisted intent against the live fan-in transport."""
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except OSError:
        return COUPLING_LOOPBACK
    return resolve_coupling(read_value(text, COUPLING_ENV_VAR))


def main(argv: "list[str] | None" = None) -> int:
    """CLI: ``jasper-fanin-coupling-reconcile <loopback|transport_pipe>``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="jasper-fanin-coupling-reconcile",
        description="Arm/disarm the fan-in -> CamillaDSP coupling in order.",
    )
    parser.add_argument(
        "coupling", choices=[COUPLING_LOOPBACK, COUPLING_TRANSPORT_PIPE],
    )
    parser.add_argument("--reason", default="cli")
    parser.add_argument(
        "--no-apply", action="store_true",
        help="write the env only; skip the daemon transition (staging).",
    )
    args = parser.parse_args(argv)
    # Hydrate os.environ from the wizard-owned env files (same set the daemons
    # load) BEFORE reconciling, so the camilla reconcile this triggers emits with
    # the persisted JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} etc. — not their
    # defaults. Without this, arming transport_pipe from a bare CLI/install shell
    # would silently RESET a tuned chunksize back to 1024 (same class caught on
    # JTS 2026-06-27). setdefault semantics keep an explicit shell override
    # winning. Mirrors jasper.cli.sound.
    from jasper.env_load import load_env_files

    load_env_files()
    result = reconcile_coupling(
        args.coupling, reason=args.reason, apply=not args.no_apply,
    )
    print(
        f"coupling reconcile: desired={result.desired} direction={result.direction} "
        f"ok={result.ok} changed={result.changed} "
        f"outputd={result.restarted_outputd} fanin={result.restarted_fanin} "
        f"camilla={result.reconciled_camilla}"
        + (
            f" transport_gate={result.validated_transport_pipe}"
            if result.desired == COUPLING_TRANSPORT_PIPE
            else ""
        )
        + (f" recovered={result.recovered}" if result.recovered else "")
        + (f" detail={result.detail}" if result.detail else "")
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
