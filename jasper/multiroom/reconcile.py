# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom grouping reconciler — the root oneshot.

Single writer of the snapcast unit state. ``main()`` is the ordered systemd
ExecStart entrypoint: reads the wizard-owned GroupingConfig
(``jasper.multiroom.config``), applies the pure plan from
``jasper.multiroom.reconcile_plan``, and drives the real systemctl/CamillaDSP
convergence. An enabled-but-INVALID config runs neither unit (never bring up
a broken bond).

After its role/data-plane work lands it hands the role to the canonical source
coordinator. Grouping never starts or stops source resources itself.

jasper-grouping-reconcile.service is Type=oneshot — there is no resident
process here.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import atomic_io
from .. import tts_routing as _tts_routing
from ..camilla import CamillaUnavailable
from ..control import restart_broker
from ..dsp_apply import DspApplyError
from ..env_load import (
    AIRPLAY_BONDED_EXTRA_DELAY_ENV,
    AIRPLAY_GROUPING_ENV_FILE,
    OUTPUTD_GROUPING_ENV_FILE,
    VOICE_GROUPING_ENV_FILE,
)
from ..fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
from ..log_event import log_event
from ..ring_assets import RING_ACTIVE_CONTENT_FILE, ring_writer_lock_path
from ..service_units import (
    AEC_RECONCILE_SERVICE, CAMILLA_SERVICE, OUTPUTD_SERVICE,
    SHAIRPORT_SYNC_SERVICE, run_systemctl,
)
from ..source_intent_units import (
    RECONCILE_SYSTEMD_TIMEOUT_SECONDS as SOURCE_RECONCILE_SYSTEMD_TIMEOUT_SECONDS,
)
from ..source_intent_units import RECONCILE_UNIT as SOURCE_INTENT_RECONCILE_UNIT
from ..systemd_probe import state_is_live, unit_query, unit_state
from . import config
from .config import SNAP_STREAM_ID, GroupingConfig
from .dac_content_ring import (
    DAC_CONTENT_LANE_ENV,
    DAC_CONTENT_RING_PERIOD_FRAMES,
    OUTPUTD_DAC_CONTENT_CHANNEL_ENV,
)
from .effective_role import (
    FOLLOWER_STATUS_FILE,
    grouping_request_fingerprint,
    normalise_boot_id,
    read_current_boot_id,
    read_effective_role_status,
)
from .grouping_env import (
    LANE_REFUSED_PERIOD,
    LaneDecision,
    airplay_grouping_env,
    box_outputd_period_frames,
    member_lane_decision,
    output_topology_state,
    outputd_grouping_env,
    voice_grouping_env,
)
from .reconcile_plan import (
    ARGS_DIR as ARGS_DIR,  # re-exported: tests patch reconcile_mod.ARGS_DIR
    ARGS_FILE,
    SNAPFIFO as SNAPFIFO,  # re-exported: jasper.active_speaker.runtime_contract imports it from here
    SNAPSERVER_UNIT,
    ReconcilePlan,
    UnitIntent,
    assemble_args,
    plan,
)
from .tts_route import VOICE_PARK_ENV
from ..logging_setup import configure_logging

logger = logging.getLogger(__name__)

VOICE_TTS_SOCKET_ENV = _tts_routing.VOICE_TTS_SOCKET_ENV


# The AirPlay receiver. A FOLLOWER parks it; a LEADER keeps it running and gets
# its backend latency offset re-derived on bond/unbond (a bonded leader folds in
# the Snapcast round-trip buffer — see airplay_grouping_env).
SHAIRPORT_UNIT = SHAIRPORT_SYNC_SERVICE
# Short manager requests (probes, reset-failed) return promptly. Blocking
# starts/restarts may wait for a normal service job, but must remain finite when
# this module is run directly during install or repair, outside the grouping
# oneshot's outer systemd timeout.
_SYSTEMCTL_CONTROL_TIMEOUT_SEC = 5.0
_SYSTEMCTL_BLOCKING_TIMEOUT_SEC = 60.0
# A role handoff never RESTARTS the source owner: it may be between ordered USB
# fan-in/gadget steps. A blocking ``systemctl start`` against a running
# activation is only a barrier (it joins and waits), bounded just beyond the
# target unit's own TimeoutStartSec.
_SOURCE_RECONCILE_START_TIMEOUT_SEC = SOURCE_RECONCILE_SYSTEMD_TIMEOUT_SECONDS + 5.0
_MAX_SOURCE_RECONCILE_STARTS = 2  # drain prior pass, then run fresh role pass


# Conservative *sequential* ceilings, not typical latency: a steady-state pass
# normally performs no blocking work.
_MAX_PLAN_UNIT_INTENTS = 2  # snapserver + snapclient; sources have one owner
_SNAPCAST_PROVISION_BUDGET_SEC = 420.0  # apt update 120 + install 300
_MAX_POST_PLAN_BLOCKING_ACTIONS = 6
# _plan_changes_units probes each plan intent's unit (one ActiveState read) BEFORE
# _apply runs it.
_UNIT_CHANGE_PROBE_CALLS = _MAX_PLAN_UNIT_INTENTS
# The plan-intent and post-plan slots are costed at the broker ceiling even
# though _apply's own start/stop calls never reach the broker: the model is a
# generic "N blocking actions" tally, not a per-call enumeration, so every
# slot in it is priced at the worst any one of them can legally cost.
_BASE_RECONCILE_BUDGET_SEC = (
    _MAX_PLAN_UNIT_INTENTS * restart_broker.operation_ceiling_sec(
        _SYSTEMCTL_BLOCKING_TIMEOUT_SEC, reset_failed=True
    )
    + _SNAPCAST_PROVISION_BUDGET_SEC
    + _MAX_POST_PLAN_BLOCKING_ACTIONS
    * restart_broker.operation_ceiling_sec(
        _SYSTEMCTL_BLOCKING_TIMEOUT_SEC, reset_failed=True
    )
    + _UNIT_CHANGE_PROBE_CALLS * _SYSTEMCTL_CONTROL_TIMEOUT_SEC
)
_OWNER_CONTROL_CALLS_PER_HANDOFF = 2  # reset-failed + ActiveState probe
_RECONCILE_TIMEOUT_MARGIN_SEC = 30.0
_RECONCILE_SYSTEMD_TIMEOUT_SEC = (
    _BASE_RECONCILE_BUDGET_SEC
    + _MAX_SOURCE_RECONCILE_STARTS * _SOURCE_RECONCILE_START_TIMEOUT_SEC
    + _OWNER_CONTROL_CALLS_PER_HANDOFF * _SYSTEMCTL_CONTROL_TIMEOUT_SEC
    + _RECONCILE_TIMEOUT_MARGIN_SEC
)

# ---------- the leader's music producer ----------
#
# The leader's CamillaDSP feeds the snapserver pipe (post-correction,
# post-master_gain — the stream inherits the volume + safety ceiling), applied by
# this reconciler via jasper.multiroom.leader_config. Producer liveness for
# runtime health reads the ACTIVE CamillaDSP config (camilla's own statefile
# names it, and the doctor's `leader pipe` check scans it), never a Python mirror
# of env intent.

# ---------- the member round-trip content lane ----------
#
# The dumb member's round-trip rides the dac-content SHM ring: snapclient writes
# DAC_CONTENT_RING_PCM through its `alsa` player and outputd reads that ring as
# its sole content source. The transport's identity — PCM name, ring file, wire,
# slot geometry — is owned by jasper.multiroom.dac_content_ring. Still never
# snd-aloop (snapclient's snd_pcm_delay would lie, inv-2) and never the raw DAC,
# which outputd owns.

OUTPUTD_UNIT = OUTPUTD_SERVICE
CAMILLA_UNIT = CAMILLA_SERVICE

# jasper-aec-reconcile is the SINGLE owner of jasper-voice + jasper-aec-bridge
# unit state. Role changes therefore KICK it rather than touching those units
# here: it reads the derived park flag below and restarts-or-parks voice per
# role + provider + mic, one writer total.
AEC_RECONCILE_UNIT = AEC_RECONCILE_SERVICE
AUDIO_HARDWARE_RECONCILE = "/usr/local/sbin/jasper-audio-hardware-reconcile"

# camilla#2 — the endpoint-crossover CamillaDSP instance (:1235), armed ONLY on
# an ACTIVE LEADER. Reconciler-gated: `enable --now` on bond (after the statefile
# is re-seeded with the re-proven driver-domain graph) and `disable --now` on
# unbond. It carries NO StartLimitAction=reboot, so a failed arm fails closed to
# silence through the crossover — never reboots the household speaker (unlike the
# always-on camilla#1).
CROSSOVER_UNIT = "jasper-camilla-crossover.service"

# The exclusive active-content PCM camilla#1 owns in solo-active mode and
# camilla#2 owns after the active-leader handoff. It is the ACTIVE ring
# (`jts_ring_active_playback` -> `/dev/shm/jts-ring/active-content.ring`), and
# the release signal is the ring's own writer lock: the C ioplug's writer holds
# an exclusive `flock` on `<ring>.writer.lock` for the life of its mapping, so a
# NON-BLOCKING exclusive `flock` that SUCCEEDS proves no writer owns the ring.
# The kernel drops an `flock` on process exit INCLUDING SIGKILL, so there is no
# frozen-state window, and it is the SAME primitive camilla#2 contends on when it
# attaches.
#
# ORDERING CONSEQUENCE: a box whose ring platform never armed has no
# `active-content.ring.writer.lock` at all, so this probe answers `unknown` and
# the arm fails closed to solo-active with the lock path in the log line. Arming
# without proof is the EBUSY reboot loop this exists to prevent.
ACTIVE_CONTENT_WRITER_LOCK_PATH = ring_writer_lock_path(RING_ACTIVE_CONTENT_FILE)
ACTIVE_CONTENT_RELEASE_TIMEOUT_SEC = 0.8
ACTIVE_CONTENT_RELEASE_POLL_SEC = 0.05


@dataclass(frozen=True)
class _PcmHandleProbeResult:
    """One bounded active-content PCM release probe result."""

    state: str  # "released" | "busy" | "unknown"
    reason: str
    detail: str = ""
    lock_path: str = ACTIVE_CONTENT_WRITER_LOCK_PATH
    attempts: int = 0
    timeout_sec: float = 0.0

    @property
    def released(self) -> bool:
        return self.state == "released"

    @property
    def busy(self) -> bool:
        return self.state == "busy"

    @property
    def unknown(self) -> bool:
        return self.state == "unknown"


@dataclass(frozen=True)
class RoleDecision:
    """The pre-apply role/permission decision for one reconcile pass.

    ``cfg`` starts as the request and is never mutated in place; a refused
    bond calls :meth:`with_fallback` to get a *new* decision with every role
    flag reset and ``cfg`` forced to ``replace(cfg, enabled=False)`` — the
    fail-safe-to-solo shape. ``requested_cfg`` never changes; the fallback reads
    its park reason from it, not from the now-disabled ``cfg``.
    """

    cfg: GroupingConfig
    requested_cfg: GroupingConfig
    plan: ReconcilePlan
    active: bool
    active_leader: bool
    passive_leader: bool
    active_speaker_leader: bool
    active_follower: bool
    active_endpoint: bool
    box_is_active: bool
    flat_output_allowed: bool
    outputd_period_frames: int | None
    lane: LaneDecision
    refused_follower_fallback: bool
    transitioning_from_parked_role: bool

    @property
    def local_sources_allowed(self) -> bool:
        """The one shared local-sources permission predicate."""
        return (
            not config.local_sources_parked(self.cfg)
            and not self.refused_follower_fallback
            and not self.transitioning_from_parked_role
        )

    def with_fallback(self) -> "RoleDecision":
        """Reset every derived bond role after a fail-safe refusal.

        ``lane`` is deliberately left as decided pre-fallback: its only reader
        runs before any fallback, and the env writer derives its own from ``cfg``.
        """
        cfg = replace(self.cfg, enabled=False)
        return replace(
            self,
            cfg=cfg,
            plan=plan(cfg),
            active=False,
            active_leader=False,
            active_follower=False,
            active_speaker_leader=False,
            passive_leader=False,
            active_endpoint=False,
            refused_follower_fallback=config.local_sources_parked(
                self.requested_cfg,
            ),
        )


def decide_role(
    requested_cfg: GroupingConfig,
    *,
    active_box_state: bool | None,
    flat_output_allowed: bool,
    outputd_period_frames: int | None,
    prior_status: dict[str, Any],
) -> RoleDecision:
    """Derive the initial role decision from one wizard-owned request. PURE.

    ``transitioning_from_parked_role`` is frozen here from the status file's
    LAST-published fact: it must never be re-derived after this reconcile
    writes its own status, or a retried transition would forget it is one.
    """
    cfg = requested_cfg
    transitioning_from_parked_role = (
        not config.local_sources_parked(requested_cfg)
        and prior_status.get("local_sources_allowed") is False
    )
    active = cfg.enabled and cfg.error is None
    active_leader = active and cfg.role == "leader"
    box_is_active = active_box_state is True
    active_follower = active and cfg.role == "follower" and box_is_active
    active_speaker_leader = active_leader and box_is_active
    passive_leader = active_leader and not box_is_active
    active_endpoint = active_follower or active_speaker_leader
    lane_decision = member_lane_decision(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
        outputd_period_frames=outputd_period_frames,
    )
    return RoleDecision(
        cfg=cfg,
        requested_cfg=requested_cfg,
        plan=plan(cfg),
        active=active,
        active_leader=active_leader,
        passive_leader=passive_leader,
        active_speaker_leader=active_speaker_leader,
        active_follower=active_follower,
        active_endpoint=active_endpoint,
        box_is_active=box_is_active,
        flat_output_allowed=flat_output_allowed,
        outputd_period_frames=outputd_period_frames,
        lane=lane_decision,
        refused_follower_fallback=False,
        transitioning_from_parked_role=transitioning_from_parked_role,
    )


# ============================================================
# I/O entrypoint. Everything above is pure; everything below does real
# systemctl calls. Keep that boundary crisp.
# ============================================================


def _systemctl_unit_state(query: str, unit: str) -> bool | None:
    """Tri-state truth for one ``systemctl is-*`` query.

    A missing systemctl binary returns ``None`` silently; other spawn failures
    return ``None`` with one warning. Completed commands are classified by their
    explicit state TEXT, not return code alone, so a manager/D-Bus error cannot
    masquerade as disabled or inactive. Classification itself lives in
    jasper.systemd_probe (shared with jasper.source_intent); this wrapper
    keeps only the observability this caller wants on an unresolved probe.
    """
    result = unit_state(query, unit, timeout=_SYSTEMCTL_CONTROL_TIMEOUT_SEC)
    verdict = unit_query(result)
    if verdict is not None:
        return verdict
    if isinstance(result.error, FileNotFoundError):
        return None
    if result.error is not None:
        log_event(
            logger,
            "multiroom.reconcile.unit_state_probe_failed",
            unit=unit,
            query=query,
            error=result.error,
            level=logging.WARNING,
        )
        return None
    log_event(
        logger,
        "multiroom.reconcile.unit_state_probe_failed",
        unit=unit,
        query=query,
        rc=result.rc,
        state=result.word or "(none)",
        stderr=result.stderr,
        level=logging.WARNING,
    )
    return None


def _unit_is_active(unit: str) -> bool:
    """``systemctl is-active`` truth. Only explicit ``active`` is true.

    Inactive, failed, absent, transitional, or unknown reads as not-active — the
    safe direction for the active-leader bake gate: a bake against a reader-less
    or missing snapserver pipe must NOT proceed, because it cannot release the
    DAC and arming camilla#2 would then fight camilla#1 for it."""
    return _systemctl_unit_state("is-active", unit) is True


def _probe_active_content_pcm_once(
    *,
    lock_path: str | None = None,
) -> _PcmHandleProbeResult:
    """Try to take the ACTIVE ring's writer lock once, non-blocking.

    The C ioplug's writer holds ``flock(LOCK_EX)`` on ``<ring>.writer.lock`` for
    the life of its mapping (``acquire_writer_lock``,
    ``c/jts-ring-ioplug/jts_ring_shm.c``), so:

      - the lock is FREE      -> ``released`` — no writer owns the ACTIVE ring.
      - ``EWOULDBLOCK``       -> ``busy``     — a live writer still owns it.
      - anything else         -> ``unknown``  — the caller fails closed.

    Three properties this probe must keep:

    1. **Never ``O_CREAT``.** A wrong-mode creation by an out-of-unit first
       creator locks the renderer out permanently (the sticky directory bit stops
       it deleting the file), which is why the ioplug ``fchmod``-heals the mode.
       An absent lock file means no writer has ever attached this ring ->
       ``unknown``.
    2. **Release immediately.** The probe is a BARRIER, not a lock handoff: it
       drops the lock before returning so it can never be the thing camilla#2
       contends with.
    3. **The TOCTOU is accepted.** camilla#1 could reattach between a successful
       probe and camilla#2's own attach; the authority is camilla#2's attach,
       which takes this same lock and gets ``-EBUSY`` if it lost the race.
       Fail-closed either way, so no handoff protocol is needed.

    ``O_RDONLY`` is deliberate: ``flock`` needs no write access, and the smaller
    request is the one more likely to succeed against a lock file created by a
    peer under a different uid.

    ``lock_path=None`` resolves :data:`ACTIVE_CONTENT_WRITER_LOCK_PATH` at CALL
    time, never as a bound default (the rule :mod:`jasper.ring_assets` states on
    ``ring_ioplug_so_path``): a def-time binding would make a caller that
    repoints the module constant silently probe the original path while every log
    line still names the constant.
    """
    lock_path = ACTIVE_CONTENT_WRITER_LOCK_PATH if lock_path is None else lock_path
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError as e:
        return _PcmHandleProbeResult(
            "unknown",
            "writer_lock_absent",
            detail=str(e),
            lock_path=lock_path,
        )
    except PermissionError as e:
        return _PcmHandleProbeResult(
            "unknown",
            "writer_lock_unopenable",
            detail=str(e),
            lock_path=lock_path,
        )
    except OSError as e:
        return _PcmHandleProbeResult(
            "unknown",
            "writer_lock_open_error",
            detail=str(e),
            lock_path=lock_path,
        )

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            # EWOULDBLOCK/EAGAIN — a live writer holds the ring. The ONE busy
            # answer; every other failure is unknown, because "could not ask"
            # must not be reported as "someone is holding it".
            return _PcmHandleProbeResult(
                "busy",
                "writer_lock_held",
                detail=str(e),
                lock_path=lock_path,
            )
        except OSError as e:
            return _PcmHandleProbeResult(
                "unknown",
                "writer_lock_probe_error",
                detail=str(e),
                lock_path=lock_path,
            )
        # Barrier, not handoff: drop it before the caller acts on the answer.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return _PcmHandleProbeResult(
            "released",
            "writer_lock_free",
            lock_path=lock_path,
        )
    finally:
        os.close(fd)


def _wait_for_active_content_pcm_release(
    *,
    timeout_sec: float = ACTIVE_CONTENT_RELEASE_TIMEOUT_SEC,
    interval_sec: float = ACTIVE_CONTENT_RELEASE_POLL_SEC,
    lock_path: str | None = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> _PcmHandleProbeResult:
    """Poll until camilla#1 has positively released the active-content PCM.

    Returns `busy` while a live writer still holds the ring's writer lock (and on
    timeout), and `unknown` when the lock cannot be asked at all — absent,
    unopenable, or an unexpected errno. The caller arms camilla#2 ONLY on a
    positive `released`; both `busy` and `unknown` fail closed to solo-active.

    ``lock_path=None`` resolves the module constant at CALL time, for the same
    reason the single-shot probe does.
    """
    lock_path = ACTIVE_CONTENT_WRITER_LOCK_PATH if lock_path is None else lock_path
    deadline = monotonic() + max(timeout_sec, 0.0)
    attempts = 0
    last = _PcmHandleProbeResult(
        "busy",
        "not_probed",
        lock_path=lock_path,
        timeout_sec=timeout_sec,
    )
    while True:
        attempts += 1
        last = _probe_active_content_pcm_once(lock_path=lock_path)
        if not last.busy:
            return replace(last, attempts=attempts, timeout_sec=timeout_sec)
        now = monotonic()
        if now >= deadline:
            detail = last.detail
            detail = f"{last.reason}: {detail}" if detail else last.reason
            return _PcmHandleProbeResult(
                "busy",
                "timeout",
                detail=detail,
                lock_path=lock_path,
                attempts=attempts,
                timeout_sec=timeout_sec,
            )
        sleep(min(interval_sec, max(deadline - now, 0.0)))


def _unit_absent_stderr(stderr: str) -> bool:
    """True when a systemctl failure means THE UNIT DOES NOT EXIST.

    A streambox box never installs some full-speaker units (e.g. the
    voice/AEC stack), so stop/park intents against absent units must be
    clean no-ops."""
    lowered = (stderr or "").lower()
    return "not loaded" in lowered or "not found" in lowered


def _unit_active(unit: str) -> bool | None:
    """Return whether `unit`'s live ``ActiveState`` counts as active: running,
    starting, reloading or stopping.

    ``None`` on a probe failure or an unrecognized state; callers treat that
    as unproven and take the safe branch.
    """
    result = unit_state("is-active", unit, timeout=_SYSTEMCTL_CONTROL_TIMEOUT_SEC)
    word = result.word or ""
    # A unit still stopping is not settled, so it counts as active here.
    if state_is_live(word, activating_is_live=True) or word == "deactivating":
        return True
    return unit_query(result)


def _plan_changes_units(intents: tuple[UnitIntent, ...]) -> bool:
    """Whether applying `intents` would flip any unit's live ``ActiveState``.

    Probed BEFORE the plan runs, so an already-active unit getting `start` (or
    an already-inactive unit getting `stop`) does not count as a change. A
    probe failure counts as a change — the caller uses this to decide whether
    the post-role source barrier can be skipped, and an unproven state must
    not license skipping it.
    """
    for it in intents:
        state = _unit_active(it.unit)
        if state is None or state != (it.desired == "start"):
            return True
    return False


def _apply(plan_: ReconcilePlan) -> int:
    """Apply a plan via systemctl. Returns a process exit code.

    A failure on one intent is logged and surfaced in the exit code but does not
    abort the rest of the plan — a half-applied bond is worse than a best-effort
    one. Units that do not exist on this install tier are clean no-ops.
    """
    rc = 0
    for it in plan_.intents:
        verb = it.desired
        try:
            run_systemctl(
                [verb, it.unit], timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
            ).check_returncode()
            log_event(
                logger,
                "multiroom.reconcile.unit",
                unit=it.unit,
                desired=it.desired,
                reason=it.reason,
            )
        except FileNotFoundError:
            log_event(
                logger,
                "multiroom.reconcile.unit_failed",
                unit=it.unit,
                desired=it.desired,
                error="systemctl_not_found",
                level=logging.ERROR,
            )
            rc = 1
        except subprocess.CalledProcessError as e:
            if _unit_absent_stderr(e.stderr):
                log_event(
                    logger,
                    "multiroom.reconcile.unit",
                    unit=it.unit,
                    desired=it.desired,
                    result="skipped_unit_absent",
                    reason=it.reason,
                )
                continue
            log_event(
                logger,
                "multiroom.reconcile.unit_failed",
                unit=it.unit,
                desired=it.desired,
                rc=e.returncode,
                stderr=(e.stderr or "").strip(),
                level=logging.ERROR,
            )
            rc = 1
        except (OSError, subprocess.SubprocessError) as e:
            log_event(
                logger,
                "multiroom.reconcile.unit_failed",
                unit=it.unit,
                desired=it.desired,
                error=e,
                stderr=(getattr(e, "stderr", "") or "").strip(),
                level=logging.ERROR,
            )
            rc = 1
    return rc


def _write_derived_env(
    keys: dict[str, str],
    *,
    path: str = OUTPUTD_GROUPING_ENV_FILE,
    consumer: str,
) -> tuple[bool, bool]:
    """Write a reconciler-owned derived environment file iff it changed.

    Returns ``(changed, ok)``. Compare-before-write keeps the common no-change
    reconcile from restarting its consumer; the caller refreshes the consumer
    only on ``changed and ok``, because ``EnvironmentFile=`` is read at unit
    start and a content change without a restart would silently not apply.
    Fail-soft; carries no secrets (mode 0644)."""
    body = "".join(f"{k}={v}\n" for k, v in keys.items())
    try:
        old = Path(path).read_text()
    except OSError:
        old = None
    if old == body:
        return (False, True)
    if old is None and body == "":
        # Nothing existed and nothing needs clearing: a fresh solo speaker's
        # first reconcile must not count as a change, which would spuriously
        # restart the consuming unit (~15 s for jasper-voice) on first boot.
        return (False, True)
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            f"multiroom.reconcile.{consumer}_env_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )
        return (True, False)
    return (True, True)


def _reset_failed_unit(unit: str) -> None:
    """Reset failed state before a DELIBERATE reconciler start or restart.

    The reconciler's restarts are control-plane CONFIG-APPLIES, not crash
    recovery. A rapid burst of /grouping/set updates legitimately re-derives the
    lane env many times in seconds, and each apply spends a slot of the target
    unit's StartLimitBurst; once that burst is exhausted inside
    StartLimitIntervalSec, systemd escalates to StartLimitAction=reboot (outputd
    / voice) or Camilla's recovery budget, turning deliberate churn into recovery
    escalation. reset-failed clears any prior failed / start-limit parking so a
    config-apply restart never consumes the crash-recovery budget. Genuine crash
    loops still escalate: a daemon's own Restart= path does NOT call this.

    Fail-soft and BEST-EFFORT: a reset-failed failure must never block the
    start/restart it precedes."""
    try:
        run_systemctl(
            ["reset-failed", unit], timeout=_SYSTEMCTL_CONTROL_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "multiroom.reconcile.reset_failed_error",
            unit=unit,
            error=e,
            level=logging.WARNING,
        )


def _restart_unit(
    unit: str,
    *,
    no_block: bool = False,
    active_only: bool = False,
) -> bool:
    """Restart a unit so it re-reads its grouping env. Fail-soft (the caller
    reflects a failure in the exit code; the doctor's drift checks surface a lane
    left unwired).

    Routes through :func:`restart_broker.reset_then_manage`, which runs
    reset-failed FIRST so a config-apply restart does not inherit the target's
    accumulated crash-reboot budget, and enforces the broker's unit/verb
    allowlist. Never raises: an unreachable broker falls back to a direct
    ``systemctl`` here because this reconciler runs as root (see the module
    docstring on :mod:`jasper.control.restart_broker`).

    `no_block` is for cross-owner kicks whose target owns its own downstream
    startup graph (grouping -> AEC -> voice). Ordered, same-owner restarts stay
    blocking so the reconciler still fails loudly when an apply step it owns does
    not land.
    """
    verb = "try-restart" if active_only else "restart"
    resp = restart_broker.reset_then_manage(
        unit,
        verb=verb,
        reason="grouping_env_changed",
        no_block=no_block,
        timeout=(
            _SYSTEMCTL_CONTROL_TIMEOUT_SEC
            if no_block
            else _SYSTEMCTL_BLOCKING_TIMEOUT_SEC
        ),
    )
    if not resp.get("ok"):
        log_event(
            logger,
            "multiroom.reconcile.unit_restart_failed",
            unit=unit,
            error=str(resp.get("error") or f"rc={resp.get('rc')}"),
            stderr=(resp.get("stderr") or "").strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.unit_restarted",
        unit=unit,
        reason="grouping_env_changed",
        no_block=no_block,
        active_only=active_only,
    )
    return True


def _source_reconciler_activation_busy() -> bool | None:
    """Return whether the source owner has an activation that can absorb a start.

    Reads the ``is-active`` state word via :func:`_unit_active`, never its exit
    code, which is non-zero for an ``activating`` oneshot and a stopped one
    alike. Unknown / probe failure returns ``None``; the caller handles it in
    the safe direction.
    """

    state = _unit_active(SOURCE_INTENT_RECONCILE_UNIT)
    if state is None:
        log_event(
            logger,
            "multiroom.reconcile.owner_state_probe_failed",
            unit=SOURCE_INTENT_RECONCILE_UNIT,
            level=logging.WARNING,
        )
    return state


def _converge_sources_after_role(*, grouping_active: bool, units_changed: bool) -> bool:
    """Run a fresh source pass after grouping's role plan and await it.

    A bare no-block start can join an activation that read the PREVIOUS role. So:
    probe after the role apply; if an activation is busy (or its state cannot be
    trusted), synchronously join it as a bounded barrier, then start a new pass.
    If it is inactive, any activation racing the final start began after the
    probe and therefore already sees the new role. ``start`` throughout — never
    ``restart`` — so an ordered source transition is not interrupted. The final
    call is blocking: grouping reports success only after source park/restore
    reaches its terminal result.

    ``source-intent-reconcile.service`` in turn ``Wants=``/``After=``
    ``audio-hardware-reconcile.service`` (a ~30 s pass on a Pi Zero 2 W), so this
    barrier is skipped when grouping is off/solo AND the role plan touched no
    unit — nothing changed for source-intent to react to.
    """
    if not grouping_active and not units_changed:
        log_event(
            logger,
            "multiroom.sources_barrier_skipped",
            reason="no_role_change",
        )
        return True

    unit = SOURCE_INTENT_RECONCILE_UNIT
    _reset_failed_unit(unit)
    busy = _source_reconciler_activation_busy()
    if busy is not False:
        try:
            run_systemctl(
                ["start", unit], timeout=_SOURCE_RECONCILE_START_TIMEOUT_SEC,
            ).check_returncode()
        except (OSError, subprocess.SubprocessError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            log_event(
                logger,
                "multiroom.reconcile.owner_barrier_failed",
                unit=unit,
                error=exc,
                stderr=stderr.strip(),
                level=logging.ERROR,
            )
            return False
        log_event(
            logger,
            "multiroom.reconcile.owner_prior_activation_drained",
            unit=unit,
            state_was_unknown=busy is None,
        )

    try:
        run_systemctl(
            ["start", unit], timeout=_SOURCE_RECONCILE_START_TIMEOUT_SEC,
        ).check_returncode()
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log_event(
            logger,
            "multiroom.reconcile.source_converge_failed",
            unit=unit,
            error=exc,
            stderr=stderr.strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.source_converged",
        unit=unit,
        reason="grouping_role_applied",
        waited_for_prior_activation=busy is not False,
    )
    return True


def _ensure_unit_active(unit: str, *, reason: str) -> bool:
    """Start a required unit after clearing a stale start-limit state.

    Active-leader self-healing can intentionally stop camilla#2 to release the
    active-content lane. If camilla#1 previously hit StartLimit while camilla#2
    held that lane, a plain ``systemctl start`` remains parked until
    ``reset-failed`` runs.
    """
    if _unit_is_active(unit):
        return True
    _reset_failed_unit(unit)
    try:
        run_systemctl(
            ["start", unit], timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        ).check_returncode()
    except FileNotFoundError:
        log_event(
            logger,
            "multiroom.reconcile.unit_start_failed",
            unit=unit,
            reason=reason,
            error="systemctl_not_found",
            level=logging.ERROR,
        )
        return False
    except subprocess.CalledProcessError as e:
        log_event(
            logger,
            "multiroom.reconcile.unit_start_failed",
            unit=unit,
            reason=reason,
            rc=e.returncode,
            stderr=(e.stderr or "").strip(),
            level=logging.ERROR,
        )
        return False
    except (OSError, subprocess.SubprocessError) as e:
        log_event(
            logger,
            "multiroom.reconcile.unit_start_failed",
            unit=unit,
            reason=reason,
            error=e,
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.unit_started",
        unit=unit,
        reason=reason,
    )
    return True


def _run_audio_hardware_reconcile(*, reason: str) -> bool:
    """Run the audio-hardware reconciler after an active-leader graph change.

    That reconciler is the single writer of /var/lib/jasper/outputd.env. Outputd
    must switch from the passive stereo lane to the active-content lane BEFORE
    camilla#2 is armed, or camilla#2 can fight an existing opener for the
    exclusive active-content playback PCM.
    """
    try:
        subprocess.run(
            [AUDIO_HARDWARE_RECONCILE, "--reason", reason],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        stderr = getattr(e, "stderr", "") or ""
        log_event(
            logger,
            "multiroom.reconcile.audio_hardware_failed",
            reason=reason,
            error=e,
            stderr=stderr.strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.audio_hardware",
        reason=reason,
        result="reconciled",
    )
    return True


def _systemctl_crossover_unit(*verb: str, action: str) -> bool:
    """Run ``systemctl <verb...>`` against camilla#2 for the active-leader
    arm/teardown. Fail-soft (the doctor's active-leader crossover-unit check
    surfaces a unit left un-armed). camilla#2 carries NO
    StartLimitAction=reboot, so a failed arm fails closed to silence."""
    try:
        run_systemctl(
            [*verb, CROSSOVER_UNIT], timeout=_SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        ).check_returncode()
    except (OSError, subprocess.SubprocessError) as e:
        stderr = getattr(e, "stderr", "") or ""
        log_event(
            logger,
            "multiroom.reconcile.crossover_unit_failed",
            unit=CROSSOVER_UNIT,
            action=action,
            error=e,
            stderr=stderr.strip(),
            level=logging.ERROR,
        )
        return False
    log_event(
        logger,
        "multiroom.reconcile.crossover_unit",
        unit=CROSSOVER_UNIT,
        action=action,
    )
    return True


def _arm_crossover_unit() -> bool:
    """``systemctl enable --now`` camilla#2 for an active leader. Idempotent.

    The crossover statefile MUST be re-seeded with the re-proven driver-domain
    graph BEFORE this (the caller orders it) so a cold start never loads a flat
    statefile — full-range to a tweeter."""
    return _systemctl_crossover_unit("enable", "--now", action="armed")


def _disable_crossover_unit() -> bool:
    """``systemctl disable --now`` camilla#2 on unbond. Idempotent (disabling a
    not-armed unit is a no-op)."""
    return _systemctl_crossover_unit("disable", "--now", action="disabled")


def _write_follower_status(
    *,
    active_follower: bool,
    blocked_reason: str,
    active_leader: bool = False,
    requested_cfg: GroupingConfig | None = None,
    local_sources_allowed: bool | None = None,
    path: str = FOLLOWER_STATUS_FILE,
    boot_id_reader: Callable[[], str] | None = None,
) -> bool:
    """Publish the effective-role authorization fact and UI status.

    Rewritten every reconcile so the surface is fresh truth; read by
    jasper.multiroom.state rather than os.environ, because jasper-control is not
    restarted on a bond. I/O failures are contained and returned as ``False``:
    safety-sensitive callers must abort before granting local sources, while
    status-only refresh paths may continue with the previous fail-safe fact.

    ``active_follower`` = this box runs its local Layer-A crossover on the bonded
    stream as a FOLLOWER; ``active_leader`` = it runs that crossover (camilla#2)
    as the bond LEADER and also bakes the wire on camilla#1; ``blocked_reason``
    (non-empty) = an active-endpoint transition was REFUSED and either fell back
    to solo active or preserved the existing graph because ownership could not be
    changed safely (invariant 5 fail-closed).

    Every payload carries the current Linux boot ID. An attempted grant without a
    valid boot ID is rewritten as a deny and returns ``False``: a persistent
    grant must never survive into a later boot as fresh truth.
    """
    read_boot_id = boot_id_reader or read_current_boot_id
    try:
        boot_id = normalise_boot_id(read_boot_id())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        boot_id = ""
        log_event(
            logger,
            "multiroom.reconcile.boot_id_failed",
            error=exc,
            level=logging.ERROR,
        )
    grant_fresh = not (local_sources_allowed is True and not boot_id)
    if not grant_fresh:
        # A persistent grant without a current boot identity could be trusted
        # after reboot: publish an explicit deny and fail the caller's grant so
        # grouping retries rather than reporting a source-unpark transition that
        # never became authoritative.
        local_sources_allowed = False
        log_event(
            logger,
            "multiroom.reconcile.source_grant_blocked",
            reason="boot_id_unavailable",
            level=logging.ERROR,
        )
    payload: dict[str, object] = {
        "active_follower": active_follower,
        "active_leader": active_leader,
        "blocked_reason": blocked_reason,
        "boot_id": boot_id,
    }
    if requested_cfg is not None:
        payload["requested_fingerprint"] = grouping_request_fingerprint(
            requested_cfg,
        )
    if local_sources_allowed is not None:
        payload["local_sources_allowed"] = local_sources_allowed
    body = json.dumps(payload, sort_keys=True) + "\n"
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            "multiroom.reconcile.follower_status_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )
        return False
    return grant_fresh


def _restart_outputd() -> bool:
    return _restart_unit(OUTPUTD_UNIT)


def _write_args_file(keys: dict[str, str], *, path: str = ARGS_FILE) -> bool:
    """Atomically write the derived snapcast args to ``path``. Fail-soft.

    One ``KEY=value`` line per key, order preserved. Returns True on success,
    False on any failure; NEVER raises — a lost args write must not crash the
    reconcile path. Carries no secrets, so mode 0644 (matches grouping.env).
    """
    body = "".join(f"{k}={v}\n" for k, v in keys.items())
    try:
        atomic_io.atomic_write_text(path, body, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            "multiroom.reconcile.args_failed",
            path=path,
            error=e,
            level=logging.WARNING,
        )
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """systemd ExecStart entrypoint for jasper-grouping-reconcile.service.

    Loads the wizard-owned config fresh, computes the pure plan, persists the
    derived env files, and applies the plan via systemctl. Returns a process exit
    code.

    `--reason` is a free-text trigger source echoed into the structured log for
    correlation. Unknown args are ignored so a future caller adding a flag cannot
    crash the reconcile path.

    ORDER (load-bearing):

      1. Derived files (snapcast args + outputd lane env) + the member FIFO —
         before any unit work, so everything a started unit reads is fresh
         (``EnvironmentFile=`` is read at unit start).
      2. CamillaDSP solo RESTORE when this speaker is not an active leader —
         BEFORE units stop, so the pipe's writer leaves before its reader.
      3. outputd restart, only when the lane env CHANGED.
      4. The unit plan (stops before starts).
      5. CamillaDSP bonded APPLY when this speaker is an active leader — LAST,
         after snapserver started, so the pipe's reader exists before
         CamillaDSP's File sink opens it for write (a FIFO write-open blocks
         until a reader exists).

    Camilla apply/restore failures are caught and logged
    (event=multiroom.reconcile.camilla_failed): the reconcile still manages
    units, the doctor's `leader pipe` / runtime-health surfaces carry the
    unapplied state, and the exit code flips so the oneshot unit shows failed.
    """
    parser = argparse.ArgumentParser(prog="jasper.multiroom.reconcile")
    parser.add_argument("--reason", default="manual")
    args, _unknown = parser.parse_known_args(argv)

    configure_logging()
    # Step 5 below swaps the live CamillaDSP graph, so its swap duck needs a
    # canonical target to release to.
    from jasper.volume_process import install_env_canonical_target_provider  # lazy: import cost — only main()'s CLI oneshot needs this; callers that import this module for its pure plan()/probe functions never reach main()

    install_env_canonical_target_provider()

    requested_cfg = config.load_config()
    prior_role_status = read_effective_role_status(FOLLOWER_STATUS_FILE)
    # An ACTIVE (multi-driver) follower relocates Layer A onto its own CamillaDSP
    # in the bonded path; a DUMB (single-DAC) follower uses outputd's dac_content
    # ChannelPick. The saved topology decides which path this reconcile takes.
    active_box_state, flat_output_allowed = output_topology_state()
    outputd_period_frames = box_outputd_period_frames()
    role = decide_role(
        requested_cfg,
        active_box_state=active_box_state,
        flat_output_allowed=flat_output_allowed,
        outputd_period_frames=outputd_period_frames,
        prior_status=prior_role_status,
    )
    log_event(
        logger,
        "multiroom.reconcile.start",
        reason=args.reason,
        enabled=role.cfg.enabled,
        role=role.cfg.role or "(none)",
        error=role.cfg.error or "(none)",
        active_box=("unknown" if active_box_state is None else role.box_is_active),
        active_follower=role.active_follower,
        active_leader=role.active_speaker_leader,
        summary=repr(role.plan.summary),
    )
    rc = 0
    endpoint_block_reason = ""
    active_leader_arm_blocked = False

    if active_box_state is None:
        endpoint_block_reason = "active_speaker_topology_unknown"
        log_event(
            logger,
            "multiroom.reconcile.active_restore_blocked",
            reason=endpoint_block_reason,
            action="preserve_runtime_graph",
            level=logging.ERROR,
        )
        _write_follower_status(
            active_follower=False,
            active_leader=False,
            blocked_reason=endpoint_block_reason,
            requested_cfg=requested_cfg,
            local_sources_allowed=role.local_sources_allowed,
            path=FOLLOWER_STATUS_FILE,
        )
        cleared, env_ok = _write_derived_env(
            outputd_grouping_env(role.cfg, flat_output_allowed=False),
            path=OUTPUTD_GROUPING_ENV_FILE,
            consumer="outputd",
        )
        if cleared and env_ok:
            _restart_outputd()
        # The PAIR, from the same two facts: an unreadable topology denies the
        # flat DAC output, which unarms outputd's TTS socket, and voice must not
        # be left aimed at a socket nobody serves.
        voice_cleared, voice_ok = _write_derived_env(
            voice_grouping_env(role.cfg, flat_output_allowed=False),
            path=VOICE_GROUPING_ENV_FILE,
            consumer="voice",
        )
        if voice_cleared and voice_ok:
            _restart_unit(AEC_RECONCILE_UNIT, no_block=True)
        return 1

    # A member whose outputd period cannot carry the return ring has no
    # round-trip transport, and arming one anyway makes outputd bail EX_CONFIG
    # under `RestartPreventExitStatus=78` — a parked daemon and a SILENT
    # speaker. Fail-SAFE to solo: the box keeps playing its own content, the
    # request stays in the wizard config, and a DAC change lets the next
    # reconcile bond. Placed BEFORE snapcast provision / any bond wiring. Only
    # this reason refuses the bond — the other two unarmed shapes (an ACTIVE
    # endpoint, a topology that forbids a flat graph) are legitimate members.
    if role.active and role.lane.reason == LANE_REFUSED_PERIOD:
        endpoint_block_reason = LANE_REFUSED_PERIOD
        log_event(
            logger,
            "multiroom.reconcile.dac_content_ring_period_mismatch",
            reason=args.reason,
            outputd_period_frames=(
                "(unresolved)" if role.outputd_period_frames is None
                else role.outputd_period_frames
            ),
            ring_period_frames=DAC_CONTENT_RING_PERIOD_FRAMES,
            detail=(
                "this box's outputd period is not the dac-content return ring's "
                "slot, so outputd would refuse the pair at startup and park. "
                "Staying solo."
            ),
            level=logging.WARNING,
        )
        role = role.with_fallback()
        rc = 1

    # Grouping prerequisite: install.sh ships the snapcast units but never the
    # binaries — that is the grouping opt-in's job (jasper.multiroom.provision).
    # Runs BEFORE the active-endpoint gate so the active-leader precheck's
    # snapcast check sees a fresh install. TOTAL + fail-soft: a failed install is
    # surfaced via /grouping's `provision` field + the doctor and flips rc, but
    # never raises — the snap units simply fail to start, the box stays
    # solo-safe, and the next reconcile retries.
    if role.active:
        from .provision import ensure_snapcast_installed  # lazy: import cost — only the active role branch of main() needs this

        prov = ensure_snapcast_installed()
        if prov["state"] == "failed":
            log_event(
                logger,
                "multiroom.reconcile.snapcast_provision_failed",
                detail=prov["detail"] or "(none)",
                level=logging.ERROR,
            )
            rc = 1
        elif prov["state"] == "installed":
            log_event(
                logger,
                "multiroom.reconcile.snapcast_provisioned",
                result="installed",
            )

    # Active-ENDPOINT readiness GATE (fail-safe to SOLO). Build + re-prove the
    # driver-domain graph BEFORE tearing down the solo path — for a follower its
    # one CamillaDSP, for an active leader BOTH camilla#2's driver-domain graph
    # AND camilla#1's program bake. If it cannot be made safe (bad channel, not
    # commissioned, graph fails re-proof), do NOT bond: fall back to solo active
    # so the box keeps playing its own content instead of half-parking silent.
    # This is invariant 5's "refuses to bond" — the unsafe graph never reaches
    # the DACs. The actual CamillaDSP applies happen later, after snapcast is up.
    if role.active_endpoint:
        try:
            if role.active_speaker_leader:
                from .active_leader_config import precheck_active_leader_sync  # lazy: import cost — only this role branch of main() needs it

                precheck_active_leader_sync(role.cfg)
            else:
                from .follower_config import precheck_active_follower_sync  # lazy: import cost — only this role branch of main() needs it

                precheck_active_follower_sync(role.cfg)
        except RuntimeError as e:
            endpoint_block_reason = getattr(
                e,
                "reason",
                "active_endpoint_precheck_error",
            )
            # Distinct event per role; both literals stay greppable.
            blocked_event = (
                "multiroom.reconcile.active_leader_blocked"
                if role.active_speaker_leader
                else "multiroom.reconcile.active_follower_blocked"
            )
            log_event(
                logger,
                blocked_event,
                reason=endpoint_block_reason,
                error=e,
                level=logging.ERROR,
            )
            # Fail-safe to solo for the rest of this reconcile: treat exactly
            # like an invalid bond. Reset EVERY role flag — including
            # active_leader, which gates the step-6 stream-binding pin — so a
            # refused bond never partially behaves like a leader/endpoint.
            role = role.with_fallback()
            rc = 1

    # A solo-active box needs positive ownership proof BEFORE any role-derived
    # file or unit mutation. Enabled intent alone is insufficient: a partial
    # `disable --now` can leave camilla#2 active. If either probe is unknown,
    # even applying the ordinary solo unit plan could remove SNAPFIFO's reader
    # and indirectly restart camilla#1 onto a DAC camilla#2 may still own.
    prior_crossover_owned = False

    def block_active_restore(reason: str) -> int:
        log_event(
            logger,
            "multiroom.reconcile.active_restore_blocked",
            reason=reason,
            unit=CROSSOVER_UNIT,
            action="preserve_runtime_graph",
            level=logging.ERROR,
        )
        _write_follower_status(
            active_follower=False,
            active_leader=False,
            blocked_reason=reason,
            requested_cfg=requested_cfg,
            local_sources_allowed=role.local_sources_allowed,
            path=FOLLOWER_STATUS_FILE,
        )
        return 1

    if role.box_is_active and not role.active_leader and not role.active_follower:
        prior_crossover_enabled = _systemctl_unit_state(
            "is-enabled",
            CROSSOVER_UNIT,
        )
        prior_crossover_active = _systemctl_unit_state(
            "is-active",
            CROSSOVER_UNIT,
        )
        if prior_crossover_enabled is None or prior_crossover_active is None:
            return block_active_restore("crossover_ownership_state_unknown")
        prior_crossover_owned = prior_crossover_enabled or prior_crossover_active
        if prior_crossover_owned:
            if not _disable_crossover_unit():
                return block_active_restore("crossover_teardown_failed")
            crossover_active_after = _systemctl_unit_state(
                "is-active",
                CROSSOVER_UNIT,
            )
            if crossover_active_after is not False:
                return block_active_restore(
                    "crossover_inactive_state_unproven",
                )

    # Endpoint status for /state + the dashboard: active-follower /
    # active-leader mode, or the fail-closed block reason if the bond was refused
    # and this reconcile fell back to solo active.
    status_block_reason = endpoint_block_reason
    if role.transitioning_from_parked_role and not status_block_reason:
        status_block_reason = "role_transition_in_progress"
    role_status_ok = _write_follower_status(
        active_follower=role.active_follower,
        active_leader=role.active_speaker_leader,
        blocked_reason=status_block_reason,
        requested_cfg=requested_cfg,
        local_sources_allowed=role.local_sources_allowed,
        path=FOLLOWER_STATUS_FILE,
    )
    if not role_status_ok:
        log_event(
            logger,
            "multiroom.reconcile.effective_role_publish_failed",
            action="preserve_runtime_graph",
            level=logging.ERROR,
        )
        return 1

    # 1. Derived files — before any unit work.
    derived = assemble_args(role.cfg, active_endpoint=role.active_endpoint)
    wrote = _write_args_file(derived)
    set_keys = [k for k, v in derived.items() if v]
    log_event(
        logger,
        "multiroom.reconcile.args",
        path=ARGS_FILE,
        ok=wrote,
        set=",".join(set_keys) or "(none)",
    )

    # Paths passed explicitly (module globals read at CALL time); a def-time
    # default would pin the production path.
    outputd_env = outputd_grouping_env(
        role.cfg,
        active_endpoint=role.active_endpoint,
        flat_output_allowed=role.flat_output_allowed,
        outputd_period_frames=role.outputd_period_frames,
    )
    env_changed, env_ok = _write_derived_env(
        outputd_env,
        path=OUTPUTD_GROUPING_ENV_FILE,
        consumer="outputd",
    )
    log_event(
        logger,
        "multiroom.reconcile.outputd_env",
        path=OUTPUTD_GROUPING_ENV_FILE,
        changed=env_changed,
        ok=env_ok,
        lane=outputd_env[DAC_CONTENT_LANE_ENV] or "(cleared)",
        channel=outputd_env[OUTPUTD_DAC_CONTENT_CHANNEL_ENV] or "(cleared)",
    )
    if not env_ok:
        rc = 1
    # NO ring file to create on either bonded path: both the grouping ring and
    # the dac-content ring are ioplug rings whose C writer creates the file at
    # open, and the install leaves them alone on purpose
    # (deploy/lib/install/ring-platform.sh).

    # 2. CamillaDSP solo RESTORE — unwind a prior bond before units tear down.
    #    A box that will APPLY a bonded config below skips restore. An ACTIVE box
    #    restores its ACTIVE baseline, Layer A intact — NEVER a passive graph,
    #    which would be full-range to a tweeter.
    solo_restore_ok = True
    if role.active_leader or role.active_follower:
        pass
    elif role.box_is_active and prior_crossover_owned:
        # Unbond of an ACTIVE LEADER: camilla#2 (the crossover unit) is enabled
        # or active only after an active leader armed it. The pre-mutation gate
        # above has already disabled it and positively proved it inactive, so
        # camilla#1 can now reclaim the DAC via the leader stash.
        try:
            from .active_leader_config import restore_active_leader_solo_sync  # lazy: import cost — only this role branch of main() needs it

            restored = restore_active_leader_solo_sync()
            if restored:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="active_leader_solo_restored",
                    path=restored,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="active_leader_restore",
                error=e,
                level=logging.ERROR,
            )
            solo_restore_ok = False
            rc = 1
    elif role.box_is_active:
        try:
            from .follower_config import restore_active_follower_solo_sync  # lazy: import cost — only this role branch of main() needs it

            restored = restore_active_follower_solo_sync()
            if restored:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="active_solo_restored",
                    path=restored,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="active_restore",
                error=e,
                level=logging.ERROR,
            )
            solo_restore_ok = False
            rc = 1
    else:
        try:
            from .leader_config import restore_solo_config_sync  # lazy: import cost — only this role branch of main() needs it

            restored = restore_solo_config_sync()
            if restored:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="solo_restored",
                    path=restored,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="restore",
                error=e,
                level=logging.ERROR,
            )
            solo_restore_ok = False
            rc = 1

    # 3. outputd picks up the lane env only at unit start. For an active leader,
    # defer that restart until camilla#1's program-bake graph is live and
    # camilla#2's statefile is seeded with the re-proven endpoint graph: the
    # audio-hardware reconciler needs that graph pair as evidence to switch
    # outputd to the active-content lane before camilla#2 is armed. Restarting
    # here would read the grouping TTS env but still use the solo baseline,
    # re-opening the passive lane camilla#2 needs.
    defer_outputd_restart = role.active_speaker_leader
    outputd_restart_ok = True
    if env_changed and env_ok and not defer_outputd_restart:
        outputd_restart_ok = _restart_outputd()
        if not outputd_restart_ok:
            rc = 1

    # 3b. Voice's grouping-derived env (TTS socket flip + park flag): written +
    # kick-on-change only — a voice restart costs ~10-15 s and must happen only
    # on a real bond/unbond, never on the routine no-change reconcile. The kick
    # goes to jasper-aec-reconcile, NOT jasper-voice directly: that script is the
    # single owner of the voice/bridge units and decides restart-vs-park from
    # this flag plus its own provider + mic gates.
    voice_env = voice_grouping_env(
        role.cfg,
        active_endpoint=role.active_endpoint,
        flat_output_allowed=role.flat_output_allowed,
    )
    voice_changed, voice_ok = _write_derived_env(
        voice_env,
        path=VOICE_GROUPING_ENV_FILE,
        consumer="voice",
    )
    log_event(
        logger,
        "multiroom.reconcile.voice_env",
        path=VOICE_GROUPING_ENV_FILE,
        changed=voice_changed,
        ok=voice_ok,
        socket=voice_env.get(VOICE_TTS_SOCKET_ENV, "(solo: fanin default)"),
        park=voice_env.get(VOICE_PARK_ENV, "0"),
    )
    if not voice_ok:
        rc = 1
    voice_refresh_ok = True
    if (
        voice_changed
        and voice_ok
        and not (
            voice_refresh_ok := _restart_unit(
                AEC_RECONCILE_UNIT,
                no_block=True,
            )
        )
    ):
        rc = 1

    # 3c. shairport's bonded-leader AirPlay offset delta: written +
    # restart-on-change. The re-derivation itself happens in shairport's
    # ExecStartPre (jasper-apply-airplay-mode reads this file), so the restart in
    # step 4b is what applies it.
    airplay_env = airplay_grouping_env(role.cfg)
    airplay_changed, airplay_ok = _write_derived_env(
        airplay_env,
        path=AIRPLAY_GROUPING_ENV_FILE,
        consumer="airplay",
    )
    log_event(
        logger,
        "multiroom.reconcile.airplay_env",
        path=AIRPLAY_GROUPING_ENV_FILE,
        changed=airplay_changed,
        ok=airplay_ok,
        extra_delay_sec=airplay_env.get(AIRPLAY_BONDED_EXTRA_DELAY_ENV, "(solo)"),
    )
    if not airplay_ok:
        rc = 1

    # 4. The unit plan (stops before starts). Probed before it runs — see
    # _plan_changes_units — so the post-role source barrier below knows
    # whether this pass actually moved a unit.
    units_changed = _plan_changes_units(role.plan.intents)
    apply_rc = _apply(role.plan)
    rc = max(rc, apply_rc)

    # 4b. Re-derive shairport's backend latency offset on a bond/unbond that
    # changed it: a restart runs the ExecStartPre that reads
    # grouping-airplay.env. Skip a bonded FOLLOWER — the plan PARKED its
    # shairport and restarting would un-park it; a follower receives no AirPlay
    # anyway. One restart, only on a real offset change.
    is_bonded_follower = config.local_sources_parked(role.cfg)
    airplay_refresh_ok = True
    if airplay_changed and airplay_ok and not is_bonded_follower:
        # AirPlay may be household-Off. A plain restart ignores unit enablement
        # and would resurrect it after a leader bond/unbond; refresh only a
        # receiver that is already active.
        airplay_refresh_ok = _restart_unit(SHAIRPORT_UNIT, active_only=True)
        if not airplay_refresh_ok:
            rc = 1

    # 5. Bonded apply LAST (snapserver is up → the pipe has its reader; snapclient
    #    is up → the grouping ring has its writer).
    if role.passive_leader:
        try:
            from .leader_config import apply_bonded_leader_config_sync  # lazy: import cost — only the passive-leader branch of main() needs it

            applied = apply_bonded_leader_config_sync(role.cfg)
            log_event(
                logger,
                "multiroom.reconcile.camilla",
                result="bonded",
                path=applied,
            )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="bonded_apply",
                error=e,
                level=logging.ERROR,
            )
            rc = 1
    elif role.active_speaker_leader:
        # WIRE-UP GUARD — the single top-of-path precondition. The two-instance
        # setup is viable ONLY if the wire is up: camilla#1's bake writes a
        # File/FIFO sink that needs snapserver as its reader, and ONLY a
        # successful bake moves camilla#1 off the DAC so camilla#2 can take it.
        # If snapserver did not start, bail here and STAY SOLO-ACTIVE (camilla#1
        # keeps the DAC on its safe solo baseline, camilla#2 un-armed) —
        # otherwise the two instances fight for the DAC and camilla#1 exhausts
        # its recovery budget.
        if not _unit_is_active(SNAPSERVER_UNIT):
            log_event(
                logger,
                "multiroom.reconcile.active_leader_blocked",
                reason="snapserver_not_active",
                detail=(
                    "active-leader wire is down; staying solo-active "
                    "(camilla#1 keeps the DAC, camilla#2 un-armed)"
                ),
                level=logging.ERROR,
            )
            if not _disable_crossover_unit():
                rc = 1
            rc = 1
        else:
            # Wire is up. camilla#1 bakes to the now-readable pipe; THEN the
            # camilla#2 statefile is RE-SEEDED with the re-proven driver-domain
            # graph before audio-hardware reconcile sizes outputd's active lane.
            # Only if that bake and outputd env handoff succeed, and camilla#1 has
            # provably released the DAC, is camilla#2 armed onto it — the
            # never-flat guarantee. camilla#2 is disabled before the bake and
            # later started from that statefile, so trim-only rewrites are picked
            # up by process start rather than by an idempotent systemd no-op.
            bake_ok = False
            if not _disable_crossover_unit():
                rc = 1
            elif not _ensure_unit_active(CAMILLA_UNIT, reason="active-leader-bake"):
                rc = 1
            else:
                active_leader_action = "active_leader_bake_apply"
                try:
                    from .active_leader_config import (
                        apply_active_leader_bake_sync,
                        seed_crossover_statefile,
                    )  # lazy: import cost — only the active-leader-bake branch of main() needs it

                    applied = apply_active_leader_bake_sync()
                    log_event(
                        logger,
                        "multiroom.reconcile.camilla",
                        result="active_leader_bake",
                        path=applied,
                    )
                    bake_ok = True
                    active_leader_action = "active_leader_crossover_seed"
                    seed_crossover_statefile()
                    if not _run_audio_hardware_reconcile(
                        reason="grouping-active-leader-bake",
                    ):
                        bake_ok = False
                        rc = 1
                except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
                    log_event(
                        logger,
                        "multiroom.reconcile.camilla_failed",
                        action=active_leader_action,
                        error=e,
                        level=logging.ERROR,
                    )
                    rc = 1
            # Arm camilla#2 ONLY when the bake provably moved camilla#1 off the
            # active-content PCM, outputd re-converged to the active lane, and
            # the exclusive handle positively released. A successful CamillaDSP
            # config reload is not enough: the transport can lag the actual
            # close, and arming into that window races EBUSY against camilla#1's
            # recovery-budget unit.
            if bake_ok:
                if _unit_is_active(CROSSOVER_UNIT):
                    log_event(
                        logger,
                        "multiroom.reconcile.active_leader_handle_probe",
                        pcm=RING_ACTIVE_PLAYBACK_DEVICE,
                        lock_path=ACTIVE_CONTENT_WRITER_LOCK_PATH,
                        result="already_armed",
                        reason="crossover_unit_active",
                    )
                else:
                    probe = _wait_for_active_content_pcm_release()
                    log_event(
                        logger,
                        "multiroom.reconcile.active_leader_handle_probe",
                        pcm=RING_ACTIVE_PLAYBACK_DEVICE,
                        lock_path=probe.lock_path,
                        result=probe.state,
                        reason=probe.reason,
                        detail=probe.detail or "(none)",
                        attempts=probe.attempts,
                        timeout_sec=probe.timeout_sec,
                        level=logging.WARNING if probe.unknown else logging.INFO,
                    )
                    if not probe.released:
                        # `busy` and `unknown` both fail closed to solo-active:
                        # arming without positive proof is the EBUSY reboot loop
                        # this barrier exists to prevent. See
                        # ACTIVE_CONTENT_WRITER_LOCK_PATH for which boxes reach
                        # `unknown` and why blocking is the honest answer there.
                        endpoint_block_reason = (
                            "active_content_pcm_busy"
                            if probe.busy
                            else "active_content_pcm_unverified"
                        )
                        active_leader_arm_blocked = True
                        log_event(
                            logger,
                            "multiroom.reconcile.active_leader_blocked",
                            reason=endpoint_block_reason,
                            detail=(
                                "active-content playback PCM not positively "
                                f"released after camilla#1 bake (state="
                                f"{probe.state}, reason={probe.reason}); "
                                "restoring solo-active and leaving camilla#2 "
                                "un-armed"
                            ),
                            pcm=RING_ACTIVE_PLAYBACK_DEVICE,
                            lock_path=probe.lock_path,
                            probe_reason=probe.reason,
                            probe_detail=probe.detail or "(none)",
                            attempts=probe.attempts,
                            timeout_sec=probe.timeout_sec,
                            level=logging.ERROR,
                        )
                        try:
                            from .active_leader_config import (
                                restore_active_leader_solo_sync,
                            )  # lazy: import cost — only this bake-failure rollback path needs it

                            restored = restore_active_leader_solo_sync()
                            if restored:
                                log_event(
                                    logger,
                                    "multiroom.reconcile.camilla",
                                    result=(
                                        "active_leader_solo_restored_after_pcm_busy"
                                    ),
                                    path=restored,
                                )
                        except (
                            CamillaUnavailable,
                            DspApplyError,
                            OSError,
                            RuntimeError,
                            TimeoutError,
                            ValueError,
                        ) as e:
                            log_event(
                                logger,
                                "multiroom.reconcile.camilla_failed",
                                action="active_leader_pcm_busy_restore",
                                error=e,
                                level=logging.ERROR,
                            )
                        _write_follower_status(
                            active_follower=False,
                            active_leader=False,
                            blocked_reason=endpoint_block_reason,
                            requested_cfg=requested_cfg,
                            local_sources_allowed=role.local_sources_allowed,
                            path=FOLLOWER_STATUS_FILE,
                        )
                        rc = 1
                    elif not _arm_crossover_unit():
                        rc = 1
            else:
                log_event(
                    logger,
                    "multiroom.reconcile.camilla",
                    result="active_leader_crossover_arm_skipped",
                    reason="crossover_not_ready",
                )
                rc = 1

    if role.active_leader and not active_leader_arm_blocked:
        # 6. The stream-binding pin (ANY leader hosts the stream; runs after the
        # camilla apply so snapserver has had its longest warm-up): re-bind every
        # PERSISTED snapcast group to our stream, because a stale server.json
        # binding silently mutes the whole bond behind green health. The ensure
        # retries internally; an unreachable snapserver flips the exit code (a
        # bond whose bindings cannot be verified is a degraded bond).
        from .snapcast_rpc import ensure_groups_on_stream  # lazy: import cost — only main()'s bonded-leader stream-binding step needs it

        report = ensure_groups_on_stream(SNAP_STREAM_ID)
        log_event(
            logger,
            "multiroom.reconcile.stream_binding",
            reachable=report["reachable"],
            groups=report["groups"],
            fixed=report["fixed"],
            failed=report["failed"],
            want=SNAP_STREAM_ID,
        )
        if not report["reachable"] or report["failed"]:
            rc = 1

    # 5b. Active FOLLOWER CamillaDSP swap LAST (snapclient is up → the grouping
    #     ring has its writer, so CamillaDSP locks immediately). The graph was
    #     built + re-proven by the readiness gate above, so no capture content
    #     (stream / silence / garbage) can produce a full-range driver feed. A
    #     swap failure here keeps CamillaDSP on its prior safe solo-active graph;
    #     the next reconcile retries.
    if role.active_follower:
        try:
            from .follower_config import apply_prebuilt_follower_config_sync  # lazy: import cost — only the active-follower branch of main() needs it

            applied = apply_prebuilt_follower_config_sync()
            log_event(
                logger,
                "multiroom.reconcile.camilla",
                result="active_follower",
                path=applied,
            )
        except Exception as e:  # noqa: BLE001 — fail-soft, surfaced via rc+doctor
            log_event(
                logger,
                "multiroom.reconcile.camilla_failed",
                action="active_follower_apply",
                error=e,
                level=logging.ERROR,
            )
            rc = 1

    # A requested follower may have been refused and safely resolved to solo.
    # Publish that effective permission only AFTER every load-bearing solo
    # transition has succeeded: until this point the earlier status explicitly
    # denies local sources, so a concurrent systemd start cannot enter while the
    # old follower graph is still live. Any failed restore/file/unit step leaves
    # the fail-safe deny in place for the next reconcile to repair.
    source_grant_pending = (
        role.refused_follower_fallback or role.transitioning_from_parked_role
    )
    if source_grant_pending:
        transition_landed = all(
            (
                wrote,
                env_ok,
                solo_restore_ok,
                outputd_restart_ok,
                voice_ok,
                voice_refresh_ok,
                airplay_ok,
                airplay_refresh_ok,
                apply_rc == 0,
            )
        )
        # A refused follower intentionally returns nonzero for the rejected bond
        # even after its safe solo fallback landed. An ordinary
        # follower->solo/leader transition has no such expected error: every
        # later role-specific step must also have succeeded before sources can be
        # granted.
        if role.transitioning_from_parked_role and not role.refused_follower_fallback:
            transition_landed = transition_landed and rc == 0
        if transition_landed:
            grant_published = _write_follower_status(
                active_follower=False,
                active_leader=(
                    role.active_speaker_leader
                    if role.transitioning_from_parked_role
                    else False
                ),
                blocked_reason=(
                    endpoint_block_reason if role.refused_follower_fallback else ""
                ),
                requested_cfg=requested_cfg,
                local_sources_allowed=True,
                path=FOLLOWER_STATUS_FILE,
            )
            if not grant_published:
                rc = 1
                log_event(
                    logger,
                    (
                        "multiroom.reconcile.fallback_source_grant_failed"
                        if role.refused_follower_fallback
                        else "multiroom.reconcile.role_transition_grant_failed"
                    ),
                    reason=endpoint_block_reason,
                    action="sources_remain_parked",
                    level=logging.ERROR,
                )
        else:
            log_event(
                logger,
                (
                    "multiroom.reconcile.fallback_sources_parked"
                    if role.refused_follower_fallback
                    else "multiroom.reconcile.role_transition_sources_parked"
                ),
                reason=endpoint_block_reason,
                action="retry_reconcile",
                level=logging.ERROR,
            )

    # 7. Hand the completed role to the one source owner. It reads grouping
    # permission fresh and performs follower park or solo/leader restore for all
    # sources, including USB's arm -> advertise -> start sequence.
    if not _converge_sources_after_role(
        grouping_active=role.active,
        units_changed=units_changed,
    ):
        rc = 1

    log_event(logger, "multiroom.reconcile.done", rc=rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
