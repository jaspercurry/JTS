# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session durable state and persistence."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2 import durable_state as v2durable


import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.atomic_io import atomic_write_text
from jasper.active_speaker.candidate_trials import tuning_trial_matches_candidate
from jasper.active_speaker.crossover_v2.durable_state import build_conductor_state
from jasper.active_speaker.crossover_v2.verification import RESULT_INCONCLUSIVE, RESULT_KEEP_PREVIOUS
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.active_speaker.model_error_store import ModelErrorStoreSnapshot

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
STATE_KIND = "jts_crossover_v2_flow_state"

_state_lock = threading.RLock()
_state_path_override: Path | None = None

# --------------------------------------------------------------------------- #
# durable state
# --------------------------------------------------------------------------- #


def _state_path() -> Path:
    return _state_path_override or v2durable.DEFAULT_V2_STATE_PATH


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
    state = dict(raw)
    state.pop("tier", None)  # ADR-0298: old records have unknown plan coverage.
    if "room_trial" in state:
        state.setdefault("tuning_trial", state.pop("room_trial"))
    return state


def save_v2_state(state: Mapping[str, Any], *, durable: bool = False) -> None:
    """Write the durable v2 state. ``durable`` decides whether it is fsync'd.

    Atomic is not durable. :func:`~jasper.atomic_io.atomic_write_text` writes a
    tempfile and renames, so a concurrent reader never sees a partial file —
    but without ``durable=True`` nothing has told the kernel to put those bytes
    on the platter, and a power cut can lose the whole write while leaving the
    speaker's DSP graph changed.

    **The rule for choosing (#2291): durable where power loss would lose the
    way-back pointer or falsify a receipt; cheap everywhere else.** Two writes
    qualify — one per half of that rule:

    * :func:`observe_apply_success`, which owns
      ``previous_candidate_fingerprint``, the only pointer the way back
      resolves its target from. It is created in the
      same moment the new graph goes live, so a lost write leaves a corrected
      speaker with no recorded way back.
    * the RECEIPT identity, written by :func:`persist_conductor_state` — but
      **only on a persist that carries a new one**. A receipt lives in the
      write-once evidence bundle, so losing the pointer to it leaves an
      immutable record nothing can find: the "falsifies a receipt" half.

    Everything else stays cheap on purpose, including
    :func:`persist_conductor_state`'s ordinary path — it runs after every
    consumed capture, and an fsync per capture buys nothing that the next
    capture's write does not already redo. :func:`reset_v2_journey_state`
    PRESERVES the pointer, so losing its write leaves the richer previous
    state — the pointer survives either way, which is why it is not on the
    durable list.
    """
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "kind": STATE_KIND,
        **{k: v for k, v in state.items() if k not in {"schema_version", "kind", "updated_at"}},
    }
    with _state_lock:
        payload["updated_at"] = time.time()
        atomic_write_text(
            _state_path(),
            # allow_nan=False: fail at the writer that produced the non-finite
            # value, not at the evidence packet hours later (#2839).
            json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            durable=durable,
        )


def _persist_execution_result(session_id: str, **result: Any) -> None:
    with _state_lock:
        state = load_v2_state()
        if not state or state.get("session_id") != session_id:
            return
        state["execution"] = {**(state.get("execution") or {}), **result}
        save_v2_state(state, durable=True)


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


def _attempt_loop_store_snapshot() -> ModelErrorStoreSnapshot:
    """The store-owned floor and current model-error count for one conductor.

    The host performs the I/O at conductor construction; the conductor
    receives values and a writer seam, and the attempts kernel remains pure.
    """
    from jasper.active_speaker.model_error_store import store_snapshot

    return store_snapshot()


def _record_live_model_error(**observation: Any) -> bool:
    """Claim one durable identity for the conductor's persistence seam."""
    from jasper.active_speaker.model_error_store import (
        ModelErrorConflictError,
        record_model_error,
    )

    try:
        record_model_error(**observation)
    except ModelErrorConflictError:
        return False
    return True


def reset_v2_journey_state() -> None:
    """Clear the journey; keep the playing graph's proof and reset disclosure."""
    from jasper.active_speaker.crossover_v2.coordinator import (
        ROUND_ORDINAL_EPOCH_STATE_KEY, round_ordinal_epoch_from_state,
    )

    state = load_v2_state()
    if state is None:
        return
    epoch = round_ordinal_epoch_from_state(state)
    applied = bool(state.get("applied"))
    if not applied and not epoch:
        clear_v2_state()
        return
    receipt = state.get("round_receipt")
    if applied and receipt is not None:
        epoch += 1
        ordinal = receipt.get("round_ordinal") if isinstance(receipt, Mapping) else None
        log_event(logger, "correction.crossover_v2_journey_reset_advanced_epoch",
                  round_ordinal_epoch=epoch,
                  reset_round_ordinal_from=ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None)
    clean: dict[str, Any] = {"session_id": None, "accepted_phases": [], "applied": applied,
             "gain_plan_db": None, "candidate": None, "verify": None, "failure": None,
             "verify_priors": None, "evidence": None,
             ROUND_ORDINAL_EPOCH_STATE_KEY: epoch}
    if applied:
        for key in ("attempts_loop", "previous_candidate_fingerprint", "previous_candidate_displaced_by", "previous_applied_profile",
                    "accepted_sound_revision", "accepted_sound_declaration_change", "accepted_sound_candidate_fingerprint"):
            clean[key] = state.get(key)
    save_v2_state(clean)
    log_event(logger, "correction.crossover_v2_journey_reset_kept_applied" if applied
              else "correction.crossover_v2_journey_reset_kept_epoch", round_ordinal_epoch=epoch)


def baseline_apply_seams(camilla: Any) -> tuple[Any, Any]:
    return (lambda path: camilla.set_config_file_path(path, best_effort=False),
            lambda: camilla.get_config_file_path(best_effort=False))


def observe_apply_success(
    candidate_fingerprint: str,
    *,
    previous_candidate_fingerprint: str | None = None,
    expected_post_apply_offset_db: float = 0.0,
    tuning_trial: Mapping[str, Any] | None = None,
    selected_candidate: Mapping[str, Any] | None = None,
    previous_applied_profile: Mapping[str, Any] | None = None,
) -> None:
    """Persist the completed apply and its displaced candidate together."""
    state = load_v2_state() or {}
    if selected_candidate is not None:
        state["candidate"] = dict(selected_candidate)
    state["applied"] = True
    state["previous_applied_profile"] = dict(previous_applied_profile) if previous_applied_profile else None
    state["tuning_trial"] = (
        dict(tuning_trial)
        if isinstance(tuning_trial, Mapping)
        and tuning_trial_matches_candidate(tuning_trial, candidate_fingerprint)
        else None
    )
    # SF1 (adversarial review, 2026-07-20): do NOT blindly clear an existing
    # failure code. In the ordinary happy path it is already None (MEASURE's
    # own accept clears it before the conductor ever triggers auto-apply) —
    # but a terminal session-death code (a Stop, a capture timeout) can
    # land while the apply transaction is in flight. If the stop lands first,
    # clobbering it here would erase the evidence that the household
    # stopped even though the crossover genuinely got applied (this call
    # proves it) — the envelope needs BOTH facts to render an honest
    # "applied, but you stopped it" screen instead of a false "nothing
    # happened" or a false "start over, nothing changed."
    # The reverse race (a stop landing AFTER this call persists) is already
    # handled: persist_conductor_state preserves ``applied`` once it
    # observes it, for the same session.
    state["previous_candidate_fingerprint"] = (
        previous_candidate_fingerprint
        if isinstance(previous_candidate_fingerprint, str)
        and previous_candidate_fingerprint
        else None
    )
    # The pointer's PAIRING: the identity of the apply that recorded it —
    # this one, named by the candidate it installed. The automatic revert
    # fires only when this equals the candidate the round displaced the prior
    # with (one equality, checked at the seam), which is what refuses a
    # pointer inherited from an OLDER apply (#2559's staleness class) and —
    # because the revert's own success re-stamps this to ``None`` — a second
    # automatic revert inside the [revert…next-apply] window (the ping-pong).
    # Re-stamped by every successful apply, exactly like the pointer above.
    state["previous_candidate_displaced_by"] = (
        str(candidate_fingerprint) if candidate_fingerprint else None
    )
    offset_db = float(expected_post_apply_offset_db)
    state["expected_post_apply_offset_db"] = (
        round(offset_db, 3) if math.isfinite(offset_db) else 0.0
    )
    # fsync'd (#2291). This write CREATES the way-back pointer, and it happens
    # after the new graph is already live on the speaker: a power cut that
    # loses it leaves a corrected speaker with no recorded way back.
    save_v2_state(state, durable=True)
    log_event(
        logger,
        "correction.crossover_v2_applied",
        expected_post_apply_offset_db=state["expected_post_apply_offset_db"],
    )


#: The one shape a recorded review decision takes, and its only value.
#:
#: ``decision`` is a word rather than a bool because the fact being recorded is
#: WHICH answer the household gave, and a bool names only one of them.
REVIEW_DECISION_DECLINED = "declined"


def review_declined(state: Mapping[str, Any] | None) -> bool:
    """Read a legacy decline against its candidate fingerprint."""
    if not isinstance(state, Mapping):
        return False
    decision = state.get("review_decision")
    if not isinstance(decision, Mapping):
        return False
    if str(decision.get("decision") or "") != REVIEW_DECISION_DECLINED:
        return False
    candidate = state.get("candidate")
    current = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping) else ""
    )
    return str(decision.get("candidate_fingerprint") or "") == current


def _applied_gate() -> bool:
    """The conductor's ``apply_complete`` seam: reads the durable applied flag."""
    state = load_v2_state()
    return bool(state and state.get("applied") is True)


def _applied_offset_gate() -> float:
    """The conductor's ``applied_offset_db`` seam: the whole-band level move
    the apply declared (#1811), read fresh off durable state.

    Written by :func:`observe_apply_success` on the apply's own request
    thread (the auto-apply worker's, before the two-stage split removed it);
    read by the delta probe on a LATER session's runner thread. Durable state
    is the only thing the two share — since the split they are not even the
    same session — which is why this is a seam rather than a constructor
    argument, the same reason ``apply_complete`` and ``apply_failed`` are.

    ``0.0`` for an absent, malformed, or non-finite value: "nothing known".
    The probe treats that honestly (the whole shift stays visible in
    ``residual_offset_db``), so a missing value degrades to today's behaviour
    rather than to a false claim that the level was accounted for.
    """
    state = load_v2_state()
    raw = (state or {}).get("expected_post_apply_offset_db")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    return value if math.isfinite(value) else 0.0


def _apply_failure_gate() -> str:
    """The conductor's ``apply_failed`` seam: reads a durable apply failure
    code (empty when none), persisted through the SAME
    ``persist_conductor_state`` path every other capture failure uses — see
    ``jasper.active_speaker.crossover_v2_flow.CrossoverV2Session.authorize_begin``,
    which refuses the deferred VERIFY hold outright once this names a code
    rather than holding it toward a dishonest capture_timeout. Retained with
    that hold and, like it, unreached by any shipped session since the
    two-stage split (D10) — the writer it was built for was the auto-apply
    worker thread, which is gone.

    N1 (adversarial review, 2026-07-20): the contract is LITERAL — this seam
    answers "did the apply itself fail?", never "is SOME failure code sitting
    in durable state for any reason." Any other code that happens to be
    persisted (e.g. a stale value from an unrelated path) must not be misread
    by authorize_begin as an apply failure; only ``REASON_APPLY_FAILED``
    qualifies.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import REASON_APPLY_FAILED

    state = load_v2_state()
    failure = (state or {}).get("failure")
    if isinstance(failure, Mapping):
        code = str(failure.get("code") or "")
        if code == REASON_APPLY_FAILED:
            return code
    return ""


def _resolve_measurement_level_trims(
    spec: Any, *, preset: Any, topology: Any,
) -> tuple[dict[str, float], str]:
    """This box's own per-driver level match, and which evidence answered.

    ``({}, "")`` for a spec that asks for none — the ordinary walk, which pays
    nothing: no statefile is read and no preview is loaded.

    The precedence is NOT decided here.
    :func:`~jasper.active_speaker.baseline_profile.measured_level_trims` is the
    one owner of which evidence source wins, and this function
    hands it the same two inputs the applied profile's own build hands it, so
    the graph a measurement plays through is levelled by the same evidence the
    speaker would be levelled by.

    **No/unreadable evidence answers empty WITHOUT raising, and there is no
    catch to dress a genuine fault up as no-evidence.** Both loaders fail soft
    — an absent, unreadable or corrupt-but-readable document returns a status
    dict, never a raise (``measurement._normalise_state`` and the preview
    loader both narrow a non-mapping back to a base document) — and the
    estimator is fail-closed, answering empty trims for every unusable-evidence
    case. So a box with nothing to level by reaches the caller's
    ``WALK_LEVEL_MATCH_NO_EVIDENCE`` refusal through the empty return, and NO
    exception is expected here at all. There is therefore nothing to catch: an
    exception that does arise is a real fault in the derivation, and it
    propagates with its traceback pointing straight at this function rather
    than being swallowed and misread as "this box has not measured its trims".
    """
    if not spec.level_matched:
        return {}, ""
    from jasper.active_speaker.baseline_profile import measured_level_trims
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.measurement import load_measurement_state

    trims, meta = measured_level_trims(
        preset,
        load_measurement_state(topology) or {},
        load_crossover_preview() or {},
    )
    return (
        {str(role): float(db) for role, db in trims.items()},
        str(meta.get("source") or ""),
    )


def persist_conductor_state(
    conductor: Any,
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
    failure_refusals: Sequence[str] = (),
    failure_detail: str = "",
) -> None:
    """Write the conductor's durable snapshot + host-observed failure state.

    ``failure_refusals`` are the underlying admission-refusal slugs behind a
    program failure (issue #1820). They are FORENSICS, never household copy:
    the envelope renders ``failure["code"]`` through the reason registry and
    ignores this key. It exists so a support read of the state file can tell
    which of ``program_unplayable``'s several causes actually fired, which the
    old single-code collapse erased.

    The DOCUMENT — every key, every carry-forward rule, and the reason each one
    is scoped the way it is — belongs to
    :func:`~jasper.active_speaker.crossover_v2.durable_state.build_conductor_state`.
    What is left here is the write: read the state being replaced, hand it over,
    put the answer back, and journal the one transition a household would
    notice.
    """
    from jasper.active_speaker.crossover_envelope_v2 import crossover_v2_phase

    from .correction_crossover_v2_status import crossover_v2_status_block  # lazy: status reads this state owner

    prior = load_v2_state() or {}
    built = build_conductor_state(
        conductor, prior,
        failure_code=failure_code,
        evidence=evidence,
        failure_refusals=failure_refusals,
        failure_detail=failure_detail,
    )
    session_id = built.state["session_id"]
    # Read BEFORE the write: this is the grade the household is currently
    # looking at, and ``crossover_v2_status_block`` reads the state file.
    prior_grade = (crossover_v2_status_block() or {}).get("post_apply_grade")
    prior_outcome = (
        str(prior_grade.get("outcome") or "")
        if isinstance(prior_grade, Mapping)
        and prior.get("session_id") == session_id else ""
    )
    from jasper.active_speaker.bundles import sessions_dir  # lazy: capture-only bundle lookup
    from jasper.active_speaker.crossover_v2.round_inputs import CAPTURE_STATE_FILENAME  # lazy: capture snapshot

    with _state_lock:
        current = load_v2_state() or {}
        if current.get("session_id") == session_id and current.get("execution"):
            built.state["execution"] = current["execution"]
        save_v2_state(built.state, durable=built.durable)
        bundle_id = (built.state.get("evidence") or {}).get("bundle_session_id")
        if isinstance(bundle_id, str) and Path(bundle_id).name == bundle_id:
            bundle = sessions_dir() / bundle_id
            if (bundle / "info.json").is_file():
                atomic_write_text(
                    bundle / CAPTURE_STATE_FILENAME,
                    json.dumps(built.state, allow_nan=False, sort_keys=True) + "\n",
                    mode=0o640, durable=built.durable,
                )
    from jasper.active_speaker.crossover_v2.journey import PHASE_DONE

    grade = (crossover_v2_status_block() or {}).get("post_apply_grade")
    grade = grade if isinstance(grade, Mapping) else {}
    was_done = crossover_v2_phase(
        prior, review_declined=review_declined(prior),
    ) == PHASE_DONE
    now_done = crossover_v2_phase(
        built.state, review_declined=review_declined(built.state),
    ) == PHASE_DONE
    if (now_done and not was_done) or (
        grade.get("outcome") == RESULT_KEEP_PREVIOUS
        and prior_outcome != RESULT_KEEP_PREVIOUS
    ):
        log_event(
            logger, "correction.crossover_v2_result_classified",
            session_id=session_id, outcome=grade.get("outcome") or RESULT_INCONCLUSIVE,
            improvement_db=grade.get("improvement_db"),
            tracking_passed=grade.get("tracking_passed"), absolute_passed=grade.get("absolute_passed"),
            absolute_miss_db=grade.get("absolute_miss_db"), absolute_worst_hz=grade.get("absolute_worst_hz"),
            candidate_fingerprint=grade.get("candidate_fingerprint"),
        )


def _persist_terminal_failure(
    conductor: Any, code: str, *, refusals: Sequence[str] = (), detail: str = "",
) -> bool:
    """Session-terminal persistence (§5.6): pre-apply, capture evidence dies
    with the session (restart at CHECK); post-apply, the applied candidate +
    verify priors survive so ``/v2/verify`` can re-arm.

    SF2 (adversarial review, 2026-07-20): ``REASON_APPLY_FAILED`` is exempted
    from the pre-apply evidence reset. The §5.6 rationale for wiping
    ``accepted_phases``/``gain_plan_db`` is that a DEAD session makes the mic
    position unverifiable — but an auto-apply that came back blocked or
    errored says nothing about the mic position; MEASURE's own evidence is
    still exactly as good as it was. Keeping MEASURE accepted here is what
    lets ``crossover_v2_phase`` resolve to ``PHASE_APPLYING`` (not
    ``PHASE_CHECK``) so the envelope's apply-step failure screen — and the
    specific blocked-issue nudge layered onto it — can actually render;
    before this fix the reset always won, so that nudge was unreachable in
    production (only reachable by injecting the phase directly in a test).
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import REASON_APPLY_FAILED

    prior = load_v2_state()
    session_id = str(getattr(conductor, "session_id", ""))
    prior_verify = (prior or {}).get("verify")
    prior_outcome = str(
        (prior_verify or {}).get("outcome")
        if isinstance(prior_verify, Mapping)
        else ""
    )
    if (
        isinstance(prior_verify, Mapping)
        and prior_outcome in {"pass", "fail", "inconclusive"}
        and (prior or {}).get("session_id") == session_id
    ):
        _persist_execution_result(session_id, cleanup_fault_code=code)
        # consume() persists VERIFY before publishing capture_result. Later
        # trouble is a cleanup fault, not a commissioning verdict.
        log_event(
            logger,
            "correction.crossover_v2_terminal_verdict_preserved",
            level=logging.WARNING,
            session_id=session_id,
            outcome=prior_outcome,
            verdict_code=prior_verify.get("code") or "",
            cleanup_fault_code=code,
        )
        return True

    persist_conductor_state(
        conductor, failure_code=code, failure_refusals=refusals, failure_detail=detail,
    )
    state = load_v2_state()
    if state is None:
        return False
    if not state.get("applied") and code != REASON_APPLY_FAILED:
        state["accepted_phases"] = []
        state["gain_plan_db"] = None
    save_v2_state(state)
    return False
