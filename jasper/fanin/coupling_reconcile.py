# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered arm/disarm of the fan-in -> CamillaDSP coupling.

WHY THIS EXISTS — the two daemons must transition in a specific order.
:mod:`jasper.fanin_coupling` owns the *vocabulary* (the flag, the ring device
names, the emit kwargs); this module owns the *transition* across all three audio
daemons. One non-loopback coupling is supported:

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
  flip never strands the realtime path.

  - **ARM** (loopback -> shm_ring): outputd (Ring B reader) MUST come up first,
    fan-in (Ring A writer) second, and only then may CamillaDSP load the ring
    config. See :func:`_arm_ring`.

  - **DISARM** (shm_ring -> loopback): CamillaDSP must leave the ring config
    before either endpoint is moved back to ALSA. A sub-second silence spans the
    transition; it is acceptable on a deliberate operator change and it never
    strands Camilla on a config it cannot open.

REMOVED 2026-07-11 — the ``transport_pipe`` coupling (a DAC-paced named-pipe path
fan-in -> RawFile pipe -> CamillaDSP -> File pipe -> outputd) was a default-off
lab transport for low latency, never selected by ``--auto``, hardware-demoted by
the 16 KiB Pi page floor, and superseded by ``shm_ring``. Its ``_arm`` /
activation-gate branches and the ``JASPER_OUTPUTD_LOCAL_CONTENT_PIPE`` env
plumbing are gone. A persisted ``transport_pipe`` value now FAILS SAFE to loopback
(see :func:`reconcile_auto`, which converges it loudly).

SINGLE WRITER. This module is the sole writer of the topology keys it owns:
``JASPER_FANIN_CAMILLA_COUPLING`` in ``/var/lib/jasper/fanin.env`` and the Ring B
bridge keys in ``/var/lib/jasper/outputd.env``. The order-preserving single-key
helpers (:mod:`jasper.env_file`) leave neighboring operator/reconciler lines
intact.

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

import fcntl
import logging
from collections.abc import Callable
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from jasper.atomic_io import atomic_write_text
from jasper.audio_runtime_plan import RouteMode, RuntimeEnvAction, fanin_coupling_action
from jasper.env_file import read_value, remove, upsert
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    OUTPUTD_CONTENT_BRIDGE_SHM_RING,
    OUTPUTD_RING_PATH_ENV_VAR,
    OUTPUTD_RING_SLOTS_ENV_VAR,
    coupling_value_removed,
    resolve_coupling,
    resolve_outputd_content_bridge,
    resolve_outputd_ring_path,
    resolve_outputd_ring_slots,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
JASPER_ENV_PATH = "/etc/jasper/jasper.env"
OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"
FANIN_UNIT = "jasper-fanin.service"
OUTPUTD_UNIT = "jasper-outputd.service"
CAMILLA_UNIT = "jasper-camilla.service"
# Root oneshot that re-detects output hardware and re-emits the route floor
# actions (incl. the outputd content-buffer floor) into outputd.env. The disarm
# path kicks it when leaving a live shm_ring bridge — see _disarm.
AUDIO_HARDWARE_RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"
# Fallback ``event=`` result token for a route-unsupported coupling block (the
# route policy's own ``support.reason`` normally wins). Today the only blocked
# combination is shm_ring on a grouped box.
UNSUPPORTED_COUPLING_BLOCK_REASON = "coupling_unsupported_for_route"

# Legacy env key of the REMOVED ``transport_pipe`` coupling (the Camilla -> outputd
# File playback pipe outputd used to read). Retained ONLY so the loopback/shm_ring
# ``_outputd_actions`` branches can UNSET a stale value off a migrating box's
# outputd.env (nothing writes it anymore; a stale value is inert but swept for
# cleanliness). Not vocabulary — a one-way migration sweep target.
_LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV = "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE"

# Cross-invocation serialization of the reconcile ENTRY verbs (#1233 follow-up).
# NOT under /run/jasper — that is jasper-voice's RuntimeDirectory, reaped on
# every voice stop; a reaped+recreated lock file would hand a second holder a
# fresh inode and defeat the exclusion exactly during deploys. Top-level /run is
# root-only tmpfs and every entry path runs as root (both oneshot units,
# install.sh, the sudo CLI). See :func:`_acquire_entry_lock`.
ENTRY_LOCK_PATH = "/run/jasper-fanin-coupling.lock"
ENTRY_LOCK_TIMEOUT_SECONDS = 10.0
ENTRY_LOCK_POLL_SECONDS = 0.2

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
    to loopback. ``detail`` carries the first failure's reason for the log/CLI;
    it can be non-empty with ``ok=True`` when the disarm's best-effort
    floor-reemit kick failed (see :func:`_disarm`).
    """

    ok: bool
    desired: str
    changed: bool
    direction: str
    restarted_fanin: bool = False
    restarted_outputd: bool = False
    reconciled_camilla: bool = False
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


def _restart_unit(
    unit: str, *, verb: str = "restart", reason: str, timeout: float
) -> tuple[bool, str]:
    """Drive a systemd unit through the broker with a closed verb. (ok, detail).

    ``verb`` is one of the broker's fixed vocabulary (``restart`` / ``stop`` /
    ``start`` / ...); ``no_block=False`` so the call returns only after systemd
    reports the transition complete — for a ``Type=notify`` unit like jasper-fanin
    that means the daemon has re-signalled ``READY=1`` (its ring/pipe writer is
    re-attached), which is the "wait for fan-in up" step the camilla coordination
    below relies on.

    Guarded lazy import (mirrors buffer_reconcile SF-2): a missing/broken
    control package degrades to a reported failure, never an exception out of
    the reconcile that would defeat the fail-safe ladder.
    """
    try:
        from jasper.control import restart_broker
    except ImportError as e:  # pragma: no cover - control pkg always present in prod
        return False, f"restart_broker unavailable: {e}"
    resp = restart_broker.manage_units(
        unit,
        verb=verb,
        reason=reason,
        no_block=False,
        timeout=timeout,
    )
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("error") or f"rc={resp.get('rc')}")


def _restart_fanin(reason: str) -> tuple[bool, str]:
    """Restart jasper-fanin through the broker. (ok, detail)."""
    return _restart_unit(FANIN_UNIT, reason=reason, timeout=8.0)


def _stop_camilla(reason: str) -> tuple[bool, str]:
    """Stop jasper-camilla through the broker. (ok, detail).

    Used to pause CamillaDSP with a clean SIGTERM BEFORE a coordinated fan-in
    restart so it exits cleanly instead of hitting the RLIMIT_RTTIME SIGKILL its
    ring-ioplug capture reader triggers when fan-in's writer detaches (see
    :func:`_restart_fanin_coordinated`). ``jasper-camilla.service`` is already a
    broker ``MANAGED_UNITS`` member (and polkit-granted for ``manage-units``, which
    covers stop/start) — no new grant is needed for this.
    """
    return _restart_unit(CAMILLA_UNIT, verb="stop", reason=reason, timeout=8.0)


def _start_camilla(reason: str) -> tuple[bool, str]:
    """Start jasper-camilla through the broker after fan-in is back up. (ok, detail).

    Mirrors the fan-in -> camilla order ``jasper-camilla-recover`` already proves
    works: fan-in's ring/pipe writer must be re-attached before CamillaDSP re-opens
    its capture, so this runs AFTER the ``Type=notify`` fan-in restart has returned.
    """
    return _restart_unit(CAMILLA_UNIT, verb="start", reason=reason, timeout=8.0)


def _restart_outputd(reason: str) -> tuple[bool, str]:
    """Restart jasper-outputd through the broker. (ok, detail)."""
    return _restart_unit(OUTPUTD_UNIT, reason=reason, timeout=8.0)


def _start_audio_hardware_reconcile(reason: str) -> tuple[bool, str]:
    """Start the audio-hardware reconciler oneshot through the broker. (ok, detail).

    Blocking, with the same 15 s bound the topology-reset kick uses
    (``jasper.cli.output_topology_reset._trigger_reconcile``), so the caller
    returns with the floor actions actually re-emitted, not just requested.
    ``start`` of this unit is broker-permitted for non-root clients
    (``START_ONLY_UNITS``) and falls back to direct systemctl for a broker-less
    root shell — the same reach every other daemon op here has.
    """
    return _restart_unit(
        AUDIO_HARDWARE_RECONCILE_UNIT, verb="start", reason=reason, timeout=15.0
    )


def _reconcile_camilla(
    coupling: str,
    *,
    reason: str,
    force: bool = True,
) -> tuple[bool, str]:
    """Re-emit + load the CamillaDSP config for ``coupling``. (ok, detail).

    ARM/DISARM callers force a full reconcile because the coupling is the
    change.  A topology-confirm caller passes ``force=False`` so unchanged
    source reconciles take the runtime's YAML-equality fast path while still
    repairing a genuinely drifted loaded config.  ``coupling`` is explicit so
    the emit does not depend on this process's stale ``os.environ`` (the env
    file was just rewritten under us). reconcile_current_dsp validates with
    ``camilladsp --check`` before loading and fail-closes on an invalid config,
    so a failure here leaves the previously-loaded config running.
    """
    import asyncio

    from jasper.sound.runtime import reconcile_current_dsp

    try:
        payload = asyncio.run(reconcile_current_dsp(force=force, coupling=coupling))
    except Exception as e:  # noqa: BLE001 - report, never raise out of the reconcile
        return False, f"camilla reconcile raised: {e}"
    status = payload.get("status")
    if status in ("reconciled", "unchanged"):
        return True, str(status)
    # A "skipped" reconcile is acceptable only for loopback (a flat box with
    # nothing to flip). For the shm_ring coupled mode the whole point is applying
    # the ring config — a skip means the config was NOT loaded, so treat it as a
    # failure and fail-safe back to loopback.
    if status == "skipped" and coupling != COUPLING_SHM_RING:
        return True, str(status)
    return False, str(payload.get("reason") or status or "unknown")


@dataclass(frozen=True)
class _CoordinatedFaninRestart:
    """Outcome of a CamillaDSP-coordinated fan-in restart.

    ``fanin_restarted`` is whether fan-in actually restarted; ``coordinated`` is
    whether camilla was paused/resumed around it (False on loopback, where the
    coordination is skipped). ``camilla_stopped`` / ``camilla_started`` record the
    pause/resume outcomes for the log + result. ``ok`` is True only when every step
    the chosen path needed succeeded.
    """

    ok: bool
    fanin_restarted: bool
    coordinated: bool
    camilla_stopped: bool
    camilla_started: bool
    detail: str = ""


def _restart_fanin_coordinated(
    do_restart: DaemonOp,
    do_stop_camilla: DaemonOp,
    do_start_camilla: DaemonOp,
    *,
    coupling: str,
    reason: str,
    phase: str,
) -> _CoordinatedFaninRestart:
    """Restart fan-in without collaterally SIGKILLing CamillaDSP.

    THE BUG (evidence-confirmed on jts.local, four timing fingerprints incl. a
    controlled repro): while the fan-in-written ``shm_ring`` coupling is live,
    CamillaDSP captures the transport via the
    ``jts_ring_capture`` ioplug. A bare fan-in *process* restart detaches the ring
    WRITER; the ioplug capture reader then busy-spins ~100% of a core, and
    camilladsp (``SCHED_FIFO``, ``LimitRTTIME=200000`` us in
    ``jasper-camilla.service``) hits the kernel ``RLIMIT_RTTIME`` hard SIGKILL
    ~213 ms later -> ``Restart=always`` start-limit -> ``OnFailure=
    jasper-camilla-recover`` -> a full core-graph bounce.

    So this pauses CamillaDSP with a clean SIGTERM FIRST, restarts fan-in, waits
    for it to come back (the ``Type=notify`` blocking broker restart returns only
    after fan-in re-attaches its ring writer + ``sd_notify`` READY=1 — that is the
    "wait fan-in up" step), then resumes CamillaDSP -- mirroring the fan-in ->
    camilla order ``deploy/bin/jasper-camilla-recover`` already proves works.
    camilladsp then exits cleanly on SIGTERM instead of an RTTIME-SIGKILL: no
    start-limit, no OnFailure, no core-graph bounce -- one intentional brief camilla
    restart replacing today's kill cascade.

    On LOOPBACK the coupling keeps a snd-aloop buffer between fan-in and CamillaDSP,
    so a fan-in restart does NOT spin the ioplug (camilla reads silence from the
    loopback, not a detached ring). The coordination is skipped there
    (``coupling == loopback``) so an ordinary loopback combo toggle keeps its single
    lightweight fan-in restart with no camilla glitch.

    FAILURE HONESTY: if CamillaDSP cannot be STOPPED it may still be running on the
    ring, so we do NOT restart fan-in (restarting it is exactly what SIGKILLs a
    running camilla) -- we ensure camilla is running (a ``start`` is a no-op if it
    never stopped) and abort, ``ok=False``. If the fan-in restart fails AFTER camilla
    was stopped, we STILL start camilla back -- never leave the DSP stopped forever
    (the chosen safe direction). Either way ``OnFailure=jasper-camilla-recover``
    stays the backstop for a resume that also fails; nothing here disables it.

    (Stopping camilla is safe for jasper-outputd even though camilla is outputd's
    Ring B writer: outputd's reader is DAC-clocked -- an absent writer yields paced
    silence, not a busy-spin -- so only the camilla side needs coordination.)

    SCOPE: this coordinates DELIBERATE Python-side fan-in restarts (the
    reconciler's own, plus out-of-module callers routed through
    :func:`coordinated_fanin_restart` — jasper.fanin.buffer_reconcile's
    adaptive-buffer restart uses that entry point). Since the ring-ioplug
    capture-reader pacing fix (PR #1271, ``c/jts-ring-ioplug/``), RTTIME safety
    no longer depends on this coordination: an UNCOORDINATED fan-in death (a
    crash / OOM-kill / an external ``systemctl restart jasper-fanin``) degrades
    to <=2 s of paced silence while camilla blocks on the reader's timerfd —
    no spin, no SIGKILL. The coordination is kept for the gap-free UX (a clean
    camilla stop/start beats a silence window plus resync). See
    ``docs/HANDOFF-usb-low-latency.md`` (USB DIRECT combo section).
    """
    if coupling == COUPLING_LOOPBACK:
        # snd-aloop decouples fan-in from camilla — a plain restart is safe.
        fan_ok, fan_detail = do_restart()
        return _CoordinatedFaninRestart(
            ok=fan_ok,
            fanin_restarted=fan_ok,
            coordinated=False,
            camilla_stopped=False,
            camilla_started=False,
            detail=fan_detail,
        )

    stop_ok, stop_detail = do_stop_camilla()
    if not stop_ok:
        # Camilla could not be paused -> it may still be on the ring. Do NOT restart
        # fan-in (that is what SIGKILLs it). Ensure camilla is running and abort.
        start_ok, start_detail = do_start_camilla()
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="camilla_pause_failed",
            reason=reason,
            phase=phase,
            coupling=coupling,
            detail=stop_detail or None,
            camilla_started=start_ok,
            level=logging.WARNING,
        )
        return _CoordinatedFaninRestart(
            ok=False,
            fanin_restarted=False,
            coordinated=True,
            camilla_stopped=False,
            camilla_started=start_ok,
            detail=(
                f"camilla pause failed ({stop_detail}); aborted fan-in restart to "
                "avoid an RTTIME-SIGKILL of a running CamillaDSP"
                + ("" if start_ok else f"; camilla start-back failed ({start_detail})")
            ),
        )

    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="camilla_paused_for_fanin_restart",
        reason=reason,
        phase=phase,
        coupling=coupling,
    )
    fan_ok, fan_detail = do_restart()
    # ALWAYS resume camilla, even if the fan-in restart failed -- never leave the DSP
    # stopped forever (OnFailure/recover is the backstop if this resume also fails).
    start_ok, start_detail = do_start_camilla()
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="camilla_resumed_after_fanin_restart"
        if start_ok
        else "camilla_resume_failed",
        reason=reason,
        phase=phase,
        coupling=coupling,
        fanin_restarted=fan_ok,
        detail=start_detail or None,
        level=logging.INFO if start_ok else logging.WARNING,
    )
    detail = "; ".join(
        d
        for d in (
            "" if fan_ok else f"fan-in restart failed ({fan_detail})",
            "" if start_ok else f"camilla resume failed ({start_detail})",
        )
        if d
    )
    return _CoordinatedFaninRestart(
        ok=fan_ok and start_ok,
        fanin_restarted=fan_ok,
        coordinated=True,
        camilla_stopped=True,
        camilla_started=start_ok,
        detail=detail,
    )


def coordinated_fanin_restart(
    reason: str,
    *,
    phase: str,
    env_path: str | os.PathLike = FANIN_ENV_PATH,
) -> tuple[bool, str]:
    """CamillaDSP-coordinated fan-in restart for OUT-OF-MODULE callers. (ok, detail).

    The public entry point for any deliberate fan-in restart that does not go
    through :func:`reconcile_coupling` itself (today:
    ``jasper.fanin.buffer_reconcile``'s adaptive-buffer restart, ``phase=
    "adaptive_buffer"``). Reads the ACTIVE coupling fresh from ``env_path`` (the
    daemons' next-start truth, fail-safe to loopback) and dispatches through
    :func:`_restart_fanin_coordinated` with the standard broker ops, so a caller
    on a live ring/pipe coupling pauses camilla around the restart instead of
    RTTIME-SIGKILLing it; loopback keeps its single plain fan-in restart.

    ``ok`` is "fan-in restarted" — the contract a caller's write/rollback ladder
    keys off (buffer_reconcile's SF-1 rolls its env back only when the daemon did
    NOT restart into it). A camilla-resume failure after a successful fan-in
    restart is logged + carried in ``detail`` but does not flip ``ok``: the
    daemon IS running the new env, and ``OnFailure=jasper-camilla-recover``
    remains the resume backstop.
    """
    coupling = read_persisted_coupling(env_path)
    coord = _restart_fanin_coordinated(
        lambda: _restart_fanin(reason=reason),
        lambda: _stop_camilla(reason=reason),
        lambda: _start_camilla(reason=reason),
        coupling=coupling,
        reason=reason,
        phase=phase,
    )
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="coordinated_fanin_restarted"
        if coord.fanin_restarted
        else "coordinated_fanin_restart_failed",
        reason=reason,
        phase=phase,
        coupling=coupling,
        camilla_coordinated=coord.coordinated,
        detail=coord.detail or None,
        level=logging.INFO if coord.fanin_restarted else logging.WARNING,
    )
    return coord.fanin_restarted, coord.detail


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
    kick_hardware_reconcile: "DaemonOp | None" = None,
    active_leader_check: "Callable[[], bool] | None" = None,
) -> CouplingResult:
    """Make the live fan-in->Camilla coupling match ``desired_raw``, in order.

    ``desired_raw`` is normalized by :func:`resolve_coupling` (unknown/typo, or the
    removed ``transport_pipe``, -> loopback, fail-safe). Writes the persisted env,
    then runs the direction's ordered daemon ops:

    - ARM (-> shm_ring): restart outputd, restart fan-in, then reconcile camilla.
      On any failure, roll the whole box back to loopback (``recovered=True``) and
      report ``ok=False``. See :func:`_arm_ring`.
    - DISARM (-> loopback): reconcile camilla, restart fan-in, then restart
      outputd. A camilla failure still proceeds to both restarts and reports
      ``ok=False``. When the box is leaving a LIVE shm_ring outputd bridge, the
      disarm additionally kicks ``jasper-audio-hardware-reconcile`` after the
      ordered ops (#1231 follow-up): that reconciler suppresses the route's
      outputd content-buffer floor while the bridge is shm_ring (the key is
      inert there), so without the kick a disarmed box sits on outputd's
      compile-default buffer until the next udev/boot/deploy event. Best-effort
      — see :func:`_disarm`.
    - CONFIRM (env already at desired): on the happy path, re-run only the
      camilla reconcile to self-heal a drifted loaded config, WITHOUT
      bouncing fan-in. One exception still bounces: an incoherent shm_ring
      box (stale ring slots/files) escalates to the full ``_arm_ring``
      ordered bounce.

    ``apply=False`` writes the env only (no daemon ops) — for staging/migration.
    ``mark_operator_choice=True`` (the explicit CLI/HTTP paths) additionally stamps
    the operator-choice marker ``JASPER_FANIN_COUPLING_CHOICE=operator`` into
    fanin.env in the SAME write, so a later ``--auto`` pass treats this coupling as
    an explicit operator choice and never overrides it (the revert lever). The
    ``--auto`` pass itself passes False so it leaves the marker absent (its writes
    stay auto-owned). ``restart_fanin`` / ``restart_outputd`` / ``reconcile_camilla``
    / ``kick_hardware_reconcile`` / ``active_leader_check`` are injectable for tests
    (default to the real broker + reconcile_current_dsp + grouping-state reader);
    the camilla hook takes the resolved coupling string.
    """
    do_restart = restart_fanin or (lambda: _restart_fanin(reason=reason))
    do_restart_outputd = restart_outputd or (lambda: _restart_outputd(reason=reason))
    do_kick_hardware = kick_hardware_reconcile or (
        lambda: _start_audio_hardware_reconcile(reason=reason)
    )

    def do_reconcile(coupling: str) -> tuple[bool, str]:
        if reconcile_camilla is not None:
            return reconcile_camilla(coupling)
        return _reconcile_camilla(coupling, reason=reason)

    def do_confirm_reconcile(coupling: str) -> tuple[bool, str]:
        if reconcile_camilla is not None:
            return reconcile_camilla(coupling)
        return _reconcile_camilla(coupling, reason=reason, force=False)

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
            block_result=support.reason or UNSUPPORTED_COUPLING_BLOCK_REASON,
            apply=apply,
            do_kick_hardware=do_kick_hardware,
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
                logger,
                "fanin.coupling_reconcile",
                result="write_failed",
                desired=desired,
                reason=reason,
                error=e,
                level=logging.ERROR,
            )
            return CouplingResult(
                ok=False,
                desired=desired,
                changed=False,
                direction="error",
                detail=str(e),
            )

    _sync_process_env_for_emit(desired, outputd_new_text)

    if not apply:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="written",
            desired=desired,
            changed=changed,
            reason=reason,
        )
        # Any non-loopback coupling (shm_ring) is an ARM direction; only loopback
        # is a disarm.
        return CouplingResult(
            ok=True,
            desired=desired,
            changed=changed,
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
            heal_needed, heal_detail = _ring_confirm_needs_self_heal(
                fanin_snapshot.text
            )
            if heal_needed:
                log_event(
                    logger,
                    "fanin.coupling_reconcile",
                    result="confirm_ring_self_heal",
                    desired=desired,
                    reason=reason,
                    detail=heal_detail,
                    level=logging.WARNING,
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
        ok, detail = do_confirm_reconcile(desired)
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="confirmed" if ok else "confirm_failed",
            desired=desired,
            reason=reason,
            detail=detail or None,
            level=logging.INFO if ok else logging.WARNING,
        )
        return CouplingResult(
            ok=ok,
            desired=desired,
            changed=changed,
            direction="confirm",
            reconciled_camilla=ok,
            detail="" if ok else detail,
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
    return _disarm(
        do_restart,
        do_restart_outputd,
        do_reconcile,
        desired,
        reason,
        # Kick the floor re-emit only when this disarm is actually leaving a live
        # shm_ring bridge — the one state in which the hardware reconciler had
        # suppressed the content-buffer floor (#1231). An already-direct disarm
        # never had the floor suppressed, so no kick.
        kick_hardware_reconcile=(
            do_kick_hardware
            if _leaves_live_shm_ring_bridge(outputd_snapshot.text)
            else None
        ),
    )


@dataclass(frozen=True)
class AutoResult:
    """Outcome of a ``--auto`` default-resolution pass (P3/P4 default-flip).

    ``owned`` reports coupling ownership: False means an operator choice froze
    the transport mode, while USB combo state still follows canonical source
    intent. ``coupling`` then reports the box's ACTUAL persisted coupling, not a
    hardcoded loopback. Otherwise ``coupling`` is the resolved default,
    ``combo_armed`` is whether the USB combo resolved on, ``usb_combo_changed``
    records whether the fan-in combo keys moved, ``coupling_result`` is the delegated
    :class:`CouplingResult`, ``restarted_fanin_for_combo`` is True when a combo-only
    change forced an extra fan-in restart (the coupling reconcile did not bounce it).
    ``ok`` reflects the delegated coupling reconcile plus the combo restart while
    preserving an operator-frozen coupling.
    """

    ok: bool
    owned: bool
    coupling: str
    gadget_present: bool
    usb_combo_changed: bool
    reason: str
    combo_armed: bool = False
    usb_intent_enabled: bool = False
    coupling_result: "CouplingResult | None" = None
    restarted_fanin_for_combo: bool = False
    detail: str = ""


def reconcile_auto(
    *,
    reason: str = "auto",
    env_path: str | Path = FANIN_ENV_PATH,
    outputd_env_path: str | Path = OUTPUTD_ENV_PATH,
    apply: bool = True,
    gadget_present: bool | None = None,
    usb_intent_enabled: bool | None = None,
    restart_fanin: "DaemonOp | None" = None,
    restart_outputd: "DaemonOp | None" = None,
    stop_camilla: "DaemonOp | None" = None,
    start_camilla: "DaemonOp | None" = None,
    reconcile_camilla=None,
    kick_hardware_reconcile: "DaemonOp | None" = None,
    active_leader_check: "Callable[[], bool] | None" = None,
) -> AutoResult:
    """DEFAULT-RESOLUTION pass (P3/P4): resolve the coupling + USB combo by
    eligibility when the household made no explicit choice.

    Runs on deploy (install.sh) and boot (the reconciler's ``--auto`` CLI). Steps:

    1. Read the operator-choice marker from fanin.env. If it names an explicit
       operator choice (``JASPER_FANIN_COUPLING_CHOICE=operator``), preserve that
       exact coupling and return ``owned=False``. USB combo state remains owned
       here and still follows canonical USB intent; an operator
       transport choice cannot authorize a household-Off capture lane.
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
       intent plus current local-source role permission (defect-B2).
    3. Write the three fan-in keys into fanin.env (explicit ``enabled`` on a combo
       box, explicit ``disabled`` off it — never unset, defeating jasper.env
       precedence). Idempotent — a second pass with the same inputs writes nothing.
    4. Delegate the coupling flip + ordered daemon transition to
       :func:`reconcile_coupling` (``mark_operator_choice=False`` so the marker
       stays absent — auto-owned). A combo-only change that took the no-bounce
       confirm path issues one extra
       fan-in restart — and on a live ``shm_ring`` coupling that restart is
       CamillaDSP-coordinated (:func:`_restart_fanin_coordinated`): camilla is
       paused before, resumed after, so the fan-in restart can't RTTIME-SIGKILL it.
       On loopback the plain restart is kept (snd-aloop decouples the two).

    If canonical USB intent is malformed or unreadable, the pass narrows itself
    to the safety action: preserve the current valid coupling, resolve effective
    USB intent False, run the same explicit-off combo write + ordered fan-in
    restart, emit ``result=auto_usb_intent_fail_closed``, and return ``ok=False``.
    A removed coupling still takes its independent loopback fail-safe.

    NO-OP on an ineligible / fanin-less box: jts3 (roleful) / jts5 (composite)
    resolve loopback with the combo off (no gadget intent) and converge with zero
    churn; jts4 (streambox, no fan-in stack) sees the coupling reconcile no-op.
    ``gadget_present`` / ``usb_intent_enabled`` / ``restart_*`` / ``stop_camilla`` /
    ``start_camilla`` / ``reconcile_camilla`` / ``kick_hardware_reconcile`` /
    ``active_leader_check`` are injectable for tests; ``gadget_present=None`` reads
    the resolved USB hardware capability and ``usb_intent_enabled=None`` reads canonical source
    intent plus current local-source role permission.
    """
    from jasper.fanin.coupling_auto import (
        AutoCouplingDecision,
        default_ring_gates,
        is_operator_choice,
        read_usb_gadget_available,
        read_marker,
        resolve_auto_decision,
        usb_combo_actions,
        usbsink_effectively_enabled as read_usbsink_intent,
    )

    fanin_snapshot = _read_snapshot(env_path)
    outputd_snapshot = _read_snapshot(outputd_env_path)
    marker = read_marker(fanin_snapshot.text)
    gadget = (
        read_usb_gadget_available() if gadget_present is None else gadget_present
    )
    usb_intent_failure = ""
    if usb_intent_enabled is None:
        try:
            usb_intent = read_usbsink_intent()
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            # The source coordinator has already failed the malformed USB source
            # closed, but a previously armed fan-in process can still retain its
            # DIRECT lane until this owner writes + applies the explicit-off combo
            # plan. Treat the unreadable preference as effective False, complete
            # the ordinary ordered disarm below, and only then return failure.
            # Derived/unit state must never authorize capture when canonical
            # intent cannot be proved valid.
            usb_intent = False
            usb_intent_failure = f"USB source intent invalid or unreadable: {exc}"[:500]
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="auto_usb_intent_invalid",
                reason=reason,
                usb_intent_enabled=False,
                detail=usb_intent_failure,
                level=logging.ERROR,
            )
    else:
        usb_intent = usb_intent_enabled
    # MIGRATION — a persisted REMOVED coupling value (the deleted transport_pipe,
    # or any typo) is NOT a valid operator choice; the mode the operator picked no
    # longer exists. Converge the box to loopback (the fail-safe rung) LOUDLY,
    # IGNORING the operator marker, so a migrating box never silently keeps a
    # deleted mode. ``resolve_coupling`` already fails such a value safe to loopback
    # at read time; this rewrites fanin.env so the file stops lying, sweeps the
    # legacy outputd pipe key, and runs the ordered disarm so a box that really
    # armed transport_pipe (CamillaDSP on a RawFile config that crash-loops without
    # a pipe writer) is recovered. Runs BEFORE the operator short-circuit for
    # exactly this reason. The doctor's ``check_fanin_coupling_value`` surfaces the
    # same condition until this pass runs.
    persisted_raw = read_value(fanin_snapshot.text, COUPLING_ENV_VAR)
    persisted_coupling_removed = coupling_value_removed(persisted_raw)
    if persisted_coupling_removed:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="removed_coupling_failsafe",
            reason=reason,
            persisted=persisted_raw,
            coupling=COUPLING_LOOPBACK,
            detail=(
                "persisted JASPER_FANIN_CAMILLA_COUPLING names a removed/unknown "
                "transport (e.g. the deleted transport_pipe); failing safe to loopback"
            ),
            level=logging.WARNING,
        )
        if not usb_intent_failure:
            result = reconcile_coupling(
                COUPLING_LOOPBACK,
                reason=f"{reason}:removed_coupling_failsafe",
                env_path=env_path,
                outputd_env_path=outputd_env_path,
                apply=apply,
                mark_operator_choice=False,
                restart_fanin=restart_fanin,
                restart_outputd=restart_outputd,
                reconcile_camilla=reconcile_camilla,
                kick_hardware_reconcile=kick_hardware_reconcile,
                active_leader_check=active_leader_check,
            )
            return AutoResult(
                ok=result.ok,
                owned=True,
                coupling=COUPLING_LOOPBACK,
                gadget_present=gadget,
                usb_intent_enabled=usb_intent,
                usb_combo_changed=False,
                reason="persisted coupling was removed — failed safe to loopback",
                coupling_result=result,
                detail=result.detail,
            )
        # Two fail-safe conditions coincide. Continue through the shared combo
        # path so its explicit-off keys and ordered fan-in restart land too; the
        # decision below also forces the removed coupling to loopback.

    forced_usb_safety_decision = bool(usb_intent_failure)
    if forced_usb_safety_decision:
        # A USB-local parse/read failure must not make an unrelated coupling
        # decision. Preserve the current valid coupling (or force a removed one
        # to loopback) while using the same explicit-off actions and transition
        # path as an ordinary combo disarm. An operator marker freezes the
        # coupling choice, not an unsafe USB capture lane.
        safe_coupling = (
            COUPLING_LOOPBACK
            if persisted_coupling_removed
            else resolve_coupling(persisted_raw)
        )
        decision = AutoCouplingDecision(
            owned=not is_operator_choice(marker),
            coupling=safe_coupling,
            usb_combo_actions=usb_combo_actions(armed=False),
            combo_armed=False,
            gadget_present=gadget,
            usb_intent_enabled=False,
            reason=(
                "USB source intent invalid — combo failed closed; "
                + (
                    "removed coupling also failed safe to loopback"
                    if persisted_coupling_removed
                    else (
                        "operator coupling preserved"
                        if is_operator_choice(marker)
                        else "current coupling preserved"
                    )
                )
            ),
        )
    elif is_operator_choice(marker):
        current = resolve_coupling(persisted_raw)
        decision = resolve_auto_decision(
            marker_raw=marker,
            gadget_present=gadget,
            usb_intent_enabled=usb_intent,
            ring_gates=(),
            current_coupling=current,
        )
    else:
        # Self-heal a shear-prone stale JASPER_FANIN_RING_SLOTS BEFORE the gates
        # read it, exactly as a manual arm does inside _arm_ring — otherwise a
        # stale `=8` old-default line fails the slot gate and DISARMS a box a
        # manual arm would migrate+keep (defect-F6). The forced malformed-USB
        # safety branch above deliberately skips this unrelated coupling
        # migration when an operator choice is frozen.
        fanin_snapshot = _migrate_stale_fanin_ring_slots(fanin_snapshot, reason)

        # Route shape for the ring ROUTE-support gate (defect-F3). Computed once
        # here and reused; reconcile_coupling recomputes its own from the same
        # active_leader_check so both agree.
        route_mode = _route_mode_for_reconcile(active_leader_check)

        # The full ordered ring preflight set: assets + fail-closed topology
        # (from default_ring_gates), then route-support, then the two geometry
        # gates that need the outputd/fanin env text (bound here as closures).
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
                logger,
                "fanin.coupling_reconcile",
                result="auto_usb_combo_write_failed",
                reason=reason,
                gadget_present=gadget,
                error=e,
                level=logging.ERROR,
            )
            return AutoResult(
                ok=False,
                owned=decision.owned,
                coupling=decision.coupling,
                gadget_present=gadget,
                usb_intent_enabled=usb_intent,
                combo_armed=decision.combo_armed,
                usb_combo_changed=False,
                reason=decision.reason,
                detail="; ".join(part for part in (usb_intent_failure, str(e)) if part),
            )
        # Keep the live env coherent for the coupling reconcile's own re-read.
        for a in decision.usb_combo_actions:
            if a.action == "set":
                os.environ[a.key] = a.value
            else:
                os.environ.pop(a.key, None)
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_combo_written",
            reason=reason,
            gadget_present=gadget,
            usb_intent_enabled=usb_intent,
            combo_armed=decision.combo_armed,
            keys=",".join(a.key for a in decision.usb_combo_actions),
        )

    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="auto_resolved",
        reason=reason,
        coupling=decision.coupling,
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        combo_armed=decision.combo_armed,
        usb_combo_changed=combo_changed,
        detail=decision.reason,
    )

    # Step 4 — delegate the coupling flip. The reconciler re-reads fanin.env fresh
    # (it snapshots inside), so the combo keys we just wrote persist untouched (it
    # owns only the coupling line + ring slots).
    if decision.owned:
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
            kick_hardware_reconcile=kick_hardware_reconcile,
            active_leader_check=active_leader_check,
        )
    else:
        coupling_result = CouplingResult(
            ok=True,
            desired=decision.coupling,
            changed=False,
            direction="confirm",
        )

    # If the fan-in combo changed but the coupling reconcile did NOT restart fan-in
    # (a combo-only change on an already-at-desired-coupling box takes the no-bounce
    # confirm path), the new combo won't be live until fan-in restarts. Issue one —
    # CamillaDSP-coordinated when a ring/pipe coupling is live so it can't RTTIME-
    # SIGKILL camilla (see _restart_fanin_coordinated). This is the combo-arm or
    # combo-disarm restart. The
    # active coupling is re-read from the just-written fanin.env so a block-forced
    # loopback is honoured (skip the pause) even when decision.coupling was shm_ring.
    restarted_for_combo = False
    if apply and combo_changed and not coupling_result.restarted_fanin:
        do_restart = restart_fanin or (lambda: _restart_fanin(reason=reason))
        do_stop_camilla = stop_camilla or (lambda: _stop_camilla(reason=reason))
        do_start_camilla = start_camilla or (lambda: _start_camilla(reason=reason))
        active_coupling = read_persisted_coupling(env_path)
        coord = _restart_fanin_coordinated(
            do_restart,
            do_stop_camilla,
            do_start_camilla,
            coupling=active_coupling,
            reason=reason,
            phase="auto_usb_combo",
        )
        restarted_for_combo = coord.fanin_restarted
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_combo_fanin_restarted"
            if coord.ok
            else "auto_usb_combo_fanin_restart_failed",
            reason=reason,
            coupling=active_coupling,
            camilla_coordinated=coord.coordinated,
            detail=coord.detail or None,
            level=logging.INFO if coord.ok else logging.WARNING,
        )
        if not coord.ok:
            return AutoResult(
                ok=False,
                owned=decision.owned,
                coupling=decision.coupling,
                gadget_present=gadget,
                usb_intent_enabled=usb_intent,
                combo_armed=decision.combo_armed,
                usb_combo_changed=combo_changed,
                reason=decision.reason,
                coupling_result=coupling_result,
                restarted_fanin_for_combo=restarted_for_combo,
                detail="; ".join(
                    part for part in (usb_intent_failure, coord.detail) if part
                ),
            )

    ok = coupling_result.ok and not usb_intent_failure
    detail = "; ".join(
        part for part in (usb_intent_failure, coupling_result.detail) if part
    )
    if usb_intent_failure:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_intent_fail_closed",
            reason=reason,
            usb_intent_enabled=False,
            combo_armed=decision.combo_armed,
            usb_combo_changed=combo_changed,
            restarted_fanin=(coupling_result.restarted_fanin or restarted_for_combo),
            detail=detail,
            level=logging.ERROR,
        )
    return AutoResult(
        ok=ok,
        owned=decision.owned,
        coupling=decision.coupling,
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        combo_armed=decision.combo_armed,
        usb_combo_changed=combo_changed,
        reason=decision.reason,
        coupling_result=coupling_result,
        restarted_fanin_for_combo=restarted_for_combo,
        detail=detail,
    )


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
    block_result: str = UNSUPPORTED_COUPLING_BLOCK_REASON,
    apply: bool,
    do_kick_hardware: "DaemonOp | None" = None,
) -> CouplingResult:
    """Refuse an unsupported coupling for this route and fail-closed to loopback.

    Covers the blocked combination from ``coupling_supported_for_route``:
    ``shm_ring`` on any grouping-enabled box. Forces fan-in loopback + clears every
    reconciler-owned outputd content-source key (Ring B, plus a sweep of the legacy
    transport_pipe key), so a previously-armed shm_ring box recovers rather than
    stranding one transport end. A force-disarm off a LIVE shm_ring bridge leaves
    the same suppressed content-buffer floor an ordinary disarm does, so the
    recovery `_disarm` gets the same gated ``do_kick_hardware`` (see
    :func:`_leaves_live_shm_ring_bridge`). ``desired`` is the coupling the operator
    asked for — reported back verbatim so ``/state`` / logs name the real request,
    not a hardcoded one. ``block_result`` is the stable ``event=`` result token (the
    route-policy ``support.reason``).
    """
    detail = block_detail or (
        f"{COUPLING_ENV_VAR}={desired} is not supported for this route; the "
        "fan-in coupling was kept on / reverted to loopback"
    )
    fanin_action = RuntimeEnvAction("set", COUPLING_ENV_VAR, COUPLING_LOOPBACK)
    fanin_new_text, fanin_changed = _apply_action(fanin_snapshot.text, fanin_action)
    # Clear ALL reconciler-owned outputd content-source keys (Ring B + the legacy
    # transport_pipe sweep) for the loopback fallback, so the block never leaves
    # outputd on a stale content source that fan-in's loopback coupling no longer
    # feeds.
    outputd_new_text, outputd_changed = _apply_actions(
        outputd_snapshot.text,
        _outputd_actions(COUPLING_LOOPBACK, outputd_snapshot.text),
    )
    # A previously-armed shm_ring box must be recovered, even
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
        _sync_process_env_for_emit(COUPLING_LOOPBACK, outputd_new_text)
        if apply:
            disarm = _disarm(
                do_restart,
                do_restart_outputd,
                do_reconcile,
                COUPLING_LOOPBACK,
                reason,
                # Same #1231 window as the ordinary disarm: a force-disarmed box
                # leaving a live shm_ring bridge needs the floor re-emitted.
                kick_hardware_reconcile=(
                    do_kick_hardware
                    if do_kick_hardware is not None
                    and _leaves_live_shm_ring_bridge(outputd_snapshot.text)
                    else None
                ),
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
        # TODO: if shm_ring later permits operator chunk/target overrides,
        # feed the resolved emitted values through jasper.ring_negotiation.accept()
        # here so arm-time refusal uses the same CamillaDSP/ioplug reason. (Not
        # #1169 — that PR was ring geometry coherence / zombie capture handle /
        # reconcile storm / %-escaping, unrelated to chunk/target overrides.)
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
        jasper_raw = read_value(
            _read_snapshot(JASPER_ENV_PATH).text, RING_SLOTS_ENV_VAR
        )
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
            logger,
            "fanin.coupling_reconcile",
            result="stale_ring_slots_override_failed",
            reason=reason,
            key=RING_SLOTS_ENV_VAR,
            value=resolution.raw,
            source=resolution.source,
            error=e,
            level=logging.WARNING,
        )
        return current
    os.environ[RING_SLOTS_ENV_VAR] = str(conf_a)
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="stale_ring_slots_overridden",
        reason=reason,
        key=RING_SLOTS_ENV_VAR,
        stale_value=resolution.raw,
        stale_source=resolution.source,
        conf_n_slots=conf_a,
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
        fanin_slots = resolve_ring_slots(read_value(fanin_text, RING_SLOTS_ENV_VAR))
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
                logger,
                "fanin.coupling_reconcile",
                result="stale_ring_unlink_failed",
                reason=reason,
                path=path,
                on_disk_n_slots=header.n_slots,
                on_disk_period_frames=header.period_frames,
                expected_n_slots=expected,
                expected_period_frames=expected_period,
                error=e,
                level=logging.WARNING,
            )
            continue
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="stale_ring_deleted",
            reason=reason,
            path=path,
            on_disk_n_slots=header.n_slots,
            on_disk_period_frames=header.period_frames,
            expected_n_slots=expected,
            expected_period_frames=expected_period,
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
    for path, expected in (
        (RING_A_PROGRAM_FILE, conf_a),
        (RING_B_CONTENT_FILE, expected_b),
    ):
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
    ordered spine — outputd (Ring B reader) first, fan-in (Ring A writer) second,
    CamillaDSP (loads the ring config, opening jts_ring_capture/jts_ring_playback)
    last — matching the validated ring-proto arm order. Any failure rolls the whole
    box back to loopback + direct (``recovered=True``). The rings are forgiving
    (empty-ring reader/writer emit/drop silence), so there is no queue-drift
    activation window; the gates are asset-presence + geometry coherence + the
    ordered restart landing, and the fan-in STATUS transport is confirmed by the
    doctor.
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
            logger,
            "fanin.coupling_reconcile",
            result="arm_ring_assets_missing",
            desired=desired,
            reason=reason,
            detail=assets_detail,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="arm",
            detail=assets_detail,
            recovered=recovered,
        )

    # Topology-eligibility preflight: the ring is a full-range stereo single-sink
    # coupling. A roleful/composite/mono box would fail outputd's Rust full-range-
    # stereo rejection later (a confusing rollback); refuse UP FRONT with a crisp
    # reason. Fail-safe: an unreadable topology does NOT block (outputd guards it).
    topo_ok, topo_detail = ring_topology_ready()
    if not topo_ok:
        recovered = _recover_to_loopback(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="arm_ring_topology_ineligible",
            desired=desired,
            reason=reason,
            detail=topo_detail,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="arm",
            detail=topo_detail,
            recovered=recovered,
        )

    # Period-geometry preflight: the conf.d ring period MUST equal outputd's
    # resolved DAC period (the ring slot IS one outputd period). A mismatch is a
    # hard ioplug open() error, so CamillaDSP's ring load would fail and this arm
    # would roll back with a confusing daemon-level error. Refuse UP FRONT with a
    # crisp reason (fail-safe: recover to loopback), before bouncing any daemon.
    geom_ok, geom_detail = ring_geometry_ready(outputd_snapshot.text)
    if not geom_ok:
        recovered = _recover_to_loopback(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="arm_ring_geometry_mismatch",
            desired=desired,
            reason=reason,
            detail=geom_detail,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="arm",
            detail=geom_detail,
            recovered=recovered,
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
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="arm_ring_slot_mismatch",
            desired=desired,
            reason=reason,
            detail=slot_detail,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="arm",
            detail=slot_detail,
            recovered=recovered,
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
            do_restart,
            do_restart_outputd,
            do_reconcile,
            fanin_snapshot.path,
            outputd_snapshot.path,
            reason,
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="arm_ring_outputd_failed",
            desired=desired,
            reason=reason,
            detail=out_detail or None,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="arm",
            restarted_outputd=False,
            detail=out_detail,
            recovered=recovered,
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
            logger,
            "fanin.coupling_reconcile",
            result="arm_ring_fanin_failed",
            desired=desired,
            reason=reason,
            detail=fan_detail or None,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="arm",
            restarted_outputd=True,
            detail=fan_detail,
            recovered=recovered,
        )

    cam_ok, cam_detail = do_reconcile(COUPLING_SHM_RING)
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
            logger,
            "fanin.coupling_reconcile",
            result="arm_ring_camilla_failed",
            desired=desired,
            reason=reason,
            detail=cam_detail or None,
            recovered=recovered,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="arm",
            restarted_fanin=True,
            restarted_outputd=True,
            detail=cam_detail,
            recovered=recovered,
        )

    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="armed_ring",
        desired=desired,
        reason=reason,
        detail=cam_detail or None,
    )
    return CouplingResult(
        ok=True,
        desired=desired,
        changed=True,
        direction="arm",
        restarted_fanin=True,
        restarted_outputd=True,
        reconciled_camilla=True,
    )


def _leaves_live_shm_ring_bridge(prior_outputd_text: str) -> bool:
    """True when the outputd.env being rewritten carried a LIVE shm_ring bridge.

    A shm_ring bridge is the condition under which
    ``jasper-audio-hardware-reconcile`` SUPPRESSES the route's outputd
    content-buffer floor (#1231: the key is inert while outputd reads Ring B, so
    emitting it there is one-knob-two-truths drift — see
    ``_resolve_outputd_content_buffer_int`` in :mod:`jasper.audio_runtime_plan`).
    A disarm from this state can land outputd on its compile-default content
    buffer until the floor re-emits, so the disarm path kicks the hardware
    reconciler when this is True. The gate is NECESSARY, not exact: the floor
    itself only exists on the USB-low-latency route, so on other routes the
    kicked reconciler converges to a no-op (its daemon restarts are conditional
    on the env actually changing) — a bounded free convergence sweep. Uses the
    same fail-safe resolver as the suppression, so only a genuine ``shm_ring``
    matches.
    """
    return (
        resolve_outputd_content_bridge(
            read_value(prior_outputd_text, OUTPUTD_CONTENT_BRIDGE_ENV_VAR)
        )
        == OUTPUTD_CONTENT_BRIDGE_SHM_RING
    )


def _disarm(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    desired,
    reason,
    kick_hardware_reconcile: "DaemonOp | None" = None,
) -> CouplingResult:
    """Camilla first (off RawFile/File -> Alsa), then fan-in and outputd. Even
    if the camilla reconcile fails, still restart both endpoints to loopback.

    ``kick_hardware_reconcile`` is set only when the box is leaving a live
    shm_ring outputd bridge (:func:`_leaves_live_shm_ring_bridge`). It starts
    ``jasper-audio-hardware-reconcile`` AFTER the ordered disarm so the route's
    outputd content-buffer floor — which that reconciler unsets while the
    bridge is shm_ring (#1231) — re-emits promptly instead of waiting for the
    next udev/boot/deploy/outputd-failure event. Best-effort: a failed kick is
    logged and carried in ``detail`` but does not fail the disarm — the
    interim compile-default content buffer is a LARGER cushion than the floor
    (fail-safe), and the next hardware-reconcile event still converges it.

    Post-#1257 the kicked pass's only committed delta on this path is
    outputd.env (the floor re-emit), and ``jasper-audio-hardware-reconcile``
    now classifies its restart by cause. An outputd-only change (no
    DAC-identity or asound-render move) takes ``restart_outputd_only`` — a
    single ``--no-block restart jasper-outputd`` with NO blocking
    ``systemctl stop jasper-voice`` and NO ``jasper-aec-reconcile`` kick — so a
    shm_ring -> direct disarm (including a household ``/sources/`` USB
    toggle-off) no longer costs the ~10-15 s of wake deafness the original
    PR #1251 did not disclose; wake detection stays up across the outputd
    bounce. outputd is still double-bounced: this function's own blocking
    restart above, then the kicked pass's no-block outputd-only restart
    seconds later — inherent to single-writer floor ownership (the hardware
    reconciler is the only writer of the floor key). A DAC-identity or asound
    change on the same pass would instead take the full path
    (``restart_audio_if_needed``, which does stop voice), because that class
    can move the mic/input profile.
    """
    cam_ok, cam_detail = do_reconcile(COUPLING_LOOPBACK)
    fan_ok, fan_detail = do_restart()
    out_ok, out_detail = do_restart_outputd()
    kick_detail = ""
    if kick_hardware_reconcile is not None:
        kick_ok, kick_fail = kick_hardware_reconcile()
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="disarm_floor_reemit" if kick_ok else "disarm_floor_reemit_failed",
            desired=desired,
            reason=reason,
            detail=kick_fail or None,
            level=logging.INFO if kick_ok else logging.WARNING,
        )
        if not kick_ok:
            kick_detail = f"audio-hardware reconcile kick failed ({kick_fail})"
    ok = cam_ok and fan_ok and out_ok
    detail = "; ".join(
        d
        for d in (
            cam_detail if not cam_ok else "",
            fan_detail if not fan_ok else "",
            out_detail if not out_ok else "",
            kick_detail,
        )
        if d
    )
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="disarmed" if ok else "disarm_partial",
        desired=desired,
        reason=reason,
        detail=detail or None,
        level=logging.INFO if ok else logging.WARNING,
    )
    return CouplingResult(
        ok=ok,
        desired=desired,
        changed=True,
        direction="disarm",
        restarted_fanin=fan_ok,
        restarted_outputd=out_ok,
        reconciled_camilla=cam_ok,
        detail=detail,
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
    succeeded.

    Unlike :func:`_disarm`, this takes no ``kick_hardware_reconcile`` and so
    never kicks ``jasper-audio-hardware-reconcile`` — including on the one
    route here that can be leaving a LIVE shm_ring bridge (the CONFIRM path's
    ring self-heal escalating to :func:`_arm_ring`, which then fails its own
    preflight). Intentional: a box already mid-failure-recovery gets the
    larger fail-safe cushion and less daemon churn instead of another
    oneshot; the content-buffer floor re-emit just waits for the next
    udev/boot/deploy event on this path, same as before #1251.
    """
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
    # Clear EVERY reconciler-owned outputd content-source key (Ring B
    # bridge/path/slots, plus the legacy transport_pipe sweep) so a failed ring
    # arm never leaves a stale JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring pointing
    # outputd at a ring nobody writes. _outputd_actions(loopback) is the single
    # source of that set.
    new_outputd, _ = _apply_actions(
        existing_outputd, _outputd_actions(COUPLING_LOOPBACK, existing_outputd)
    )
    try:
        _write_env_text(Path(outputd_path), new_outputd)
    except OSError:
        return False
    _sync_process_env_for_emit(COUPLING_LOOPBACK, new_outputd)
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

    - ``shm_ring``: set ``JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`` + the Ring B
      path/slots (outputd reads content.ring). The two rings flip together —
      fan-in's Ring A capture (fanin.env) and outputd's Ring B bridge (here) are
      ONE coupling.
    - ``loopback``: clear the Ring B keys — outputd reads the snd-aloop content
      lane.

    Every branch also UNSETS the legacy ``JASPER_OUTPUTD_LOCAL_CONTENT_PIPE`` key
    (the removed transport_pipe coupling's outputd content source) — a one-way
    migration sweep so a box that once armed transport_pipe converges clean on its
    next reconcile (nothing writes the key anymore).
    """
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
            RuntimeEnvAction("unset", _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV),
        )
    # loopback / anything else: outputd reads the snd-aloop content lane.
    return (
        RuntimeEnvAction("unset", _LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV),
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
    outputd_text: str,
) -> None:
    """Make the in-process Camilla re-emit see the env we just persisted.

    Mirrors :func:`_outputd_actions`: the in-process env must carry the SAME
    content-source keys the files now carry so the immediate camilla re-emit names
    the right devices for any reader. Note the coupling TOKEN itself no longer
    rides ``os.environ`` for the live emit: since the CLI-render-coupling fix,
    ``fanin_coupling_capture_kwargs(None)`` reads the coupling file-fresh from the
    persisted ``fanin.env`` (which we wrote BEFORE calling this). shm_ring's
    capture/playback devices come from the coupling constant, not the env, so the
    coupling key alone drives the emit; the outputd ring keys below keep the
    in-process env coherent for any other reader. The legacy transport_pipe outputd
    key is popped on every branch (migration sweep).
    """
    os.environ[COUPLING_ENV_VAR] = coupling
    if coupling == COUPLING_SHM_RING:
        os.environ[OUTPUTD_CONTENT_BRIDGE_ENV_VAR] = OUTPUTD_CONTENT_BRIDGE_SHM_RING
        os.environ[OUTPUTD_RING_PATH_ENV_VAR] = resolve_outputd_ring_path(
            read_value(outputd_text, OUTPUTD_RING_PATH_ENV_VAR)
        )
        os.environ[OUTPUTD_RING_SLOTS_ENV_VAR] = str(
            resolve_outputd_ring_slots(
                read_value(outputd_text, OUTPUTD_RING_SLOTS_ENV_VAR)
            )
        )
        os.environ.pop(_LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV, None)
    else:
        os.environ.pop(_LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV, None)
        os.environ.pop(OUTPUTD_CONTENT_BRIDGE_ENV_VAR, None)
        os.environ.pop(OUTPUTD_RING_PATH_ENV_VAR, None)
        os.environ.pop(OUTPUTD_RING_SLOTS_ENV_VAR, None)


def read_persisted_coupling(env_path: str | os.PathLike = FANIN_ENV_PATH) -> str:
    """The coupling the daemons will read on their next start (resolved,
    fail-safe to loopback). Doctor + observability use this to compare the
    persisted intent against the live fan-in transport."""
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except OSError:
        return COUPLING_LOOPBACK
    return resolve_coupling(read_value(text, COUPLING_ENV_VAR))


@dataclass(frozen=True)
class EntryLock:
    """Outcome of the entry-verb lock acquisition.

    ``outcome`` is ``acquired`` (``fh`` holds the advisory flock — the caller
    keeps it open for the WHOLE pass and closes it after), ``contended``
    (another reconcile pass held the lock past the bounded wait — the caller
    must abort loudly before touching env or daemons), or ``unavailable`` (the
    lock file could not be opened — fail-open: proceed unserialized rather than
    brick the reconcile; already logged at WARNING inside the helper).
    ``detail`` carries the holder pid / open error for the log line.
    """

    outcome: str
    fh: "IO[str] | None" = None
    detail: str = ""


def _acquire_entry_lock(
    path: str | Path = ENTRY_LOCK_PATH,
    *,
    timeout_seconds: float = ENTRY_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = ENTRY_LOCK_POLL_SECONDS,
) -> EntryLock:
    """Serialize the reconcile entry verbs behind one advisory flock.

    ``jasper-fanin-coupling-auto.service``, install.sh, and an operator CLI can
    invoke the same transition concurrently. One flock held for the whole pass
    keeps their ordered CamillaDSP/fan-in/outputd transitions atomic. systemd
    serializes starts of the same unit; this lock covers unit-vs-CLI and direct
    CLI-vs-CLI pairs.

    Bounded wait, never open-ended: contention past ``timeout_seconds`` returns
    ``contended`` and the caller reacts before any env write or daemon op (no
    partial state to unwind). ``--auto`` / explicit abort through
    :func:`_handle_entry_lock_contention`. The wait absorbs the common fast
    confirm-path ``--auto`` holder; a genuinely long transition in flight is the
    case that SHOULD abort rather than stack.

    Fail-open on an unopenable lock file (missing /run on a dev host, a
    non-root probe): a broken lock path must not brick reconciles — proceed
    unserialized at WARNING. The holder stamps its pid into the file so the
    contention log can name it.
    """
    p = Path(path)
    try:
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fh: IO[str] = os.fdopen(fd, "r+", encoding="utf-8")
        except Exception:  # noqa: BLE001 - never leak the fd on a fdopen failure
            os.close(fd)
            raise
    except OSError as e:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="entry_lock_unavailable",
            lock_path=str(p),
            error=e,
            level=logging.WARNING,
        )
        return EntryLock(outcome="unavailable", detail=str(e))
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                try:
                    fh.seek(0)
                    holder = fh.read(64).strip()
                except OSError:
                    holder = ""
                fh.close()
                return EntryLock(
                    outcome="contended",
                    detail=f"held by pid {holder or 'unknown'}",
                )
            time.sleep(poll_seconds)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        pass  # pid stamp is diagnostic only — never fail an acquired lock on it
    return EntryLock(outcome="acquired", fh=fh)


def main(argv: "list[str] | None" = None) -> int:
    """CLI: ``jasper-fanin-coupling-reconcile <loopback|shm_ring>``
    (explicit operator choice) or ``--auto`` (P3/P4 default resolution).

    The explicit positional path stamps the operator-choice marker so a later
    ``--auto`` pass never overrides the operator's pick; ``--auto`` resolves the
    coupling + USB combo by eligibility and leaves the marker absent (auto-owned).

    Every verb runs under the shared entry flock (:func:`_acquire_entry_lock`)
    so two passes can never interleave their ordered daemon transitions.
    """
    import argparse

    # This CLI is the jasper-fanin-coupling-auto systemd entrypoint, so its
    # journal is where INFO-level transition evidence lands. Without a configured
    # handler the root logger falls back to Python's lastResort handler (WARNING+)
    # and silently drops normal confirmations.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="jasper-fanin-coupling-reconcile",
        description="Arm/disarm the fan-in -> CamillaDSP coupling in order.",
    )
    parser.add_argument(
        "coupling",
        nargs="?",
        choices=[COUPLING_LOOPBACK, COUPLING_SHM_RING],
        help=(
            "explicit operator choice (stamps the operator-choice marker so --auto "
            "won't override it): loopback (snd-aloop); shm_ring (Ring A + Ring B SHM "
            "rings — arms both fan-in and outputd). Mutually exclusive with "
            "--auto."
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "DEFAULT-RESOLUTION pass (P3/P4): when NO operator choice is recorded, "
            "resolve shm_ring on a ring-eligible box (else loopback) and arm the USB "
            "combo on a gadget box. An operator marker preserves the coupling choice "
            "while USB still follows source intent."
        ),
    )
    parser.add_argument("--reason", default="cli")
    parser.add_argument(
        "--no-apply",
        action="store_true",
        help="write the env only; skip the daemon transition (staging).",
    )
    args = parser.parse_args(argv)
    _modes = [args.auto, args.coupling is not None]
    if sum(bool(m) for m in _modes) > 1:
        parser.error("--auto and an explicit coupling choice are mutually exclusive")
    if not any(_modes):
        parser.error("give an explicit coupling choice or --auto")

    # Serialize the WHOLE pass against the sibling entry verbs (the two oneshot
    # units + install.sh / operator CLI runs) — see _acquire_entry_lock. On
    # contention past the bounded wait, do NOT touch env or daemons; the verb
    # decides how loud (below).
    lock = _acquire_entry_lock(
        ENTRY_LOCK_PATH,
        timeout_seconds=ENTRY_LOCK_TIMEOUT_SECONDS,
        poll_seconds=ENTRY_LOCK_POLL_SECONDS,
    )
    if lock.outcome == "contended":
        return _handle_entry_lock_contention(args, detail=lock.detail)
    try:
        return _run_entry_verb(args)
    finally:
        if lock.fh is not None:
            lock.fh.close()


def _handle_entry_lock_contention(args, *, detail: str = "") -> int:
    """Abort an apply verb that could not acquire the coupling entry lock.

    ``--auto`` / an explicit coupling wanted to apply a change and could not, so
    they abort loudly.
    """
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="entry_lock_contended",
        reason=args.reason,
        lock_path=ENTRY_LOCK_PATH,
        timeout_seconds=ENTRY_LOCK_TIMEOUT_SECONDS,
        detail=detail or None,
        level=logging.ERROR,
    )
    print(
        "fan-in coupling reconcile: another reconcile pass holds "
        f"{ENTRY_LOCK_PATH} ({detail or 'unknown holder'}); "
        f"aborted after {ENTRY_LOCK_TIMEOUT_SECONDS:g}s without touching env "
        "or daemons.",
        file=sys.stderr,
    )
    return 1


def _run_entry_verb(args) -> int:
    """Body after validation and entry-lock acquisition."""
    # Hydrate os.environ from the wizard-owned env files (same set the daemons
    # load) BEFORE reconciling, so the camilla reconcile this triggers emits with
    # the persisted JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} etc. — not their
    # defaults. Without this, arming a coupling from a bare CLI/install shell
    # would silently RESET a tuned chunksize back to 1024 (same class caught on
    # JTS 2026-06-27). setdefault semantics keep an explicit shell override
    # winning. Mirrors jasper.cli.sound.
    from jasper.env_load import load_env_files

    load_env_files()

    if args.auto:
        auto = reconcile_auto(reason=args.reason, apply=not args.no_apply)
        print(
            f"coupling auto: owned={auto.owned} coupling={auto.coupling} "
            f"gadget={auto.gadget_present} usb_intent={auto.usb_intent_enabled} "
            f"combo_armed={auto.combo_armed} "
            f"usb_combo_changed={auto.usb_combo_changed} ok={auto.ok}"
            + (
                f" fanin_restarted_for_combo={auto.restarted_fanin_for_combo}"
                if auto.usb_combo_changed
                else ""
            )
            + (f" reason={auto.reason}" if auto.reason else "")
            + (f" detail={auto.detail}" if auto.detail else "")
        )
        return 0 if auto.ok else 1

    # Explicit operator choice: mark_operator_choice=True freezes the box to this
    # pick across future --auto passes (the revert lever).
    result = reconcile_coupling(
        args.coupling,
        reason=args.reason,
        apply=not args.no_apply,
        mark_operator_choice=True,
    )
    print(
        f"coupling reconcile: desired={result.desired} direction={result.direction} "
        f"ok={result.ok} changed={result.changed} "
        f"outputd={result.restarted_outputd} fanin={result.restarted_fanin} "
        f"camilla={result.reconciled_camilla}"
        + (f" recovered={result.recovered}" if result.recovered else "")
        + (f" detail={result.detail}" if result.detail else "")
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
