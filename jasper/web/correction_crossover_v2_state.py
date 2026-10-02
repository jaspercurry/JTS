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
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from jasper.platform.atomic_io import advisory_file_lock, atomic_write_text
from jasper.active_speaker.crossover_v2.coordinator import (
    ROUND_ORDINAL_EPOCH_STATE_KEY, round_ordinal_epoch_from_state,
)
from jasper.active_speaker.crossover_v2.durable_state import build_conductor_state
from jasper.active_speaker import driver_base_trim
from jasper.platform.log_event import log_event

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
STATE_KIND = "jts_crossover_v2_flow_state"

_state_lock = threading.RLock()
# ``depth``: how many v2_state_locked() holds this thread is inside.
_door = threading.local()
_state_path_override: Path | None = None

#: How long a request waits for the other web process to release the state.
#: Sized like the DSP writer lock (``jasper.dsp_control.dsp_apply``).
STATE_LOCK_TIMEOUT_S = 10.0
#: The wait for a write past a commit point (a graph already live, a session
#: already over): losing that write costs the way back, and a live holder
#: writes in milliseconds. It stays well under the apply route's 60 s
#: ``run_async`` budget, which it runs inside.
POST_COMMIT_STATE_LOCK_TIMEOUT_S = 20.0


class V2StateLockTimeout(TimeoutError):
    """Another process held the durable v2 state past :data:`STATE_LOCK_TIMEOUT_S`."""

    code = "crossover_v2_state_busy"


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
    return state


def save_v2_state(state: Mapping[str, Any], *, durable: bool = False) -> None:
    """Write the durable v2 state. ``durable`` decides whether it is fsync'd.

    Atomic is not durable. :func:`~jasper.platform.atomic_io.atomic_write_text` writes a
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


@contextmanager
def v2_state_locked(*, timeout_s: float | None = None) -> Iterator[None]:
    """Hold the durable v2 state across a read-modify-write.

    Both web processes write the state, so a thread's outermost hold takes
    the sidecar flock first, waiting ``timeout_s`` (default
    :data:`STATE_LOCK_TIMEOUT_S`) before raising :class:`V2StateLockTimeout`,
    and only then the in-process lock, which it never holds while waiting.
    A nested hold re-enters without taking the flock again.
    """
    depth = getattr(_door, "depth", 0)
    timeout = STATE_LOCK_TIMEOUT_S if timeout_s is None else timeout_s
    with ExitStack() as held:
        if not depth:
            path = _state_path()
            started = time.monotonic()
            try:
                held.enter_context(advisory_file_lock(
                    path.with_name(f".{path.name}.lock"), timeout_sec=timeout,
                ))
            except TimeoutError:
                log_event(
                    logger, "correction.crossover_v2_state_lock", level=logging.WARNING,
                    result="timeout", wait_ms=round((time.monotonic() - started) * 1000),
                    timeout_ms=round(timeout * 1000),
                )
                raise V2StateLockTimeout("another process holds the crossover state") from None
        held.enter_context(_state_lock)
        _door.depth = depth + 1
        try:
            yield
        finally:
            _door.depth = depth


def persist_execution_result(session_id: str, **result: Any) -> None:
    with v2_state_locked(timeout_s=POST_COMMIT_STATE_LOCK_TIMEOUT_S):
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


def reset_v2_journey_state() -> None:
    """Clear the journey; keep the playing graph's proof and reset disclosure."""
    # One hold across the read and the write: an apply between them would
    # otherwise lose the way-back pointer it just wrote durably.
    with v2_state_locked():
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
                 "gain_plan_db": None, "candidate": None, "failure": None,
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
    selected_candidate: Mapping[str, Any] | None = None,
    previous_applied_profile: Mapping[str, Any] | None = None,
) -> None:
    """Persist the completed apply and its displaced candidate together."""
    state = load_v2_state() or {}
    if selected_candidate is not None:
        state["candidate"] = dict(selected_candidate)
    state["applied"] = True
    state["previous_applied_profile"] = dict(previous_applied_profile) if previous_applied_profile else None
    # Do NOT blindly clear an existing
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


def resolve_measurement_level_trims(
    spec: Any, *, preset: Any, topology: Any,
) -> tuple[dict[str, float], str]:
    """This box's own per-driver level match, and which evidence answered.

    ``({}, "")`` for a spec that asks for none — the ordinary walk, which pays
    nothing: no statefile is read and no preview is loaded.

    The answer is NOT decided here:
    :func:`~jasper.active_speaker.driver_base_trim.measured_level_trims` owns
    it, and this function only loads the declaration that answer is keyed to.

    **No/unreadable evidence answers empty WITHOUT raising, and there is no
    catch to dress a genuine fault up as no-evidence.** The preview loader
    fails soft — an absent, unreadable or corrupt-but-readable document returns
    a status dict, never a raise — and the estimator is fail-closed, answering
    empty trims for every unusable-evidence case. So a box with nothing to
    level by reaches the caller's ``WALK_LEVEL_MATCH_NO_EVIDENCE`` refusal
    through the empty return, and NO exception is expected here at all. There is therefore nothing to catch: an
    exception that does arise is a real fault in the derivation, and it
    propagates with its traceback pointing straight at this function rather
    than being swallowed and misread as "this box has not measured its trims".
    """
    if not spec.level_matched:
        return {}, ""
    from jasper.active_speaker.crossover_preview import build_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    draft = load_design_draft()
    trims, meta = driver_base_trim.measured_level_trims(
        preset, build_crossover_preview(draft), design_draft=draft,
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
    failure_roles: Sequence[str] = (),
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
    and put the answer back.
    """
    from jasper.active_speaker.bundles import sessions_dir  # lazy: capture-only bundle lookup
    from jasper.active_speaker.crossover_v2.round_inputs import CAPTURE_STATE_FILENAME  # lazy: capture snapshot

    # One hold from reading the state being replaced to writing its successor:
    # a write between them (an apply's way-back pointer) would otherwise be lost.
    with v2_state_locked():
        prior = load_v2_state() or {}
        built = build_conductor_state(
            conductor, prior,
            failure_code=failure_code,
            evidence=evidence,
            failure_refusals=failure_refusals,
            failure_detail=failure_detail,
            failure_roles=failure_roles,
        )
        if prior.get("session_id") == built.state["session_id"] and prior.get("execution"):
            built.state["execution"] = prior["execution"]
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


def persist_terminal_failure(
    conductor: Any, code: str, *, refusals: Sequence[str] = (), detail: str = "",
    failed_roles: Sequence[str] = (),
) -> None:
    """Session-terminal persistence (§5.6): pre-apply, capture evidence dies
    with the session (restart at CHECK); post-apply, the applied candidate +
    verify priors survive so ``/v2/verify`` can re-arm.

    ``REASON_APPLY_FAILED`` is exempted
    from the pre-apply evidence reset. The §5.6 rationale for wiping
    ``accepted_phases``/``gain_plan_db`` is that a DEAD session makes the mic
    position unverifiable — but an auto-apply that came back blocked or
    errored says nothing about the mic position; MEASURE's own evidence is
    still exactly as good as it was. Keeping MEASURE accepted here is what
    lets ``crossover_v2_phase`` resolve to ``PHASE_APPLYING`` (not
    ``PHASE_CHECK``) so the envelope's apply-step failure screen — and the
    specific blocked-issue nudge layered onto it — can actually render.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import REASON_APPLY_FAILED

    with v2_state_locked(timeout_s=POST_COMMIT_STATE_LOCK_TIMEOUT_S):
        persist_conductor_state(
            conductor, failure_code=code, failure_refusals=refusals, failure_detail=detail,
            failure_roles=failed_roles,
        )
        state = load_v2_state()
        if state is None:
            return
        if not state.get("applied") and code != REASON_APPLY_FAILED:
            state["accepted_phases"] = []
            state["gain_plan_db"] = None
        save_v2_state(state)
