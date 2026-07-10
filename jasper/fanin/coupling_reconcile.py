# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered arm/disarm of the fan-in -> CamillaDSP coupling.

WHY THIS EXISTS — the two daemons must transition in a specific order.
:mod:`jasper.fanin_coupling` owns the *vocabulary* (the flag, the pipe path, the
ring device names, the emit kwargs); this module owns the *transition* across all
three audio daemons. Two non-loopback couplings are supported:

- ``shm_ring`` (audio-graph consolidation P2) — the end-to-end SHM-ring path.
  fan-in writes Ring A (program.ring) that CamillaDSP captures via
  ``jts_ring_capture``; CamillaDSP writes its post-DSP program to Ring B
  (content.ring) via ``jts_ring_playback`` that jasper-outputd reads. Arming it is
  ONE coherent flip of BOTH ends: ``JASPER_FANIN_CAMILLA_COUPLING=shm_ring``
  (fanin.env) AND ``JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`` + the Ring B
  path/slots (outputd.env). ``_outputd_actions`` is the single writer of that
  pair; ``_arm_ring`` PREFLIGHTs the P1 ring assets (``ring_assets_ready``), the
  topology eligibility, and BOTH geometry axes (period AND Ring-A slot count),
  self-heals a shear-prone stale ``JASPER_FANIN_RING_SLOTS``, deletes a
  geometry-mismatched on-disk ring, and fail-safes to loopback+direct on any
  failure, so a half-installed ring platform, an incoherent geometry, or a partial
  flip never strands the realtime path. Arming stays explicit env/reconciler-driven;
  the DEFAULT is still loopback until P4.

- ``transport_pipe`` (lab) — an end-to-end DAC-paced named-pipe path:

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
from jasper.fanin.combo_health import (
    FALLBACK_MARKER_PATH as COMBO_HEALTH_FALLBACK_MARKER_PATH,
    TICK_STATE_PATH as COMBO_HEALTH_TICK_STATE_PATH,
)
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    COUPLING_TRANSPORT_PIPE,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    OUTPUTD_RING_PATH_ENV_VAR,
    OUTPUTD_RING_SLOTS_ENV_VAR,
    PIPE_PATH_ENV_VAR,
    OUTPUTD_PIPE_PATH_ENV_VAR,
    resolve_coupling,
    resolve_outputd_ring_path,
    resolve_outputd_ring_slots,
    resolve_pipe_path,
    resolve_outputd_pipe_path,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
JASPER_ENV_PATH = "/etc/jasper/jasper.env"
OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"
# usbsink.env carries the P3 combo's bridge-standby half; the reconciler is its
# single writer for that key (jasper-usbsink.service loads it after jasper.env).
USBSINK_ENV_PATH = "/var/lib/jasper/usbsink.env"
# Runtime-fallback watcher state (defect 2026-07-10). Re-exported from the pure
# policy module (its SSOT) so the reconciler's --health verb and the CLI share the
# exact paths without a second literal.
FANIN_UNIT = "jasper-fanin.service"
OUTPUTD_UNIT = "jasper-outputd.service"
USBSINK_UNIT = "jasper-usbsink.service"
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


@dataclass(frozen=True)
class FaninRingSlotsResolution:
    """Effective Ring-A slot resolution for the fan-in systemd env chain."""

    value: int | None
    source: str
    raw: str | None
    error: str = ""


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


def _restart_usbsink(reason: str) -> tuple[bool, str]:
    """Restart jasper-usbsink through the broker so it re-reads its standby env.

    The bridge reads ``JASPER_USBSINK_AUDIO_STANDBY`` only at startup, so a combo
    arm/disarm that flips the standby key in usbsink.env needs a restart to take
    effect. Returns (ok, detail).
    """
    return _restart_unit(USBSINK_UNIT, reason=reason, timeout=8.0)


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
    # A "skipped" reconcile is acceptable only for loopback (a flat box with
    # nothing to flip). For a COUPLED mode (transport_pipe OR shm_ring) the whole
    # point is applying the pipe/ring config — a skip means the config was NOT
    # loaded, so treat it as a failure and fail-safe back to loopback.
    if status == "skipped" and coupling not in (
        COUPLING_TRANSPORT_PIPE,
        COUPLING_SHM_RING,
    ):
        return True, str(status)
    return False, str(payload.get("reason") or status or "unknown")


def reconcile_coupling(
    desired_raw: str | None,
    *,
    reason: str,
    env_path: str | Path = FANIN_ENV_PATH,
    outputd_env_path: str | Path = OUTPUTD_ENV_PATH,
    apply: bool = True,
    mark_operator_choice: bool = False,
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
    ``mark_operator_choice=True`` (the explicit CLI/HTTP paths) additionally stamps
    the operator-choice marker ``JASPER_FANIN_COUPLING_CHOICE=operator`` into
    fanin.env in the SAME write, so a later ``--auto`` pass treats this coupling as
    an explicit operator choice and never overrides it (the revert lever). The
    ``--auto`` pass itself passes False so it leaves the marker absent (its writes
    stay auto-owned). ``restart_fanin`` / ``restart_outputd`` / ``reconcile_camilla``
    / ``active_leader_check`` are injectable for tests (default to the real broker +
    reconcile_current_dsp + grouping-state reader); the camilla hook takes the
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
        return _block_unsupported_coupling(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot,
            outputd_snapshot,
            current,
            reason,
            desired=desired,
            block_detail=support.detail,
            block_result=support.reason or ACTIVE_LEADER_TRANSPORT_BLOCK_REASON,
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
    fanin_new_text, coupling_changed = _apply_action(fanin_snapshot.text, action)
    # ``coupling_changed`` is the COUPLING line moving alone — it (with
    # ``outputd_changed``) drives the arm/disarm-vs-confirm decision below. A
    # marker-only write must NOT be mistaken for a coupling flip (that would bounce
    # the daemons on an already-at-desired box), so the marker's own change is
    # tracked separately and folded only into ``fanin_changed`` (whether to rewrite
    # the file), never into the transition decision.
    fanin_changed = coupling_changed
    if mark_operator_choice:
        # Stamp the operator-choice marker in the SAME fanin.env write as the
        # coupling flip, so an explicit CLI/HTTP arm is recorded as an operator
        # choice the --auto pass must never override (the revert lever). Absence =
        # auto-owned; presence-and-operator = frozen to the operator's pick.
        from jasper.fanin.coupling_auto import (
            COUPLING_CHOICE_ENV_VAR,
            COUPLING_CHOICE_OPERATOR,
        )

        fanin_new_text, marker_changed = _apply_action(
            fanin_new_text,
            RuntimeEnvAction("set", COUPLING_CHOICE_ENV_VAR, COUPLING_CHOICE_OPERATOR),
        )
        fanin_changed = fanin_changed or marker_changed
    outputd_new_text, outputd_changed = _apply_actions(
        outputd_snapshot.text, _outputd_actions(desired, outputd_snapshot.text)
    )
    # ``changed`` = should we rewrite either file. ``coupling_moved`` = did the
    # actual coupling topology move (gates the transition vs the confirm path).
    changed = fanin_changed or outputd_changed
    coupling_moved = coupling_changed or outputd_changed

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
        # ANY non-loopback coupling (transport_pipe OR shm_ring) is an ARM
        # direction; only loopback is a disarm. The prior check compared against
        # transport_pipe alone and mislabeled a `--no-apply` shm_ring write as
        # "disarm".
        return CouplingResult(
            ok=True, desired=desired, changed=changed,
            direction="disarm" if desired == COUPLING_LOOPBACK else "arm",
        )

    if not coupling_moved:
        # Coupling already at desired (a marker-only or combo-only fanin.env write
        # still lands here — the env was rewritten above, but the coupling topology
        # did not move, so there is no daemon transition to run). An already-armed
        # shm_ring box can still be
        # INCOHERENT — a stale JASPER_FANIN_RING_SLOTS or a stale on-disk ring file
        # from a pre-fix arm that leaves CamillaDSP crash-looping on the ioplug
        # geometry mismatch. The coupling-flip write didn't change (already
        # shm_ring), so the arm self-heal never ran; the doctor then pointed the
        # operator at a reconcile that only re-loaded camilla and healed nothing
        # (defect A CONFIRM-path gap, 2026-07-05). Detect that exact incoherence and
        # escalate to the full _arm_ring spine (self-heal THEN ordered bounce). A
        # coherent box skips this and keeps the lightweight camilla-only confirm
        # below (no daemon bounce on every reconcile tick).
        if desired == COUPLING_SHM_RING:
            heal_needed, heal_detail = _ring_confirm_needs_self_heal(fanin_snapshot.text)
            if heal_needed:
                log_event(
                    logger, "fanin.coupling_reconcile",
                    result="confirm_ring_self_heal", desired=desired, reason=reason,
                    detail=heal_detail, level=logging.WARNING,
                )
                return _arm_ring(
                    do_restart,
                    do_restart_outputd,
                    do_reconcile,
                    desired,
                    reason,
                    fanin_snapshot,
                    outputd_snapshot,
                )

        # Env already at desired AND coherent: re-confirm camilla only (self-heal a
        # drifted loaded config) — no fan-in bounce on a no-op tick.
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
            ok=ok, desired=desired, changed=changed, direction="confirm",
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
    if desired == COUPLING_SHM_RING:
        return _arm_ring(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
        )
    return _disarm(do_restart, do_restart_outputd, do_reconcile, desired, reason)


@dataclass(frozen=True)
class AutoResult:
    """Outcome of a ``--auto`` default-resolution pass (P3/P4 default-flip).

    ``owned`` is False when an operator choice froze the box — the pass made ZERO
    changes (and ``coupling`` then reports the box's ACTUAL persisted coupling, not a
    hardcoded loopback). Otherwise ``coupling`` is the resolved default,
    ``combo_armed`` is whether the USB combo resolved on, ``usb_combo_changed`` /
    ``usbsink_standby_changed`` record whether the fan-in combo keys / the
    usbsink.env standby key moved, ``coupling_result`` is the delegated
    :class:`CouplingResult`, ``restarted_fanin_for_combo`` is True when a combo-only
    change forced an extra fan-in restart (the coupling reconcile did not bounce it),
    and ``restarted_usbsink`` is True when the standby change forced a usbsink
    restart. ``ok`` reflects the delegated coupling reconcile plus the combo restarts
    (or True when the pass was a clean operator no-op).
    """

    ok: bool
    owned: bool
    coupling: str
    gadget_present: bool
    usb_combo_changed: bool
    reason: str
    combo_armed: bool = False
    usb_intent_enabled: bool = False
    usbsink_standby_changed: bool = False
    # True when the runtime-fallback marker forced the combo OFF on an otherwise
    # combo-eligible box (defect 2026-07-10). See ``fallback_active`` on
    # :func:`reconcile_auto`.
    fallback_active: bool = False
    coupling_result: "CouplingResult | None" = None
    restarted_fanin_for_combo: bool = False
    restarted_usbsink: bool = False
    detail: str = ""


def reconcile_auto(
    *,
    reason: str = "auto",
    env_path: str | Path = FANIN_ENV_PATH,
    outputd_env_path: str | Path = OUTPUTD_ENV_PATH,
    usbsink_env_path: str | Path = USBSINK_ENV_PATH,
    apply: bool = True,
    gadget_present: bool | None = None,
    usb_intent_enabled: bool | None = None,
    fallback_active: bool | None = None,
    restart_fanin: "DaemonOp | None" = None,
    restart_outputd: "DaemonOp | None" = None,
    restart_usbsink: "DaemonOp | None" = None,
    reconcile_camilla=None,
    active_leader_check: "Callable[[], bool] | None" = None,
) -> AutoResult:
    """DEFAULT-RESOLUTION pass (P3/P4): resolve the coupling + USB combo by
    eligibility when the household made no explicit choice.

    Runs on deploy (install.sh) and boot (the reconciler's ``--auto`` CLI). Steps:

    1. Read the operator-choice marker from fanin.env. If it names an explicit
       operator choice (``JASPER_FANIN_COUPLING_CHOICE=operator``), make ZERO
       changes — the operator's revert (loopback + direct + combo-off) sticks. Log
       ``result=auto_skipped_operator_choice`` and return ``owned=False`` with the
       box's ACTUAL persisted coupling (not a hardcoded loopback).
    2. Otherwise the pass OWNS the box. First self-heal a shear-prone stale
       ``JASPER_FANIN_RING_SLOTS`` (the same migration a manual arm runs) so the
       auto slot gate sees the corrected value — a stale ``=8`` old-default line
       must not DISARM a box a manual arm would migrate+keep (defect-F6). Then
       resolve the
       coupling default via :func:`jasper.fanin.coupling_auto.resolve_auto_decision`,
       gating on the SAME #1169 ring preflights a manual arm uses PLUS a
       ROUTE-support gate (grouped boxes resolve loopback — defect-F3) and a
       fail-CLOSED topology gate (unreadable topology → loopback — defect-F4).
       Resolve the USB combo from gadget presence AND the household's USB-audio
       intent (``jasper-usbsink.service`` enabled — defect-B2).
    3. Write BOTH combo halves (reconciler = single writer of each): the three
       fan-in keys into fanin.env and the ``JASPER_USBSINK_AUDIO_STANDBY`` key into
       usbsink.env — explicit ``enabled``/``1`` on a combo box, explicit
       ``disabled``/``0`` off it (never unset — defeats jasper.env precedence,
       defect-F5). Idempotent — a second pass with the same inputs writes nothing.
    4. Delegate the coupling flip + ordered daemon transition to
       :func:`reconcile_coupling` (``mark_operator_choice=False`` so the marker
       stays absent — auto-owned). Order the usbsink restart around it so the two
       gadget-capture owners never both hold ``hw:UAC2Gadget``: on ARM restart
       usbsink into standby FIRST (it releases the gadget before fan-in opens it
       directly); on DISARM restart usbsink LAST (after fan-in has released the
       gadget). A combo-only change that took the no-bounce confirm path also issues
       one extra fan-in restart.

    NO-OP on an ineligible / fanin-less box: jts3 (roleful) / jts5 (composite)
    resolve loopback with the combo off (no gadget intent) and converge with zero
    churn; jts4 (streambox, no fan-in stack) sees the coupling reconcile no-op.
    ``gadget_present`` / ``usb_intent_enabled`` / ``restart_*`` / ``reconcile_camilla``
    / ``active_leader_check`` are injectable for tests; ``gadget_present=None`` reads
    the live boot config and ``usb_intent_enabled=None`` reads the live unit state.
    """
    from jasper.fanin.combo_health import fallback_active as read_fallback_active
    from jasper.fanin.coupling_auto import (
        default_ring_gates,
        is_operator_choice,
        read_boot_config_gadget_present,
        read_marker,
        resolve_auto_decision,
        usbsink_intent_enabled as read_usbsink_intent,
    )

    fanin_snapshot = _read_snapshot(env_path)
    outputd_snapshot = _read_snapshot(outputd_env_path)
    usbsink_snapshot = _read_snapshot(usbsink_env_path)
    marker = read_marker(fanin_snapshot.text)
    gadget = (
        read_boot_config_gadget_present() if gadget_present is None else gadget_present
    )
    usb_intent = (
        read_usbsink_intent() if usb_intent_enabled is None else usb_intent_enabled
    )
    # Runtime-fallback flap guard (defect 2026-07-10). None → read the live marker.
    # The ``--auto`` CLI clears the marker BEFORE calling us (clear-and-retry on
    # boot/deploy/toggle), so an --auto pass normally sees no marker; the periodic
    # ``--health`` disarm path writes the marker then calls us with it forced True.
    fallback = read_fallback_active() if fallback_active is None else fallback_active

    # Operator-frozen short-circuit FIRST — before any migration or gate work — so
    # an operator revert is a true zero-touch no-op. Report the box's ACTUAL
    # persisted coupling (defect-Nit8), not a hardcoded loopback: an operator who
    # froze the box at shm_ring must see shm_ring on /state / the CLI.
    if is_operator_choice(marker):
        current = resolve_coupling(read_value(fanin_snapshot.text, COUPLING_ENV_VAR))
        reason_detail = "operator choice in force — auto pass is a no-op"
        log_event(
            logger, "fanin.coupling_reconcile", result="auto_skipped_operator_choice",
            reason=reason, coupling_marker="operator", coupling=current,
            detail=reason_detail,
        )
        return AutoResult(
            ok=True, owned=False, coupling=current,
            gadget_present=gadget, usb_intent_enabled=usb_intent,
            usb_combo_changed=False, reason=reason_detail,
        )

    # Self-heal a shear-prone stale JASPER_FANIN_RING_SLOTS BEFORE the gates read it,
    # exactly as a manual arm does inside _arm_ring — otherwise a stale `=8`
    # old-default line fails the slot gate and DISARMS a box a manual arm would
    # migrate and keep armed (defect-F6). No-op on a coherent/absent value or an
    # unreadable conf.d.
    # Runs regardless of ``apply`` (it is an env write, and ``reconcile_coupling``
    # itself writes env under ``--no-apply``) so the resolved decision is consistent
    # between a staging preview and a real apply.
    fanin_snapshot = _migrate_stale_fanin_ring_slots(fanin_snapshot, reason)

    # Route shape for the ring ROUTE-support gate (defect-F3). Computed once here and
    # reused; reconcile_coupling recomputes its own from the same active_leader_check
    # so both agree.
    route_mode = _route_mode_for_reconcile(active_leader_check)

    # The full ordered ring preflight set: assets + fail-closed topology (from
    # default_ring_gates), then route-support, then the two geometry gates that need
    # the outputd/fanin env text (bound here as closures).
    ring_gates = default_ring_gates() + (
        ("ring_route", lambda: ring_route_ready(route_mode)),
        ("ring_geometry", lambda: ring_geometry_ready(outputd_snapshot.text)),
        (
            "ring_slot_geometry",
            lambda: ring_slot_geometry_ready(fanin_snapshot.text),
        ),
    )
    decision = resolve_auto_decision(
        marker_raw=marker,
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        ring_gates=ring_gates,
        fallback_active=fallback,
    )

    # Step 3a — fan-in combo keys (reconciler = single writer). Write only on change.
    fanin_after_combo, combo_changed = _apply_actions(
        fanin_snapshot.text, decision.usb_combo_actions
    )
    if combo_changed:
        try:
            _write_env_text(fanin_snapshot.path, fanin_after_combo)
        except OSError as e:
            log_event(
                logger, "fanin.coupling_reconcile", result="auto_usb_combo_write_failed",
                reason=reason, gadget_present=gadget, error=e, level=logging.ERROR,
            )
            return AutoResult(
                ok=False, owned=True, coupling=decision.coupling,
                gadget_present=gadget, usb_intent_enabled=usb_intent,
                combo_armed=decision.combo_armed,
                fallback_active=decision.fallback_active, usb_combo_changed=False,
                reason=decision.reason, detail=str(e),
            )
        # Keep the live env coherent for the coupling reconcile's own re-read.
        for a in decision.usb_combo_actions:
            if a.action == "set":
                os.environ[a.key] = a.value
            else:
                os.environ.pop(a.key, None)
        log_event(
            logger, "fanin.coupling_reconcile", result="auto_usb_combo_written",
            reason=reason, gadget_present=gadget, usb_intent_enabled=usb_intent,
            combo_armed=decision.combo_armed,
            keys=",".join(a.key for a in decision.usb_combo_actions),
        )

    # Step 3b — usbsink standby key (reconciler = single writer of this key in
    # usbsink.env). The OTHER combo half; without it the fan-in direct capture and
    # the still-live bridge both hold hw:UAC2Gadget and USB audio goes silent /
    # crash-loops (defect-B1).
    usbsink_after, standby_changed = _apply_actions(
        usbsink_snapshot.text, decision.usbsink_standby_actions
    )
    if standby_changed:
        try:
            _write_env_text(usbsink_snapshot.path, usbsink_after)
        except OSError as e:
            log_event(
                logger, "fanin.coupling_reconcile",
                result="auto_usbsink_standby_write_failed",
                reason=reason, gadget_present=gadget, error=e, level=logging.ERROR,
            )
            return AutoResult(
                ok=False, owned=True, coupling=decision.coupling,
                gadget_present=gadget, usb_intent_enabled=usb_intent,
                combo_armed=decision.combo_armed,
                fallback_active=decision.fallback_active,
                usb_combo_changed=combo_changed,
                usbsink_standby_changed=False, reason=decision.reason, detail=str(e),
            )
        log_event(
            logger, "fanin.coupling_reconcile", result="auto_usbsink_standby_written",
            reason=reason, combo_armed=decision.combo_armed,
            keys=",".join(a.key for a in decision.usbsink_standby_actions),
        )

    log_event(
        logger, "fanin.coupling_reconcile", result="auto_resolved",
        reason=reason, coupling=decision.coupling, gadget_present=gadget,
        usb_intent_enabled=usb_intent, combo_armed=decision.combo_armed,
        combo_fallback=decision.fallback_active,
        usb_combo_changed=combo_changed, usbsink_standby_changed=standby_changed,
        detail=decision.reason,
    )

    do_restart_usbsink = restart_usbsink or (lambda: _restart_usbsink(reason=reason))
    restarted_usbsink = False

    # Step 4a — on ARM, restart usbsink into standby BEFORE fan-in opens the gadget
    # directly, so the bridge has released hw:UAC2Gadget first (no EBUSY race).
    if apply and standby_changed and decision.combo_armed:
        us_ok, us_detail = do_restart_usbsink()
        restarted_usbsink = us_ok
        log_event(
            logger, "fanin.coupling_reconcile",
            result="auto_usbsink_standby_restarted" if us_ok
            else "auto_usbsink_standby_restart_failed",
            reason=reason, phase="arm_before_fanin", detail=us_detail or None,
            level=logging.INFO if us_ok else logging.WARNING,
        )

    # Step 4b — delegate the coupling flip. The reconciler re-reads fanin.env fresh
    # (it snapshots inside), so the combo keys we just wrote persist untouched (it
    # owns only the coupling line + ring slots).
    coupling_result = reconcile_coupling(
        decision.coupling,
        reason=reason,
        env_path=env_path,
        outputd_env_path=outputd_env_path,
        apply=apply,
        mark_operator_choice=False,
        restart_fanin=restart_fanin,
        restart_outputd=restart_outputd,
        reconcile_camilla=reconcile_camilla,
        active_leader_check=active_leader_check,
    )

    # If the fan-in combo changed but the coupling reconcile did NOT restart fan-in
    # (a combo-only change on an already-at-desired-coupling box takes the no-bounce
    # confirm path), the new combo won't be live until fan-in restarts. Issue one.
    restarted_for_combo = False
    if apply and combo_changed and not coupling_result.restarted_fanin:
        do_restart = restart_fanin or (lambda: _restart_fanin(reason=reason))
        fan_ok, fan_detail = do_restart()
        restarted_for_combo = fan_ok
        log_event(
            logger, "fanin.coupling_reconcile",
            result="auto_usb_combo_fanin_restarted" if fan_ok
            else "auto_usb_combo_fanin_restart_failed",
            reason=reason, detail=fan_detail or None,
            level=logging.INFO if fan_ok else logging.WARNING,
        )
        if not fan_ok:
            return AutoResult(
                ok=False, owned=True, coupling=decision.coupling,
                gadget_present=gadget, usb_intent_enabled=usb_intent,
                combo_armed=decision.combo_armed,
                fallback_active=decision.fallback_active,
                usb_combo_changed=combo_changed,
                usbsink_standby_changed=standby_changed, reason=decision.reason,
                coupling_result=coupling_result, restarted_fanin_for_combo=False,
                restarted_usbsink=restarted_usbsink, detail=fan_detail,
            )

    # Step 4c — on DISARM, restart usbsink LAST (after fan-in has released the
    # gadget), so the bridge re-opens hw:UAC2Gadget without racing fan-in's release.
    if apply and standby_changed and not decision.combo_armed:
        us_ok, us_detail = do_restart_usbsink()
        restarted_usbsink = us_ok
        log_event(
            logger, "fanin.coupling_reconcile",
            result="auto_usbsink_standby_restarted" if us_ok
            else "auto_usbsink_standby_restart_failed",
            reason=reason, phase="disarm_after_fanin", detail=us_detail or None,
            level=logging.INFO if us_ok else logging.WARNING,
        )

    # ``ok`` folds in the standby restart: a combo transition that could not restart
    # usbsink left the two gadget owners in a split state, so surface it as a failure
    # (the unit exits non-zero; the doctor/operator sees the incoherence) rather than
    # a silently-broken USB path.
    ok = coupling_result.ok and (restarted_usbsink or not standby_changed or not apply)
    return AutoResult(
        ok=ok, owned=True, coupling=decision.coupling,
        gadget_present=gadget, usb_intent_enabled=usb_intent,
        combo_armed=decision.combo_armed, fallback_active=decision.fallback_active,
        usb_combo_changed=combo_changed,
        usbsink_standby_changed=standby_changed, reason=decision.reason,
        coupling_result=coupling_result, restarted_fanin_for_combo=restarted_for_combo,
        restarted_usbsink=restarted_usbsink, detail=coupling_result.detail,
    )


@dataclass(frozen=True)
class HealthResult:
    """Outcome of one ``--health`` runtime-fallback watcher tick.

    ``watched`` is False when the box is NOT running the combo (no direct usbsink
    lane in fan-in STATUS) — a non-combo box or one the fallback already disarmed;
    the tick is a silent no-op. ``broken`` / ``disarmed`` / ``transition`` /
    ``consecutive_broken`` mirror the pure :class:`~jasper.fanin.combo_health.HealthTickDecision`;
    ``auto_result`` is the delegated :class:`AutoResult` when a disarm fired.
    ``ok`` is True unless a fired disarm failed.
    """

    ok: bool
    watched: bool
    broken: bool = False
    disarmed: bool = False
    transition: str = ""
    consecutive_broken: int = 0
    auto_result: "AutoResult | None" = None
    detail: str = ""


def run_health_check(
    *,
    reason: str = "health",
    apply: bool = True,
    tick_state_path: str = COMBO_HEALTH_TICK_STATE_PATH,
    marker_path: str = COMBO_HEALTH_FALLBACK_MARKER_PATH,
    read_fanin_status: "Callable[[], tuple[dict[str, object] | None, str]] | None" = None,
    run_reconcile: "Callable[[], AutoResult] | None" = None,
) -> HealthResult:
    """RUNTIME-FALLBACK watcher tick (defect 2026-07-10). Journal-quiet on a
    healthy tick; only real transitions log.

    Fired every ~3 min by ``jasper-fanin-combo-health.timer`` (mirrors
    ``jasper-wifi-recover`` — a timer + oneshot, no resident daemon). Steps:

    1. Read fan-in STATUS and extract the USB DIRECT lane's health sample. No
       direct lane → NOT a combo box (or already disarmed): reset the tick
       accounting and return a silent no-op (``watched=False``).
    2. Advance the consecutive-broken accounting (pure
       :func:`~jasper.fanin.combo_health.decide_health_tick`): a tick is broken on
       fan-in's own ``health=="broken"`` OR the self-heal reopen counters climbing
       since the last tick — idle/no-host can never trip either.
    3. On brokenness SUSTAINED across ``FALLBACK_CONSECUTIVE_TICKS`` (~6 min): write
       the fallback marker (timestamp + reason) and delegate to
       :func:`reconcile_auto`, which — reading the marker we just wrote — forces the
       combo OFF the same way it arms it (env writes + ordered restarts), landing
       the box on the aloop-bridge solo state. The marker then blocks the periodic
       pass from re-arming until the next ``--auto`` clear-event (boot/deploy/toggle).

    Injectables (``read_fanin_status`` / ``run_reconcile`` / paths) keep this
    hardware-free testable; the defaults read the live fan-in socket and run the
    real :func:`reconcile_auto`.
    """
    from jasper.fanin.combo_health import (
        decide_health_tick,
        extract_direct_sample,
        read_tick_state,
        write_fallback_marker,
        write_tick_state,
    )

    status_reader = read_fanin_status or (
        lambda: _read_status_socket(FANIN_STATUS_SOCKET)
    )
    fanin_status, read_err = status_reader()
    sample = extract_direct_sample(fanin_status)
    if sample is None:
        # Not a combo box (or already disarmed) — nothing to watch. Reset the tick
        # accounting so a later --auto re-arm starts from a clean slate, and stay
        # journal-quiet (a dead/socketless fan-in is not this watcher's concern).
        write_tick_state(_combo_health_empty_tick(), tick_state_path)
        return HealthResult(
            ok=True, watched=False, detail=read_err or "no direct usbsink lane"
        )

    prev = read_tick_state(tick_state_path)
    decision = decide_health_tick(sample, prev)
    write_tick_state(decision.next_state, tick_state_path)

    if decision.transition == "first_broken":
        log_event(
            logger, "fanin.combo_health", result="broken_tick",
            reason=reason, health=sample.health, present=sample.present,
            reopens=sample.reopens, card_gen_reopens=sample.card_gen_reopens,
            frames_read=sample.frames_read,
            consecutive_broken=decision.next_state.consecutive_broken,
            level=logging.WARNING,
        )
    elif decision.transition == "recovered":
        log_event(
            logger, "fanin.combo_health", result="recovered",
            reason=reason, health=sample.health, present=sample.present,
            level=logging.INFO,
        )

    if not decision.disarm:
        return HealthResult(
            ok=True, watched=True, broken=decision.broken,
            transition=decision.transition,
            consecutive_broken=decision.next_state.consecutive_broken,
        )

    # SUSTAINED brokenness → fall the combo back to the aloop bridge.
    fallback_reason = (
        f"direct capture broke on {decision.next_state.consecutive_broken} "
        f"consecutive ticks (health={sample.health}, reopens={sample.reopens}, "
        f"card_gen_reopens={sample.card_gen_reopens})"
    )
    log_event(
        logger, "fanin.combo_health", result="fallback_disarm",
        reason=reason, health=sample.health, reopens=sample.reopens,
        card_gen_reopens=sample.card_gen_reopens,
        consecutive_broken=decision.next_state.consecutive_broken,
        detail=fallback_reason, level=logging.WARNING,
    )
    write_fallback_marker(fallback_reason, marker_path)
    # reconcile_auto reads the marker fresh (fallback_active=None) → forces combo
    # off + runs the ordered disarm restarts. Reset the tick accounting after so a
    # post-disarm residual can't immediately re-fire.
    reconciler = run_reconcile or (lambda: reconcile_auto(reason=reason, apply=apply))
    auto = reconciler()
    write_tick_state(_combo_health_empty_tick(), tick_state_path)
    log_event(
        logger, "fanin.combo_health",
        result="fallback_disarmed" if auto.ok else "fallback_disarm_failed",
        reason=reason, coupling=auto.coupling, combo_armed=auto.combo_armed,
        usb_combo_changed=auto.usb_combo_changed, ok=auto.ok,
        detail=auto.detail or None, level=logging.INFO if auto.ok else logging.WARNING,
    )
    return HealthResult(
        ok=auto.ok, watched=True, broken=True, disarmed=True,
        transition=decision.transition,
        consecutive_broken=decision.next_state.consecutive_broken,
        auto_result=auto, detail=fallback_reason,
    )


def _combo_health_empty_tick():
    """The empty :class:`~jasper.fanin.combo_health.TickState` (lazy import keeps
    this module import-cheap for the non-health CLI paths)."""
    from jasper.fanin.combo_health import TickState

    return TickState.empty()


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


def _block_unsupported_coupling(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    fanin_snapshot: _EnvSnapshot,
    outputd_snapshot: _EnvSnapshot,
    current: str,
    reason: str,
    *,
    desired: str,
    block_detail: str | None = None,
    block_result: str = ACTIVE_LEADER_TRANSPORT_BLOCK_REASON,
    apply: bool,
) -> CouplingResult:
    """Refuse an unsupported coupling for this route and fail-closed to loopback.

    Covers BOTH blocked combinations from ``coupling_supported_for_route``:
    ``transport_pipe`` on an active leader, and ``shm_ring`` on any grouping-
    enabled box. Forces fan-in loopback + clears every reconciler-owned outputd
    content-source key (pipe AND Ring B), so a previously-armed box (transport_pipe
    OR shm_ring) recovers rather than stranding one transport end. ``desired`` is
    the coupling the operator asked for — reported back verbatim so ``/state`` /
    logs name the real request, not a hardcoded one. ``block_result`` is the stable
    ``event=`` result token (the route-policy ``support.reason``) so a ring block
    and a transport-pipe block stay distinguishable in the journal.
    """
    detail = block_detail or (
        f"{COUPLING_ENV_VAR}={desired} is not supported for this route; the "
        "fan-in coupling was kept on / reverted to loopback"
    )
    fanin_action = RuntimeEnvAction("set", COUPLING_ENV_VAR, COUPLING_LOOPBACK)
    fanin_new_text, fanin_changed = _apply_action(fanin_snapshot.text, fanin_action)
    # Clear ALL reconciler-owned outputd content-source keys (pipe + Ring B) for
    # the loopback fallback, so the block never leaves outputd on a stale content
    # source that fan-in's loopback coupling no longer feeds.
    outputd_new_text, outputd_changed = _apply_actions(
        outputd_snapshot.text, _outputd_actions(COUPLING_LOOPBACK, outputd_snapshot.text)
    )
    # A previously-armed box (transport_pipe OR shm_ring) must be recovered, even
    # if its outputd keys happen to already be clear.
    stale_non_loopback = current != COUPLING_LOOPBACK or outputd_changed
    if stale_non_loopback:
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
                result=block_result,
                action="loopback_write_failed",
                reason=reason,
                detail=e,
                level=logging.ERROR,
            )
            return CouplingResult(
                ok=False,
                desired=desired,
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
                result=block_result,
                action="recovered_to_loopback",
                reason=reason,
                recovered=disarm.ok,
                detail=disarm.detail or None,
                level=logging.WARNING,
            )
            return CouplingResult(
                ok=False,
                desired=desired,
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
            result=block_result,
            action="wrote_loopback_no_apply",
            reason=reason,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=True,
            direction="blocked",
            detail=detail,
        )

    log_event(
        logger,
        "fanin.coupling_reconcile",
        result=block_result,
        action="kept_loopback",
        reason=reason,
        level=logging.WARNING,
    )
    return CouplingResult(
        ok=False,
        desired=desired,
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


def ring_assets_ready() -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate: are the P1 ring-platform assets present?

    Checked BEFORE arming the ring coupling. Fail-SAFE: if the ioplug ``.so`` /
    conf.d / ``/dev/shm/jts-ring`` are not all present, arming would install a
    CamillaDSP config whose ``jts_ring_capture`` / ``jts_ring_playback`` devices
    cannot resolve — CamillaDSP would crash-loop on its statefile and the fan-in
    ``StartLimitAction=reboot`` could compound it. So the reconciler refuses to
    arm and stays on loopback. Presence-only (the doctor owns the deep open-probe);
    ``jasper.ring_assets`` is the SSOT shared with ``check_ring_platform_assets``.
    """
    from jasper.ring_assets import ring_asset_presence

    presence = ring_asset_presence()
    if presence.all_present:
        return True, "ring platform assets present (ioplug .so + conf.d + shm dir)"
    return False, "ring platform assets incomplete: " + "; ".join(presence.missing())


def _resolved_outputd_period_frames(outputd_text: str) -> int:
    """outputd's resolved ``JASPER_OUTPUTD_PERIOD_FRAMES`` (env-file, else default).

    The reconciler-owned ``outputd.env`` carries the DAC-floor-derived period the
    audio-hardware reconciler writes (e.g. the Apple-dongle floor's 128); when
    absent, outputd falls back to the packaged default written on its unit
    (``DEFAULT_OUTPUTD_PERIOD_FRAMES`` = 1024). Reading the env file matches what
    outputd will resolve on its next start — the SAME source scripts/ring-proto/
    arm.sh reads (it reads the live process environ; this reads the file that
    seeds it). A malformed value falls back to the default.
    """
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_PERIOD_FRAMES

    raw = read_value(outputd_text, "JASPER_OUTPUTD_PERIOD_FRAMES")
    if raw is None:
        return DEFAULT_OUTPUTD_PERIOD_FRAMES
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_OUTPUTD_PERIOD_FRAMES
    return value if value > 0 else DEFAULT_OUTPUTD_PERIOD_FRAMES


def ring_topology_ready(*, strict_unreadable: bool = False) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate for topology eligibility.

    Ring A/Ring B carry a full-range STEREO program on a single coherent ALSA
    sink, so ``shm_ring`` is legal only for the plain-stereo / unconfigured output
    contract — NOT roleful/protected/subwoofer (needs a per-driver crossover),
    NOT composite (dual-Apple — the ring is one 2-ch device, not the 4-ch child
    sink), NOT explicit mono. This consults ``topology_supports_shm_ring`` (the
    single ring-eligibility predicate) so arming a non-eligible box refuses with a
    crisp reason here, instead of failing later at outputd's Rust full-range-stereo
    rejection (a confusing daemon-level rollback).

    Unreadable-topology policy is caller-selectable:

    - ``strict_unreadable=False`` (the DEFAULT, a HUMAN-initiated arm): fail-OPEN —
      an indeterminate read is not a confirmed non-eligible topology, and outputd's
      own guard is the backstop. A human accepts that risk when they type the arm.
    - ``strict_unreadable=True`` (the UNATTENDED ``--auto`` default pass): fail-
      CLOSED — an unattended default that armed a ring on an unreadable topology
      would arm→rollback on every boot/deploy the file is transiently corrupt, so
      the auto pass treats an unreadable topology as ineligible and stays loopback.
      (See ``jasper.fanin.coupling_auto`` module docstring, FAIL-SAFE DIRECTION.)
    """
    from jasper.active_speaker.runtime_contract import topology_supports_shm_ring
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        topology = load_output_topology_strict()
    except (OutputTopologyError, OSError, ValueError) as exc:
        if strict_unreadable:
            # Unattended auto path: an unreadable topology is NOT proven eligible —
            # fail closed to loopback so the default never arm/rollback-churns.
            return False, (
                f"topology unreadable ({exc}); the unattended default resolves "
                "loopback (fail-closed) rather than arm a ring it cannot prove is "
                "eligible"
            )
        # Human arm: indeterminate topology -> don't refuse (fail-open, backstopped
        # by outputd's own guard).
        return True, f"topology unreadable ({exc}); deferring to outputd's own guard"
    if topology_supports_shm_ring(topology):
        return True, "topology is ring-eligible (stereo/unconfigured single sink)"
    # Not ring-eligible. This is CORRECT for a genuinely roleful/composite/mono box
    # (dac8x active speaker, dual-Apple 4-ch, explicit mono) — the household knows
    # that setup and loopback is the right coupling. A shipped-default plain stereo
    # single-sink box (one Apple dongle / one registered DAC) is NOT refused here:
    # ``topology_supports_shm_ring`` reports it eligible above (its lone
    # ``child_devices`` entry is the single coherent sink the ring drives — the
    # DEFECT-2 fix). The one way a plain single-sink box lands in THIS branch is a
    # SAVED topology that still declares STALE roleful/subwoofer ``speaker_groups``
    # from a prior campaign after the hardware reverted to plain stereo: the
    # classifier honestly reports the saved sub role and a stereo ring truly cannot
    # drive it. The remediation is to CLEAR the drifted topology so it re-derives
    # the plain-stereo shape from detected hardware —
    # ``jasper-output-topology-reset`` (rewrites speaker_groups=[] -> unconfigured
    # -> ring-eligible). Name it here so the operator has an actionable next step
    # instead of an opaque refusal.
    return False, (
        "saved output topology is not ring-eligible (shm_ring is a full-range "
        "stereo single-sink coupling; roleful/protected/subwoofer, composite "
        "dual-DAC, and explicit-mono topologies are excluded until ring v2 / P8). "
        "Keeping the coupling on loopback. If this box is actually a plain stereo "
        "single-sink speaker carrying a stale roleful/subwoofer topology, run "
        "`jasper-output-topology-reset` to re-derive a clean passive topology from "
        "detected hardware, then re-arm."
    )


def ring_topology_ready_strict() -> tuple[bool, str]:
    """``ring_topology_ready`` for the unattended auto path — fail-CLOSED on an
    unreadable topology. See the ``strict_unreadable`` note there (defect-F4)."""
    return ring_topology_ready(strict_unreadable=True)


def ring_route_ready(route_mode: RouteMode) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate for ROUTE support (defect-F3).

    ``shm_ring`` is a solo-stereo-only coupling until ring v2 (P8): a grouped box
    (active leader/follower, or an invalid grouping config) has no solo content path
    for the ring to drive, so ``coupling_supported_for_route`` blocks it. The auto
    default MUST resolve loopback on such a box — otherwise ``resolve_auto_decision``
    would resolve ``shm_ring`` (the topology/geometry gates pass on the box's stereo
    output shape), the delegated ``reconcile_coupling`` would then route-block it
    (``direction=blocked``, ``ok=False``), and the boot/deploy oneshot unit would
    FAIL on every boot of a perfectly healthy grouped box. Gating on route support
    UP FRONT resolves loopback (the correct default there) and the reconcile
    succeeds. Solo / unknown never block (unknown = a transient indeterminate
    grouping read that must not refuse a legitimate solo arm — same fail-open as the
    support matrix itself).
    """
    from jasper.audio_runtime_plan import coupling_supported_for_route

    support = coupling_supported_for_route(COUPLING_SHM_RING, route_mode)
    if support.supported:
        return True, f"route supports shm_ring (route_mode={route_mode})"
    return False, (
        f"shm_ring is not supported for this route ({support.reason}); a grouped "
        "box has no solo content path for the ring until ring v2 (P8) — the default "
        "resolves loopback"
    )


def ring_geometry_ready(outputd_text: str) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate for slot geometry: conf.d period == outputd period.

    Checked BEFORE arming (after asset presence). The ``jts_ring_playback`` ioplug
    opens Ring B with the conf.d ``period_frames``; outputd's ``ShmRingSource``
    attaches with its resolved ``JASPER_OUTPUTD_PERIOD_FRAMES`` (one slot per DAC
    period). A mismatch is a hard ``open()`` error, so CamillaDSP's ring load would
    fail and the arm would roll back with a confusing daemon-level error. This
    turns that into a crisp, actionable fail-closed reason. Mirrors
    ``ring_assets_ready``'s fail-safe shape.
    """
    from jasper.ring_assets import ring_geometry_matches_outputd

    match = ring_geometry_matches_outputd(_resolved_outputd_period_frames(outputd_text))
    if match.ok:
        # TODO(#1169): if shm_ring later permits operator chunk/target overrides,
        # feed the resolved emitted values through jasper.ring_negotiation.accept()
        # here so arm-time refusal uses the same CamillaDSP/ioplug reason.
        return True, (
            "ring slot geometry matches "
            f"(conf.d period_frames={match.conf_period_frames} == outputd "
            f"period_frames={match.outputd_period_frames})"
        )
    return False, match.detail


def resolve_effective_fanin_ring_slots(fanin_text: str) -> FaninRingSlotsResolution:
    """Resolve Ring-A slots from the same env-file order ``jasper-fanin`` uses.

    ``jasper-fanin.service`` reads ``/etc/jasper/jasper.env`` first and
    ``/var/lib/jasper/fanin.env`` last, so the reconciler and doctor must model the
    same chain. Looking only at ``fanin.env`` can report the new default while an
    old ``JASPER_FANIN_RING_SLOTS=8`` in the earlier system env still controls the
    next daemon start.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR, resolve_ring_slots

    fanin_raw = read_value(fanin_text, RING_SLOTS_ENV_VAR)
    if fanin_raw is not None:
        raw = fanin_raw
        source = FANIN_ENV_PATH
    else:
        jasper_raw = read_value(_read_snapshot(JASPER_ENV_PATH).text, RING_SLOTS_ENV_VAR)
        if jasper_raw is not None:
            raw = jasper_raw
            source = JASPER_ENV_PATH
        else:
            raw = None
            source = "default"
    try:
        return FaninRingSlotsResolution(
            value=resolve_ring_slots(raw),
            source=source,
            raw=raw,
        )
    except ValueError as e:
        return FaninRingSlotsResolution(
            value=None,
            source=source,
            raw=raw,
            error=str(e),
        )


def _resolved_fanin_ring_slots(fanin_text: str) -> int | None:
    """fan-in's effective Ring-A slot count, or ``None`` when invalid."""

    resolution = resolve_effective_fanin_ring_slots(fanin_text)
    if resolution.error:
        return None
    return resolution.value


def ring_slot_geometry_ready(fanin_text: str) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate for Ring-A slot COUNT: fanin env == conf.d n_slots.

    Checked BEFORE arming (alongside the period gate). fan-in creates Ring A with
    ``resolve_ring_slots(JASPER_FANIN_RING_SLOTS)`` slots; the ``jts_ring_capture``
    ioplug attaches expecting the conf.d ``n_slots``. A mismatch is a hard
    ``hw_params`` EINVAL + ioplug ``attach_fatal reason=ring header does not match
    expected geometry`` → CamillaDSP crash-loop → start-limit-hit. This is the
    default-migration class: old 8-slot state would make fan-in write an 8-slot
    program.ring against the conf.d's pinned 2. The period gate
    (:func:`ring_geometry_ready`) does NOT cover this second axis. Fail-SAFE:
    refuse to arm (recover to loopback) with a crisp reason.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR
    from jasper.ring_assets import ring_slot_geometry_matches_conf

    resolution = resolve_effective_fanin_ring_slots(fanin_text)
    if resolution.value is None:
        return False, (
            f"{RING_SLOTS_ENV_VAR} from {resolution.source} is invalid "
            f"({resolution.error}) — a shear-prone Ring A slot geometry must fail "
            "loud; clear the stale value (default 2) before arming"
        )
    match = ring_slot_geometry_matches_conf(resolution.value)
    if match.ok:
        return True, (
            "Ring A slot count matches "
            f"(JASPER_FANIN_RING_SLOTS={match.fanin_n_slots} == conf.d "
            f"jts_ring_capture n_slots={match.conf_n_slots})"
        )
    return False, match.detail


def _migrate_stale_fanin_ring_slots(
    fanin_snapshot: _EnvSnapshot, reason: str
) -> _EnvSnapshot:
    """Override a stale, shear-prone ``JASPER_FANIN_RING_SLOTS`` into fanin.env.

    ``JASPER_FANIN_RING_SLOTS`` is an operator-tunable env (documented range
    2..16), so this does NOT blindly remove a non-default — a value that MATCHES
    the conf.d ``jts_ring_capture`` ``n_slots`` is a coherent operator override and
    stays. It writes the key into the later-loaded reconciler file ONLY when the
    shipped conf.d pins the current product default but an earlier env layer or
    fanin.env carries an env-only mismatch (the field residue is old default
    ``8``; any mismatched env-only value is incoherent without a matching conf.d).
    Writing the coherent value is deliberate: simply deleting from fanin.env can
    expose a stale value in ``/etc/jasper/jasper.env`` on the next systemd start.

    Fail-safe: an unreadable conf.d (indeterminate expected geometry), an
    absent/default env value, a non-default custom conf.d mismatch, or an invalid
    value is a no-op — the slot preflight is the backstop. A write failure logs and
    returns the CURRENT snapshot; the preflight then refuses on the still-stale
    effective value, never a silent bad arm.

    IMPORTANT: this runs INSIDE ``_arm_ring``, AFTER ``reconcile_coupling`` already
    persisted the coupling flip (``JASPER_FANIN_CAMILLA_COUPLING=shm_ring``) to
    fanin.env. The passed ``fanin_snapshot`` is the PRE-flip snapshot, so we re-read
    the file fresh here and write the override into the CURRENT content — writing
    the stale snapshot back would clobber the just-written coupling line.
    """
    from jasper.fanin_coupling import (
        DEFAULT_FANIN_RING_SLOTS,
        RING_SLOTS_ENV_VAR,
    )
    from jasper.ring_assets import RING_A_CONF_PCM, ring_conf_n_slots

    # Re-read fresh: the coupling flip was already written to this file above.
    current = _read_snapshot(fanin_snapshot.path)
    conf_a = ring_conf_n_slots(RING_A_CONF_PCM)
    if conf_a is None:
        return current  # indeterminate conf.d → the preflight fails closed.
    resolution = resolve_effective_fanin_ring_slots(current.text)
    if resolution.raw is None or (
        resolution.raw.strip() == "" and resolution.source == "default"
    ):
        return current  # nothing persisted → default already coherent.
    if resolution.value is None:
        return current  # invalid → preflight refuses with a crisp reason.
    if resolution.value == conf_a:
        return current  # coherent operator override → keep it.
    if conf_a != DEFAULT_FANIN_RING_SLOTS:
        return current  # custom conf.d mismatch → preflight must fail loud.

    new_text, changed = _apply_action(
        current.text, RuntimeEnvAction("set", RING_SLOTS_ENV_VAR, str(conf_a))
    )
    if not changed:
        return current
    try:
        _write_env_text(current.path, new_text)
    except OSError as e:
        log_event(
            logger, "fanin.coupling_reconcile", result="stale_ring_slots_override_failed",
            reason=reason, key=RING_SLOTS_ENV_VAR, value=resolution.raw,
            source=resolution.source, error=e,
            level=logging.WARNING,
        )
        return current
    os.environ[RING_SLOTS_ENV_VAR] = str(conf_a)
    log_event(
        logger, "fanin.coupling_reconcile", result="stale_ring_slots_overridden",
        reason=reason, key=RING_SLOTS_ENV_VAR, stale_value=resolution.raw,
        stale_source=resolution.source, conf_n_slots=conf_a,
    )
    return _EnvSnapshot(current.path, new_text, True)


def _delete_stale_ring_files(reason: str, fanin_text: str = "") -> None:
    """Delete on-disk ring files whose geometry != the expected arm geometry.

    A ring file left over from a PRIOR geometry (e.g. an 8-slot program.ring from
    before the 2-slot default shipped) is a
    create-or-ATTACH ``open()`` error for the writer: ``RingWriter::create_or_attach``
    validates the existing header's geometry against the requested one and bails on
    a mismatch. The files live on tmpfs (``/dev/shm``) — pure transport state,
    recreated by the writer on the next arm, NOT user data — so deleting a
    geometry-mismatched file is safe and lets the arm re-create it fresh.

    Only deletes a file whose header is VALID (carries the ``JRIN`` magic) AND whose
    geometry differs from what fan-in / the conf.d will create — on EITHER axis:
    ``n_slots`` OR ``period_frames`` (the ring slot IS one outputd period, so a file
    with matching slots but a stale period also fails the ioplug attach). A magic-
    less / absent / correct-geometry file is left untouched (the writer reclaims a
    magic-less file itself; a correct file is reused). Best-effort: a delete failure
    is logged, never raised — the writer's own attach error is the backstop.

    ``fanin_text`` is the (post-migration) fanin.env text — used ONLY as the
    fallback expected Ring-A slot count when the conf.d is unreadable.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR, resolve_ring_slots
    from jasper.ring_assets import (
        RING_A_CONF_PCM,
        RING_A_PROGRAM_FILE,
        RING_B_CONF_PCM,
        RING_B_CONTENT_FILE,
        read_ring_header,
        ring_conf_n_slots,
        ring_conf_period_frames,
    )

    # Expected Ring-A slot count: the conf.d is the attach authority for what the
    # ioplug expects; fall back to fan-in's resolved env if the conf.d is
    # unreadable. The stale-file guard's job is to clear a file that will NOT
    # attach, so compare on-disk against the value the ioplug attaches with.
    try:
        fanin_slots = resolve_ring_slots(
            read_value(fanin_text, RING_SLOTS_ENV_VAR)
        )
    except ValueError:
        fanin_slots = None
    expected_a = ring_conf_n_slots(RING_A_CONF_PCM)
    if expected_a is None:
        expected_a = fanin_slots
    expected_b = ring_conf_n_slots(RING_B_CONF_PCM)
    # Expected period is a single conf.d line shared by both rings (the ring slot is
    # one outputd period). None (unreadable) → skip the period axis, don't guess.
    expected_period = ring_conf_period_frames()

    for path, expected in (
        (RING_A_PROGRAM_FILE, expected_a),
        (RING_B_CONTENT_FILE, expected_b),
    ):
        if expected is None:
            continue  # indeterminate expected geometry — leave it for the writer.
        header = read_ring_header(path)
        if not header.valid:
            continue  # absent / magic-less: the writer reclaims it itself.
        slots_mismatch = header.n_slots != expected
        period_mismatch = (
            expected_period is not None and header.period_frames != expected_period
        )
        if not slots_mismatch and not period_mismatch:
            continue  # coherent on both axes: reused by the writer.
        try:
            os.unlink(path)
        except OSError as e:
            log_event(
                logger, "fanin.coupling_reconcile", result="stale_ring_unlink_failed",
                reason=reason, path=path, on_disk_n_slots=header.n_slots,
                on_disk_period_frames=header.period_frames,
                expected_n_slots=expected, expected_period_frames=expected_period,
                error=e, level=logging.WARNING,
            )
            continue
        log_event(
            logger, "fanin.coupling_reconcile", result="stale_ring_deleted",
            reason=reason, path=path, on_disk_n_slots=header.n_slots,
            on_disk_period_frames=header.period_frames,
            expected_n_slots=expected, expected_period_frames=expected_period,
        )


def _ring_confirm_needs_self_heal(fanin_text: str) -> tuple[bool, str]:
    """Does an ALREADY-armed shm_ring box have a ring-geometry incoherence the arm
    self-heal would fix? (defect A CONFIRM-path gap, 2026-07-05.)

    The CONFIRM path (``reconcile_coupling`` with the env already at ``shm_ring``)
    used to only re-load CamillaDSP — it never ran the slot-migration / stale-file
    self-heal, because those live inside ``_arm_ring`` and ``_arm_ring`` is only
    reached when the coupling-flip WRITE changed something. So a box armed pre-fix
    with a stale ``JASPER_FANIN_RING_SLOTS=8`` (or a stale on-disk ring file) —
    CamillaDSP crash-looping on the ioplug geometry mismatch — stayed broken: the
    doctor told the operator to run the reconciler, they ran it, it logged
    ``confirmed ok``, and nothing healed. This predicate lets the CONFIRM path
    detect exactly that incoherence and escalate to the full ``_arm_ring`` spine
    (which self-heals THEN bounces the daemons), while a coherent box keeps the
    lightweight camilla-only confirm (no bounce on every reconcile tick).

    Returns ``(True, reason)`` ONLY on POSITIVE evidence of a self-healable
    incoherence — a stale/invalid ``JASPER_FANIN_RING_SLOTS`` that disagrees with a
    READABLE conf.d, or an on-disk ring file whose valid header geometry disagrees
    with the READABLE conf.d. Fail-SAFE: an unreadable/indeterminate conf.d returns
    ``(False, ...)`` so the CONFIRM path does NOT escalate to an arm that might fail
    its own asset/topology gates and recover a working box to loopback. We only
    escalate when we can prove the box is in the exact state the self-heal repairs.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR
    from jasper.ring_assets import (
        RING_A_CONF_PCM,
        RING_A_PROGRAM_FILE,
        RING_B_CONF_PCM,
        RING_B_CONTENT_FILE,
        read_ring_header,
        ring_conf_n_slots,
        ring_conf_period_frames,
    )

    conf_a = ring_conf_n_slots(RING_A_CONF_PCM)
    if conf_a is None:
        # Indeterminate expected geometry: can't prove incoherence — stay
        # lightweight (never disarm a working box on a hunch).
        return False, "conf.d Ring-A n_slots unreadable — CONFIRM stays lightweight"

    # Axis 1 — stale/invalid slots line. Resolve through the same jasper.env ->
    # fanin.env chain systemd gives jasper-fanin; a stale old-default line in the
    # earlier system env is still live when fanin.env has no override.
    resolution = resolve_effective_fanin_ring_slots(fanin_text)
    if resolution.raw is not None and resolution.raw.strip():
        if resolution.value is None:
            return True, (
                f"{RING_SLOTS_ENV_VAR} from {resolution.source} is invalid "
                f"({resolution.error}) — needs the arm self-heal to fail loud / "
                "re-converge"
            )
        if resolution.value != conf_a:
            return True, (
                f"{RING_SLOTS_ENV_VAR}={resolution.value} from {resolution.source} "
                f"disagrees with conf.d jts_ring_capture n_slots={conf_a} — needs "
                "the arm slot self-heal"
            )

    # Axis 2 — stale on-disk ring file. A valid header whose geometry differs from
    # the readable conf.d expectation on EITHER axis (n_slots or period_frames) is
    # what _delete_stale_ring_files clears; its presence means the writer would hit a
    # create-or-attach mismatch on next start. (Mirror the delete's two-axis compare
    # so the CONFIRM path escalates on exactly the files the arm would then remove.)
    expected_b = ring_conf_n_slots(RING_B_CONF_PCM)
    expected_period = ring_conf_period_frames()
    for path, expected in ((RING_A_PROGRAM_FILE, conf_a), (RING_B_CONTENT_FILE, expected_b)):
        if expected is None:
            continue
        header = read_ring_header(path)
        if not header.valid:
            continue
        if header.n_slots != expected:
            return True, (
                f"on-disk ring {path} has n_slots={header.n_slots} != expected "
                f"{expected} — needs the arm stale-file self-heal"
            )
        if expected_period is not None and header.period_frames != expected_period:
            return True, (
                f"on-disk ring {path} has period_frames={header.period_frames} != "
                f"expected {expected_period} — needs the arm stale-file self-heal"
            )

    return False, "ring geometry coherent — CONFIRM stays lightweight"


def _arm_ring(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    desired,
    reason,
    fanin_snapshot,
    outputd_snapshot,
) -> CouplingResult:
    """Arm the ``shm_ring`` coupling (Ring A + Ring B), fail-safe to loopback.

    PREFLIGHTs run in order, each fail-safe to loopback (no daemon bounced until
    all pass): (1) P1 ring assets present (``ring_assets_ready`` — a half-installed
    ring platform would strand the realtime path); (2) topology ring-eligible
    (``ring_topology_ready``); (3) conf.d period == outputd period
    (``ring_geometry_ready``); (4) Ring-A slot count == conf.d n_slots
    (``ring_slot_geometry_ready``, after ``_migrate_stale_fanin_ring_slots``
    self-heals a shear-prone stale ``JASPER_FANIN_RING_SLOTS`` — the 2026-07-05
    defect-A geometry hole); then (5) ``_delete_stale_ring_files`` clears a
    geometry-mismatched on-disk ring so the writer re-creates it fresh. Then the
    SAME ordered spine as transport_pipe — outputd (Ring B reader) first, fan-in
    (Ring A writer) second, CamillaDSP (loads the ring config, opening
    jts_ring_capture/jts_ring_playback) last — matching the validated ring-proto arm
    order. Any failure rolls the whole box back to loopback + direct
    (``recovered=True``). The rings are forgiving (empty-ring reader/writer
    emit/drop silence), so unlike transport_pipe there is no queue-drift activation
    window; the gates are asset-presence + geometry coherence + the ordered restart
    landing, and the fan-in STATUS transport is confirmed by the doctor.
    """
    assets_ok, assets_detail = ring_assets_ready()
    if not assets_ok:
        recovered = _recover_to_loopback(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_ring_assets_missing",
            desired=desired, reason=reason, detail=assets_detail,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            detail=assets_detail, recovered=recovered,
        )

    # Topology-eligibility preflight: the ring is a full-range stereo single-sink
    # coupling. A roleful/composite/mono box would fail outputd's Rust full-range-
    # stereo rejection later (a confusing rollback); refuse UP FRONT with a crisp
    # reason. Fail-safe: an unreadable topology does NOT block (outputd guards it).
    topo_ok, topo_detail = ring_topology_ready()
    if not topo_ok:
        recovered = _recover_to_loopback(
            do_restart, do_restart_outputd, do_reconcile,
            fanin_snapshot.path, outputd_snapshot.path, reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_ring_topology_ineligible",
            desired=desired, reason=reason, detail=topo_detail,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            detail=topo_detail, recovered=recovered,
        )

    # Period-geometry preflight: the conf.d ring period MUST equal outputd's
    # resolved DAC period (the ring slot IS one outputd period). A mismatch is a
    # hard ioplug open() error, so CamillaDSP's ring load would fail and this arm
    # would roll back with a confusing daemon-level error. Refuse UP FRONT with a
    # crisp reason (fail-safe: recover to loopback), before bouncing any daemon.
    geom_ok, geom_detail = ring_geometry_ready(outputd_snapshot.text)
    if not geom_ok:
        recovered = _recover_to_loopback(
            do_restart, do_restart_outputd, do_reconcile,
            fanin_snapshot.path, outputd_snapshot.path, reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_ring_geometry_mismatch",
            desired=desired, reason=reason, detail=geom_detail,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            detail=geom_detail, recovered=recovered,
        )

    # Migrate a stale, shear-prone JASPER_FANIN_RING_SLOTS FIRST (defect A
    # migration): an old-default `=8` effective value from jasper.env or fanin.env
    # that disagrees with the conf.d self-heals to an explicit coherent value in
    # fanin.env, so the arm proceeds instead of being blocked forever. A value that
    # MATCHES the conf.d (a coherent operator override) is kept. The preflight below
    # validates the post-migration state.
    fanin_snapshot = _migrate_stale_fanin_ring_slots(fanin_snapshot, reason)

    # Slot-COUNT preflight (defect A): fan-in's resolved Ring-A n_slots
    # (JASPER_FANIN_RING_SLOTS) MUST equal the conf.d jts_ring_capture n_slots. A
    # mismatch — the old-default `=8` residue class — makes fan-in write an
    # 8-slot program.ring while CamillaDSP's ioplug attaches expecting 2:
    # hw_params EINVAL + attach_fatal → CamillaDSP crash-loop → start-limit-hit.
    # The period gate above does NOT cover this second axis. Refuse UP FRONT.
    # (After the migration this only still fails for a genuinely custom conf.d
    # needing a matching env, where the crisp reason names both values.)
    slot_ok, slot_detail = ring_slot_geometry_ready(fanin_snapshot.text)
    if not slot_ok:
        recovered = _recover_to_loopback(
            do_restart, do_restart_outputd, do_reconcile,
            fanin_snapshot.path, outputd_snapshot.path, reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_ring_slot_mismatch",
            desired=desired, reason=reason, detail=slot_detail,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            detail=slot_detail, recovered=recovered,
        )

    # Stale-ring-file guard (defect A): a ring file left over from a PRIOR geometry
    # is a create-or-ATTACH open() error for the writer (the header geometry won't
    # match the requested one). Delete any geometry-mismatched on-disk ring before
    # bouncing the daemons so the writer re-creates it fresh. tmpfs transport state,
    # not user data. Best-effort — the writer's own attach error is the backstop.
    _delete_stale_ring_files(reason, fanin_snapshot.text)

    out_ok, out_detail = do_restart_outputd()
    if not out_ok:
        recovered = _recover_to_loopback(
            do_restart, do_restart_outputd, do_reconcile,
            fanin_snapshot.path, outputd_snapshot.path, reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_ring_outputd_failed",
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
            do_restart, do_restart_outputd, do_reconcile,
            fanin_snapshot.path, outputd_snapshot.path, reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_ring_fanin_failed",
            desired=desired, reason=reason, detail=fan_detail or None,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            restarted_outputd=True, detail=fan_detail, recovered=recovered,
        )

    cam_ok, cam_detail = do_reconcile(COUPLING_SHM_RING)
    if not cam_ok:
        recovered = _recover_to_loopback(
            do_restart, do_restart_outputd, do_reconcile,
            fanin_snapshot.path, outputd_snapshot.path, reason,
        )
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_ring_camilla_failed",
            desired=desired, reason=reason, detail=cam_detail or None,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            restarted_fanin=True, restarted_outputd=True,
            detail=cam_detail, recovered=recovered,
        )

    log_event(
        logger, "fanin.coupling_reconcile", result="armed_ring",
        desired=desired, reason=reason, detail=cam_detail or None,
    )
    return CouplingResult(
        ok=True, desired=desired, changed=True, direction="arm",
        restarted_fanin=True, restarted_outputd=True, reconciled_camilla=True,
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
    except RuntimeError as e:
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
    # Clear EVERY reconciler-owned outputd content-source key (pipe AND Ring B
    # bridge/path/slots) so a failed ring arm never leaves a stale
    # JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring pointing outputd at a ring nobody
    # writes. _outputd_actions(loopback) is the single source of that set.
    new_outputd, _ = _apply_actions(
        existing_outputd, _outputd_actions(COUPLING_LOOPBACK, existing_outputd)
    )
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


def _outputd_actions(coupling: str, outputd_text: str) -> tuple[RuntimeEnvAction, ...]:
    """The COMPLETE set of reconciler-owned outputd.env actions for a coupling.

    outputd's content source is coupling-specific and MUTUALLY EXCLUSIVE across
    couplings, so this writes exactly one content-source key set and unsets the
    others — the two ends must never split (a stale outputd key while fan-in flips
    strands one transport):

    - ``transport_pipe``: set ``JASPER_OUTPUTD_LOCAL_CONTENT_PIPE`` (Camilla ->
      outputd File pipe); clear the Ring B bridge keys.
    - ``shm_ring``: set ``JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`` + the Ring B
      path/slots (outputd reads content.ring); clear the transport-pipe key. The
      two rings flip together — fan-in's Ring A capture (fanin.env) and outputd's
      Ring B bridge (here) are ONE coupling.
    - ``loopback``: clear BOTH — outputd reads the snd-aloop content lane.
    """
    if coupling == COUPLING_TRANSPORT_PIPE:
        return (
            # Preserve an existing custom pipe path in the reconciler-owned outputd
            # env; otherwise write the canonical default.
            RuntimeEnvAction(
                "set",
                OUTPUTD_PIPE_PATH_ENV_VAR,
                resolve_outputd_pipe_path(
                    read_value(outputd_text, OUTPUTD_PIPE_PATH_ENV_VAR)
                ),
            ),
            RuntimeEnvAction("unset", OUTPUTD_CONTENT_BRIDGE_ENV_VAR),
            RuntimeEnvAction("unset", OUTPUTD_RING_PATH_ENV_VAR),
            RuntimeEnvAction("unset", OUTPUTD_RING_SLOTS_ENV_VAR),
        )
    if coupling == COUPLING_SHM_RING:
        return (
            RuntimeEnvAction(
                "set", OUTPUTD_CONTENT_BRIDGE_ENV_VAR, OUTPUTD_CONTENT_BRIDGE_SHM_RING
            ),
            # Preserve custom ring path/slots if the operator set them; else the
            # canonical Ring B defaults. resolve_* validates the slot range.
            RuntimeEnvAction(
                "set",
                OUTPUTD_RING_PATH_ENV_VAR,
                resolve_outputd_ring_path(
                    read_value(outputd_text, OUTPUTD_RING_PATH_ENV_VAR)
                ),
            ),
            RuntimeEnvAction(
                "set",
                OUTPUTD_RING_SLOTS_ENV_VAR,
                str(
                    resolve_outputd_ring_slots(
                        read_value(outputd_text, OUTPUTD_RING_SLOTS_ENV_VAR)
                    )
                ),
            ),
            RuntimeEnvAction("unset", OUTPUTD_PIPE_PATH_ENV_VAR),
        )
    # loopback / anything else: outputd reads the snd-aloop content lane.
    return (
        RuntimeEnvAction("unset", OUTPUTD_PIPE_PATH_ENV_VAR),
        RuntimeEnvAction("unset", OUTPUTD_CONTENT_BRIDGE_ENV_VAR),
        RuntimeEnvAction("unset", OUTPUTD_RING_PATH_ENV_VAR),
        RuntimeEnvAction("unset", OUTPUTD_RING_SLOTS_ENV_VAR),
    )


def _apply_actions(
    text: str, actions: tuple[RuntimeEnvAction, ...]
) -> tuple[str, bool]:
    """Fold a sequence of env actions onto ``text``; changed = any moved the file."""
    changed = False
    for action in actions:
        text, moved = _apply_action(text, action)
        changed = changed or moved
    return text, changed


def _sync_process_env_for_emit(
    coupling: str,
    fanin_text: str,
    outputd_text: str,
) -> None:
    """Make the in-process Camilla re-emit see the env we just persisted.

    Mirrors :func:`_outputd_actions`: the in-process env must carry the SAME
    content-source keys the files now carry so the immediate camilla re-emit names
    the right devices for any reader. Note the coupling TOKEN itself no longer
    rides ``os.environ`` for the live emit: since the CLI-render-coupling fix,
    ``fanin_coupling_capture_kwargs(None)`` reads the coupling file-fresh from the
    persisted ``fanin.env`` (which we wrote BEFORE calling this), while the pipe /
    ring PATH env vars set here remain the live override source the file-fresh
    reader consults. shm_ring's capture/playback devices come from the coupling
    constant, not the env, so the coupling key alone drives the emit; the outputd
    ring keys below keep the in-process env coherent for any other reader.
    """
    os.environ[COUPLING_ENV_VAR] = coupling
    os.environ[PIPE_PATH_ENV_VAR] = resolve_pipe_path(
        read_value(fanin_text, PIPE_PATH_ENV_VAR)
    )
    if coupling == COUPLING_TRANSPORT_PIPE:
        os.environ[OUTPUTD_PIPE_PATH_ENV_VAR] = resolve_outputd_pipe_path(
            read_value(outputd_text, OUTPUTD_PIPE_PATH_ENV_VAR)
        )
        os.environ.pop(OUTPUTD_CONTENT_BRIDGE_ENV_VAR, None)
        os.environ.pop(OUTPUTD_RING_PATH_ENV_VAR, None)
        os.environ.pop(OUTPUTD_RING_SLOTS_ENV_VAR, None)
    elif coupling == COUPLING_SHM_RING:
        os.environ[OUTPUTD_CONTENT_BRIDGE_ENV_VAR] = OUTPUTD_CONTENT_BRIDGE_SHM_RING
        os.environ[OUTPUTD_RING_PATH_ENV_VAR] = resolve_outputd_ring_path(
            read_value(outputd_text, OUTPUTD_RING_PATH_ENV_VAR)
        )
        os.environ[OUTPUTD_RING_SLOTS_ENV_VAR] = str(
            resolve_outputd_ring_slots(
                read_value(outputd_text, OUTPUTD_RING_SLOTS_ENV_VAR)
            )
        )
        os.environ.pop(OUTPUTD_PIPE_PATH_ENV_VAR, None)
    else:
        os.environ.pop(OUTPUTD_PIPE_PATH_ENV_VAR, None)
        os.environ.pop(OUTPUTD_CONTENT_BRIDGE_ENV_VAR, None)
        os.environ.pop(OUTPUTD_RING_PATH_ENV_VAR, None)
        os.environ.pop(OUTPUTD_RING_SLOTS_ENV_VAR, None)


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
    """CLI: ``jasper-fanin-coupling-reconcile <loopback|transport_pipe|shm_ring>``
    (explicit operator choice), ``--auto`` (P3/P4 default resolution), or
    ``--health`` (the USB-combo runtime-fallback watcher tick).

    The explicit positional path stamps the operator-choice marker so a later
    ``--auto`` pass never overrides the operator's pick; ``--auto`` resolves the
    coupling + USB combo by eligibility and leaves the marker absent (auto-owned);
    ``--health`` polls fan-in's direct-capture health and disarms the combo (to the
    aloop bridge) when it is broken at runtime for >= 2 consecutive ticks.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="jasper-fanin-coupling-reconcile",
        description="Arm/disarm the fan-in -> CamillaDSP coupling in order.",
    )
    parser.add_argument(
        "coupling",
        nargs="?",
        choices=[COUPLING_LOOPBACK, COUPLING_TRANSPORT_PIPE, COUPLING_SHM_RING],
        help=(
            "explicit operator choice (stamps the operator-choice marker so --auto "
            "won't override it): loopback (snd-aloop); transport_pipe (lab "
            "dual-pipe); shm_ring (Ring A + Ring B SHM rings — arms both fan-in and "
            "outputd). Mutually exclusive with --auto/--health."
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "DEFAULT-RESOLUTION pass (P3/P4): when NO operator choice is recorded, "
            "resolve shm_ring on a ring-eligible box (else loopback) and arm the USB "
            "combo on a gadget box. A no-op when the operator marker is set. Clears "
            "the runtime-fallback marker first (clear-and-retry on boot/deploy/toggle)."
        ),
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help=(
            "RUNTIME-FALLBACK watcher tick: poll fan-in's USB direct-capture health "
            "and, if it is broken for >= 2 consecutive ticks, disarm the combo to the "
            "aloop bridge and write the fallback marker. Journal-quiet on a healthy "
            "tick. Mutually exclusive with --auto / an explicit coupling."
        ),
    )
    parser.add_argument("--reason", default="cli")
    parser.add_argument(
        "--no-apply", action="store_true",
        help="write the env only; skip the daemon transition (staging).",
    )
    args = parser.parse_args(argv)
    _modes = [args.auto, args.health, args.coupling is not None]
    if sum(bool(m) for m in _modes) > 1:
        parser.error(
            "--auto, --health, and an explicit coupling choice are mutually exclusive"
        )
    if not any(_modes):
        parser.error("give an explicit coupling choice, --auto, or --health")
    # Hydrate os.environ from the wizard-owned env files (same set the daemons
    # load) BEFORE reconciling, so the camilla reconcile this triggers emits with
    # the persisted JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} etc. — not their
    # defaults. Without this, arming transport_pipe from a bare CLI/install shell
    # would silently RESET a tuned chunksize back to 1024 (same class caught on
    # JTS 2026-06-27). setdefault semantics keep an explicit shell override
    # winning. Mirrors jasper.cli.sound.
    from jasper.env_load import load_env_files

    load_env_files()

    if args.health:
        health = run_health_check(reason=args.reason, apply=not args.no_apply)
        # Print only when there is something to say (a disarm or a broken-tick
        # transition); a healthy/idle tick prints nothing (journal-quiet, mirrors
        # jasper-wifi-recover).
        if health.watched and (health.disarmed or health.transition):
            print(
                f"combo health: watched={health.watched} broken={health.broken} "
                f"disarmed={health.disarmed} "
                f"consecutive_broken={health.consecutive_broken} ok={health.ok}"
                + (f" detail={health.detail}" if health.detail else "")
            )
        return 0 if health.ok else 1

    if args.auto:
        # Clear-and-retry: --auto runs on exactly the three fallback-marker
        # clear-events (boot, deploy, /sources/ toggle), so it drops any marker a
        # prior --health disarm wrote and re-attempts the combo from eligibility.
        # The periodic --health pass never clears the marker, so combo never
        # oscillates combo<->solo within a boot on its own.
        from jasper.fanin.combo_health import (
            clear_fallback_marker,
            read_fallback_marker,
        )

        prior = read_fallback_marker()
        if clear_fallback_marker() and prior is not None:
            log_event(
                logger, "fanin.combo_health", result="fallback_marker_cleared",
                reason=args.reason, prior_reason=prior.reason or None,
            )
        auto = reconcile_auto(reason=args.reason, apply=not args.no_apply)
        print(
            f"coupling auto: owned={auto.owned} coupling={auto.coupling} "
            f"gadget={auto.gadget_present} usb_intent={auto.usb_intent_enabled} "
            f"combo_armed={auto.combo_armed} "
            f"usb_combo_changed={auto.usb_combo_changed} "
            f"usbsink_standby_changed={auto.usbsink_standby_changed} ok={auto.ok}"
            + (
                f" fanin_restarted_for_combo={auto.restarted_fanin_for_combo}"
                if auto.usb_combo_changed
                else ""
            )
            + (
                f" usbsink_restarted={auto.restarted_usbsink}"
                if auto.usbsink_standby_changed
                else ""
            )
            + (f" reason={auto.reason}" if auto.reason else "")
            + (f" detail={auto.detail}" if auto.detail else "")
        )
        return 0 if auto.ok else 1

    # Explicit operator choice: mark_operator_choice=True freezes the box to this
    # pick across future --auto passes (the revert lever).
    result = reconcile_coupling(
        args.coupling, reason=args.reason, apply=not args.no_apply,
        mark_operator_choice=True,
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
