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
  (content.ring) via ``jts_ring_playback`` that jasper-outputd reads — or, on a
  roleful box whose active endpoint is armed, to the ACTIVE ring
  (active-content.ring) via ``jts_ring_active_playback``. Arming it is
  ONE coherent flip of BOTH ends: ``JASPER_FANIN_CAMILLA_COUPLING=shm_ring``
  (fanin.env) AND ``JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`` + the post-DSP
  ring's path/slots (outputd.env). ``_outputd_actions`` is the single writer of that
  pair; ``_arm_ring`` PREFLIGHTs the P1 ring assets (``ring_assets_ready``), the
  topology eligibility, and BOTH geometry axes (period AND Ring-A slot count),
  self-heals a shear-prone stale ``JASPER_FANIN_RING_SLOTS``, deletes a
  geometry-mismatched on-disk ring, and fail-safes to loopback+direct on any
  failure, so a half-installed ring platform, an incoherent geometry, or a partial
  flip never strands the realtime path.

  - **ARM** (loopback -> shm_ring): outputd (the post-DSP ring's reader) MUST
    come up first,
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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from jasper.atomic_io import atomic_write_text
from jasper.audio_runtime_plan import RouteMode, RuntimeEnvAction, fanin_coupling_action
from jasper.env_file import read_value, remove, upsert
from jasper.fanin.coupling_auto import (
    AutoCouplingDecision,
    COUPLING_CHOICE_ENV_VAR,
    COUPLING_CHOICE_OPERATOR,
    RingGate,
    is_operator_choice,
    read_marker,
    read_usb_gadget_available,
    resolve_auto_decision,
    ring_install_profile_ready,
    usb_combo_actions,
    usbsink_effectively_enabled,
)
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
# Not part of the ordered audio-graph bounce. jasper-voice is restarted by this
# module for exactly ONE reason: a coupling flip changed the box's resolved
# ASSISTANT wire width, which voice resolves once at start (U2 PR-2). See
# :func:`_restart_voice_for_assistant_width`.
VOICE_UNIT = "jasper-voice.service"
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
class _LoopbackDaemonOps:
    """Ordered Camilla/fan-in/outputd convergence result for loopback."""

    camilla_ok: bool
    fanin_ok: bool
    outputd_ok: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.camilla_ok and self.fanin_ok and self.outputd_ok


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


# A DELIBERATE restart must not spend a daemon's CRASH-recovery start budget.
# Every restart this reconciler issues is a control-plane config-apply (a source
# toggle, a deploy, a boot pass) — never crash recovery. systemd counts them in
# the same StartLimitBurst window anyway, and jasper-fanin / jasper-outputd
# escalate an exhausted window straight to StartLimitAction=reboot (jasper-camilla
# parks instead, via its recovery handler). Issue #2175: pressing the /bluetooth/
# power toggle rebooted a Zero 2 W, because EVERY source transaction asks this
# owner to converge and a desired-On USB source that cannot compose re-arms then
# disarms fan-in on each pass — five fan-in starts inside 300 s, and PID 1
# rebooted the speaker. ``systemctl reset-failed`` clears the failed latch AND
# the start rate counter, so a config-apply restart starts from a clean budget.
# Genuine crash loops still escalate: a daemon's own Restart= path never reaches
# this code, so only reconciler-initiated starts are exempted.
# Same fix and same rationale as jasper.multiroom.reconcile._reset_failed_unit
# (the 2026-06-24 follower reboot: six /grouping/set POSTs in 44 s tripped
# outputd's start-limit) — this module was the remaining audio-graph reconciler
# without it. The units below are exactly the long-running daemons this module
# bounces; the oneshot owners it starts have no crash budget to protect
# (jasper-fanin-coupling-auto pins StartLimitIntervalSec=0 for that reason) and
# are START_ONLY_UNITS in the broker, which would deny ``reset-failed`` anyway.
_START_BUDGET_VERBS = frozenset({"start", "restart", "try-restart"})
_CRASH_BUDGET_UNITS = frozenset({FANIN_UNIT, OUTPUTD_UNIT, CAMILLA_UNIT})
_RESET_FAILED_TIMEOUT_SEC = 5.0


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

    A start-consuming verb on one of the crash-budget daemons is preceded by a
    best-effort ``reset-failed`` so this deliberate apply cannot walk the target
    into StartLimitAction=reboot (see the block comment above). The reset never
    gates the action it precedes: a denied or failed reset is logged and the
    restart still runs.

    Guarded lazy import: a missing/broken control package degrades to a
    reported failure, never an exception out of the reconcile that would
    defeat the fail-safe ladder.
    """
    try:
        from jasper.control import restart_broker
    except ImportError as e:  # pragma: no cover - control pkg always present in prod
        return False, f"restart_broker unavailable: {e}"
    if verb in _START_BUDGET_VERBS and unit in _CRASH_BUDGET_UNITS:
        reset = restart_broker.manage_units(
            unit,
            verb="reset-failed",
            reason=reason,
            no_block=False,
            timeout=_RESET_FAILED_TIMEOUT_SEC,
        )
        if not reset.get("ok"):
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="start_budget_reset_failed",
                unit=unit,
                reason=reason,
                detail=str(reset.get("error") or f"rc={reset.get('rc')}"),
                level=logging.WARNING,
            )
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


def _try_restart_voice(reason: str) -> tuple[bool, str]:
    """``try-restart`` jasper-voice through the broker. (ok, detail).

    ``try-restart``, not ``restart``, and the difference is load-bearing: a
    stopped jasper-voice must STAY stopped. A no-mic box parks the unit through
    its ``ConditionPathExists=!/var/lib/jasper/voice-input-absent`` gate, and an
    operator can stop it deliberately; a coupling flip is not permission to
    start either one. ``try-restart`` is a no-op on an inactive unit.

    Not in ``_CRASH_BUDGET_UNITS``: this fires only on an actual width
    TRANSITION — at most once per coupling flip, which is itself an operator or
    deploy event — so it cannot walk the start-limit window the way the
    per-transaction fan-in bounces could.
    """
    return _restart_unit(VOICE_UNIT, verb="try-restart", reason=reason, timeout=8.0)


def _assistant_width_token(env_path: str | Path) -> str:
    """The box's resolved ASSISTANT wire width, from the persisted files.

    Read through :func:`jasper.fanin_coupling.assistant_wire_is_wide` — the same
    one rule ``jasper-fanin``'s ``Config::program_wire_is_wide`` calls and
    ``jasper-voice`` resolves at start — so this observes the transition voice
    would observe, rather than a second opinion about it.

    BOTH halves come from ``env_path`` when it declares them, and only then fall
    back to the standard ``jasper.env`` -> ``fanin.env`` chain (where the format
    key may legitimately live in the base file). With the default path those are
    the same read; with an explicit one — the CLI's ``--env-path``, and every
    test — reading the coupling from the caller's file and the format from a
    module constant would make the predicate only accidentally coherent.
    """
    from jasper.fanin_coupling import (
        RING_WIRE_FORMAT,
        RING_WIRE_FORMAT_ENV_VAR,
        RING_WIRE_FORMAT_WIDE,
        assistant_wire_is_wide,
        read_declared_ring_wire_format,
        resolve_ring_wire_format,
    )

    try:
        raw_format: str | None = None
        try:
            raw_format = read_value(
                Path(env_path).read_text(encoding="utf-8"), RING_WIRE_FORMAT_ENV_VAR
            )
        except OSError:
            raw_format = None
        wire_format = (
            resolve_ring_wire_format(raw_format)
            if raw_format is not None
            else read_declared_ring_wire_format()
        )
        wide = assistant_wire_is_wide(
            wire_format=wire_format,
            coupling=read_persisted_coupling(env_path),
        )
    except (OSError, ValueError):
        # An unreadable/typo'd declaration is fan-in's fault to report (it parks
        # at exit 78). Resolving narrow here matches what jasper-voice resolves
        # in the same situation, so the comparison stays honest.
        return RING_WIRE_FORMAT
    return RING_WIRE_FORMAT_WIDE if wide else RING_WIRE_FORMAT


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


# How long a blocking start of the audio-hardware reconciler may take.
#
# The DISARM kick keeps the 15 s bound it shipped with — the same one the
# topology-reset kick uses (``jasper.cli.output_topology_reset._trigger_reconcile``)
# — because a timeout there costs only a delayed content-BUFFER floor re-emit,
# which the next udev/boot/deploy event converges anyway.
#
# The ARM converge gets real headroom instead, and the reason is a measured one:
# on jts4 (Pi Zero 2 W) a full reconciler pass takes on the order of the old
# bound itself, so 15 s left approximately zero margin on the slowest board in
# the fleet — and a timeout there is not a delayed nicety, it is a refused arm
# plus a re-converge (see :func:`_arm_ring`). 60 s is four times the observed
# duration.
#
# THE BUDGET THIS SPENDS IS THE CALLER UNIT'S, not the broker's. The broker's
# ``_EXEC_TIMEOUT_CEILING_SEC`` (120 s) only CLAMPS a larger request; it sets no
# value and 60 s is nowhere near it. The binding limit is
# ``TimeoutStartSec=120`` on the ``Type=oneshot``
# ``deploy/systemd/jasper-fanin-coupling-auto.service``, and the timeout branch
# can overrun it: a converge that times out (60 s), then the ordered recovery
# (~20 s), then the best-effort re-converge (up to another 60 s) is ~140 s.
#
# That overrun is bounded and non-corrupting, which is why the value stands
# rather than being trimmed to fit. Recovery — the part that returns audio — has
# already landed by ~80 s, well inside the budget. What systemd's SIGTERM at
# 120 s can cut is only the trailing re-converge, whose failure this code
# already treats as best-effort; the resulting end state is exactly the REFUSAL
# branch's (box on loopback, ``JASPER_OUTPUTD_CONTENT_FORMAT`` possibly still
# naming the ring wire until the next udev/boot/deploy hardware-reconcile event
# converges it). Reaching it needs a compound-rare condition — the converge must
# time out rather than fail, on a box slow enough to exceed a bound already 4x
# its measured pass. Trimming 60 s to fit the worst case would instead make the
# ordinary first arm time out on the slowest board, which is the failure this
# constant exists to prevent.
_HARDWARE_RECONCILE_TIMEOUT_SEC = 15.0
_ARM_CONTENT_FORMAT_CONVERGE_TIMEOUT_SEC = 60.0


def _start_audio_hardware_reconcile(
    reason: str, *, timeout: float = _HARDWARE_RECONCILE_TIMEOUT_SEC
) -> tuple[bool, str]:
    """Start the audio-hardware reconciler oneshot through the broker. (ok, detail).

    Blocking, so the caller returns with the env actions actually re-emitted, not
    just requested. ``start`` of this unit is broker-permitted for non-root
    clients (``START_ONLY_UNITS``) and falls back to direct systemctl for a
    broker-less root shell — the same reach every other daemon op here has.

    ``timeout`` is per-caller because the two callers have different stakes; see
    :data:`_HARDWARE_RECONCILE_TIMEOUT_SEC`.
    """
    return _restart_unit(
        AUDIO_HARDWARE_RECONCILE_UNIT, verb="start", reason=reason, timeout=timeout
    )


def kick_timed_out(detail: str) -> bool:
    """Did a failed daemon op TIME OUT, rather than being refused outright?

    The distinction decides whether a still-running oneshot can land a write
    BEHIND a rollback, and it is the only reason the two are told apart:

    - **Refused** (allowlist rejection, unknown verb, a completed unit exiting
      non-zero): nothing is in flight when the caller gives up, so a rollback
      that rewrites the env afterwards is the last writer and stays the last
      writer.
    - **Timed out**: the unit may still be RUNNING. It read its inputs before
      the rollback and can write its outputs after it, inverting the order the
      rollback depends on. The caller must re-run it once the rollback's own
      write has landed — see ``reconverge_content_format`` in
      :func:`_fail_ring_arm`.

    Both of the broker's timeout paths carry the same substring and nothing else
    does: ``subprocess.TimeoutExpired`` stringifies as "... timed out after N
    seconds" (the exec bound, on the broker thread and on the root direct
    fallback alike), and a client socket timeout reaches
    :class:`~jasper.control.restart_broker.BrokerUnavailable` as bare "timed
    out". Matching on the broker's wording is the fragile part, so
    ``tests/test_fanin_coupling_reconcile.py`` builds BOTH real exception
    strings and asserts this returns True for each: a broker-side re-wording
    fails there loudly instead of silently downgrading every timeout to a
    refusal, which is the fail-OPEN direction.
    """
    return "timed out" in detail.lower()


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

    SCOPE: this coordinates DELIBERATE Python-side fan-in restarts — today
    only this module's own caller, :func:`reconcile_auto`'s auto USB-combo
    restart. The public out-of-module entry point this docstring used to
    describe, ``coordinated_fanin_restart``, was deleted in P5c: its sole
    caller (the adaptive output-buffer arm) was deleted with it, and its
    ok-flattening-on-resume-failure behavior had no remaining consumer to
    justify keeping. Since the ring-ioplug
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
    restart_voice: "DaemonOp | None" = None,
) -> CouplingResult:
    """Reconcile the coupling, then bound the ASSISTANT-width transient it can open.

    The coupling reconcile itself is :func:`_reconcile_coupling_inner`; this
    wrapper adds ONE thing, and wraps rather than threading it through because
    the inner function has a dozen exits (arm, disarm, confirm, three recovery
    ladders) and every one of them can land on a different coupling than it was
    asked for.

    WHY VOICE IS IN THIS MODULE'S RESTART SET AT ALL. The box's assistant IPC
    width is ``wire_format == S32_LE AND coupling == shm_ring``
    (:func:`jasper.fanin_coupling.assistant_wire_is_wide`), and ``jasper-voice``
    resolves it ONCE at start — it is not restarted by the ordered audio-graph
    bounce. So a coupling flip alone can leave voice speaking the old width into
    a fan-in that now expects the other one. That is converted losslessly and
    logged (``event=fanin.tts_wire_width_mismatch``), never a level error — but
    without this it is a STANDING disagreement, not a transient, and nothing in
    the system would ever end it. Comparing the resolved width across the
    reconcile and issuing one ``try-restart`` makes the window the length of a
    flip.

    Both reads are file-fresh and go through the same rule voice uses, so the
    comparison sees the transition voice would see. The restart is best-effort:
    a failure is logged and never changes the coupling verdict, because the
    coupling IS reconciled either way and the remaining exposure is a precision
    difference the reader already handles.
    """
    before = _assistant_width_token(env_path)
    result = _reconcile_coupling_inner(
        desired_raw,
        reason=reason,
        env_path=env_path,
        outputd_env_path=outputd_env_path,
        apply=apply,
        mark_operator_choice=mark_operator_choice,
        restart_fanin=restart_fanin,
        restart_outputd=restart_outputd,
        reconcile_camilla=reconcile_camilla,
        kick_hardware_reconcile=kick_hardware_reconcile,
        active_leader_check=active_leader_check,
    )
    if not apply:
        # Staging/migration writes the env but runs no daemon ops; restarting
        # voice here would be the one daemon op an apply=False pass performed.
        return result
    after = _assistant_width_token(env_path)
    if after == before:
        return result
    do_restart_voice = restart_voice or (lambda: _try_restart_voice(reason=reason))
    ok, detail = do_restart_voice()
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="assistant_width_voice_restarted" if ok else "assistant_width_voice_restart_failed",
        reason=reason,
        assistant_width_before=before,
        assistant_width_after=after,
        detail=detail or None,
        level=logging.INFO if ok else logging.WARNING,
    )
    return result


def _reconcile_coupling_inner(
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
      bouncing fan-in. Three exceptions on an armed shm_ring box, in order:
      an ioplug that cannot parse the resolved wire recovers to loopback
      immediately (re-arming would meet the same refusal); an incoherent box
      (stale ring slots/files) escalates to the full ``_arm_ring`` ordered
      bounce; and ``RING_CONFIRM_STRIKE_LIMIT`` consecutive camilla-confirm
      failures recover to loopback, so a box whose CamillaDSP cannot load the
      ring config stops sitting there silently.

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
    # The SAME unit, a different bound, because the two callers' stakes differ
    # (see _HARDWARE_RECONCILE_TIMEOUT_SEC). An injected op overrides both, so a
    # test still has one hook rather than two.
    do_converge_content_format = kick_hardware_reconcile or (
        lambda: _start_audio_hardware_reconcile(
            reason=reason, timeout=_ARM_CONTENT_FORMAT_CONVERGE_TIMEOUT_SEC
        )
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

    assert action is not None, "supported coupling must resolve an env action"
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
            # CAPABILITY first, and it does not escalate to an arm: a box armed
            # on a wire the INSTALLED ioplug cannot open (the degraded-deploy
            # walk — a stale .so beside new daemons) has CamillaDSP failing to
            # start, and re-running the arm would meet the same -EINVAL. The only
            # state that plays audio is loopback, so go there directly.
            caps_ok, caps_detail = ring_wire_caps_ready()
            if not caps_ok:
                log_event(
                    logger,
                    "fanin.coupling_reconcile",
                    result="confirm_ring_ioplug_caps_missing",
                    desired=desired,
                    reason=reason,
                    detail=caps_detail,
                    level=logging.ERROR,
                )
                recovered = _recover_to_loopback(
                    do_restart,
                    do_restart_outputd,
                    do_reconcile,
                    fanin_snapshot.path,
                    outputd_snapshot.path,
                    reason,
                )
                _clear_ring_confirm_failures()
                return CouplingResult(
                    ok=False,
                    desired=COUPLING_LOOPBACK,
                    changed=True,
                    direction="confirm",
                    recovered=recovered,
                    detail=caps_detail,
                )

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
                    do_converge_content_format,
                )

        # Env already at desired AND coherent: re-confirm camilla only (self-heal a
        # drifted loaded config) — no fan-in bounce on a no-op tick.
        ok, detail = do_confirm_reconcile(desired)

        # A FAILED confirm on an ARMED ring used to end here: logged, ok=False,
        # box left armed with CamillaDSP unable to load the ring config and
        # nothing that would ever move it. Count consecutive failures and
        # escalate to recovery, which is the only outcome that restores audio.
        if desired == COUPLING_SHM_RING:
            if ok:
                _clear_ring_confirm_failures()
            else:
                strikes = _record_ring_confirm_failure(detail, reason)
                if strikes >= RING_CONFIRM_STRIKE_LIMIT:
                    log_event(
                        logger,
                        "fanin.coupling_reconcile",
                        result="confirm_ring_failure_escalated",
                        desired=desired,
                        reason=reason,
                        strikes=strikes,
                        limit=RING_CONFIRM_STRIKE_LIMIT,
                        detail=detail or None,
                        level=logging.ERROR,
                    )
                    recovered = _recover_to_loopback(
                        do_restart,
                        do_restart_outputd,
                        do_reconcile,
                        fanin_snapshot.path,
                        outputd_snapshot.path,
                        reason,
                    )
                    _clear_ring_confirm_failures()
                    return CouplingResult(
                        ok=False,
                        desired=COUPLING_LOOPBACK,
                        changed=True,
                        direction="confirm",
                        recovered=recovered,
                        detail=(
                            f"CamillaDSP failed to confirm the ring config "
                            f"{strikes} times in a row ({detail}); recovered the "
                            "box to loopback so audio returns"
                        ),
                    )
                log_event(
                    logger,
                    "fanin.coupling_reconcile",
                    result="confirm_ring_failure_strike",
                    desired=desired,
                    reason=reason,
                    strikes=strikes,
                    limit=RING_CONFIRM_STRIKE_LIMIT,
                    detail=detail or None,
                    level=logging.WARNING,
                )

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
            do_converge_content_format,
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
    fanin_snapshot = _read_snapshot(env_path)
    outputd_snapshot = _read_snapshot(outputd_env_path)
    marker = read_marker(fanin_snapshot.text)
    gadget = (
        read_usb_gadget_available() if gadget_present is None else gadget_present
    )
    usb_intent_failure = ""
    if usb_intent_enabled is None:
        try:
            usb_intent = usbsink_effectively_enabled()
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


# outputd's own declarations, read from the reconciler-owned outputd.env. The
# FORMAT key is written by jasper-audio-hardware-reconcile (from
# ``content_lane_format_for_coupling``), not by this reconciler — which is why
# the width gate only compares it on an already-armed box. The CHANNELS key is
# the DacProfile's active-lane width; unset means outputd derives 2
# (``config.rs``: ``SinkMode::SingleAlsa => active_channels.unwrap_or(2)``).
#
# BOTH keys DECLARE A DEFAULT when absent rather than being indeterminate, and
# the defaults are the daemon's own (``config.rs``): an empty/unset
# CONTENT_FORMAT resolves ``SampleFormat::S16Le``, and an unset ACTIVE_CHANNELS
# resolves 2 on a single-ALSA sink. Reading an absent key as "unknown" would
# refuse the arm on every box whose hardware reconciler has not written the key
# yet, for a wire the daemon would in fact have declared correctly.
_OUTPUTD_CONTENT_FORMAT_ENV_VAR = "JASPER_OUTPUTD_CONTENT_FORMAT"
_OUTPUTD_ACTIVE_CHANNELS_ENV_VAR = "JASPER_OUTPUTD_ACTIVE_CHANNELS"
_OUTPUTD_DEFAULT_CONTENT_CHANNELS = 2
_OUTPUTD_DEFAULT_CONTENT_FORMAT = "S16_LE"


# Which ring a declaration is held to, on the CHANNELS axis. Named rather than
# spelled as bare strings at each construction site: the ACTIVE value arrived
# after the other two and a typo'd third literal would silently fall through to
# whichever branch the comparison ends with.
RING_A = "A"
RING_B = "B"
RING_ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class RingWireDeclaration:
    """One declaring end's statement of the ring wire, and where it was read.

    ``sample_format`` / ``channels`` are ``None`` for an axis this end does not
    declare — not a wildcard that matches anything, but "this end is silent
    here", which the comparison reports rather than passes.

    ``ring`` is :data:`RING_A`, :data:`RING_B` or :data:`RING_ACTIVE` and selects
    which channel count this end is held to; the three are separate axes by
    design — Ring A is always the stereo program fan-in mixes, Ring B follows the
    box's output topology, and the ACTIVE ring carries a roleful box's
    post-crossover per-driver width — so the comparison cannot use one number.
    (``active_ring_endpoint_proof`` still owns the ACTIVE ring's conf.d + marker
    STAGING; what reaches here on that ring is the loaded CamillaDSP graph, whose
    format and width nothing else compares.)

    ``channels_excused`` marks an end that STRUCTURALLY states no channel count,
    which is a different fact from one that tried and could not — only the
    latter is an indeterminate declaration the gate refuses. It is a per-axis
    flag rather than a reuse of ``note`` because the notes are not per-axis: the
    outputd end carries a note explaining why its FORMAT is not compared before
    arming, and that note must not also excuse its channels, which ARE compared
    on an unarmed box.
    """

    end: str
    source: str
    ring: str
    sample_format: str | None = None
    channels: int | None = None
    note: str = ""
    channels_excused: bool = False


@dataclass(frozen=True)
class LoadedCamillaGraph:
    """ONE snapshot of the CamillaDSP graph the durable statefile points at.

    A snapshot object rather than three field reads: the width gate compares a
    lane's device, format and channels together, and reading them one at a time
    through :func:`jasper.camilla_config_contract.read_camilla_device_field`
    would re-open the file per field — three answers that need not come from one
    revision of it. ``devices`` is
    :func:`~jasper.camilla_config_contract.parse_camilla_devices_config`'s subset
    over that single read.

    ``note`` is empty when the graph WAS read and non-empty saying why not
    otherwise. It is never an exception: a box with no statefile yet is the
    ordinary fresh-install state, and a gate that refused it would refuse the
    unattended pass on every new box.
    """

    path: str
    devices: Mapping[str, Any]
    note: str = ""


def read_loaded_camilla_graph() -> LoadedCamillaGraph:
    """Read the loaded CamillaDSP graph once, for the callers that compare it.

    Statefile -> ``config_path`` -> the config's ``devices:`` subset, through the
    same public reader (``read_camilla_statefile_config_path``) every other
    surface uses, so this adds no fourth copy of the statefile scan and honours
    ``JASPER_CAMILLA_STATEFILE``.
    """
    from jasper.active_speaker.environment import read_camilla_statefile_config_path
    from jasper.camilla_config_contract import parse_camilla_devices_config

    config_path = read_camilla_statefile_config_path()
    if not config_path:
        return LoadedCamillaGraph(
            path="",
            devices={},
            note="no CamillaDSP statefile config_path to read",
        )
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedCamillaGraph(
            path=config_path,
            devices={},
            note=f"{config_path} is unreadable ({exc.strerror or type(exc).__name__})",
        )
    devices = parse_camilla_devices_config(text)
    if not devices:
        return LoadedCamillaGraph(
            path=config_path,
            devices={},
            note=f"{config_path} declares no parseable devices block",
        )
    return LoadedCamillaGraph(path=config_path, devices=devices)


def load_topology_for_wire():
    """The saved output topology for a wire resolution, or ``None``.

    Fail-SOFT on every error: ``resolve_ring_wire(None)`` answers the shipped
    stereo geometry, which is the right question for a box whose topology cannot
    be read — and refusing to arm on an unreadable topology is
    :func:`ring_topology_ready`'s decision to make, with its own documented
    strict/lenient split, not this helper's.
    """
    try:
        from jasper.output_topology import (
            OutputTopologyError,
            load_output_topology_strict,
        )

        return load_output_topology_strict()
    except (OutputTopologyError, OSError, ValueError, ImportError):
        return None


def _effective_env_value(
    later_text: str, key: str, *, later_path: str
) -> tuple[str | None, str]:
    """What a two-file ``EnvironmentFile=`` chain resolves for ``key``, and from where.

    Every audio daemon this module gates lists ``/etc/jasper/jasper.env`` as its
    FIRST ``EnvironmentFile=`` and its own ``/var/lib/jasper/<daemon>.env`` as a
    LATER one, so the later file wins and the earlier one is the fallback
    (``jasper-fanin.service``, ``jasper-outputd.service``). A reader that
    consults only the later file reports a default while an operator value in
    the system env still controls the next daemon start — and that is not
    hypothetical: it is the
    documented operator seam, which ``outputd_latency_floor_actions`` reaches by
    REMOVING the generated key from the later file precisely so the earlier one
    is the only declaration left.

    ``later_text`` is the caller's already-read snapshot of the later file (the
    arm path holds one it may have just written); ``jasper.env`` is read here.
    Returns the RAW string and its source path, applying no emptiness or parse
    policy — each caller's own vocabulary for "declared but empty" differs, and
    collapsing them here would make one of them wrong. ``source`` is meaningful
    only when the value is not ``None``.

    ONE chain, three readers: this, :func:`resolve_effective_fanin_ring_slots`
    and :func:`resolve_effective_fanin_wire_format`. Before it there were two
    hand-rolled copies and one reader (:func:`_resolved_outputd_period_frames`)
    that had never grown one.
    """
    raw = read_value(later_text, key)
    if raw is not None:
        return raw, later_path
    return read_value(_read_snapshot(JASPER_ENV_PATH).text, key), JASPER_ENV_PATH


def resolve_effective_fanin_wire_format(fanin_text: str) -> tuple[str, str]:
    """fan-in's declared Ring-A wire format, and which file declared it.

    Same ``jasper.env`` -> ``fanin.env`` chain systemd gives ``jasper-fanin``
    (:func:`_effective_env_value`, shared with
    :func:`resolve_effective_fanin_ring_slots`): looking only at
    ``fanin.env`` would report the default while an operator's value in the
    earlier system env still controls the next daemon start. An unset value
    declares the narrow default, which is what the Rust daemon resolves.

    THIS END IS NOW THE RESOLVER'S INPUT, and the width gate says so rather than
    pretending otherwise: since ``resolve_ring_wire`` reads the same key off the
    same chain, a live comparison of this end against the resolved wire agrees by
    construction. It stays a declaration because this reader takes the caller's
    fanin.env TEXT while the resolver reads the FILE — a snapshot that has
    diverged from disk mid-write is the one divergence left to report, and
    reporting it costs nothing. The independent witnesses on that axis are the
    conf.d, outputd's env and the loaded graph, each written by a different
    writer at a different time.
    """
    from jasper.fanin_coupling import RING_WIRE_FORMAT, RING_WIRE_FORMAT_ENV_VAR

    raw, source = _effective_env_value(
        fanin_text, RING_WIRE_FORMAT_ENV_VAR, later_path=FANIN_ENV_PATH
    )
    if raw is None or not raw.strip():
        return RING_WIRE_FORMAT, "default"
    return raw.strip(), source


def graph_wire_declarations(
    graph: LoadedCamillaGraph,
) -> tuple[RingWireDeclaration, ...]:
    """What the LOADED CamillaDSP graph declares, for each lane that IS a ring.

    The graph is a declaring end only for a lane whose device is one of the three
    ring PCMs (:data:`~jasper.fanin_coupling.RING_PCM_DEVICES`) — a lane on the
    dsnoop capture or the ALSA active lane declares a width for a transport that
    is not the ring, and holding it to the ring's wire would refuse every box
    that has not armed yet. So this returns ZERO declarations on an unarmed box
    and one or two on an armed (or mid-arm) one, and the caller says which
    happened rather than reporting the same sentence either way.

    Both lanes are inspected, not just playback: the ring reaches a graph from
    either side (Ring A is CamillaDSP's capture, Ring B and the ACTIVE ring are
    its playback), and a device-keyed test costs nothing to apply twice.
    """
    from jasper.fanin_coupling import (
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_CAPTURE_DEVICE,
        RING_PCM_DEVICES,
    )

    declarations: list[RingWireDeclaration] = []
    for lane in ("capture", "playback"):
        device = graph.devices.get(f"{lane}_device")
        if not isinstance(device, str) or device not in RING_PCM_DEVICES:
            continue
        if device == RING_CAPTURE_DEVICE:
            ring = RING_A
        elif device == RING_ACTIVE_PLAYBACK_DEVICE:
            ring = RING_ACTIVE
        else:
            ring = RING_B
        raw_format = graph.devices.get(f"{lane}_format")
        raw_channels = graph.devices.get(f"{lane}_channels")
        declarations.append(
            RingWireDeclaration(
                end=f"loaded CamillaDSP graph ({lane} {device})",
                source=graph.path,
                ring=ring,
                sample_format=raw_format if isinstance(raw_format, str) else None,
                channels=raw_channels if isinstance(raw_channels, int) else None,
            )
        )
    return tuple(declarations)


def ring_wire_declarations(
    *,
    fanin_text: str,
    outputd_text: str,
    armed: bool,
    graph: LoadedCamillaGraph | None = None,
) -> tuple[RingWireDeclaration, ...]:
    """What each of the ring's declaring ends says the wire is.

    ``armed`` gates the outputd FORMAT axis, and the reason is a real ordering
    fact rather than caution: ``JASPER_OUTPUTD_CONTENT_FORMAT`` is written by
    ``jasper-audio-hardware-reconcile`` (from ``content_lane_format_for_coupling``),
    NOT by this reconciler, and while the box is still on loopback it correctly
    carries the LOOPBACK lane's format. Comparing it against the ring wire at
    preflight time would refuse every arm on every box — the exact shape of the
    PR-1 defect this gate's history records. So before the arm that end is
    reported as not-yet-declared; once armed it is compared, which is where a
    degraded deploy's half-moved format actually shows up.

    ``graph`` adds the loaded CamillaDSP graph's own ring lanes
    (:func:`graph_wire_declarations`) — the end that made this list four rather
    than five names. Omitting it is legal (an env-only comparison) and the gate
    above is what refuses to CLAIM the graph agreed when it was not passed.
    """
    from jasper.fanin_coupling import (
        RING_A_CHANNELS,
        COUPLING_SHM_RING as _SHM,
        content_lane_format_for_coupling,
    )
    from jasper.ring_assets import (
        RING_A_CONF_PCM,
        RING_B_CONF_PCM,
        RING_CONF_D,
        ring_asset_presence,
        ring_conf_channels,
        ring_conf_format,
    )

    # An ABSENT conf.d is ``ring_assets_ready``'s refusal to own, not a second
    # one here — one missing file should produce one reason. A conf.d that is
    # PRESENT but declares no readable wire is a torn file, which no other gate
    # inspects, so that one stays this gate's to refuse.
    conf_present = ring_asset_presence().conf_present
    conf_absent_note = (
        ""
        if conf_present
        else f"{RING_CONF_D} absent — ring_assets_ready owns that refusal"
    )
    fanin_format, fanin_source = resolve_effective_fanin_wire_format(fanin_text)
    outputd_channels_raw = read_value(outputd_text, _OUTPUTD_ACTIVE_CHANNELS_ENV_VAR)
    try:
        outputd_channels = (
            int(outputd_channels_raw.strip())
            if outputd_channels_raw and outputd_channels_raw.strip()
            else _OUTPUTD_DEFAULT_CONTENT_CHANNELS
        )
    except ValueError:
        outputd_channels = None
    outputd_format_raw = read_value(outputd_text, _OUTPUTD_CONTENT_FORMAT_ENV_VAR)
    outputd_format = (
        outputd_format_raw.strip()
        if outputd_format_raw and outputd_format_raw.strip()
        else _OUTPUTD_DEFAULT_CONTENT_FORMAT
    )
    return (
        RingWireDeclaration(
            end="fan-in (Ring A writer)",
            source=fanin_source,
            ring=RING_A,
            sample_format=fanin_format,
            # fan-in's mixer is stereo and NOT configurable
            # (``mixer.rs``'s ``CHANNELS: u32 = 2``), mirrored here as
            # RING_A_CHANNELS. Comparing it catches a resolver that starts
            # answering a Ring A width the writer cannot produce.
            channels=RING_A_CHANNELS,
        ),
        RingWireDeclaration(
            end=f"conf.d {RING_A_CONF_PCM}",
            source=RING_CONF_D,
            ring=RING_A,
            sample_format=ring_conf_format(RING_A_CONF_PCM) if conf_present else None,
            channels=ring_conf_channels(RING_A_CONF_PCM) if conf_present else None,
            note=conf_absent_note,
            # An ABSENT conf.d states nothing on either axis and the asset gate
            # owns that refusal; a PRESENT one that cannot be parsed is a torn
            # file whose channels line this gate must refuse.
            channels_excused=not conf_present,
        ),
        RingWireDeclaration(
            end=f"conf.d {RING_B_CONF_PCM}",
            source=RING_CONF_D,
            ring=RING_B,
            sample_format=ring_conf_format(RING_B_CONF_PCM) if conf_present else None,
            channels=ring_conf_channels(RING_B_CONF_PCM) if conf_present else None,
            note=conf_absent_note,
            channels_excused=not conf_present,
        ),
        RingWireDeclaration(
            end="CamillaDSP emitted stanzas",
            source="capture_kwargs_for_coupling(shm_ring)",
            ring=RING_B,
            sample_format=content_lane_format_for_coupling(_SHM),
            note="counterfactual: what arming would emit",
            # The coupling's kwargs carry a format and no channel count — this
            # end genuinely has nothing to say on that axis, ever.
            channels_excused=True,
        ),
        RingWireDeclaration(
            end="outputd (Ring B reader)",
            source=str(OUTPUTD_ENV_PATH),
            ring=RING_B,
            sample_format=outputd_format if armed else None,
            channels=outputd_channels,
            note=(
                ""
                if armed
                else (
                    f"{_OUTPUTD_CONTENT_FORMAT_ENV_VAR} still declares the "
                    "loopback lane until the hardware reconciler re-emits it on "
                    "arm, so the format axis is not compared before arming"
                )
            ),
        ),
        *(graph_wire_declarations(graph) if graph is not None else ()),
    )


def resolve_wire_for_gate(topology: Any = None) -> tuple[Any | None, str]:
    """``(wire, "")`` — or ``(None, why)`` when the box declares an illegal wire.

    ``resolve_ring_wire`` FAILS LOUD on a
    ``JASPER_FANIN_RING_WIRE_FORMAT`` value neither language recognizes, exactly
    as ``jasper-fanin`` does (it parks at exit 78 rather than guessing). That is
    right for an emitter, and wrong for a GATE: the arm has already written the
    ring env by the time the preflights run, and an uncaught exception would skip
    the snapshot restore that makes a refused arm non-destructive — leaving the
    partial flip the whole fail-closed design exists to prevent.

    So every gate that needs the wire resolves it through here and turns a bad
    declaration into a refusal with the parser's own sentence. One helper rather
    than a ``try`` per gate: a gate added later gets the behaviour by using it.
    """
    from jasper.fanin_coupling import resolve_ring_wire

    try:
        return resolve_ring_wire(topology), ""
    except ValueError as exc:
        return None, (
            f"{exc} — refusing to arm on a wire this box cannot declare; "
            "keeping loopback"
        )


def _wire_channels_for_ring(ring: str, wire: Any) -> int | None:
    """Which of the resolved wire's three channel fields ``ring`` is held to.

    Keyed on the ring TOKEN an end carries rather than on a PCM name, because
    two of this gate's ends (fan-in's env, outputd's env) name no device at all.
    ``None`` only for :data:`RING_ACTIVE` on a box whose wire resolves no active
    width — the caller treats that as unproven, never as a wildcard.
    """
    if ring == RING_A:
        return int(wire.ring_a_channels)
    if ring == RING_B:
        return int(wire.ring_b_channels)
    active = wire.ring_active_channels
    return None if active is None else int(active)


def ring_edge_width_ready(
    *,
    fanin_text: str | None = None,
    outputd_text: str | None = None,
    graph: LoadedCamillaGraph | None = None,
) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate: do ALL the declaring ends state one wire?

    THE INVARIANT. For each ring, ``(sample_format, channels)`` is resolved once
    per box by ``jasper.fanin_coupling.resolve_ring_wire``, and every end that
    declares a geometry must declare exactly that. Any end that cannot ⇒ refuse
    to arm, fail-safe to loopback, naming the end and the value it declared.
    **Equality only, never a ranking**: no width-comparison primitive exists
    in-repo for ALSA format strings and ``S24_3LE`` — live on the DAC edge —
    already breaks any ordering by byte count, so this refuses ANY mismatch
    rather than asserting a direction the code does not independently verify.

    THE ENDS, and what each contributes:

    - **fan-in** — ``JASPER_FANIN_RING_WIRE_FORMAT`` off the daemon's own env
      chain, plus its compile-time stereo mixer width. Its FORMAT axis is the
      resolver's own input now (see
      :func:`resolve_effective_fanin_wire_format`), so it agrees by construction
      on the live path; the ends below are the independent ones;
    - **the conf.d** — both stereo PCM blocks, PER BLOCK, because Ring A and
      Ring B may legitimately differ on channels (Ring A is always the stereo
      program; Ring B follows the box's output topology) and only the file can
      say what the ioplug will attach with. The ACTIVE conf.d block is
      deliberately NOT one of this gate's ends — ``active_ring_endpoint_proof``
      proves it on its own path, with its own remedy;
    - **CamillaDSP's emitted stanzas** — the counterfactual "what would arming
      emit", which is what catches the kwargs override path breaking (if
      ``capture_kwargs_for_coupling`` ever stopped forcing the ring's own
      format, the emit would silently fall back to the box-wide program-lane
      default and mis-transcode every sample);
    - **outputd** — its declared content format (once armed; see
      :func:`ring_wire_declarations`) and its active-lane channel width;
    - **the LOADED CamillaDSP graph** — the config the statefile points at, for
      each lane whose device IS a ring PCM.

    WHY THE LOADED GRAPH IS AN END AND WHY THAT ARRIVED LATE. The counterfactual
    stanza end above answers "what would arming emit for the STEREO ring", which
    is not the same question as "what does the graph on this box's disk actually
    declare" — and on the ACTIVE-ring ladder it is not even the same ring. That
    ladder moves the GRAPH first, so by the time this gate runs on a roleful box
    the artifact naming the ring already exists and is the only end that can
    report a shear in it. On jts3 (2026-08-11,
    ``captures/r7b-jts3-arm2-20260811T132227Z``) the re-emit had written
    ``format: S32_LE`` against a resolver answering ``S16_LE`` and this gate
    returned ``(True, 'all declaring ends state one ring wire …')`` — it proved
    the ends it could see and reported the ones it could not, which is worse
    than a missing gate because it reads as covered. The graph is now inspected,
    and when it CANNOT be (no statefile, unreadable config, or a graph naming no
    ring PCM at all) the ok detail says so instead of counting it.

    NOT INSPECTED IS NOT REFUSED, deliberately. A box that has not armed yet
    loads a non-ring graph, and a fresh box has no statefile — refusing either
    would refuse the unattended pass on every box in the fleet, which is the
    same shape as the PR-1 defect below. So an absent graph end costs the
    message its claim, never the arm its verdict.

    WHAT REPLACED WHAT. This gate shipped as a zero-I/O counterfactual comparing
    two constants that both read one source — coherence by construction, which
    is not a check. It now reads the conf.d, both env files and the loaded graph,
    so it is no longer the cheapest gate and no longer runs first: it runs after
    topology eligibility, because on a box that resolves no ring width the wire
    question is not well-posed (``resolve_ring_wire`` falls back to the shipped
    stereo declaration there) and a mismatch report would name the wrong defect.

    THE RULING IN ITS HISTORY (wide-output-path PR-6, architect, 2026-08-08).
    An earlier form compared ``RING_WIRE_FORMAT`` against
    ``DEFAULT_PLAYBACK_FORMAT``, the box-wide program-lane default. That was
    correct while the two were equal, but once PR-6 widened the default it would
    have refused the ring on EVERY ring-eligible box — including jts.local,
    whose armed ring stays coherently narrow through the kwargs override and
    which carries a CERTIFIED USB-route latency artifact measured on that ring.
    Ring-coupled boxes keep the ring at its own resolved wire; the wide lane is
    the LOOPBACK path's property.

    ``fanin_text`` / ``outputd_text`` / ``graph`` default to reading their
    sources, so the gate stays callable with no arguments from
    :func:`default_ring_gates`; the arm path passes the snapshots it has already
    written so the gate judges the text the daemons will actually load. Each
    source is read ONCE per call.
    """
    if fanin_text is None:
        fanin_text = _read_snapshot(FANIN_ENV_PATH).text
    if outputd_text is None:
        outputd_text = _read_snapshot(OUTPUTD_ENV_PATH).text
    if graph is None:
        graph = read_loaded_camilla_graph()

    wire, wire_problem = resolve_wire_for_gate(load_topology_for_wire())
    if wire is None:
        return False, wire_problem
    armed = resolve_coupling(read_value(fanin_text, COUPLING_ENV_VAR)) == (
        COUPLING_SHM_RING
    )
    declarations = ring_wire_declarations(
        fanin_text=fanin_text,
        outputd_text=outputd_text,
        armed=armed,
        graph=graph,
    )

    problems: list[str] = []
    for decl in declarations:
        if decl.sample_format is not None and decl.sample_format != (
            wire.sample_format
        ):
            problems.append(
                f"{decl.end} declares format {decl.sample_format} "
                f"(from {decl.source})"
            )
        elif decl.sample_format is None and not decl.note:
            problems.append(
                f"{decl.end} declares no format at all (from {decl.source}) — "
                "an indeterminate end cannot be proven to match"
            )
        want_channels = _wire_channels_for_ring(decl.ring, wire)
        if want_channels is None:
            # Reachable only on the ACTIVE ring: the wire resolves no active
            # width (a non-roleful box, or a roleful one whose sink cannot carry
            # one). ``None`` there means "this box has no active ring", never
            # "any width matches", so an end declaring a width against it is
            # unproven rather than agreed.
            if decl.channels is not None:
                problems.append(
                    f"{decl.end} declares {decl.channels} channels, but this "
                    f"box's wire resolves NO active-ring width at all (from "
                    f"{decl.source}) — there is nothing to prove that against"
                )
        elif decl.channels is not None and decl.channels != want_channels:
            problems.append(
                f"{decl.end} declares {decl.channels} channels, expected "
                f"{want_channels} (from {decl.source})"
            )
        elif decl.channels is None and not decl.channels_excused:
            # SYMMETRY WITH THE FORMAT AXIS. An end that meant to state a channel
            # count and could not is indeterminate, and an indeterminate end
            # cannot be proven to match — the shape that reaches here is a
            # PRESENT conf.d whose block declares ``channels`` twice with
            # different values (``ring_conf_channels`` answers None for exactly
            # that torn file), or an outputd key that will not parse as an int.
            # Without this the channels axis passed such a box silently while
            # the format axis refused it.
            problems.append(
                f"{decl.end} declares no channel count at all (from "
                f"{decl.source}) — an indeterminate end cannot be proven to match"
            )
    if problems:
        return False, (
            f"the ring wire resolves to {wire.sample_format} / Ring A "
            f"{wire.ring_a_channels}ch / Ring B {wire.ring_b_channels}ch, but "
            "these ends disagree: "
            + "; ".join(problems)
            + ". Every declaring end must state the SAME wire or the ioplug "
            "attach fails hard at arm; keeping loopback until they agree"
        )
    # The COUNT and the NAMES come from the declarations that were actually
    # compared, so the message cannot outlive an end being dropped from the
    # list. The graph clause is what stops the ok from claiming an end this call
    # never saw — the jts3 shape in the docstring.
    inspected = ", ".join(decl.end for decl in declarations)
    graph_inspected = any(
        decl.end.startswith("loaded CamillaDSP graph") for decl in declarations
    )
    graph_clause = (
        ""
        if graph_inspected
        else (
            "; the loaded CamillaDSP graph was NOT one of them ("
            + (graph.note or "it names no ring PCM on either lane")
            + ") — it becomes a declaring end once the arm's first rung has "
            "re-emitted it against the ring"
        )
    )
    return True, (
        f"{len(declarations)} declaring ends state one ring wire "
        f"({wire.sample_format}, Ring A {wire.ring_a_channels}ch, Ring B "
        f"{wire.ring_b_channels}ch): {inspected}{graph_clause}"
    )


def ring_wire_caps_ready() -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate: can the INSTALLED ioplug open this wire?

    A RECORD COMPARE, never an open-probe. The reconciler must never open a ring
    PCM to find out what the plugin can do: on an armed box the ioplug's SPSC
    guard EBUSYs the probe, and probing a live ring is exactly the disturbance
    the doctor's armed-skip exists to avoid. So the installer records the sha and
    capability set of the ``.so`` it installed
    (``deploy/lib/install/ring-platform.sh``) and this compares that record
    against the resolved wire's needs — see
    :func:`jasper.ring_assets.ring_ioplug_wire_supported`.

    THE WALK IT CLOSES. The ioplug build degrades to a WARN, so a failed rebuild
    leaves the PREVIOUS ``.so`` installed beside freshly-installed Rust daemons.
    If the resolved wire renders a conf.d ``format`` / ``channels`` key that old
    plugin does not parse, it refuses the device at ``open()`` with ``-EINVAL``
    and CamillaDSP cannot start against the ring — on an ALREADY-armed box that
    is a crash loop the CONFIRM path used to watch without acting.

    DORMANT ON AN UNDECLARED BOX, BY CONSTRUCTION: the shipped wire renders no
    conf.d field beyond the ioplug's own defaults, so the needed capability set
    is empty and this returns ok WITHOUT reading the record or hashing anything.
    A box with no provenance record is unaffected — until it DECLARES a
    non-default wire (``JASPER_FANIN_RING_WIRE_FORMAT``), which is exactly when
    the record starts to matter and this gate starts to refuse without one.

    An unparseable declaration is refused here rather than raised — see
    :func:`resolve_wire_for_gate` for why a gate must not throw mid-arm.
    """
    from jasper.ring_assets import ring_ioplug_wire_supported

    wire, wire_problem = resolve_wire_for_gate(load_topology_for_wire())
    if wire is None:
        return False, wire_problem
    support = ring_ioplug_wire_supported(wire)
    return support.ok, support.detail


def ring_assets_ready() -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate: are the P1 ring-platform assets present?

    Checked BEFORE arming the ring coupling. Fail-SAFE: if the ioplug ``.so`` /
    conf.d / ``/dev/shm/jts-ring`` are not all present, arming would install a
    CamillaDSP config whose ``jts_ring_capture`` plus post-DSP ring device
    (``jts_ring_playback``, or ``jts_ring_active_playback`` on an armed roleful
    box) cannot resolve — CamillaDSP would crash-loop on its statefile and the fan-in
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
    """outputd's resolved ``JASPER_OUTPUTD_PERIOD_FRAMES`` (env chain, else default).

    Same two-file chain systemd gives ``jasper-outputd`` — ``/etc/jasper/jasper.env``
    first, ``/var/lib/jasper/outputd.env`` last, so the later file wins — resolved
    through :func:`_effective_env_value`, the shared primitive the fan-in gates use.
    When neither file declares it, outputd falls back to the packaged default
    written on its unit (``DEFAULT_OUTPUTD_PERIOD_FRAMES`` = 1024). A malformed
    value falls back to the default too.

    READING ``outputd.env`` ALONE WAS A GATE BUG, and the two-file chain is not a
    theoretical nicety here. ``outputd_latency_floor_actions`` gives an operator
    key in ``jasper.env`` precedence by REMOVING the generated key from
    ``outputd.env`` — so on a box using the documented operator seam, the period
    lives ONLY in the file this reader used to skip. It then reported the 1024
    default and ``ring_geometry_ready`` refused the arm while the RUNNING outputd
    was correctly at 128 (reproduced on jts4, 2026-08-14). Its sibling
    :func:`resolve_effective_fanin_ring_slots` already modelled the chain for
    fan-in's identical ``jasper.env`` -> ``fanin.env`` ordering; this is the same
    fix on the outputd side.

    DELIBERATELY NOT MIRRORED INTO THE DOCTOR, which reads the same key through
    a DIFFERENT and already-correct path: ``jasper.audio_runtime_plan``'s
    layered resolver, which threads ``base_env`` and reports this exact shape as
    ``source_kind="operator_env"`` from ``/etc/jasper/jasper.env``. The doctor
    never had this bug, so there is nothing there to fix — the gap was this
    module's alone.
    """
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_PERIOD_FRAMES

    raw, _ = _effective_env_value(
        outputd_text, "JASPER_OUTPUTD_PERIOD_FRAMES", later_path=OUTPUTD_ENV_PATH
    )
    if raw is None:
        return DEFAULT_OUTPUTD_PERIOD_FRAMES
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_OUTPUTD_PERIOD_FRAMES
    return value if value > 0 else DEFAULT_OUTPUTD_PERIOD_FRAMES


def active_ring_endpoint_proof() -> tuple[bool, str]:
    """Is this box's ACTIVE-ring endpoint actually staged? Two independent facts.

    A roleful topology having an active ring WIDTH says only that a ring could
    exist for it. Arming needs the endpoint to be STAGED, and that is two
    separate things, owned by two different writers:

    1. **The marker** — ``JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT`` in
       ``outputd.env``, written by ``jasper-audio-hardware-reconcile`` from the
       accepted active-lane decision. It is what tells outputd to expect the
       active ring, and outputd's own allowlist bails if the ring path and this
       marker disagree. Arming ahead of it would flip the coupling into a daemon
       that refuses the pairing — an exit-78 park, not a working ring.
    2. **The rendered conf.d block** — ``pcm.jts_ring_active_playback`` declaring
       this box's resolved active width. The ioplug attaches with what the block
       says; a block still on the shipped default while the graph declares a
       different width is a guaranteed attach failure.

    Both are checked because they have different failure modes and different
    remedies, so collapsing them into one reason would send an operator to the
    wrong fix. Fail-CLOSED on anything indeterminate: an unreadable conf.d
    declares nothing, which is not proof.
    """
    from jasper.active_speaker.runtime_contract import (
        active_ring_channels_for_topology,
    )
    from jasper.fanin_coupling import ring_active_endpoint_armed
    from jasper.ring_assets import RING_ACTIVE_CONF_PCM, ring_conf_channels

    if not ring_active_endpoint_armed():
        return False, (
            "outputd's active-ring endpoint marker "
            "(JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT in outputd.env) is not set — "
            "run `sudo systemctl start jasper-audio-hardware-reconcile` first so "
            "the endpoint pair is written from the active-lane decision, then "
            "re-arm"
        )
    topology = load_topology_for_wire()
    width = (
        active_ring_channels_for_topology(topology) if topology is not None else None
    )
    if width is None:
        return False, (
            "the saved topology resolves no active-ring width, so there is no "
            "width the conf.d block could be proved against"
        )
    declared = ring_conf_channels(RING_ACTIVE_CONF_PCM)
    if declared is None:
        return False, (
            f"the ring conf.d declares no readable channels for "
            f"pcm.{RING_ACTIVE_CONF_PCM} (absent, torn, or unreadable) — redeploy "
            "to reinstall it, then re-run jasper-audio-hardware-reconcile to "
            "render the per-box wire"
        )
    if declared != width:
        return False, (
            f"pcm.{RING_ACTIVE_CONF_PCM} declares channels={declared} but this "
            f"box's active ring resolves to {width} — the ioplug attaches with "
            "what the block says, so this would fail the attach. Run `sudo "
            "systemctl start jasper-audio-hardware-reconcile` to render the "
            "conf.d wire, then re-arm"
        )
    return True, (
        f"active-ring endpoint staged (marker set, pcm.{RING_ACTIVE_CONF_PCM} "
        f"declares channels={declared})"
    )


def composite_ring_wire_ready(topology: Any) -> tuple[bool, str]:
    """May THIS composite sink ride the ACTIVE ring at the wire the box declares?

    **Only at the WIDE wire (P8b item 1e).** Named and tested on its own so the
    rule is greppable, but wired into exactly ONE call site —
    :func:`ring_topology_ready`'s ACTIVE arm — because both arming paths (the
    unattended ``--auto`` pass and the operator arm) reach the ring through that
    one gate. A separate entry in ``default_ring_gates`` would have had to be
    threaded into ``_arm_ring``'s hand-written gate sequence as well, and a rule
    wired into one of two paths reads as covered while half of it is not.

    THE REGRESSION THIS REFUSES, which is invisible on every other axis. The
    CamillaDSP→outputd content hop takes its format from
    :func:`jasper.fanin_coupling.content_lane_format_for_coupling`: under
    ``loopback`` that is ``DEFAULT_PLAYBACK_FORMAT`` (**S32_LE**), under
    ``shm_ring`` it is ``resolve_ring_wire().sample_format`` — which defaults to
    the NARROW ``S16_LE`` unless the box declares otherwise. So moving a
    composite from its aloop lane onto the ring, changing nothing else, would
    narrow the POST-crossover per-driver program from 32 to 16 bits. That is the
    exact quantization class the wide-output-path program exists to remove,
    arriving through a transport change nobody would look at for it.

    ``ring_edge_width_ready`` cannot catch it: that gate proves every declaring
    end states the SAME wire, and a narrow composite arm is perfectly
    self-consistent — every end says ``S16_LE`` and it passes. Coherence is not
    width. This is the only gate that asks whether the width itself is a
    regression, and it asks it for the composite alone, because the composite is
    the only sink whose ring arm is a fresh decision this campaign is making.

    THE SCOPE OF THE CLAIM, stated honestly because the obvious objection is a
    fair one. This is about the CamillaDSP→outputd HOP, not the DAC edge. The
    composite's own ``final_edge_format`` is ``S16_LE`` today — the paired sink
    has no packed-24 child write path (#2257) — so a reader may reasonably ask
    what a 32-bit hop buys when the edge is 16 anyway. The answer is the
    invariant, not a measured delta: the wide-output-path program's rule is that
    the post-crossover hop carries the i32 program spine's width and quantizes
    ONCE, at the edge, where the DAC's own format decides it. A 16-bit hop
    quantizes early and then again after outputd's per-driver gain, trim and
    protection have scaled it — and it silently pre-empts #2257, which exists to
    widen that edge. No audible-harm figure is claimed here; none has been
    measured on a composite.

    NOT a policy override of the operator's declaration: the wire stays the
    box's own ``JASPER_FANIN_RING_WIRE_FORMAT`` (one writer, one source of
    truth). This refuses the unsafe COMBINATION and names the remedy, rather
    than silently rewriting the operator's file.

    Non-composite topologies pass untouched — jts3's roleful DAC8x arm and every
    stereo-ring box keep the wire they have today.
    """
    from jasper.active_speaker.runtime_contract import topology_sink_is_composite
    from jasper.fanin_coupling import (
        RING_WIRE_FORMAT_ENV_VAR,
        RING_WIRE_FORMAT_WIDE,
        read_declared_ring_wire_format,
    )

    if topology is None or not topology_sink_is_composite(topology):
        return True, "not a composite sink; the wide-wire rule does not apply"
    try:
        declared = read_declared_ring_wire_format()
    except ValueError as exc:
        # A wire token neither language recognizes. fan-in parks at exit 78 on
        # the same value, so refusing here is the same verdict, earlier.
        return False, (
            f"this box declares an unusable ring wire ({exc}), so the composite "
            "wide-wire rule cannot be proved — fix the token, then re-arm"
        )
    if declared != RING_WIRE_FORMAT_WIDE:
        return False, (
            f"a composite sink may ride the ACTIVE ring only at the WIDE wire, "
            f"but this box declares {RING_WIRE_FORMAT_ENV_VAR}={declared}. Its "
            f"aloop lane carries the post-crossover per-driver program at "
            f"S32_LE, so arming the ring narrow would quantize every driver's "
            f"signal from 32 to 16 bits — a width REGRESSION disguised as a "
            f"transport change, which the every-end wire gate cannot see "
            f"(a narrow arm is perfectly self-consistent). Set "
            f"{RING_WIRE_FORMAT_ENV_VAR}={RING_WIRE_FORMAT_WIDE} in "
            f"/var/lib/jasper/fanin.env and re-run "
            f"jasper-audio-hardware-reconcile so every end re-renders, then "
            f"re-arm. Keeping the coupling on loopback."
        )
    return True, (
        f"composite sink declares the wide ring wire ({RING_WIRE_FORMAT_WIDE}), "
        "so the arm does not narrow the per-driver program"
    )


def ring_topology_ready(*, strict_unreadable: bool = False) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate for topology eligibility.

    TWO admitting arms, because there are two rings:

    - **the STEREO arm** — Ring A/Ring B carry a full-range stereo program on a
      single coherent ALSA sink, so this is legal for the plain-stereo /
      unconfigured output contract only. It consults
      ``topology_supports_shm_ring``, the single stereo-ring-eligibility
      predicate, so arming a non-eligible box refuses with a crisp reason here
      instead of failing later at outputd's Rust full-range-stereo rejection (a
      confusing daemon-level rollback);
    - **the ACTIVE arm** — a ROLEFUL topology is admitted iff it resolves an
      active-ring width AND :func:`active_ring_endpoint_proof` holds. Since P8b
      item 1b a ROLEFUL COMPOSITE resolves a width (4) and reaches this arm, so
      it carries one extra condition the single-sink shapes do not:
      :func:`composite_ring_wire_ready`, the wide-wire rule. Explicit-mono still
      resolves no active width, and a PASSIVE composite is not roleful at all,
      so both stay refused.

    **Why an arm here and NOT a widening of ``topology_supports_shm_ring``.**
    Making that predicate true for roleful is the forbidden one-liner: it has two
    other consumers, and both would silently change meaning. The unattended
    ``--auto`` pass would find every gate passing on a roleful box and AUTO-ARM
    the fleet — marker absent, so outputd would refuse the pairing and park the
    speaker with no operator anywhere near it. And ``jasper.sound.camilla_yaml``'s
    flat-cutover defusal gate protects exactly the boxes that widening would
    re-expose. The eligibility question genuinely differs per ring, so it is asked
    per ring, here, where the endpoint proof is also in scope.

    Unreadable-topology policy is caller-selectable:

    - ``strict_unreadable=True``: fail-CLOSED. Both the unattended ``--auto``
      default pass AND the explicit operator arm now use this. For the auto pass
      it is the original reason — an unattended default that armed on an
      unreadable topology would arm→rollback on every boot/deploy the file is
      transiently corrupt. For the OPERATOR arm it is newer: the arm used to
      fail-OPEN on the stated grounds that outputd's own guard was the backstop,
      and that backstop was proven to fail open on the very same error (the
      topology read failure clears the active-lane marker, the stereo predicate
      then admits the ring). The allowlist restores the backstop, but the operator
      arm stays fail-CLOSED anyway: a human is present to fix an unreadable
      topology, and refusing costs them a rerun where admitting costs a park.
    - ``strict_unreadable=False``: fail-OPEN, kept for callers that only want the
      topology's OPINION rather than an arm decision.
    """
    from jasper.active_speaker.runtime_contract import (
        active_ring_channels_for_topology,
        topology_supports_shm_ring,
    )
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        topology = load_output_topology_strict()
    except (OutputTopologyError, OSError, ValueError) as exc:
        if strict_unreadable:
            # An unreadable topology is NOT proven eligible — fail closed to
            # loopback rather than arm a ring we cannot prove is eligible.
            return False, (
                f"topology unreadable ({exc}); resolving loopback (fail-closed) "
                "rather than arm a ring it cannot prove is eligible"
            )
        return True, f"topology unreadable ({exc}); deferring to outputd's own guard"
    if topology_supports_shm_ring(topology):
        return True, "topology is ring-eligible (stereo/unconfigured single sink)"
    if active_ring_channels_for_topology(topology) is not None:
        # A composite sink additionally has to clear the WIDE-wire rule (P8b
        # item 1e). Asked BEFORE the endpoint proof because it is a property of
        # the box's own declaration rather than of what the reconciler has
        # staged, so its remedy ("declare the wide wire") is actionable whether
        # or not the endpoint is up — and reporting the staging defect first
        # would send an operator to fix the wrong thing twice.
        wire_ok, wire_detail = composite_ring_wire_ready(topology)
        if not wire_ok:
            return False, wire_detail
        proved, detail = active_ring_endpoint_proof()
        if proved:
            return True, f"topology is ACTIVE-ring eligible (roleful); {detail}"
        return False, (
            f"topology resolves an active-ring width, but the endpoint is not "
            f"staged: {detail}"
        )
    # Neither ring fits. Reaching HERE on a roleful box means it resolved no
    # ACTIVE-ring width either — an explicit mono, or a roleful topology whose
    # driven width is indeterminate; a roleful box that DOES resolve one was
    # answered by the active arm above, admitted or refused on its wide-wire
    # rule and endpoint proof. A composite reaches here only when it is
    # PASSIVE (not roleful, so no active ring) — a roleful composite resolves 4
    # since P8b item 1b. For these shapes loopback is the right coupling and
    # the household knows the setup.
    # A shipped-default plain stereo
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
        "saved output topology is not ring-eligible (the STEREO shm_ring is a "
        "full-range single-sink coupling; roleful/protected/subwoofer "
        "topologies need a per-driver crossover it cannot carry — those ride "
        "the ACTIVE ring instead, which this box did not qualify for either; a "
        "PASSIVE composite dual-DAC is neither, so it has no ring at all; and "
        "explicit-mono is excluded by policy, not a ring-v2 timing gap). "
        "Keeping the coupling on loopback. If this box is actually a plain stereo "
        "single-sink speaker carrying a stale roleful/subwoofer topology, run "
        "`jasper-output-topology-reset` to re-derive a clean passive topology from "
        "detected hardware, then re-arm."
    )


def ring_topology_ready_strict() -> tuple[bool, str]:
    """``ring_topology_ready`` fail-CLOSED on an unreadable topology.

    Used by BOTH arming paths — the unattended ``--auto`` default pass
    (defect-F4) and the explicit operator arm. See the ``strict_unreadable`` note
    on :func:`ring_topology_ready` for why the operator arm stopped failing open.
    """
    return ring_topology_ready(strict_unreadable=True)


def ring_not_roleful_ready() -> tuple[bool, str]:
    """The UNATTENDED pass's dedicated roleful exclusion — its own gate, on purpose.

    This asks nothing about ring eligibility. It asks whether the box is roleful,
    and refuses the unattended default if it is, FULL STOP.

    It is deliberately redundant with :func:`ring_topology_ready` today, and the
    redundancy is the point. That gate now has an arm that ADMITS a roleful
    topology (when the endpoint is staged), so the auto pass can no longer rely on
    "roleful boxes fail the topology gate" — and an unattended arm of a roleful
    box is the C-B2 hazard: a crossover speaker arming itself on boot or deploy
    with no operator present. Arming the active ring is an explicit-CLI decision;
    this gate is what keeps that true no matter how the eligibility predicates
    later evolve.

    Fail-CLOSED on an unreadable topology, matching every other unattended gate:
    a topology we cannot read is not proof the box is passive.
    """
    from jasper.active_speaker.runtime_contract import classify_output_contract
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        contract = classify_output_contract(load_output_topology_strict())
    except (OutputTopologyError, OSError, ValueError) as exc:
        return False, (
            f"topology unreadable ({exc}); the unattended default cannot prove "
            "this box is not roleful, so it resolves loopback (fail-closed)"
        )
    if contract.requires_roleful_graph:
        return False, (
            "roleful/protected/subwoofer topology — the ACTIVE ring is armed by "
            "an explicit `jasper-fanin-coupling-reconcile shm_ring` only, never "
            "by the unattended default pass. Keeping the coupling on loopback."
        )
    return True, "topology is not roleful"


def default_ring_gates() -> tuple[tuple[str, RingGate], ...]:
    """Return the unattended ring preflights in manual-arm order.

    This factory lives beside the reconciler-owned asset and topology probes it
    composes.  Keeping the pure decision module independent of this transition
    owner makes the dependency one-way while still sharing the exact predicates
    used by a manual arm.  The unattended path deliberately uses the strict
    topology probe so an unreadable topology fails closed.

    ORDER IS A DIAGNOSTIC DECISION, and it is why ``ring_edge_width`` no longer
    runs first.  Each gate is ordered ahead of the gates whose answers would be
    MEANINGLESS or MISLEADING without it: the box class before anything
    ring-specific; topology eligibility next, because a box that resolves no
    ring width makes the wire question ill-posed (``resolve_ring_wire`` falls
    back to the shipped stereo declaration there, so a wire mismatch would name
    the wrong defect on a roleful box); asset presence before the two gates that
    READ those assets; capability before width, because a plugin that cannot
    parse the wire's fields is a blunter refusal than any per-end disagreement.
    ``ring_edge_width`` was ordered first while it was a zero-I/O check over two
    constants; it now reads the conf.d and both env files, so "cheapest first"
    no longer describes it.

    ``ring_not_roleful`` sits immediately after the box class and BEFORE
    ``ring_topology``, because it is the coarser question and its refusal is the
    one an operator of a crossover box needs to read. It is independent of every
    eligibility predicate on purpose — see its docstring.
    """
    return (
        ("install_profile", ring_install_profile_ready),
        ("ring_not_roleful", ring_not_roleful_ready),
        ("ring_topology", ring_topology_ready_strict),
        ("ring_assets", ring_assets_ready),
        ("ring_wire_caps", ring_wire_caps_ready),
        ("ring_edge_width", ring_edge_width_ready),
    )


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
        # TODO: if shm_ring later permits operator chunk/target overrides, add
        # a production validation boundary here and keep its source-derived
        # CamillaDSP/ioplug contract aligned with the CI negotiation model.
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
    same chain (:func:`_effective_env_value`). Looking only at ``fanin.env`` can
    report the new default while an old ``JASPER_FANIN_RING_SLOTS=8`` in the
    earlier system env still controls the next daemon start.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR, resolve_ring_slots

    raw, source = _effective_env_value(
        fanin_text, RING_SLOTS_ENV_VAR, later_path=FANIN_ENV_PATH
    )
    if raw is None:
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

    IT DOES NOT CONVERGE A BOX SHEARED ON AN AXIS IT DOES NOT OWN. This writes
    ONE axis — the Ring-A slot count. If the box also disagrees about the WIRE
    (fan-in's declared format vs the conf.d's), converging the slots would make
    the geometry look repaired while the arm still cannot succeed, and the
    operator would read a ``stale_ring_slots_overridden`` line as progress. So
    the wire is read first and a shear there DECLINES the write, leaving
    ``ring_edge_width_ready`` to refuse with the reason that actually describes
    the box. Declining costs nothing: the slots value it would have written is
    still writable on the next pass, once the wire agrees.

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
    from jasper.ring_assets import (
        RING_A_CONF_PCM,
        ring_conf_channels,
        ring_conf_format,
    )
    from jasper.ring_assets import ring_conf_n_slots

    # Re-read fresh: the coupling flip was already written to this file above.
    current = _read_snapshot(fanin_snapshot.path)

    # The axes this function does NOT own, read before it writes the one it does.
    conf_format = ring_conf_format(RING_A_CONF_PCM)
    conf_channels = ring_conf_channels(RING_A_CONF_PCM)
    fanin_format, fanin_format_source = resolve_effective_fanin_wire_format(
        current.text
    )
    from jasper.fanin_coupling import RING_A_CHANNELS

    wire_shear = ""
    if conf_format is not None and conf_format != fanin_format:
        wire_shear = (
            f"fan-in declares wire format {fanin_format} (from "
            f"{fanin_format_source}) but conf.d pcm.{RING_A_CONF_PCM} declares "
            f"{conf_format}"
        )
    elif conf_channels is not None and conf_channels != RING_A_CHANNELS:
        wire_shear = (
            f"conf.d pcm.{RING_A_CONF_PCM} declares {conf_channels} channels but "
            f"fan-in's mixer is fixed at {RING_A_CHANNELS}"
        )
    if wire_shear:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="stale_ring_slots_override_declined",
            reason=reason,
            key=RING_SLOTS_ENV_VAR,
            detail=(
                f"{wire_shear} — converging the slot count alone would report a "
                "geometry this box does not have; the wire gate refuses the arm "
                "with the accurate reason"
            ),
            level=logging.WARNING,
        )
        return current

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

    Only deletes a file whose header is VALID (carries the ``JRIN`` magic) AND
    whose geometry differs from what fan-in / the conf.d will create, on ANY of
    the four attach-compared axes: ``n_slots``, ``period_frames`` (the ring slot
    IS one outputd period), ``sample_format`` and ``channels``. The comparison
    is :func:`jasper.ring_assets.ring_header_matches_conf`, shared with the
    CONFIRM-path self-heal predicate and the doctor so the three cannot mean
    different things by "coherent".

    THE FORMAT AXIS IS WHAT MAKES THE ROLLBACK LEVER ONE-SHOT. While this guard
    was blind to ``sample_format``, forcing the wire narrow again
    (``JASPER_FANIN_RING_WIRE_FORMAT=S16_LE``) left the WIDE ring file on disk,
    the writer rejected it at attach as a config-class fault, and the box parked
    until someone ran ``rm`` by hand — so the lever worked once and then needed
    an operator. Clearing a format-mismatched file here is what lets the lever be
    pulled and released.

    A magic-less / absent / correct-geometry file is left untouched (the writer
    reclaims a magic-less file itself; a correct file is reused). Best-effort: a
    delete failure is logged, never raised — the writer's own attach error is the
    backstop.

    ``fanin_text`` is the (post-migration) fanin.env text — used ONLY as the
    fallback expected Ring-A slot count when the conf.d is unreadable.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR, resolve_ring_slots
    from jasper.ring_assets import (
        RING_A_CONF_PCM,
        RING_A_PROGRAM_FILE,
        RING_ACTIVE_CONF_PCM,
        RING_ACTIVE_CONTENT_FILE,
        RING_B_CONF_PCM,
        RING_B_CONTENT_FILE,
        ring_conf_n_slots,
        ring_header_matches_conf,
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

    for path, pcm_name, expected_slots in (
        (RING_A_PROGRAM_FILE, RING_A_CONF_PCM, expected_a),
        (RING_B_CONTENT_FILE, RING_B_CONF_PCM, None),
        # The ACTIVE ring is judged like the other two, against ITS OWN conf.d
        # block — which is the axis that matters here, because the active block
        # is the one whose CHANNELS legitimately differ per box. A stale
        # active-content.ring from a prior commissioned width is exactly the
        # create-or-attach fault this guard exists to clear, and unlike Ring B it
        # can go stale without anything else changing (a re-commission that moves
        # a 2-way to a 3-way moves only this file's width).
        (RING_ACTIVE_CONTENT_FILE, RING_ACTIVE_CONF_PCM, None),
    ):
        verdict = ring_header_matches_conf(
            path, pcm_name, expected_n_slots=expected_slots
        )
        if not verdict.present or verdict.ok:
            continue  # nothing to judge, or coherent on every axis.
        try:
            os.unlink(path)
        except OSError as e:
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="stale_ring_unlink_failed",
                reason=reason,
                path=path,
                axis=verdict.axis,
                detail=verdict.detail,
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
            axis=verdict.axis,
            detail=verdict.detail,
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
        RING_ACTIVE_CONF_PCM,
        RING_ACTIVE_CONTENT_FILE,
        RING_B_CONF_PCM,
        RING_B_CONTENT_FILE,
        ring_conf_n_slots,
        ring_header_matches_conf,
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

    # Axis 2 — stale on-disk ring file. A valid header whose geometry differs
    # from the readable conf.d expectation on ANY attach-compared axis (n_slots,
    # period_frames, sample_format, channels) is what _delete_stale_ring_files
    # clears; its presence means the writer would hit a create-or-attach mismatch
    # on next start. Both call the SAME comparator, so the CONFIRM path escalates
    # on exactly the files the arm would then remove — a narrower predicate here
    # would leave a file the arm deletes looking coherent, and a wider one would
    # escalate to an arm that heals nothing.
    for path, pcm_name, expected_slots in (
        (RING_A_PROGRAM_FILE, RING_A_CONF_PCM, conf_a),
        (RING_B_CONTENT_FILE, RING_B_CONF_PCM, None),
        # Same three files _delete_stale_ring_files clears, in the same order,
        # against the same comparator: this predicate must escalate on EXACTLY
        # the files that self-heal would then remove. A narrower list here leaves
        # a file the arm deletes looking coherent; a wider one escalates to an
        # arm that heals nothing.
        (RING_ACTIVE_CONTENT_FILE, RING_ACTIVE_CONF_PCM, None),
    ):
        verdict = ring_header_matches_conf(
            path, pcm_name, expected_n_slots=expected_slots
        )
        if verdict.present and not verdict.ok:
            return True, f"{verdict.detail} — needs the arm stale-file self-heal"

    return False, "ring geometry coherent — CONFIRM stays lightweight"


def _fail_ring_arm(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    desired: str,
    reason: str,
    fanin_snapshot: _EnvSnapshot,
    outputd_snapshot: _EnvSnapshot,
    *,
    event_result: str,
    detail: str,
    restarted_fanin: bool = False,
    restarted_outputd: bool = False,
    reconverge_content_format: "DaemonOp | None" = None,
) -> CouplingResult:
    """Recover one failed ring-arm stage and publish its common outcome.

    Every ring preflight and ordered daemon step has the same fail-safe contract:
    force both env files and all three daemons back to loopback, emit one warning,
    and return a failed arm result.  Keeping that sequence here prevents a new
    stage from accidentally omitting recovery or reporting different progress
    flags while each caller still owns its domain-specific event name and detail.

    ``reconverge_content_format`` is passed by the stages that can leave
    ``JASPER_OUTPUTD_CONTENT_FORMAT`` naming the RING wire on a box being sent
    back to loopback, and it kicks that key's single writer once more. That is
    four callers, of two kinds: the three ordered spine steps (outputd, fan-in,
    CamillaDSP), which run after the converge confirmably LANDED; and the
    converge's own TIMEOUT branch, which passes it precisely because the
    converge did NOT confirmably land — a timed-out oneshot may still be
    running, holding the pre-rollback coupling it read on entry, and can write
    the ring wire after the rollback. The converge's REFUSAL branch is the one
    failure that does NOT pass it: nothing is in flight there, so the rollback's
    write is the last one. See :func:`kick_timed_out`. Recovery has just
    written ``loopback`` back into fanin.env, so the same oneshot re-derives the
    LOOPBACK lane's width from it — without this, a box recovered to loopback
    would keep asking for the ring's narrow S16_LE while CamillaDSP emits the
    wide program default, which the plug-wrapped passive lane silently
    requantizes and the RAW active lane refuses at the open (an outputd park).
    ``_recover_to_loopback`` deliberately does not kick for the content-BUFFER
    floor (a cushion, where being wrong is safe); a content-lane WIDTH is not a
    cushion, which is why this one is reconverged and that one is not.
    Best-effort: a failed re-converge is carried in ``detail`` and never
    downgrades the recovery, whose own env + daemon work already landed.
    """
    recovered = _recover_to_loopback(
        do_restart,
        do_restart_outputd,
        do_reconcile,
        fanin_snapshot.path,
        outputd_snapshot.path,
        reason,
    )
    if reconverge_content_format is not None:
        reconverge_ok, reconverge_detail = reconverge_content_format()
        if not reconverge_ok:
            detail = "; ".join(
                d
                for d in (
                    detail,
                    "and the content-format re-converge to loopback failed "
                    f"({reconverge_detail}) — jasper-outputd may still request the "
                    "ring wire until the next audio-hardware reconcile",
                )
                if d
            )
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result=event_result,
        desired=desired,
        reason=reason,
        detail=detail or None,
        recovered=recovered,
        level=logging.WARNING,
    )
    return CouplingResult(
        ok=False,
        desired=desired,
        changed=False,
        direction="arm",
        restarted_fanin=restarted_fanin,
        restarted_outputd=restarted_outputd,
        detail=detail,
        recovered=recovered,
    )


def _arm_ring(
    do_restart,
    do_restart_outputd,
    do_reconcile,
    desired,
    reason,
    fanin_snapshot,
    outputd_snapshot,
    do_converge_content_format: "DaemonOp",
) -> CouplingResult:
    """Arm the ``shm_ring`` coupling (Ring A + Ring B), fail-safe to loopback.

    PREFLIGHTs run in order, each fail-safe to loopback (no daemon bounced until
    all pass): (0) P1 ring assets present (``ring_assets_ready`` — a
    half-installed ring platform would strand the realtime path); (1) topology
    ring-eligible (``ring_topology_ready_strict`` — either the STEREO arm or the
    ACTIVE arm, and fail-CLOSED on an unreadable topology); (2) the installed ioplug can parse
    the wire (``ring_wire_caps_ready`` — a record compare against what the
    installer built, never an open-probe); (3) every declaring end states one
    wire (``ring_edge_width_ready``: fan-in's env, both stereo conf.d blocks,
    CamillaDSP's emitted stanzas, outputd's declarations, and the LOADED
    CamillaDSP graph's own ring lanes — a disagreement is a hard ioplug attach
    failure at arm); (4) conf.d period == outputd period
    (``ring_geometry_ready``); (5) Ring-A slot count == conf.d n_slots
    (``ring_slot_geometry_ready``, after ``_migrate_stale_fanin_ring_slots``
    self-heals a shear-prone stale ``JASPER_FANIN_RING_SLOTS`` — the 2026-07-05
    defect-A geometry hole); then (5) ``_delete_stale_ring_files`` clears a
    geometry-mismatched on-disk ring so the writer re-creates it fresh. Then the
    CONTENT-FORMAT CONVERGE (``do_converge_content_format``, below), and only
    then the
    ordered spine — outputd (the post-DSP ring's reader) first, fan-in (Ring A
    writer) second,
    CamillaDSP (loads the ring config, opening jts_ring_capture plus the post-DSP
    ring the marker selects — jts_ring_playback, or jts_ring_active_playback on an
    armed roleful box)
    last — matching the validated ring-proto arm order. Any failure rolls the whole
    box back to loopback + direct (``recovered=True``). The rings are forgiving
    (empty-ring reader/writer emit/drop silence), so there is no queue-drift
    activation window; the gates are wire-width + asset-presence + geometry
    coherence + the ordered restart landing, and the fan-in STATUS transport is
    confirmed by the doctor.

    THE CONTENT-FORMAT CONVERGE, and the first-arm reboot it closes.
    ``JASPER_OUTPUTD_CONTENT_FORMAT`` is a pure function of the coupling
    (``content_lane_format_for_coupling``) but its single writer is
    ``jasper-audio-hardware-reconcile``, not this module — so on a FIRST arm
    nothing had re-derived it by the time the spine restarts outputd, and outputd
    came up still asking for the LOOPBACK lane's wide ``S32_LE`` while CamillaDSP's
    ioplug attached the ring at the resolved ``S16_LE``. That is a hard
    ``attach_fatal``, so jasper-camilla crash-looped into its start limit and
    ``StartLimitAction=reboot`` rebooted the speaker; a SECOND arm then converged,
    because by then some ordinary hardware-reconcile pass had moved the key. jts4
    reproduced exactly that on 2026-08-14.

    The fix asks the OWNER to converge rather than writing the key here: this
    reconcile has already persisted ``JASPER_FANIN_CAMILLA_COUPLING=shm_ring``
    into fanin.env (the write happens before the preflights run), and the
    hardware reconciler reads that token file-fresh on every pass, so one blocking
    start of that oneshot re-derives the ring wire into outputd.env. Writing
    ``JASPER_OUTPUTD_CONTENT_FORMAT`` from here instead would make two writers of
    one key; reordering the spine would fix nothing, since no step of it re-derived
    the format at all. The kick lands AFTER every preflight so a refused arm still
    bounces nothing and moves no width, and FAIL-CLOSED: a kick that does not land
    refuses the arm rather than restarting outputd into the stale value the whole
    step exists to retire (with one asymmetry on the way out — see
    :func:`kick_timed_out`).

    OUTPUTD IS DOUBLE-BOUNCED ON A FIRST ARM, and that is a COST, not a
    correctness problem — the same trade the disarm sibling documents for its own
    kick. The converge changes a key, so the kicked pass takes
    ``restart_outputd_only`` (a single ``--no-block restart jasper-outputd``, no
    blocking ``systemctl stop jasper-voice``, no ``jasper-aec-reconcile`` kick),
    and this function's own blocking restart follows seconds later. Two starts
    rather than one, inherent to single-writer ownership: the only way to avoid
    them is to write the key here, which is the second writer this design exists
    to refuse. They cannot compound into ``StartLimitAction=reboot``, because
    every start this module issues is preceded by a best-effort ``reset-failed``
    on the crash-budget units (see :func:`_restart_unit`), so a deliberate
    config-apply starts from a clean budget. And the cost is paid only on a FIRST
    arm: on an already-converged box the kicked pass changes no key, so it
    restarts nothing at all.

    ``do_converge_content_format`` is that kick, injected rather than resolved
    here so tests drive it; production passes a blocking start of
    ``jasper-audio-hardware-reconcile`` bound by
    :data:`_ARM_CONTENT_FORMAT_CONVERGE_TIMEOUT_SEC`. It is REQUIRED, not
    defaulted — a default here would silently reach the real broker from a test
    that forgot it, which is the one caller shape that must fail loudly.
    """
    assets_ok, assets_detail = ring_assets_ready()
    if not assets_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_assets_missing",
            detail=assets_detail,
        )

    # Topology-eligibility preflight. Two admitting arms (see
    # ``ring_topology_ready``): a full-range stereo single-sink box takes the
    # STEREO ring; a roleful box takes the ACTIVE ring, and only once its
    # endpoint is staged. A composite/mono box fits neither and is refused UP
    # FRONT with a crisp reason, rather than failing outputd's Rust rejection
    # later as a confusing rollback.
    #
    # STRICT on an unreadable topology, and this is a deliberate change of
    # direction. The operator arm used to fail OPEN here, justified as
    # "backstopped by outputd's own guard" — and that backstop was proven to fail
    # open on the SAME error: a topology read failure clears the active-lane
    # marker, outputd's stereo predicate then admits the ring, and on a 2-way box
    # no width check can tell that apart. The allowlist restores the backstop,
    # but the arm stays fail-closed regardless: an operator is present to fix an
    # unreadable topology, so refusing costs a rerun where admitting costs a park.
    topo_ok, topo_detail = ring_topology_ready_strict()
    if not topo_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_topology_ineligible",
            detail=topo_detail,
        )

    # ioplug CAPABILITY preflight (the degraded-deploy walk): the ring's
    # resolved wire may render a conf.d `format`/`channels` key the INSTALLED
    # ioplug cannot parse — the plugin build degrades to a WARN, so a failed
    # rebuild leaves the previous .so beside new daemons. That plugin refuses the
    # device at open() with -EINVAL and CamillaDSP cannot start against the ring.
    # A RECORD compare (installer-written provenance vs the .so on disk), never
    # an open-probe: probing a ring PCM from the reconciler would disturb a live
    # arm. No-op on the shipped wire, which renders no such key.
    caps_ok, caps_detail = ring_wire_caps_ready()
    if not caps_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_ioplug_caps_missing",
            detail=caps_detail,
        )

    # EVERY-END wire preflight: fan-in's env, both stereo conf.d blocks,
    # CamillaDSP's emitted stanzas, outputd's declarations AND the loaded
    # CamillaDSP graph must state ONE wire. Runs after the topology gate because
    # a box that resolves no ring width makes the comparison ill-posed, and after
    # the asset gate because it reads the conf.d. The graph is left to the gate
    # to read: this function holds the two env snapshots it wrote, and nothing on
    # the arm path has read the statefile, so passing one here would mean opening
    # it a second time rather than sharing a snapshot.
    width_ok, width_detail = ring_edge_width_ready(
        fanin_text=fanin_snapshot.text, outputd_text=outputd_snapshot.text
    )
    if not width_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_edge_width_mismatch",
            detail=width_detail,
        )

    # Period-geometry preflight: the conf.d ring period MUST equal outputd's
    # resolved DAC period (the ring slot IS one outputd period). A mismatch is a
    # hard ioplug open() error, so CamillaDSP's ring load would fail and this arm
    # would roll back with a confusing daemon-level error. Refuse UP FRONT with a
    # crisp reason (fail-safe: recover to loopback), before bouncing any daemon.
    geom_ok, geom_detail = ring_geometry_ready(outputd_snapshot.text)
    if not geom_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_geometry_mismatch",
            detail=geom_detail,
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
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_slot_mismatch",
            detail=slot_detail,
        )

    # Stale-ring-file guard (defect A): a ring file left over from a PRIOR geometry
    # is a create-or-ATTACH open() error for the writer (the header geometry won't
    # match the requested one). Delete any geometry-mismatched on-disk ring before
    # bouncing the daemons so the writer re-creates it fresh. tmpfs transport state,
    # not user data. Best-effort — the writer's own attach error is the backstop.
    _delete_stale_ring_files(reason, fanin_snapshot.text)

    # CONTENT-FORMAT CONVERGE — see the docstring. Kick the single writer of
    # JASPER_OUTPUTD_CONTENT_FORMAT so it re-derives that key from the coupling
    # already persisted above, BEFORE the spine restarts outputd against it.
    # Blocking, so this returns with the key actually re-emitted rather than
    # merely requested.
    kick_ok, kick_detail = do_converge_content_format()
    if not kick_ok:
        # TIMED OUT vs REFUSED, and the asymmetry is about a RACE, not severity.
        # Both refuse the arm. Only a timeout leaves the oneshot possibly still
        # RUNNING: it read `shm_ring` before the rollback and can write the ring
        # wire after it, so recovery's loopback write would not be the last one.
        # Re-running it settles that — systemd serialises starts of one unit, so
        # the second run begins after the in-flight one ends and reads the
        # loopback coupling recovery has by then persisted. A REFUSAL has nothing
        # in flight, so a second kick would be pointless work that fails the same
        # way. See :func:`kick_timed_out`.
        timed_out = kick_timed_out(kick_detail)
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="arm_content_format_converge_failed",
            desired=desired,
            reason=reason,
            detail=kick_detail or None,
            timed_out=timed_out,
            level=logging.WARNING,
        )
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_content_format_converge_failed",
            detail=(
                "could not converge JASPER_OUTPUTD_CONTENT_FORMAT to the ring "
                f"wire ({kick_detail}); arming would restart jasper-outputd on "
                "the loopback lane's width and fail CamillaDSP's ring attach — "
                "run `sudo systemctl start jasper-audio-hardware-reconcile`, "
                "then re-arm"
            ),
            reconverge_content_format=(
                do_converge_content_format if timed_out else None
            ),
        )
    log_event(
        logger,
        "fanin.coupling_reconcile",
        result="arm_content_format_converged",
        desired=desired,
        reason=reason,
        detail=kick_detail or None,
    )

    out_ok, out_detail = do_restart_outputd()
    if not out_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_outputd_failed",
            detail=out_detail,
            reconverge_content_format=do_converge_content_format,
        )

    fan_ok, fan_detail = do_restart()
    if not fan_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_fanin_failed",
            detail=fan_detail,
            restarted_outputd=True,
            reconverge_content_format=do_converge_content_format,
        )

    cam_ok, cam_detail = do_reconcile(COUPLING_SHM_RING)
    if not cam_ok:
        return _fail_ring_arm(
            do_restart,
            do_restart_outputd,
            do_reconcile,
            desired,
            reason,
            fanin_snapshot,
            outputd_snapshot,
            event_result="arm_ring_camilla_failed",
            detail=cam_detail,
            restarted_fanin=True,
            restarted_outputd=True,
            reconverge_content_format=do_converge_content_format,
        )

    # A completed arm is a SUCCESS for the strike record's purpose: CamillaDSP
    # just loaded the ring config. Leaving stale strikes here would silently
    # degrade the two-strike policy to one — an operator's fresh re-arm would be
    # one transient confirm away from being recovered to loopback, with the
    # escalation log citing a failure from before the arm that fixed it.
    _clear_ring_confirm_failures()
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


def _run_loopback_daemon_ops(
    do_restart,
    do_restart_outputd,
    do_reconcile,
) -> _LoopbackDaemonOps:
    """Run the single ordered daemon sequence that converges to loopback."""
    cam_ok, cam_detail = do_reconcile(COUPLING_LOOPBACK)
    fan_ok, fan_detail = do_restart()
    out_ok, out_detail = do_restart_outputd()
    detail = "; ".join(
        d
        for d in (
            cam_detail if not cam_ok else "",
            fan_detail if not fan_ok else "",
            out_detail if not out_ok else "",
        )
        if d
    )
    return _LoopbackDaemonOps(cam_ok, fan_ok, out_ok, detail)


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
    daemon_ops = _run_loopback_daemon_ops(
        do_restart,
        do_restart_outputd,
        do_reconcile,
    )
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
    ok = daemon_ops.ok
    if ok:
        # A completed disarm ends the incident the strikes were evidence of: the
        # box is off the ring, and the next arm starts a fresh one. A PARTIAL
        # disarm keeps the record — some of the box may still be on the ring, so
        # the accumulated evidence is still about the live state.
        _clear_ring_confirm_failures()
    detail = "; ".join(
        d
        for d in (
            daemon_ops.detail,
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
        restarted_fanin=daemon_ops.fanin_ok,
        restarted_outputd=daemon_ops.outputd_ok,
        reconciled_camilla=daemon_ops.camilla_ok,
        detail=detail,
    )


# Repeated CONFIRM failure on an ARMED ring escalates to recovery. The limit
# mirrors fan-in's own two-strike ProbeFail precedent
# (``host_compliance.rs``'s ``PROBE_FAIL_STRIKE_LIMIT``): one failure can be a
# transient (CamillaDSP momentarily busy, a lost websocket), two consecutive
# ones on a box whose audio is already down is evidence, not noise.
#
# The reconciler is EVENT-DRIVEN (boot, deploy, a /sources/ toggle, an operator
# start) — there is no timer — so strikes accumulate across events rather than
# on a schedule, and the window only DISCARDS evidence too old to be about the
# same incident.
#
# STRIKES ARE CONSECUTIVE, so EVERY successful transition clears the record, not
# just a successful confirm: a completed ``_arm_ring`` and a completed
# ``_disarm`` clear it too. Without those two the two-strike policy silently
# degraded to one — an operator whose fresh re-arm succeeded was left holding a
# pre-arm strike, and the next single transient recovered the box to loopback
# citing a failure the arm had already fixed.
#
# INSTALL DOES NOT CLEAR IT, deliberately. The record survives a deploy inside
# its window, and that is the honest behaviour: this reconciler is its single
# owner, and an unconditional deploy-time wipe would discard real evidence on
# exactly the box that needs the escalation — one whose confirm fails, gets
# redeployed WITHOUT the cause being fixed, and fails again. Those are two
# consecutive failures of one incident; a deploy in between proves nothing about
# the ring. The 24 h window is what bounds stale evidence.
#
# WHAT DOES clear it is a successful TRANSITION, and a deploy only reaches one on
# an AUTO-OWNED box. Install drives the `--auto` pass, which delegates to
# ``reconcile_coupling`` only when the pass OWNS the box; on an OPERATOR-FROZEN
# box (``JASPER_FANIN_COUPLING_CHOICE=operator``) it preserves the choice and
# synthesises a confirm result WITHOUT running one, so no deploy clears the
# record there. That is not a corner case: arming the ACTIVE ring is
# explicit-CLI-only and stamps that very marker, so every active-ring box is
# operator-frozen by construction. On such a box the record is cleared by the
# operator's own next `jasper-fanin-coupling-reconcile <coupling>` — or it ages
# out of the 24 h window.
RING_CONFIRM_STRIKE_STATE = "/var/lib/jasper/ring-confirm-strikes.json"
RING_CONFIRM_STRIKE_LIMIT = 2
RING_CONFIRM_STRIKE_WINDOW_SEC = 24 * 3600


def _read_ring_confirm_strikes(path: str | None = None) -> int:
    """Strikes recorded within the window; 0 for absent / stale / unreadable.

    ``path=None`` resolves :data:`RING_CONFIRM_STRIKE_STATE` at CALL time, not as
    a bound default — the same rule ``ring_conf_n_slots`` follows, and for the
    same reason: a default bound at import captures the constant forever, so a
    caller (or a test) that repoints the module attribute is silently ignored
    and the write lands on the real path.
    """
    import json

    path = RING_CONFIRM_STRIKE_STATE if path is None else path
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        count = int(data["count"])
        first_ts = float(data["first_ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if time.time() - first_ts > RING_CONFIRM_STRIKE_WINDOW_SEC:
        return 0  # evidence too old to be about this incident
    return max(0, count)


def _record_ring_confirm_failure(
    detail: str, reason: str, path: str | None = None
) -> int:
    """Add one strike and return the new in-window count. Never raises.

    ``path=None`` resolves the module constant at call time — see
    :func:`_read_ring_confirm_strikes`.
    """
    import json

    path = RING_CONFIRM_STRIKE_STATE if path is None else path
    prior = _read_ring_confirm_strikes(path)
    count = prior + 1
    first_ts = time.time()
    if prior:
        try:
            with open(path, encoding="utf-8") as fh:
                first_ts = float(json.load(fh)["first_ts"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        atomic_write_text(
            Path(path),
            json.dumps(
                {
                    "count": count,
                    "first_ts": first_ts,
                    "last_ts": time.time(),
                    "last_detail": detail,
                    "last_reason": reason,
                }
            )
            + "\n",
            mode=0o644,
        )
    except OSError as e:
        # A strike we cannot persist is a strike we cannot accumulate; say so
        # rather than let the escalation silently never fire.
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="ring_confirm_strike_write_failed",
            reason=reason,
            path=path,
            error=e,
            level=logging.WARNING,
        )
    return count


def _clear_ring_confirm_failures(path: str | None = None) -> None:
    """Drop the strike record (a successful confirm). Never raises.

    ``path=None`` resolves the module constant at call time — see
    :func:`_read_ring_confirm_strikes`.
    """
    try:
        os.unlink(RING_CONFIRM_STRIKE_STATE if path is None else path)
    except OSError:
        pass


def _reseed_loopback_statefile(reason: str) -> tuple[bool, str]:
    """Re-point CamillaDSP's statefile at a loopback-safe graph, WITHOUT a socket.

    THE CONVERGENCE HOLE THIS CLOSES. Recovery's camilla step goes through
    ``reconcile_current_dsp``, which talks to the running daemon over its
    websocket. When the reason we are recovering is that CamillaDSP cannot START
    — a ring config it cannot open, the degraded-deploy walk — there is no
    websocket to talk to: the reconcile fails, the statefile still names the ring
    config, and the daemon's NEXT start comes up on the same ring and fails
    again. The env said loopback; the graph never moved. So when the live path is
    unavailable we write the statefile directly, through the same decision the
    installer's seeder uses (``safe_graph_for_current_topology`` +
    ``apply_safe_graph_decision_to_statefile``), with the coupling pinned to
    loopback so the decision selects the loopback flat graph rather than
    re-reading the persisted token.

    Write-on-change and topology-aware by construction — it is the same seeder,
    not a second one, so a roleful box gets its roleful/parked graph and not a
    flat stereo one. Best-effort: a failure here is logged and reported, never
    raised, because it runs inside a recovery that must not itself explode.
    """
    try:
        from jasper.active_speaker.runtime_contract import (
            apply_safe_graph_decision_to_statefile,
            safe_graph_for_current_topology,
        )

        topology = load_topology_for_wire()
        decision = safe_graph_for_current_topology(
            topology, coupling=COUPLING_LOOPBACK
        )
        if not decision.ok:
            return False, (
                f"safe-graph decision unusable ({decision.status}: "
                f"{decision.reason})"
            )
        wrote = apply_safe_graph_decision_to_statefile(decision, topology=topology)
    except (OSError, ValueError, TypeError, AttributeError, ImportError) as e:
        # Concrete set, not a blind except: an unreadable/corrupt topology
        # (OutputTopologyError subclasses ValueError), a statefile that cannot be
        # written, or a missing import are the ways this fails. It still must not
        # raise — it runs INSIDE a recovery — but swallowing everything would
        # hide a genuine programming error behind a recovery that reported
        # "re-seed failed" and moved on.
        return False, f"statefile re-seed failed: {e}"
    if wrote:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="recovery_statefile_reseeded",
            reason=reason,
            config_path=decision.selected_config_path,
            decision=decision.status,
        )
        return True, f"statefile re-seeded to {decision.selected_config_path}"
    return True, (
        f"statefile already names {decision.selected_config_path} — no re-seed "
        "needed"
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
    never kicks ``jasper-audio-hardware-reconcile`` itself — including on the one
    route here that can be leaving a LIVE shm_ring bridge (the CONFIRM path's
    ring self-heal escalating to :func:`_arm_ring`, which then fails its own
    preflight). Intentional: a box already mid-failure-recovery gets the
    larger fail-safe cushion and less daemon churn instead of another
    oneshot; the content-buffer floor re-emit just waits for the next
    udev/boot/deploy event on this path, same as before #1251.

    ON THE ARM PATH THAT IS NOW USUALLY MOOT, because a caller kicks it just
    after this returns. :func:`_fail_ring_arm`'s ``reconverge_content_format``
    starts the SAME oneshot to settle the content-lane WIDTH, and that pass
    re-emits the content-BUFFER floor in the same run — so on those four
    branches the wait above does not happen at all. It still describes this
    function's own behaviour, and it still holds on the routes that call this
    directly (the two CONFIRM-path recoveries) and whenever that best-effort
    re-converge does not land.

    When the camilla step FAILS, the CamillaDSP statefile is re-seeded directly
    (:func:`_reseed_loopback_statefile`) so recovery converges even with no
    daemon to talk to — the case where recovery matters most.
    """
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
    daemon_ops = _run_loopback_daemon_ops(
        do_restart,
        do_restart_outputd,
        do_reconcile,
    )
    if not daemon_ops.camilla_ok:
        # The live re-point did not land — most likely because CamillaDSP is not
        # running to be talked to, which is the shape of the failure we are
        # recovering FROM. Write the statefile directly so the daemon's next
        # start comes up on loopback instead of the ring config it cannot open.
        # Without this the env says loopback while the graph still says ring, and
        # the box never converges on its own.
        reseeded, reseed_detail = _reseed_loopback_statefile(reason)
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result=(
                "recovery_statefile_reseed"
                if reseeded
                else "recovery_statefile_reseed_failed"
            ),
            reason=reason,
            detail=reseed_detail,
            level=logging.WARNING if not reseeded else logging.INFO,
        )
    if not daemon_ops.ok:
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="recovery_daemon_ops_failed",
            reason=reason,
            detail=daemon_ops.detail or None,
            level=logging.WARNING,
        )
    return daemon_ops.ok


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


def _outputd_ring_path_for(outputd_text: str) -> str:
    """The ring file outputd must read, derived from the endpoint marker.

    ONE writer for the path half of outputd's ring-path/marker biconditional.
    Armed -> the ACTIVE ring's file; unarmed -> the operator's custom Ring B
    path if they set one, else the canonical Ring B default.

    The marker is read from the outputd.env TEXT the caller is reconciling
    rather than from the file on disk, so the path written and the marker
    written are read from one snapshot; a second file read could straddle a
    concurrent hardware reconcile and emit a crossed pair.

    Note the asymmetry, which is deliberate: an operator's custom path is
    honoured on the STEREO ring and ignored on the ACTIVE one. There is exactly
    one legal active-ring file — outputd's allowlist compares against that named
    constant — so "preserving" a custom value there would only ever produce the
    crossed pair the allowlist refuses.
    """
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
        OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR,
        ring_active_endpoint_armed,
    )

    armed = ring_active_endpoint_armed(
        {
            OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR: read_value(
                outputd_text, OUTPUTD_RING_ACTIVE_ENDPOINT_ENV_VAR
            )
            or ""
        }
    )
    if armed:
        return DEFAULT_OUTPUTD_ACTIVE_RING_PATH
    return resolve_outputd_ring_path(read_value(outputd_text, OUTPUTD_RING_PATH_ENV_VAR))


def _outputd_actions(coupling: str, outputd_text: str) -> tuple[RuntimeEnvAction, ...]:
    """The COMPLETE set of reconciler-owned outputd.env actions for a coupling.

    outputd's content source is coupling-specific and MUTUALLY EXCLUSIVE across
    couplings, so this writes exactly one content-source key set and unsets the
    others — the two ends must never split (a stale outputd key while fan-in flips
    strands one transport):

    - ``shm_ring``: set ``JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`` + the post-DSP
      ring's path/slots — content.ring, or active-content.ring on an armed
      roleful box (see the convergence note below). The two rings flip together —
      fan-in's Ring A capture (fanin.env) and outputd's post-DSP ring bridge
      (here) are ONE coupling.
    - ``loopback``: clear the ring keys — outputd reads the snd-aloop content
      lane.

    Every branch also UNSETS the legacy ``JASPER_OUTPUTD_LOCAL_CONTENT_PIPE`` key
    (the removed transport_pipe coupling's outputd content source) — a one-way
    migration sweep so a box that once armed transport_pipe converges clean on its
    next reconcile (nothing writes the key anymore).

    **The ring PATH converges from the endpoint MARKER, it is not preserved.**
    outputd enforces a biconditional between the two — the active ring file may
    be read only by an armed active endpoint, and an armed active endpoint may
    read only that file — so path and marker are ONE pairing with two writers,
    and this is the writer of the path half. Deriving it here means the pair is
    coherent by construction: a preserve-else-stereo default would write the
    full-range Ring B path onto an armed box and the arm would ADMIT and then
    park at exit 78, with the only workaround being a hand-edited half of a
    safety pair. The marker is read from ``outputd_text`` — the same file this
    call is reconciling, already carrying the hardware reconciler's answer,
    because the arm ladder runs that reconciler before this step.
    """
    if coupling == COUPLING_SHM_RING:
        return (
            RuntimeEnvAction(
                "set", OUTPUTD_CONTENT_BRIDGE_ENV_VAR, OUTPUTD_CONTENT_BRIDGE_SHM_RING
            ),
            RuntimeEnvAction(
                "set",
                OUTPUTD_RING_PATH_ENV_VAR,
                _outputd_ring_path_for(outputd_text),
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

    The ring PATH is taken from :func:`_outputd_ring_path_for`, the same single
    derivation the persisted write uses, so the in-process env can never carry a
    different ring than the file just written.
    """
    os.environ[COUPLING_ENV_VAR] = coupling
    if coupling == COUPLING_SHM_RING:
        os.environ[OUTPUTD_CONTENT_BRIDGE_ENV_VAR] = OUTPUTD_CONTENT_BRIDGE_SHM_RING
        os.environ[OUTPUTD_RING_PATH_ENV_VAR] = _outputd_ring_path_for(outputd_text)
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
            "won't override it): loopback (snd-aloop); shm_ring (Ring A plus the "
            "post-DSP SHM ring — Ring B, or the ACTIVE ring on an armed roleful "
            "box; arms both fan-in and outputd). Mutually exclusive with --auto."
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
