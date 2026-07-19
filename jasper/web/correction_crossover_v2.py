# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The v2 crossover conductor's web host (Wave 5a endpoint binding).

Owns everything between the ``/correction/crossover/v2/*`` POST routes (thin
dispatch branches in :mod:`jasper.web.correction_setup`) and the pure conductor
(:mod:`jasper.active_speaker.crossover_v2_flow`):

* the **durable v2 flow state** (one JSON file) that ``status_payload`` threads
  into the envelope as ``status["crossover_v2"]`` — phase / candidate / verify
  / failure / apply_blocked / needs_recovery / applied;
* the **session volume plan** singleton (one fixed measurement volume per
  session, §5.5) and its open/close/abandon wiring — including the
  walked-away guarantee: every terminal relay outcome drains the restore-once
  path;
* the **production seam bindings** — real ``analyze_program_capture``, real
  evidence-store publication (publish → tamper-checked reopen, §5.6), the real
  CamillaController-backed program playback via
  :func:`jasper.active_speaker.crossover_v2_flow.bind_program_playback_seams`,
  and the apply gate reading the durable applied flag;
* the **plan runner host** (:func:`build_v2_run_and_consume`) that drives the
  REAL :func:`jasper.capture_relay.session.run_capture_plan` on a worker
  thread, mirroring ``build_crossover_relay_plan_run_and_consume``'s thread
  model (``asyncio.to_thread`` + shielded cancel-drain + purge on teardown +
  ``stop_event``/``stop_lock`` stop semantics);
* the **host-owned error mapping**: ``CaptureTimeout`` / relay-session death →
  ``relay_timeout`` failure state + volume abandon; conductor failure codes →
  ``status["crossover_v2"]["failure"]``.

Session binding (§5.6): the durable state is keyed to the relay session id. A
new ``/v2/session`` POST hydrates through
:meth:`CrossoverV2Conductor.hydrate`, which invalidates CHECK/MEASURE evidence
for a different session; ``/v2/verify`` re-arms VERIFY only (a 1-entry plan)
from the persisted post-apply state, per §5.2's re-verify action.

ON-DEVICE: the acoustic playback binding is not exercised hardware-free (same
status as the room/sync relay flows) — W6 validates it end-to-end on JTS3.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
STATE_KIND = "jts_crossover_v2_flow_state"
DEFAULT_V2_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_v2_state.json"
)

# The wizard-facing relay kind label (mirrors the legacy
# "crossover_sweep:<kind>" labels so /status.relay consumers need no new
# vocabulary beyond the prefix).
V2_RELAY_KIND_SESSION = "crossover_v2:session"
V2_RELAY_KIND_VERIFY = "crossover_v2:verify"

# Downsample ceiling for the persisted predicted-sum verify prior — enough
# resolution for the ±1.5 dB [Fc/2, 2Fc] comparison at 1/6-octave smoothing
# while keeping the durable state file small.
MAX_PERSISTED_SUM_POINTS = 512

_state_lock = threading.Lock()
_state_path_override: Path | None = None

_volume_plan_lock = threading.Lock()
_volume_plan: Any = None


class CrossoverV2Refused(ValueError):
    """A v2 endpoint refusal (maps to HTTP 400 in the dispatch ladder)."""


class CrossoverV2LocalSeamError(RuntimeError):
    """A LOCAL play/analyze seam raised ``OSError`` — not a relay-transport death.

    W6 hardware run 3 finding G: the DSP writer lock's ``os.open`` on a
    read-only ``config_dir`` (finding F) raised a bare ``OSError`` from inside
    ``on_armed`` — the exact same exception TYPE
    ``jasper.capture_relay.session.run_capture_plan``'s transport polling
    loop raises on a genuine relay-connection failure (``client.status``
    reaching an unreachable host). ``build_v2_run_and_consume``'s relay-death
    except arm cannot tell those apart by type alone, so it misclassified a
    local filesystem fault as ``relay_timeout``. ``on_armed``/``consume``
    convert a local ``OSError`` to THIS type at the seam boundary before it
    can reach that arm, so it falls through to the catch-all cleanup arm's
    honest ``internal_error`` classification instead — a genuine transport
    ``OSError`` (never wrapped) still hits the relay-death arm unchanged.
    """


# --------------------------------------------------------------------------- #
# flow selector + durable state
# --------------------------------------------------------------------------- #


def v2_flow_active() -> bool:
    from jasper.active_speaker.crossover_flow import (
        CROSSOVER_FLOW_V2,
        active_crossover_flow,
    )

    return active_crossover_flow() == CROSSOVER_FLOW_V2


def _state_path() -> Path:
    return _state_path_override or DEFAULT_V2_STATE_PATH


def set_state_path_for_tests(path: str | Path | None) -> None:
    """Test seam: point the durable v2 state at a temp file (None resets)."""
    global _state_path_override
    with _state_lock:
        _state_path_override = Path(path) if path is not None else None


def load_v2_state() -> dict[str, Any] | None:
    """Read the durable v2 flow state; malformed/missing reads as ``None``."""
    with _state_lock:
        try:
            raw = json.loads(_state_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            log_event(
                logger,
                "correction.crossover_v2_state_unreadable",
                level=logging.WARNING,
            )
            return None
    if (
        not isinstance(raw, Mapping)
        or raw.get("kind") != STATE_KIND
        or raw.get("schema_version") != STATE_SCHEMA_VERSION
    ):
        return None
    return dict(raw)


def save_v2_state(state: Mapping[str, Any]) -> None:
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        "updated_at": time.time(),
        **{k: v for k, v in state.items() if k not in {"schema_version", "kind"}},
    }
    with _state_lock:
        atomic_write_text(
            _state_path(),
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )


def clear_v2_state() -> None:
    with _state_lock:
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log_event(
                logger,
                "correction.crossover_v2_state_clear_failed",
                level=logging.WARNING,
            )


def reset_v2_journey_state() -> None:
    """Start-over's v2 clear (W6.10, gate-amended for W6.8's Undo).

    Clears the measurement-JOURNEY fields (session binding, accepted phases,
    candidate, verify, failure, gain plan, priors, evidence) so the envelope
    serves the clean start screen — but when a candidate is APPLIED, preserves
    ``applied`` + ``pre_apply_profile``: those are the ONLY durable pointers the
    v2-aware Undo (:func:`handle_v2_restore`, W6.8) restores from. A full
    :func:`clear_v2_state` here would unlink the sole reference to the retained
    pre-candidate snapshot, leaving the applied graph playing with Undo
    permanently unreachable. Not applied ⇒ full clear, as before.
    """
    state = load_v2_state()
    if state is None:
        return
    if not state.get("applied"):
        clear_v2_state()
        return
    pre_apply_profile = state.get("pre_apply_profile")
    save_v2_state({
        "session_id": None,
        "accepted_phases": [],
        "applied": True,
        "gain_plan_db": None,
        "candidate": None,
        "verify": None,
        "failure": None,
        "apply_blocked": None,
        "verify_priors": None,
        "evidence": None,
        "pre_apply_profile": (
            dict(pre_apply_profile)
            if isinstance(pre_apply_profile, Mapping)
            else None
        ),
    })
    log_event(logger, "correction.crossover_v2_journey_reset_kept_applied")


def observe_apply_success(
    candidate_fingerprint: str,
    *,
    pre_apply_profile: Mapping[str, Any] | None = None,
) -> None:
    """Mark the v2 candidate applied — the apply-complete event that arms the
    soft-held VERIFY (§5.2). Called by the v2 apply endpoint on success.

    ``pre_apply_profile`` is the frozen applied-baseline snapshot the apply
    replaced (``build_baseline_profile_candidate``'s
    ``applied_recomposition_profile`` — see ``handle_v2_apply``), stashed here
    because the apply's own persisted profile pops that field once it becomes
    the new applied SSOT. This is the ONLY durable record of what the Undo
    path (``handle_v2_restore``) restores to; ``None`` when this was the
    speaker's first-ever applied crossover (nothing to undo back to).
    """
    state = load_v2_state()
    if state is None:
        return
    candidate = state.get("candidate")
    stored = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping)
        else ""
    )
    if stored and candidate_fingerprint and stored != candidate_fingerprint:
        log_event(
            logger,
            "correction.crossover_v2_apply_fingerprint_mismatch",
            level=logging.WARNING,
        )
        return
    state["applied"] = True
    state["failure"] = None
    state["apply_blocked"] = None
    state["pre_apply_profile"] = (
        dict(pre_apply_profile) if isinstance(pre_apply_profile, Mapping) else None
    )
    save_v2_state(state)
    log_event(logger, "correction.crossover_v2_applied")


def observe_restore() -> None:
    """Clear the durable v2 state after a successful Undo (mirrors
    :func:`observe_apply_success`). Resets the whole flow to a clean
    unmeasured state — matching the reset a pre-apply terminal failure
    already gets (:func:`_persist_terminal_failure`) — so the envelope lands
    back on the pre-measurement screen rather than a half-consistent
    review_apply pointing at the now-undone candidate."""
    state = load_v2_state()
    if state is None:
        return
    state["applied"] = False
    state["candidate"] = None
    state["verify"] = None
    state["failure"] = None
    state["apply_blocked"] = None
    state["pre_apply_profile"] = None
    state["accepted_phases"] = []
    state["gain_plan_db"] = None
    save_v2_state(state)


def _applied_gate() -> bool:
    """The conductor's ``apply_complete`` seam: reads the durable applied flag."""
    state = load_v2_state()
    return bool(state and state.get("applied") is True)


# --------------------------------------------------------------------------- #
# session volume plan singleton (§5.5)
# --------------------------------------------------------------------------- #


def session_volume_plan() -> Any:
    """The one durable-state-backed SessionVolumePlan this process owns."""
    global _volume_plan
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_SESSION_VOLUME_STATE_PATH,
        SessionVolumePlan,
    )

    with _volume_plan_lock:
        if _volume_plan is None:
            _volume_plan = SessionVolumePlan(
                state_path=DEFAULT_SESSION_VOLUME_STATE_PATH
            )
        return _volume_plan


def set_volume_plan_for_tests(plan: Any) -> None:
    global _volume_plan
    with _volume_plan_lock:
        _volume_plan = plan


# --------------------------------------------------------------------------- #
# session-scoped measurement pause (§5.5 + W6.1 — hold voice OFF for the whole
# session, not just per-play)
# --------------------------------------------------------------------------- #
#
# The per-play ``measurement_window()`` (bind_production_play._emit) protected
# each stimulus, but between opening the fixed measurement volume and the first
# play — and in the gaps between plays — nothing held voice paused, so
# jasper-voice's idle reconciler reverted the -20 dB session volume back toward
# the household level within ~200 ms of ``session_volume_opened`` (W6.1 hardware
# run 2). Cap enforcement then silently understates: programs would play hotter
# than admission assumed. Fix: like the room / balance / sync flows, HOLD one
# ``measurement_window`` for the whole session (its MEASURE_PAUSE keeps the idle
# reconciler off), acquired when the volume opens and released on every drain.
#
# ``measurement_window`` is EXCLUSIVE (a single ``_window_active`` mutex — a
# second concurrent window raises), not nestable, so the per-play window is
# nest-SKIPPED while the session holds one (see ``bind_production_play``). The
# held context manager lives here as a process-global entered on jasper-web's
# single background loop; acquire/release are idempotent so a drain that runs
# after the session already released (recover / ceiling / a crash-fresh process)
# is a safe no-op. The paired ``MeasurementAbortTarget`` keeps the coordinator's
# isolation-loss abort effective under a held window: the per-play path
# registers the actual play task, so a mux gate-lease renew failure cancels the
# in-flight sweep (not the long-lived session task) and latches ``failed`` so
# the next play refuses honestly.
_session_pause_cm: Any = None
_session_abort_target: Any = None


async def acquire_session_measurement_pause() -> None:
    """Enter (once) the coordinator measurement window for the whole session.

    Idempotent: if the session already holds it, this is a no-op so a spurious
    second acquire cannot open a second exclusive window. Raises
    ``MeasurementWindowError`` if the window cannot be opened (e.g. a live voice
    session) — the caller surfaces that as a session-open failure.
    """
    global _session_pause_cm, _session_abort_target
    if _session_pause_cm is not None:
        return
    from jasper.correction import coordinator

    target = coordinator.MeasurementAbortTarget()
    cm = coordinator.measurement_window(abort_target=target)
    await cm.__aenter__()
    _session_pause_cm = cm
    _session_abort_target = target
    log_event(logger, "correction.crossover_v2_measurement_pause", action="acquire")


async def release_session_measurement_pause() -> None:
    """Exit the held session measurement window (idempotent).

    Every drain path (close / abandon / ceiling / unresolved-recover) calls
    this; a drain that runs when nothing is held (already released, or a
    crash-fresh process that never entered it) is a safe no-op — never a
    double-release.
    """
    global _session_pause_cm, _session_abort_target
    cm = _session_pause_cm
    if cm is None:
        return
    _session_pause_cm = None
    _session_abort_target = None
    await cm.__aexit__(None, None, None)
    log_event(logger, "correction.crossover_v2_measurement_pause", action="release")


def session_measurement_pause_held() -> bool:
    """True while the session holds the one measurement window (per-play skip)."""
    return _session_pause_cm is not None


def reset_session_measurement_pause_for_tests() -> None:
    """Test seam: drop the held-window reference without an ``__aexit__``."""
    global _session_pause_cm, _session_abort_target
    _session_pause_cm = None
    _session_abort_target = None


async def _play_under_session_pause(play_body: Callable[[], Any]) -> None:
    """Run one play under the session-held window, abort-target registered.

    Before Finding C the per-play window's ENTERING task was the play task, so
    the coordinator's isolation-loss abort (cancel the entering task) stopped
    the sweep. The held window's entering task is the session runner, whose
    cancel would not stop an in-flight play — so the play task registers
    itself as the abort target while playing. A latched abort (gate-lease
    renew failure between plays) refuses the next play with a NAMED error, and
    a cancel that lands mid-play surfaces as the same named error, so the
    runner's cleanup arm persists an honest failure either way.
    """
    from jasper.correction.coordinator import MeasurementWindowError

    target = _session_abort_target
    if target is not None and target.failed:
        raise MeasurementWindowError(
            "measurement isolation was lost (the music-isolation gate lease "
            "could not be renewed); restart the measurement session"
        )
    task = asyncio.current_task()
    if target is not None and task is not None:
        target.register(task)
    try:
        await play_body()
    except asyncio.CancelledError:
        if target is not None and target.failed:
            # The coordinator aborted THIS play on isolation loss — surface a
            # named terminal error (not a bare cancellation) so the session
            # runner's cleanup arm persists it and tells the phone.
            raise MeasurementWindowError(
                "measurement isolation was lost mid-play; playback was "
                "stopped before household music could re-enter the mix"
            ) from None
        raise
    finally:
        if target is not None:
            target.clear()


# --------------------------------------------------------------------------- #
# session-volume recovery + ceiling (§5.5 + W6.1 — recover routing, lazy ceiling)
# --------------------------------------------------------------------------- #

# Bound each session-volume drain (recover / ceiling) — CamillaDSP set+confirm
# is a few RPCs; longer than that means CamillaDSP is wedged, and the drain
# should surface a failure rather than hang the request thread.
_SESSION_VOLUME_DRAIN_TIMEOUT_S = 15.0


def _session_volume_io(camilla_factory: Any) -> tuple[Callable[[float], Any], Callable[[], Any]]:
    """(set, get) main-volume callables for the plan drains, fail-closed on
    CamillaUnavailable so a wedged DSP cannot silently no-op a recovery."""
    from jasper.camilla import CamillaUnavailable

    async def _set(db: float) -> bool:
        try:
            return await camilla_factory().set_volume_db(db, best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    async def _get() -> float | None:
        try:
            return await camilla_factory().get_volume_db(best_effort=False)
        except CamillaUnavailable as exc:
            raise RuntimeError("CamillaDSP is unavailable") from exc

    return _set, _get


def _release_pause_best_effort(run_async: Any) -> None:
    """Release the session measurement pause from a drain that runs outside the
    runner (recover / ceiling). Idempotent no-op when nothing is held (already
    released, or a crash-fresh process that never entered it)."""
    try:
        run_async(
            release_session_measurement_pause(),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        logger.warning("v2 session measurement-pause release failed", exc_info=True)


def enforce_session_volume_ceiling_if_stale(
    run_async: Any, camilla_factory: Any
) -> bool:
    """Lazy wall-clock-ceiling enforcement (W6.1 — ``enforce_ceiling`` had zero
    callers, so the 1800 s ceiling never existed at runtime).

    Invoked on envelope build (on read) and at session open. Cheap on the happy
    path: ``stale_active`` is an in-memory check, so a healthy session pays
    nothing; only a session that has outlived
    ``DEFAULT_WALL_CLOCK_CEILING_S`` is force-drained here, restoring the
    household volume and releasing any held measurement pause. v2-only. Returns
    True iff a stale session was drained.
    """
    if not v2_flow_active():
        return False
    plan = session_volume_plan()
    try:
        if not plan.stale_active():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        run_async(
            plan.enforce_ceiling(set_v, get_v),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_ceiling_enforce_failed",
            level=logging.ERROR,
        )
    _release_pause_best_effort(run_async)
    return True


def v2_volume_recovery_active() -> bool:
    """True when the v2 session-volume plan holds a state the recover-volume
    endpoint must drain (unresolved, or a crash-hydrated active plan). The
    legacy-lease path 409s these because they live on the v2 plan, not the
    lease — the observed ``crossover_volume_recovery_not_required`` bug."""
    if not v2_flow_active():
        return False
    try:
        return bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        return True  # fail-closed: an unreadable state still offers recovery


def recover_session_volume(
    run_async: Any, camilla_factory: Any
) -> tuple[bool, str]:
    """Drain the v2 plan's unresolved / stale-active state (the volume_recovery
    screen's ``recover_volume`` action). Returns ``(succeeded, result_value)``.

    Routes to ``SessionVolumePlan.recover_unresolved`` — the v2 owner of the
    unresolved state — instead of the legacy lease, and releases any held
    measurement pause on success.
    """
    from jasper.active_speaker.session_volume_plan import (
        SessionVolumeRestoreResult,
    )

    plan = session_volume_plan()
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        result = run_async(
            plan.recover_unresolved(set_v, get_v),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
    except concurrent.futures.TimeoutError:
        log_event(
            logger,
            "correction.crossover_v2_volume_recovery_timeout",
            level=logging.ERROR,
        )
        result = SessionVolumeRestoreResult.FAILED
    succeeded = result is not SessionVolumeRestoreResult.FAILED
    if succeeded:
        _release_pause_best_effort(run_async)
    return succeeded, getattr(result, "value", str(result))


def reconcile_session_volume_for_new_session(
    run_async: Any, camilla_factory: Any
) -> None:
    """Drain any residual session volume before a fresh session opens (W6.1 E1).

    A stale-active is force-drained by the ceiling; a residual owned-active
    leftover from a prior failed session in THIS process (``open`` refuses over
    any non-``None`` state) is drained too, so ``plan.open`` starts clean rather
    than raising ``SessionVolumePlanError`` into the silent
    200→adapter_failed loop observed live (run 2's retry). A latched
    ``unresolved`` / crash-hydrated ``needs_recovery`` state is NOT drained here
    — the caller's ``needs_recovery`` gate refuses it toward the recover-volume
    screen.
    """
    if not v2_flow_active():
        return
    plan = session_volume_plan()
    enforce_session_volume_ceiling_if_stale(run_async, camilla_factory)
    if plan.measurement_volume_db is None or plan.needs_recovery:
        return
    set_v, get_v = _session_volume_io(camilla_factory)
    try:
        run_async(
            plan.abandon(set_v, get_v, reason="stale_session_reset"),
            timeout=_SESSION_VOLUME_DRAIN_TIMEOUT_S,
        )
        log_event(logger, "correction.crossover_v2_stale_session_reset")
    except (concurrent.futures.TimeoutError, OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_stale_session_reset_failed",
            level=logging.ERROR,
        )
    _release_pause_best_effort(run_async)


# --------------------------------------------------------------------------- #
# status threading (S1b)
# --------------------------------------------------------------------------- #


def _phase_from_state(state: Mapping[str, Any] | None) -> str:
    from jasper.active_speaker.crossover_v2_flow import (
        CAPTURE_PHASES,
        PHASE_DONE,
        PHASE_MEASURE,
        PHASE_REVIEW_APPLY,
        PHASE_VERIFY,
    )

    accepted = set(
        state.get("accepted_phases") or () if isinstance(state, Mapping) else ()
    )
    applied = bool(state and state.get("applied"))
    for phase in CAPTURE_PHASES:
        if phase not in accepted:
            if phase == PHASE_VERIFY and PHASE_MEASURE in accepted and not applied:
                return PHASE_REVIEW_APPLY
            return phase
    return PHASE_DONE


def crossover_v2_status_block() -> dict[str, Any] | None:
    """The ``status["crossover_v2"]`` block, or ``None`` when the flow is legacy.

    ``needs_recovery`` comes from the SessionVolumePlan (the W2 gate ruling:
    key on ``needs_recovery``, never ``unresolved_volume_safety`` alone — a
    crash-hydrated active plan surfaces no unresolved payload but still needs
    draining before a new session).
    """
    if not v2_flow_active():
        return None
    state = load_v2_state()
    try:
        needs_recovery = bool(session_volume_plan().needs_recovery)
    except (OSError, RuntimeError, ValueError):
        needs_recovery = True  # unreadable volume state fails closed
    block: dict[str, Any] = {
        "phase": _phase_from_state(state),
        "candidate": (state or {}).get("candidate"),
        "verify": (state or {}).get("verify"),
        "failure": (state or {}).get("failure"),
        "apply_blocked": (state or {}).get("apply_blocked"),
        "needs_recovery": needs_recovery,
        "applied": bool(state and state.get("applied")),
        "session_id": (state or {}).get("session_id"),
    }
    return block


# --------------------------------------------------------------------------- #
# conductor persistence
# --------------------------------------------------------------------------- #


def _decimate_sum(predicted_sum: Any) -> dict[str, Any] | None:
    if predicted_sum is None:
        return None
    freqs, mags = predicted_sum
    n = len(freqs)
    if n == 0:
        return None
    step = max(1, n // MAX_PERSISTED_SUM_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs[::step]],
        "magnitude_db": [float(m) for m in mags[::step]],
    }


def _candidate_summary(candidate: Any) -> dict[str, Any] | None:
    if candidate is None:
        return None
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        "alignment": candidate.alignment.to_dict(),
        # Threaded through for the review_apply low-confidence nudge (W6.7
        # ruling 4, crossover_envelope_v2.ALIGNMENT_CONFIDENCE_NUDGE_FLOOR).
        "alignment_confidence": analysis.get("alignment_confidence"),
    }


def persist_conductor_state(
    conductor: Any,
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    """Write the conductor's durable snapshot + host-observed failure state."""
    snap = conductor.snapshot()
    verify_outcome = conductor.verify_outcome
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "accepted_phases": list(snap.accepted_phases),
        "applied": snap.applied,
        "gain_plan_db": dict(snap.gain_plan_db) if snap.gain_plan_db else None,
        "candidate": _candidate_summary(conductor.candidate),
        "verify": (
            {"outcome": verify_outcome} if verify_outcome is not None else None
        ),
        "failure": {"code": failure_code} if failure_code else None,
        "verify_priors": {
            "predicted_sum": _decimate_sum(conductor.measure_predicted_sum),
            "gate_window_ms": conductor.measure_gate_window_ms,
        },
        "evidence": dict(evidence) if evidence else None,
    }
    prior = load_v2_state() or {}
    # The applied flag is host-durable (set by the apply endpoint) — never
    # regressed by a conductor snapshot that predates it.
    if prior.get("applied") is True and prior.get("session_id") == snap.session_id:
        state["applied"] = True
    if state["candidate"] is None and isinstance(prior.get("candidate"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["candidate"] = dict(prior["candidate"])
    if state["evidence"] is None and isinstance(prior.get("evidence"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["evidence"] = dict(prior["evidence"])
    # ``pre_apply_profile`` (the Undo stash — observe_apply_success /
    # handle_v2_restore) and ``apply_blocked`` (the review_apply nudge) are
    # NOT conductor-owned fields: the conductor neither produces nor reads
    # either one, so they are absent from the ``state`` literal above and
    # every OTHER caller of this function only ever sets the fields it does
    # know about. Unlike ``candidate``/``evidence``, this carry-forward is
    # unconditional (not gated on a matching session_id): the deferred
    # VERIFY that auto-arms right after every apply runs under a BRAND-NEW
    # relay session id (``prepare_v2_verify`` mints one and "rebinds" the
    # conductor's session_id before its own ``persist_conductor_state``
    # call), so a session-id-gated carry-forward would still lose the stash
    # on that very first post-apply snapshot. W6.12 P0: without this, the
    # verify phase that always immediately follows an apply wiped the
    # just-stashed ``pre_apply_profile`` before a household could ever reach
    # the verify_fail Undo screen — ``/crossover/v2/restore`` 400'd with "no
    # previous crossover to restore to" after literally every apply.
    state["pre_apply_profile"] = prior.get("pre_apply_profile")
    state["apply_blocked"] = prior.get("apply_blocked")
    save_v2_state(state)


def _persist_terminal_failure(conductor: Any, code: str) -> None:
    """Session-terminal persistence (§5.6): pre-apply, capture evidence dies
    with the session (restart at CHECK); post-apply, the applied candidate +
    verify priors survive so ``/v2/verify`` can re-arm."""
    persist_conductor_state(conductor, failure_code=code)
    state = load_v2_state()
    if state is None:
        return
    if not state.get("applied"):
        state["accepted_phases"] = []
        state["gain_plan_db"] = None
    save_v2_state(state)


# --------------------------------------------------------------------------- #
# production seam bindings (S1a/S1e)
# --------------------------------------------------------------------------- #


def _wav_bytes_to_samples(wav_bytes: bytes) -> tuple[Any, int]:
    import io

    import numpy as np
    from scipy.io import wavfile

    rate, data = wavfile.read(io.BytesIO(wav_bytes))
    if data.ndim > 1:
        data = data[:, 0]
    if np.issubdtype(data.dtype, np.integer):
        scale = float(np.iinfo(data.dtype).max)
        samples = data.astype(np.float64) / scale
    else:
        samples = data.astype(np.float64)
    return samples, int(rate)


def resolve_relay_calibration(setup: Any, device: Any) -> Any:
    """The production mic-calibration resolver for a v2 capture.

    Reuses ``correction_setup._relay_calibration_from_setup`` — the ONE point
    the room + legacy crossover relay flows already use to materialize the
    phone wizard's serial/upload/stored calibration choice as a stored
    ``CalibrationRecord`` (and persist it as the household default mic).
    Returns the record or ``None`` (phone mic / no calibration chosen).
    ``device`` is accepted for parity with the seam signature; the setup
    payload is the authoritative choice today.
    """
    from .correction_setup import _relay_calibration_from_setup

    del device  # the setup payload carries the phone's calibration choice
    return _relay_calibration_from_setup(
        dict(setup) if isinstance(setup, Mapping) else None
    )


def default_setup_calibration_for_v2() -> Any | None:
    """The v2 session's OPTIONAL household-mic prefill hint (W6.12).

    Every v2 capture logged ``crossover_v2_uncalibrated_capture`` even when
    the household had a resolvable stored mic (a UMIK-2 by serial, ingested
    via ``/correction/calibration/fetch``). Root cause: ``resolve_relay_calibration``
    is only as good as what the phone posts in ``setup.calibration`` — and a
    v2 capture-plan session has no calibration-picker screen of its own. The
    LEGACY per-driver crossover flow gets away with this because its
    calibration choice comes from the ``level_ramp`` level-match page the
    household visits FIRST in the same phone tab (the capture page's
    ``setupState`` module variable survives the in-tab hash navigation from
    that page into the driver sweeps that follow); v2 has no preceding
    level-match page (design: CHECK's own pilot pairs solve gain), so it
    never had a carrier for the same hint.

    Reuses ``correction_setup._default_setup_calibration_for_spec`` — the
    SAME household-mic-hint resolver the legacy level-match handlers already
    pass into ``build_level_ramp_spec``. Threaded into
    ``build_v2_session_spec``/``build_v2_verify_session_spec`` via their
    shared ``**spec_kwargs`` forward to ``build_crossover_sweep_spec``
    (W6.12 added the parameter there); the capture page applies it SILENTLY
    (no extra tap) when nothing has already been chosen for that page load.
    Fail-soft: any resolution miss yields no hint, never blocks session open.
    """
    from .correction_setup import _default_setup_calibration_for_spec

    try:
        return _default_setup_calibration_for_spec()
    except (OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_default_calibration_hint_failed",
            level=logging.WARNING,
        )
        return None


def bind_production_analyze(
    *,
    resolve_calibration: Callable[[Any, Any], Any] | None = resolve_relay_calibration,
    meta: dict[str, Any] | None = None,
) -> Callable[[Any, Any, Any, Any], Any]:
    """The real ``analyze`` seam: CaptureResult → ``analyze_program_capture``.

    Design §5.6.4 applies the mic cal to every gated response, so this binding
    resolves the calibration from the capture's phone-reported setup (the same
    ``_relay_calibration_from_setup`` machinery the legacy relay flows use)
    and threads BOTH the resolved curve and the conductor's declared geometry
    into ``analyze_program_capture``. When no calibration resolves, the
    analysis still runs — relative timing/level stay valid per the design —
    but the fact is never silent: a WARN ``event=`` fires and ``meta``
    (persisted with the session's evidence refs) records the per-phase
    ``{"applied": False}`` annotation.
    """

    def _analyze(program: Any, result: Any, priors: Any, geometry: Any) -> Any:
        from jasper.audio_measurement import program_analysis as _pa

        wav = getattr(result, "wav", result)
        samples, rate = _wav_bytes_to_samples(wav)
        record = None
        if resolve_calibration is not None:
            try:
                record = resolve_calibration(
                    getattr(result, "setup", None), getattr(result, "device", None)
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # A resolver failure downgrades to an annotated-uncalibrated
                # analysis, never a crashed capture — but it is logged.
                log_event(
                    logger,
                    "correction.crossover_v2_calibration_resolve_failed",
                    level=logging.WARNING,
                    phase=program.phase,
                )
                record = None
        curve = getattr(record, "curve", None)
        if record is not None and curve is None:
            # A bare CalibrationCurve (tests / future callers) is accepted too;
            # anything else stays None (annotated uncalibrated, never a crash).
            from jasper.audio_measurement.calibration import CalibrationCurve

            if isinstance(record, CalibrationCurve):
                curve = record
        if curve is None:
            log_event(
                logger,
                "correction.crossover_v2_uncalibrated_capture",
                level=logging.WARNING,
                phase=program.phase,
            )
        if meta is not None:
            meta.setdefault("calibration", {})[program.phase] = {
                "applied": curve is not None,
                "calibration_id": getattr(record, "calibration_id", None),
            }
        return _pa.analyze_program_capture(
            program,
            samples,
            rate,
            calibration=curve,
            geometry=geometry,
            priors=priors,
        )

    return _analyze


def open_v2_evidence_store(topology: Any) -> tuple[Any, str]:
    """Open a fresh v2 commissioning bundle + its exact evidence store (§5.6).

    Every v2 measurement session gets its own retention-bounded bundle under
    ``sessions_dir()`` (the same SC-4 bundle machinery the legacy flow uses),
    and every phase artifact is published through the store's write-once +
    tamper-checked-reopen path. Returns ``(store, bundle_session_id)``.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    info = open_bundle(topology, calibration_id="")
    if not isinstance(info, Mapping) or not info.get("session_id"):
        raise CrossoverV2Refused(
            "could not open a commissioning evidence bundle for this session"
        )
    session_id = str(info["session_id"])
    store = CommissioningEvidenceStore.open(
        Path(str(info["bundle_dir"])), expected_session_id=session_id
    )
    return store, session_id


def bind_evidence_publishers(
    store: Any, relay_session_id: str
) -> tuple[Callable[[Any, Mapping[str, Any]], None], Callable[[Any], None], dict[str, Any]]:
    """Real ``publish_check`` / ``publish_candidate`` seams (§5.6).

    CHECK publishes the ambient report + solved gain plan; MEASURE publishes
    the full candidate dict and re-opens it through
    ``MeasuredCrossoverCandidate.from_mapping`` — the same tamper check the
    apply path runs, so a candidate that cannot survive exact reopen never
    becomes reviewable. Artifact fingerprints land in the returned ``refs``
    mapping (persisted into the durable state for the status surface).
    """
    refs: dict[str, Any] = {"bundle_session_id": store.session_id}

    def publish_check(gain_plan: Any, ambient_report: Mapping[str, Any]) -> None:
        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/check.json",
            {
                "schema_version": 1,
                "kind": "jts_crossover_v2_check_evidence",
                "relay_session_id": relay_session_id,
                "gain_plan_db": dict(gain_plan.gain_db),
                "predicted_peak_dbfs": gain_plan.predicted_peak_dbfs,
                "snr_floor_ok": gain_plan.snr_floor_ok,
                "ambient_report": dict(ambient_report),
            },
        )
        refs["check_artifact"] = artifact.fingerprint

    def publish_candidate(candidate: Any) -> None:
        from jasper.active_speaker.measured_crossover_candidate import (
            MeasuredCrossoverCandidate,
        )

        artifact = store.publish_json_artifact(
            f"crossover_v2/{relay_session_id}/candidate.json",
            candidate.to_dict(),
        )
        reopened_raw = store.reopen_json_artifact(artifact)
        reopened = MeasuredCrossoverCandidate.from_mapping(reopened_raw)
        if reopened.fingerprint != candidate.fingerprint:
            raise RuntimeError(
                "published measured candidate changed on exact readback"
            )
        refs["candidate_artifact"] = artifact.fingerprint
        log_event(
            logger,
            "correction.crossover_v2_candidate_published",
            relay_session_id=relay_session_id,
            candidate_fingerprint=candidate.fingerprint,
            artifact_fingerprint=artifact.fingerprint,
        )

    return publish_check, publish_candidate, refs


def bind_production_play(
    *,
    run_async: Any,
    camilla_factory: Any,
    evidence_store: Any,
    relay_session_id: str,
    topology: Any,
    preset: Any,
    role_channels: Mapping[str, int],
    playback_device: str,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    declared_sensitivities: Mapping[str, float] | None = None,
    config_dir: str | None = None,
) -> Callable[[str, Any], None]:
    """The real ``play`` seam: program WAV → admitted playback through the DSP.

    CHECK/MEASURE render + publish the program WAV into the session's evidence
    bundle, emit the channel-routed program graph
    (``emit_active_speaker_program_config``), and ride
    :func:`jasper.active_speaker.program_playback.play_program` with the
    CamillaController seams from ``bind_program_playback_seams``; VERIFY plays
    its mono WAV through the APPLIED production graph (verified-aplay only —
    no graph load). Both run inside the mux measurement window so the
    correction lane actually reaches the speaker.

    ``config_dir`` defaults to the SAME
    :data:`jasper.active_speaker.staging.DEFAULT_CAMILLA_CONFIG_DIR` every
    sibling DSP writer (commissioning apply/verify, ``web_commissioning``,
    ``correction_setup``) locks against — W6 hardware run 3 finding F caught
    this binding still defaulting to the stale literal ``"/etc/camilladsp"``:
    two defects at once. (a) ``jasper-correction-web`` runs
    ``ProtectSystem=full`` with ``ReadWritePaths=/var/lib/jasper
    /var/lib/camilladsp`` only (see ``deploy/jasper-correction-web.service``),
    so opening ``/etc/camilladsp/.dsp_apply.lock`` raised EROFS 70 ms into the
    first play. (b) even under a writable ``/etc``, it would have been the
    WRONG lock identity — every other writer locks
    ``/var/lib/camilladsp/configs/.dsp_apply.lock``, so a real ``/etc``
    lock would not have serialized against them at all.

    ON-DEVICE: not exercised hardware-free; W6 validates acoustically.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_VERIFY,
        bind_program_playback_seams,
    )
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.program import write_program_wav

    resolved_config_dir = (
        config_dir if config_dir is not None else str(DEFAULT_CAMILLA_CONFIG_DIR)
    )

    def _play(phase: str, program: Any) -> None:
        bundle_dir = evidence_store.bundle_dir
        wav_rel = f"crossover_v2/{relay_session_id}/{phase}_program.wav"
        wav_path = Path(bundle_dir) / wav_rel
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        write_program_wav(wav_path, program)
        artifact = evidence_store.identify_artifact(wav_rel)

        async def _play_body() -> None:
            from jasper.active_speaker.program_playback import (
                verified_program_aplay,
            )

            if phase == PHASE_VERIFY:
                # The applied production graph IS the system under test —
                # no graph load, just the verified WAV into the lane.
                await verified_program_aplay(
                    bundle_dir, artifact, timeout_s=60.0
                )
                return
            from jasper.active_speaker.camilla_yaml import (
                emit_active_speaker_program_config,
            )
            from jasper.active_speaker.program_playback import play_program

            program_yaml = emit_active_speaker_program_config(
                preset,
                role_channels=dict(role_channels),
                playback_device=playback_device,
            )
            seams = bind_program_playback_seams(
                camilla_factory(),
                bundle_dir=str(bundle_dir),
                artifact=artifact,
                config_dir=resolved_config_dir,
                program=program,
                wav_path=str(wav_path),
                topology=topology,
                safety_profile=safety_profile,
                role_targets=role_targets,
                session_volume_db=session_volume_db,
                declared_sensitivities=declared_sensitivities,
            )
            await play_program(
                program,
                program_graph_yaml=program_yaml,
                session_volume_plan=session_volume_plan(),
                **seams,
            )

        async def _emit() -> None:
            # The session holds ONE measurement window for its whole life (W6.1
            # — see acquire_session_measurement_pause). ``measurement_window``
            # is exclusive/non-nestable, so nest-SKIP it here when the session
            # already holds it — registering this play task as the window's
            # abort target so an isolation-loss abort still stops the sweep.
            # Only fall back to a per-play window if the session pause is
            # somehow not held.
            if session_measurement_pause_held():
                await _play_under_session_pause(_play_body)
                return
            from jasper.correction import coordinator

            async with coordinator.measurement_window():
                await _play_body()

        run_async(_emit())

    return _play


# --------------------------------------------------------------------------- #
# the plan-runner host (S1a/S1c)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2VolumeHooks:
    """The session-volume lifecycle the runner drives (§5.5)."""

    open: Callable[[], Any]      # async
    close: Callable[[], Any]     # async
    abandon: Callable[[], Any]   # async


# W6 hardware run 3 finding H: the catch-all cleanup arm below posts a
# terminal host event (§5.10's ``capture_result``) so the phone stops
# recording into silence, then purges the relay session. Purging
# immediately after can race the phone's very next poll — the driver saw a
# bare 404 on the session's own status endpoint ~1 s after the terminal
# event, because the session was already gone. This grace gives the just-
# posted event a bounded window to actually reach the phone (over the
# public relay) before the session disappears; it does not delay the
# household's volume restore, which stays immediate.
TERMINAL_FAILURE_PURGE_GRACE_S = 3.0


def build_v2_run_and_consume(
    conductor: Any,
    *,
    volume: V2VolumeHooks,
    stop_event: threading.Event,
    stop_lock: Any,
    evidence_refs: Mapping[str, Any] | None = None,
    poll_interval_s: float | None = None,
    timeout_s: float | None = None,
    first_begin_timeout_s: float | None = None,
) -> Callable[[Any, Any], Any]:
    """The async ``run_and_consume(client, pi_session)`` for one v2 session.

    Mirrors ``build_crossover_relay_plan_run_and_consume``'s thread model: the
    REAL :func:`jasper.capture_relay.session.run_capture_plan` runs on a
    worker thread (``asyncio.to_thread``), the awaiting task shields it
    through cancellation (Stop drains the runner before purging), and the
    relay session is purged on every exit path.

    Host-owned error mapping (S1c):

    * ``CaptureTimeout`` / ``CaptureAborted`` / ``RelayError`` / ``OSError`` /
      generic ``CaptureFailed`` — relay-session death ⇒ ``relay_timeout``
      failure state + volume ABANDON (the §5.5 walked-away guarantee). The
      ``OSError`` here is genuinely the relay TRANSPORT (e.g.
      ``run_capture_plan``'s poll loop reaching an unreachable host) — a
      LOCAL play/analyze seam OSError never reaches this arm: ``on_armed``/
      ``consume`` below convert it to :class:`CrossoverV2LocalSeamError`
      first (W6 hardware run 3 finding G), so it falls through to the
      catch-all arm's ``internal_error`` instead.
    * ``CaptureBeginRefused`` — the conductor already recorded the phase's own
      failure code; persist it + abandon.
    * ``CaptureStopped`` / cancellation — expected control flow: abandon the
      volume, no failure code.
    * ANY other ``Exception`` — the W6.1 catch-all cleanup arm: the seams
      raise open-endedly (``CamillaUnavailable`` is a bare Exception), so
      every non-relay failure posts a terminal host event, persists
      ``program_unplayable`` (program/admission/flow classes) or
      ``internal_error`` (everything else — including
      ``CrossoverV2LocalSeamError``), abandons the volume (releasing the
      session measurement pause), waits a bounded grace period (finding H —
      the just-posted terminal host event must reach the phone before the
      relay session is purged out from under its next poll), purges, and
      re-raises.
    * Plan complete with VERIFY accepted ⇒ CLOSE (exact restore); a completed
      plan that did not reach done (attempt budget exhausted) abandons.
    """

    async def _run_and_consume(client: Any, pi_session: Any) -> None:
        from jasper.capture_relay.client import RelayError
        from jasper.capture_relay.session import (
            HOST_PHASE_CAPTURE_RESULT,
            HOST_PHASE_CAPTURE_SET_EXHAUSTED,
            CaptureAborted,
            CaptureBeginRefused,
            CaptureFailed,
            CaptureStopped,
            CaptureTimeout,
            purge,
            run_capture_plan,
        )
        from jasper.active_speaker.crossover_v2_flow import (
            PHASE_DONE,
            REASON_INTERNAL_ERROR,
            REASON_PROGRAM_UNPLAYABLE,
            REASON_REGISTRY,
            REASON_RELAY_TIMEOUT,
            V2_FIRST_BEGIN_TIMEOUT_S,
            TRANSIENT_AUTO_RETRY_CODES,
            CrossoverV2FlowError,
        )
        from jasper.active_speaker.program_admission import ProgramAdmissionError
        from jasper.active_speaker.program_playback import ProgramPlaybackError
        from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
        from jasper.correction.coordinator import MeasurementWindowError

        def authorize(index: int, attempt: int, entry: Any = None) -> None:
            with stop_lock:
                if stop_event.is_set():
                    raise CaptureStopped("capture stopped")
            conductor.authorize_begin(index, attempt, entry)

        def _post_sweep_phase_best_effort(phase: str) -> None:
            """Tell the phone playback is starting/finished (§5.10 progress).

            The capture page's ``waitForSweepComplete``
            (``capture-page/js/main.js``) polls ``host_event.phase`` for
            ``"sweep_started"`` / ``"sweep_complete"`` around its own play
            wait and otherwise sits until ITS OWN timeout elapses — the v2
            runner posted neither (W6 run 5), so a real phone could never
            complete a v2 capture. Mirrors the legacy per-driver capture-plan
            flow's ``post_phase`` around its own play call
            (``correction_crossover_flow.py``). Best-effort like that
            sibling: a transient post failure here is a progress-only miss,
            not a capture failure — the existing terminal host event (or the
            phone's own wait timeout) still resolves the phone's wait on any
            real failure.
            """
            armed = conductor.armed_capture
            index, attempt = armed if armed is not None else (None, None)
            try:
                client.post_host_event(
                    pi_session.session_id,
                    pi_session.pull_token,
                    {"phase": phase, "index": index, "attempt": attempt},
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 sweep progress host-event post failed", exc_info=True
                )

        def on_armed(state: Any) -> None:
            if stop_event.is_set():
                raise CaptureStopped("capture stopped")
            # Finding G: on_armed's ``conductor.on_armed`` → ``seams.play`` is a
            # LOCAL seam (the DSP writer lock, CamillaController) — an OSError
            # here (e.g. EROFS opening the lock file) is not a relay-transport
            # death and must not be caught by the relay-death arm below.
            _post_sweep_phase_best_effort("sweep_started")
            try:
                conductor.on_armed(state)
            except OSError as exc:
                raise CrossoverV2LocalSeamError(str(exc)) from exc
            _post_sweep_phase_best_effort("sweep_complete")

        def consume(index: int, attempt: int, result: Any, entry: Any = None):
            # Same local-seam boundary as on_armed, for consume_capture's
            # analyze seam.
            try:
                verdict = conductor.consume_capture(index, attempt, result, entry)
            except OSError as exc:
                raise CrossoverV2LocalSeamError(str(exc)) from exc
            code = verdict.get("code") if isinstance(verdict, Mapping) else None
            persist_conductor_state(
                conductor,
                failure_code=code if not verdict.get("accepted") else None,
                evidence=evidence_refs,
            )
            return verdict

        async def _purge_best_effort() -> None:
            try:
                await asyncio.to_thread(purge, client, pi_session)
            except (OSError, RuntimeError, ValueError):
                logger.warning("v2 relay purge failed", exc_info=True)

        async def _abandon_best_effort() -> None:
            try:
                await volume.abandon()
            except (OSError, RuntimeError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_v2_volume_abandon_failed",
                    level=logging.CRITICAL,
                )

        async def _post_terminal_failure_host_event(code: str) -> None:
            """Tell the phone the session is over so it stops waiting (§5.10).

            A play-seam failure escapes ``run_capture_plan`` WITHOUT posting a
            capture verdict, so the phone records into silence and then polls
            ``capture_result`` forever (W6.1 hardware run 2 froze at
            ``capture_authorized``). Address a terminal ``capture_result``
            (accepted=false, carrying the §5.10 reason so the phone can render
            the failure screen) to the armed capture; fall back to
            ``capture_set_exhausted`` when no capture was armed. Best-effort —
            the operator wizard also shows the persisted failure.
            """
            spec = REASON_REGISTRY.get(code)
            armed = conductor.armed_capture
            if armed is not None and spec is not None:
                index, attempt = armed
                event: dict[str, Any] = {
                    "phase": HOST_PHASE_CAPTURE_RESULT,
                    "index": index,
                    "attempt": attempt,
                    "accepted": False,
                    "code": spec.code,
                    "template": spec.template,
                    "reason": spec.message or spec.banner,
                    "banner": spec.banner,
                    "auto_retry": spec.code in TRANSIENT_AUTO_RETRY_CODES,
                }
            else:
                event = {"phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED}
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    event,
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 terminal host-event post failed", exc_info=True
                )

        async def _post_session_over_host_event() -> None:
            """Tell the phone the whole SESSION ended so its deferred-retry loop
            stops waiting (W6.10 blocker #3).

            A watchdog collapse during the "waiting for apply" REVIEW hold
            (``CaptureTimeout``) otherwise left the phone re-posting the same
            ``begin_capture`` against a still-200 relay session with NO terminal
            signal — it sat on the hold screen forever (Chrome round 2: "the
            phone saw nothing"). Unlike ``_post_terminal_failure_host_event``
            this is session-level (``capture_set_exhausted``), not addressed to
            the last-armed capture (MEASURE was accepted — a per-index
            ``capture_result`` there would misreport it): the collapse is not a
            per-capture verdict. Best-effort — the purge-driven 404 the phone
            reads as ``deadSession`` is the backstop, and this post fails
            harmlessly when the failure was the relay transport itself.
            """
            try:
                await asyncio.to_thread(
                    client.post_host_event,
                    pi_session.session_id,
                    pi_session.pull_token,
                    {"phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED},
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "v2 session-over host-event post failed", exc_info=True
                )

        try:
            opened = await volume.open()
        except (SessionVolumePlanError, MeasurementWindowError) as exc:
            # volume.open() raised BEFORE the capture loop owns cleanup — the
            # relay session is already minted (run 2's retry leaked one here
            # when the prior session's volume state was still open, firing
            # SessionVolumePlanError). Purge it best-effort before surfacing so
            # it cannot linger to worker TTL; the volume hook already released
            # any measurement pause it took.
            log_event(
                logger,
                "correction.crossover_v2_volume_open_failed",
                level=logging.WARNING,
                reason=type(exc).__name__,
            )
            await _purge_best_effort()
            raise CaptureFailed(
                "the measurement volume could not be opened"
            ) from exc
        opened_value = getattr(opened, "value", opened)
        if opened is not None and str(opened_value) != "opened":
            # The plan drained itself (emergency attenuation / failure); the
            # recovery screen keys on needs_recovery via the status block.
            # The freshly-minted relay session must not linger to worker TTL
            # when no capture will ever run against it.
            await _purge_best_effort()
            raise CaptureFailed(
                "the fixed measurement volume could not be confirmed"
            )
        plan_kwargs: dict[str, Any] = {}
        if poll_interval_s is not None:
            plan_kwargs["poll_interval_s"] = poll_interval_s
        if timeout_s is not None:
            plan_kwargs["timeout_s"] = timeout_s
        # The FIRST begin gets the wider v2 placement-reading budget (fold-in);
        # every later window (arm/upload/between-capture) keeps the tight
        # per-phase backstop. The REVIEW hold's own rescope lives in the runner.
        plan_kwargs["first_begin_timeout_s"] = (
            first_begin_timeout_s if first_begin_timeout_s is not None
            else V2_FIRST_BEGIN_TIMEOUT_S
        )
        capture_task = asyncio.create_task(
            asyncio.to_thread(
                run_capture_plan,
                client,
                pi_session,
                authorize_begin=authorize,
                on_armed=on_armed,
                consume_capture=consume,
                stop_requested=stop_event.is_set,
                **plan_kwargs,
            )
        )
        try:
            try:
                await asyncio.shield(capture_task)
            except asyncio.CancelledError:
                stop_event.set()
                while not capture_task.done():
                    try:
                        await asyncio.shield(capture_task)
                    except asyncio.CancelledError:
                        continue
                    except (OSError, RuntimeError, ValueError):
                        break
                if capture_task.done() and not capture_task.cancelled():
                    capture_task.exception()
                await _abandon_best_effort()
                await _purge_best_effort()
                raise
        except CaptureStopped:
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        except CaptureBeginRefused:
            # The conductor's own budget refusal — its failure code is already
            # in _last_reason; persist the terminal state.
            code = conductor.last_failure_code or REASON_RELAY_TIMEOUT
            _persist_terminal_failure(conductor, code)
            await _abandon_best_effort()
            await _purge_best_effort()
            raise
        except (CaptureTimeout, CaptureAborted, CaptureFailed, RelayError, OSError):
            # Relay-session death (§5.10): relay_timeout ⇒ session restart; the
            # walked-away user's volume is always drained. Tell the phone the
            # session is over BEFORE purging (W6.10 blocker #3) — mirror the
            # catch-all arm's terminal-then-grace-then-purge so a watchdog
            # collapse during the REVIEW hold reaches the phone's deferred-retry
            # loop instead of leaving it polling a still-live session forever.
            await _post_session_over_host_event()
            _persist_terminal_failure(conductor, REASON_RELAY_TIMEOUT)
            await _abandon_best_effort()
            await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
            await _purge_best_effort()
            raise
        except Exception as exc:  # noqa: BLE001 — cleanup-and-reraise, see below
            # CATCH-ALL cleanup arm (W6.1 gate ruling). The seams raise
            # open-endedly — CamillaUnavailable is a bare Exception (a DSP
            # wedge in load/restore escaped the previously-enumerated arms:
            # volume left active, relay session leaked, phone frozen at
            # capture_authorized), analyze/emit raise ValueError/RuntimeError,
            # the held measurement window raises MeasurementWindowError — so
            # ANY non-relay failure gets the same honest cleanup: tell the
            # phone (still polling capture_result), persist a terminal
            # failure, drain the volume (whose hook also releases the session
            # measurement pause), purge the relay session, then RE-RAISE so
            # the outer relay net still logs and flips /status.relay to
            # failed. Program-side classes keep their distinct
            # program_unplayable code; everything else is internal_error.
            code = (
                REASON_PROGRAM_UNPLAYABLE
                if isinstance(
                    exc,
                    (
                        ProgramPlaybackError,
                        ProgramAdmissionError,
                        CrossoverV2FlowError,
                    ),
                )
                else REASON_INTERNAL_ERROR
            )
            await _post_terminal_failure_host_event(code)
            _persist_terminal_failure(conductor, code)
            await _abandon_best_effort()
            # Finding H: give the just-posted terminal host event a bounded
            # grace window to reach the phone before the session is purged
            # out from under its next poll. Volume restore above stays
            # immediate — only the purge waits.
            await asyncio.sleep(TERMINAL_FAILURE_PURGE_GRACE_S)
            await _purge_best_effort()
            raise
        # Plan finished without a transport failure.
        done = conductor.current_phase == PHASE_DONE
        persist_conductor_state(
            conductor,
            failure_code=None if done else conductor.last_failure_code,
            evidence=evidence_refs,
        )
        if done:
            try:
                await volume.close()
            except (OSError, RuntimeError, ValueError):
                log_event(
                    logger,
                    "correction.crossover_v2_volume_close_failed",
                    level=logging.CRITICAL,
                )
        else:
            await _abandon_best_effort()
        await _purge_best_effort()

    return _run_and_consume


# --------------------------------------------------------------------------- #
# conductor context resolution (production inputs)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2ConductorContext:
    """Everything the production conductor needs, resolved from live status."""

    preset: Any
    roles_bands: tuple
    fc_hz: float
    driver_caps_dbfs: dict[str, float]
    role_targets: dict[str, str]
    safety_profile: Mapping[str, Any]
    session_volume_db: float
    driver_spacing_m: float
    topology: Any
    playback_device: str
    role_channels: dict[str, int]
    # Per-role declared datasheet sensitivities from the design draft's
    # declaration (the one owner of that fact — W6.5). Threaded into every cap
    # resolution AND the play-time readmission so the composed levels and the
    # admission gate can never disagree about a derived HF ceiling.
    declared_sensitivities: dict[str, float] = field(default_factory=dict)


def ensure_crossover_preview_ready() -> dict[str, Any]:
    """Ensure a ready crossover preview exists before a v2 session reads one.

    ``/sound/``'s Preview button was the ONLY historical writer of
    ``active_speaker_crossover_preview.json``; the v2 flow never called it, so
    a household that went straight to ``/correction/`` without visiting
    ``/sound/`` first baked its MEASURE candidate's ``source_preset`` against
    the generic bundled-preset fallback (:func:`~jasper.active_speaker.commission_wiring.resolve_capture_preset`'s
    no-preview branch) — which then can NEVER match a preview generated later,
    so Apply refuses ``measured_candidate_preset_mismatch`` forever. This is
    called at the top of :func:`resolve_conductor_context` — the one place
    both session-open (:func:`prepare_v2_session`) and the verify re-arm
    (:func:`prepare_v2_verify`) resolve the design draft/topology — so the
    fallback branch is never reached from a v2 entry point again.

    Reuses the SAME generator ``/sound/`` drives
    (:func:`~jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft`,
    itself a thin wrapper around :func:`~jasper.active_speaker.crossover_preview.save_crossover_preview`)
    rather than reimplementing preview generation. Idempotent: an existing
    preview that is already ``ready_for_protected_staging`` for the CURRENT
    design draft (the freshness/fingerprint check already built into
    :func:`~jasper.active_speaker.crossover_preview.load_crossover_preview`)
    is left byte-untouched — reused, not regenerated. Anything else (absent,
    stale, or blocked) is regenerated once; if the fresh attempt still cannot
    reach ``ready_for_protected_staging`` (an unconfirmed safety profile, a
    blocked design draft, etc.), this raises a named :class:`CrossoverV2Refused`
    pointing at ``/sound/`` instead of leaving the surprise for apply time.
    """
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.web_commissioning import (
        regenerate_crossover_preview_from_current_draft,
    )

    preview = load_crossover_preview(current_design_draft=load_design_draft())
    outcome = "reused"
    if preview.get("status") != "ready_for_protected_staging":
        preview = regenerate_crossover_preview_from_current_draft()
        outcome = (
            "generated"
            if preview.get("status") == "ready_for_protected_staging"
            else "refused"
        )
    log_event(
        logger,
        "correction.crossover_v2_preview_ensured",
        outcome=outcome,
        preview_status=str(preview.get("status")),
    )
    if outcome == "refused":
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in (preview.get("issues") or [])
            if isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        ]
        raise CrossoverV2Refused(
            "the crossover preview is not ready for measurement; finish "
            "speaker setup at /sound/ first"
            + (": " + "; ".join(messages[:2]) if messages else "")
        )
    return preview


def resolve_conductor_context(status: Mapping[str, Any]) -> V2ConductorContext:
    """Resolve preset/bands/caps/targets/volume from live status + topology.

    Fail-closed: every missing input is a :class:`CrossoverV2Refused` naming
    what to finish first — never a guessed default.
    """
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.active_speaker.crossover_v2_flow import derive_session_volume_db
    from jasper.active_speaker.design_draft import (
        declared_driver_sensitivities,
        load_design_draft,
    )
    from jasper.active_speaker.excitation_safety_plan import (
        ExcitationSafetyPlanError,
        resolve_driver_excitation_ceilings,
    )
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.audio_measurement.program import RoleBand
    from jasper.output_topology import load_output_topology

    if not status.get("active"):
        raise CrossoverV2Refused(
            "this speaker has no active crossover to measure"
        )
    setup = status.get("setup")
    if not isinstance(setup, Mapping) or setup.get("status") != "ready":
        raise CrossoverV2Refused(
            "protected speaker setup is not ready; finish it before measuring"
        )
    topology = load_output_topology()
    # Ensure a ready crossover preview BEFORE resolving the capture preset —
    # otherwise resolve_capture_preset's no-preview fallback silently bakes
    # the generic bundled preset into every MEASURE candidate (see docstring).
    ensure_crossover_preview_ready()
    preset = resolve_capture_preset(topology)
    if preset.way_count != 2:
        raise CrossoverV2Refused("the v2 conductor flow is scoped to 2-way presets")
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    if not isinstance(safety_profile, Mapping):
        raise CrossoverV2Refused(
            "driver safety limits are not confirmed; finish the driver details "
            "in speaker setup"
        )
    targets_raw = status.get("targets")
    drivers = (
        targets_raw.get("drivers") if isinstance(targets_raw, Mapping) else None
    ) or []
    role_targets: dict[str, str] = {}
    for target in drivers:
        if isinstance(target, Mapping):
            role = str(target.get("role") or "").lower()
            fingerprint = str(target.get("target_fingerprint") or "")
            if role and fingerprint:
                role_targets[role] = fingerprint
    if set(role_targets) != {"woofer", "tweeter"}:
        raise CrossoverV2Refused(
            "the woofer and tweeter measurement targets are not both active"
        )
    # The declaration's per-role datasheet sensitivities (the one owner of
    # that fact — W6.5), threaded into every cap resolution below.
    declared_sensitivities = declared_driver_sensitivities(draft)
    roles_bands = []
    caps: dict[str, float] = {}
    for channel, role in enumerate(("woofer", "tweeter")):
        try:
            # program_admission=True: this context exists solely to serve the
            # admission-gated CHECK/MEASURE programs, whose channel routing
            # carries each driver's crossover filter (the tweeter's protective
            # HP included) by construction — the proven-HP path, the same
            # justification as session_volume_plan. Without it the W6.5
            # derived HF ceiling is inert exactly where it matters: these
            # context caps clamp every composed level (CHECK pilot bases,
            # MEASURE back_off_gain, VERIFY min(caps)).
            band, cap = resolve_driver_excitation_ceilings(
                safety_profile,
                role_targets[role],
                program_admission=True,
                declared_sensitivities=declared_sensitivities,
            )
        except (ExcitationSafetyPlanError, ValueError) as exc:
            raise CrossoverV2Refused(
                f"the {role}'s safe excitation limits could not be resolved"
            ) from exc
        roles_bands.append(RoleBand(role, channel, band))
        caps[role] = float(cap)
    region = preset.crossover_regions[0]
    fc_hz = float(region.fc_hz)
    session_volume_db = derive_session_volume_db(
        safety_profile,
        [role_targets["woofer"], role_targets["tweeter"]],
        declared_sensitivities=declared_sensitivities,
    )
    playback_device, _playback_device_source = resolve_active_playback_device(
        topology
    )
    playback_device = str(playback_device or "")
    if not playback_device:
        raise CrossoverV2Refused(
            "the active output device is not declared; finish speaker setup"
        )
    return V2ConductorContext(
        preset=preset,
        roles_bands=tuple(roles_bands),
        fc_hz=fc_hz,
        driver_caps_dbfs=caps,
        role_targets=role_targets,
        safety_profile=safety_profile,
        session_volume_db=session_volume_db,
        # W6 CHECKLIST ITEM: driver_spacing_m stays 0.0 until a declared
        # woofer↔tweeter spacing input exists (topology/preset carry none
        # today), so the §3.2 parallax correction is INERT — the analysis
        # subtracts nothing. Do not assume VERIFY covers this: a missing
        # parallax correction is SELF-CANCELLING at the mic position (the
        # same geometric excess is baked into both MEASURE and VERIFY), so
        # VERIFY passes while the LISTENING POSITION carries the full error
        # (~23° at 2 kHz for 15 cm spacing measured at 1 m).
        # W6 CHECKLIST ITEM (pre-existing): a deliberate household volume
        # action mid-session (dial / voice "louder" / :8780 HTTP) still moves
        # the CamillaDSP main volume — the session measurement pause holds off
        # the idle reconciler, not VolumeCoordinator writes. W6 validation
        # runs hands-off; a session-long volume guard is a follow-up.
        driver_spacing_m=0.0,
        topology=topology,
        playback_device=playback_device,
        role_channels={"woofer": 0, "tweeter": 1},
        declared_sensitivities=declared_sensitivities,
    )


# --------------------------------------------------------------------------- #
# endpoint preparation (S1a/S1d)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class V2PreparedSession:
    """What the correction_setup dispatch needs to host one v2 relay session."""

    label: str
    open: Callable[..., Any]
    run_and_consume: Callable[[Any, Any], Any]
    request_stop: Callable[[], None]


def _volume_hooks(camilla_factory: Any, context: V2ConductorContext) -> V2VolumeHooks:
    plan = session_volume_plan()
    # The shared IO pair converts CamillaUnavailable (a bare Exception) into
    # RuntimeError, which plan.open's internal catches and set_and_confirm's
    # fail-closed contract actually cover — a DSP wedge during open then drains
    # to a non-OPENED result instead of raising an unhandled exception past the
    # runner (the W6.1 gate's escape class).
    _set, _get = _session_volume_io(camilla_factory)

    async def _open() -> Any:
        # Hold voice paused for the WHOLE session BEFORE setting the volume, so
        # the idle reconciler cannot revert the measurement volume in the gap
        # before the first play (W6.1). If the volume does not actually open —
        # a non-OPENED result OR any raise — release the pause in the finally
        # so a failed open never strands voice paused.
        await acquire_session_measurement_pause()
        opened: Any = None
        try:
            opened = await plan.open(context.session_volume_db, _set, _get)
        finally:
            if str(getattr(opened, "value", opened)) != "opened":
                await release_session_measurement_pause()
        return opened

    async def _close() -> Any:
        try:
            return await plan.close(_set, _get)
        finally:
            await release_session_measurement_pause()

    async def _abandon() -> Any:
        try:
            return await plan.abandon(_set, _get)
        finally:
            await release_session_measurement_pause()

    return V2VolumeHooks(open=_open, close=_close, abandon=_abandon)


def prepare_v2_session(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> V2PreparedSession:
    """Prepare the ``POST /crossover/v2/session`` relay hosting (S1a).

    Gates (fail-closed, before any relay registration): the flow selector, the
    volume-recovery gate (``needs_recovery`` — the W2 ruling), and the
    conductor-context resolution. Hydration (S1d): the durable state is
    hydrated through :meth:`CrossoverV2Conductor.hydrate` with the NEW relay
    session id — a prior session's CHECK/MEASURE evidence is invalidated per
    §5.6 (and logged); the fresh session starts at CHECK.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2Conductor,
        V2ConductorSnapshot,
        V2FlowSeams,
        build_v2_session_spec,
    )
    from jasper.capture_relay import correction_adapter

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before starting "
            "a new session"
        )
    # W6.1 E1/E3-at-open: force-drain a stale-active (ceiling) and reset a
    # residual owned-active leftover so plan.open() starts clean — the silent
    # 200→adapter_failed loop happened when a prior session left the volume
    # active and open() then refused. Runs AFTER the needs_recovery gate (which
    # refused a crash-active / unresolved state toward the recover screen), then
    # re-gates in case a drain here could not confirm and latched unresolved.
    reconcile_session_volume_for_new_session(run_async, camilla_factory)
    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before starting "
            "a new session"
        )
    context = resolve_conductor_context(status)
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()

    prior_raw = load_v2_state()
    prior_snapshot = (
        V2ConductorSnapshot(
            session_id=str(prior_raw.get("session_id") or ""),
            accepted_phases=tuple(prior_raw.get("accepted_phases") or ()),
            applied=bool(prior_raw.get("applied")),
            gain_plan_db=prior_raw.get("gain_plan_db"),
        )
        if isinstance(prior_raw, Mapping)
        else None
    )

    holder: dict[str, Any] = {}

    def _open(client: Any, base: str, capture_origin: str, return_url: str) -> Any:
        spec = build_v2_session_spec(
            context.roles_bands,
            context.fc_hz,
            acknowledgement_binding=acknowledgement_binding,
            default_setup_calibration=default_setup_calibration_for_v2(),
        ).with_return_url(return_url)
        rc = correction_adapter.open_capture(
            client,
            spec,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )
        # The conductor + publishers bind to the MINTED relay session id.
        relay_session_id = rc.pi_session.session_id
        publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            declared_sensitivities=context.declared_sensitivities,
        )
        conductor = CrossoverV2Conductor.hydrate(
            prior_snapshot,
            session_id=relay_session_id,
            source_preset=context.preset,
            roles_bands=context.roles_bands,
            fc_hz=context.fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=V2FlowSeams(
                play=play,
                analyze=bind_production_analyze(meta=refs),
                publish_check=publish_check,
                publish_candidate=publish_candidate,
                apply_complete=_applied_gate,
            ),
            driver_spacing_m=context.driver_spacing_m,
        )
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = build_v2_run_and_consume(
            conductor,
            volume=_volume_hooks(camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            evidence_refs=refs,
        )
        return rc

    async def _run(client: Any, pi_session: Any) -> None:
        await holder["run"](client, pi_session)

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    return V2PreparedSession(
        label=V2_RELAY_KIND_SESSION,
        open=_open,
        run_and_consume=_run,
        request_stop=_request_stop,
    )


def prepare_v2_verify(
    raw: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> V2PreparedSession:
    """Prepare ``POST /crossover/v2/verify`` — the §5.2 re-verify re-arm.

    Requires a durable post-apply state (MEASURE accepted + applied). Opens a
    NEW relay session hosting a 1-entry verify-only plan; the conductor is
    rebuilt in verify-only mode (CHECK/MEASURE marked accepted, applied,
    verify priors rehydrated from the durable state), with relay index 1
    mapped to VERIFY.
    """
    import numpy as np

    from jasper.active_speaker.crossover_v2_flow import (
        PHASE_CHECK,
        PHASE_MEASURE,
        PHASE_VERIFY,
        CrossoverV2Conductor,
        V2FlowSeams,
        build_v2_verify_session_spec,
    )
    from jasper.capture_relay import correction_adapter

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    if session_volume_plan().needs_recovery:
        raise CrossoverV2Refused(
            "the measurement volume needs recovery; recover it before verifying"
        )
    state = load_v2_state()
    if not state or not state.get("applied"):
        raise CrossoverV2Refused(
            "verification needs an applied measured crossover; measure and "
            "apply first"
        )
    context = resolve_conductor_context(status)
    evidence_store, _bundle_id = open_v2_evidence_store(context.topology)
    priors_raw = state.get("verify_priors") or {}
    sum_raw = priors_raw.get("predicted_sum") if isinstance(priors_raw, Mapping) else None
    predicted_sum = None
    if isinstance(sum_raw, Mapping) and sum_raw.get("freqs_hz"):
        predicted_sum = (
            np.asarray(sum_raw["freqs_hz"], dtype=float),
            np.asarray(sum_raw["magnitude_db"], dtype=float),
        )
    gate_ms = (
        priors_raw.get("gate_window_ms") if isinstance(priors_raw, Mapping) else None
    )
    acknowledgement_binding = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    stop_lock = threading.Lock()
    holder: dict[str, Any] = {}

    def _open(client: Any, base: str, capture_origin: str, return_url: str) -> Any:
        spec = build_v2_verify_session_spec(
            context.fc_hz,
            acknowledgement_binding=acknowledgement_binding,
            default_setup_calibration=default_setup_calibration_for_v2(),
        ).with_return_url(return_url)
        rc = correction_adapter.open_capture(
            client,
            spec,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )
        relay_session_id = rc.pi_session.session_id
        _publish_check, publish_candidate, refs = bind_evidence_publishers(
            evidence_store, relay_session_id
        )
        play = bind_production_play(
            run_async=run_async,
            camilla_factory=camilla_factory,
            evidence_store=evidence_store,
            relay_session_id=relay_session_id,
            topology=context.topology,
            preset=context.preset,
            role_channels=context.role_channels,
            playback_device=context.playback_device,
            safety_profile=context.safety_profile,
            role_targets=context.role_targets,
            session_volume_db=context.session_volume_db,
            declared_sensitivities=context.declared_sensitivities,
        )
        conductor = CrossoverV2Conductor(
            session_id=relay_session_id,
            source_preset=context.preset,
            roles_bands=context.roles_bands,
            fc_hz=context.fc_hz,
            driver_caps_dbfs=context.driver_caps_dbfs,
            session_volume_db=context.session_volume_db,
            seams=V2FlowSeams(
                play=play,
                analyze=bind_production_analyze(meta=refs),
                publish_check=_publish_check,
                publish_candidate=publish_candidate,
                apply_complete=_applied_gate,
            ),
            driver_spacing_m=context.driver_spacing_m,
            accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
            applied=True,
            gain_plan_db=state.get("gain_plan_db"),
            index_phase_map={1: PHASE_VERIFY},
            measure_predicted_sum=predicted_sum,
            measure_gate_window_ms=(
                float(gate_ms) if isinstance(gate_ms, (int, float)) else None
            ),
        )
        # Keep the durable candidate/applied facts; rebind the session id.
        persist_conductor_state(conductor, failure_code=None, evidence=refs)
        holder["run"] = build_v2_run_and_consume(
            conductor,
            volume=_volume_hooks(camilla_factory, context),
            stop_event=stop_event,
            stop_lock=stop_lock,
            evidence_refs=refs,
        )
        return rc

    async def _run(client: Any, pi_session: Any) -> None:
        await holder["run"](client, pi_session)

    def _request_stop() -> None:
        with stop_lock:
            stop_event.set()

    return V2PreparedSession(
        label=V2_RELAY_KIND_VERIFY,
        open=_open,
        run_and_consume=_run,
        request_stop=_request_stop,
    )


# --------------------------------------------------------------------------- #
# apply (the existing baseline transaction, W4 seam)
# --------------------------------------------------------------------------- #


def handle_v2_apply(
    raw: Mapping[str, Any],
    run_async: Any,
    camilla_factory: Any,
) -> dict[str, Any]:
    """POST /crossover/v2/apply — apply the reviewed measured candidate.

    Reopens the published candidate artifact through
    ``MeasuredCrossoverCandidate.from_mapping`` (the tamper check), gates on
    the reviewed ``expected_candidate_fingerprint``, and rides the EXISTING
    atomic apply-with-rollback transaction —
    ``apply_baseline_profile(measured_candidate=...)`` (the W4 seam) — then
    marks the durable v2 state applied, which arms the deferred VERIFY.

    W6 run-6 Blocker M: the seam's own freshness guard
    (``apply_baseline_profile``'s ``expected_candidate_fingerprint``) compares
    against ``baseline_candidate_fingerprint`` — the COMPOSED baseline
    candidate's own identity — never the MEASURED candidate's fingerprint
    this endpoint reviews with the household. Forwarding the measured
    fingerprint straight through made every apply refuse
    ``baseline_candidate_fingerprint_mismatch``, unconditionally. This host
    translates between the two vocabularies: it composes the baseline
    candidate read-only first (``build_baseline_profile_candidate(...,
    write=False)`` — the exact builder the seam itself uses), asserts that
    composition is still bound to the reviewed MEASURED candidate (preserving
    the review-freshness guarantee at the measured-candidate level), then
    passes the COMPOSED candidate's own ``candidate_fingerprint`` through to
    the seam.
    """
    from jasper.active_speaker.baseline_profile import (
        apply_baseline_profile,
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverCandidate,
        MeasuredCrossoverCandidateError,
    )
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.output_topology import load_output_topology

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    expected = str(raw.get("expected_candidate_fingerprint") or "")
    if not expected:
        raise CrossoverV2Refused("expected_candidate_fingerprint is required")
    state = load_v2_state()
    candidate_ref = (state or {}).get("candidate")
    evidence = (state or {}).get("evidence") or {}
    if not isinstance(candidate_ref, Mapping) or not candidate_ref.get("fingerprint"):
        raise CrossoverV2Refused(
            "no measured crossover candidate is ready to apply; measure first"
        )
    if str(candidate_ref["fingerprint"]) != expected:
        raise CrossoverV2Refused(
            "the reviewed crossover is no longer current; review the newest "
            "measurement before applying"
        )
    candidate_payload = raw.get("candidate")
    if not isinstance(candidate_payload, Mapping):
        # Endpoint contract: the wizard posts only the fingerprint; the host
        # reopens the artifact from the evidence bundle recorded at publish.
        candidate_payload = _reopen_candidate_artifact(state, evidence)
    try:
        candidate = MeasuredCrossoverCandidate.from_mapping(candidate_payload)
    except MeasuredCrossoverCandidateError as exc:
        raise CrossoverV2Refused(str(exc)) from exc
    if candidate.fingerprint != expected:
        raise CrossoverV2Refused(
            "the persisted candidate does not match the reviewed fingerprint"
        )
    topology = load_output_topology()
    draft = load_design_draft(topology=topology)
    preview = load_crossover_preview(current_design_draft=draft)
    measurements = load_measurement_state(topology)

    # Blocker M translation: compose read-only (the seam's own build_candidate
    # closure re-derives this identically under its writer lock, so this is a
    # deterministic recompose, not a second opinion) and confirm the
    # composition is still bound to the reviewed measured candidate before
    # asking the seam to apply anything.
    reviewed_baseline = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
        tuning_owner="automatic",
        measured_candidate=candidate,
    )
    reviewed_measured_fingerprint = str(
        (reviewed_baseline.get("source") or {}).get("measured_candidate_fingerprint")
        or ""
    )
    if reviewed_measured_fingerprint != expected:
        raise CrossoverV2Refused(
            "the reviewed crossover is no longer current; review the newest "
            "measurement before applying"
        )
    baseline_expected_fingerprint = str(
        reviewed_baseline.get("candidate_fingerprint") or ""
    )
    # The pre-candidate applied profile, if any (``None`` on the speaker's
    # first-ever apply). ``build_baseline_profile_candidate`` freezes it here
    # as ``applied_recomposition_profile`` before the actual apply below
    # commits and pops that field off the new applied SSOT — this is the
    # ONLY place it survives to stash for the v2 Undo path
    # (``handle_v2_restore``).
    pre_apply_profile = reviewed_baseline.get("applied_recomposition_profile")
    if not isinstance(pre_apply_profile, Mapping):
        pre_apply_profile = None

    cam = camilla_factory()
    payload = run_async(
        apply_baseline_profile(
            topology,
            design_draft=draft,
            crossover_preview=preview,
            measurements=measurements,
            load_config=lambda path: cam.set_config_file_path(
                path, best_effort=False
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=False
            ),
            tuning_owner="automatic",
            expected_candidate_fingerprint=baseline_expected_fingerprint,
            measured_candidate=candidate,
        )
    )
    if payload.get("status") == "applied":
        observe_apply_success(expected, pre_apply_profile=pre_apply_profile)
    issue = None
    if payload.get("status") == "blocked":
        # Finding N: name the blocker compactly (not buried in the full
        # composed profile) and persist it so the review_apply screen can
        # surface it instead of looking like a dead button forever.
        issue = _blocking_apply_issue(payload)
        if issue is not None:
            payload["issue"] = issue
        _persist_apply_blocked(issue)
        log_event(
            logger,
            "correction.crossover_v2_apply_blocked",
            level=logging.WARNING,
            issue_id=(issue or {}).get("id", ""),
        )
    log_event(
        logger,
        "correction.crossover_v2_apply",
        status=payload.get("status"),
        candidate_fingerprint=expected,
    )
    return payload


def handle_v2_restore(
    run_async: Any,
    camilla_factory: Any,
) -> dict[str, Any]:
    """POST /crossover/v2/restore — the v2-aware Undo (§5.2 verify_fail).

    The legacy ``/crossover/restore`` expects a PENDING candidate-apply
    transaction from the per-driver commissioning-run machinery
    (``commissioning_apply.restore_pending_candidate_apply``); a v2 apply
    never creates one — :func:`handle_v2_apply` commits straight through
    :func:`~jasper.active_speaker.baseline_profile.apply_baseline_profile`'s
    atomic transaction, so by the time a household reaches ``verify_fail``
    there is nothing "pending" left for the legacy path to find, and it
    500s (W6 run-8 Blocker Q: ``there is no pending candidate apply to
    restore``). This is the v2-aware replacement: reload the pre-candidate
    applied profile ``handle_v2_apply`` stashed (``pre_apply_profile`` in the
    durable v2 state) through the SAME apply transaction family
    (:func:`~jasper.active_speaker.baseline_profile.restore_applied_baseline_profile`),
    then clear the durable v2 applied/candidate/failure state so the
    envelope returns to a clean measure/review state.

    Never raises a bare exception for an ordinary refusal — every refusal is
    a :class:`CrossoverV2Refused` (maps to 400 at the dispatch ladder,
    exactly like every other v2 endpoint refusal) so a household stuck on a
    bad-sounding candidate always gets a named answer, never a 500.
    """
    from jasper.active_speaker.baseline_profile import (
        restore_applied_baseline_profile,
    )

    if not v2_flow_active():
        raise CrossoverV2Refused(
            "the v2 crossover flow is not active — set JASPER_CROSSOVER_FLOW=v2"
        )
    state = load_v2_state()
    if not state or not state.get("applied"):
        raise CrossoverV2Refused(
            "nothing is applied to undo; measure and apply a crossover first"
        )
    pre_apply_profile = state.get("pre_apply_profile")
    if not isinstance(pre_apply_profile, Mapping):
        # Now that persist_conductor_state carries pre_apply_profile forward
        # across every conductor snapshot (W6.12 P0), this branch is reached
        # ONLY on a genuine first-ever apply — there was never an earlier
        # profile to stash. Say so plainly rather than reading like a bug
        # report or a generic "no undo available" error.
        raise CrossoverV2Refused(
            "this is the first measured crossover on this speaker — there's "
            "no earlier one to restore; use Speaker setup to remove it instead"
        )
    cam = camilla_factory()
    payload = run_async(
        restore_applied_baseline_profile(
            pre_apply_profile,
            load_config=lambda path: cam.set_config_file_path(
                path, best_effort=False
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=False
            ),
        )
    )
    if payload.get("status") == "restored":
        observe_restore()
    log_event(
        logger,
        "correction.crossover_v2_restored",
        status=payload.get("status"),
    )
    return payload


def _blocking_apply_issue(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """The single most relevant blocker from a blocked apply payload.

    ``payload["issues"]`` already carries the full severity-tagged list; this
    picks the first blocker (the seam always orders the real cause before any
    generic trailer issue) so a compact ``{id, message}`` pointer reaches the
    browser without digging through the composed profile.
    """
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return None
    candidates = [issue for issue in issues if isinstance(issue, Mapping)]
    for issue in candidates:
        if issue.get("severity") == "blocker":
            return {
                "id": str(issue.get("code") or ""),
                "message": str(issue.get("message") or ""),
            }
    if candidates:
        first = candidates[0]
        return {
            "id": str(first.get("code") or ""),
            "message": str(first.get("message") or ""),
        }
    return None


def _persist_apply_blocked(issue: Mapping[str, str] | None) -> None:
    """Record (or clear) the last blocked-apply issue for the review_apply nudge."""
    state = load_v2_state()
    if state is None:
        return
    state["apply_blocked"] = dict(issue) if issue else None
    save_v2_state(state)


def _reopen_candidate_artifact(
    state: Mapping[str, Any] | None, evidence: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Reopen the published candidate JSON from the session's evidence bundle."""
    from jasper.active_speaker.bundles import sessions_dir

    session_id = str((state or {}).get("session_id") or "")
    bundle_session = str(evidence.get("bundle_session_id") or "")
    candidates = []
    root = sessions_dir()
    try:
        bundle_dirs = (
            [root / bundle_session] if bundle_session else sorted(root.iterdir())
        )
    except OSError:
        bundle_dirs = []
    for bundle in bundle_dirs:
        path = (
            bundle / "evidence" / "v1" / "artifacts" / "crossover_v2"
            / session_id / "candidate.json"
        )
        if path.is_file():
            candidates.append(path)
    if not candidates:
        raise CrossoverV2Refused(
            "the published measured candidate could not be found; measure again"
        )
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrossoverV2Refused(
            "the published measured candidate could not be reopened"
        ) from exc
