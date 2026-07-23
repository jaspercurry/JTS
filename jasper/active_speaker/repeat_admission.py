# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable, fail-closed admission controller for crossover repeat playback.

The measurement ledger owns accepted acoustic evidence and commissioning
bundles are optional forensics. Neither can safely arbitrate whether another
audible attempt may start. This small state machine reserves one of four
attempts atomically *before* ambient capture/audio, binds completion to an
unguessable token, and leaves uncertain writes blocking rather than reopening
the audio gate.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

STATE_KIND = "jts_active_speaker_repeat_admission"
SCHEMA_VERSION = 1
# The audible MEASUREMENT budget: how many attempts that PROVABLY played a
# tone a set may spend. It is DERIVED from the durable results (see
# ``measurement_attempts``), never from the raw reservation counter — an
# attempt that never emitted audio (a transport/infra failure) is refunded
# from it, so infra flakiness cannot exhaust the room-variance tolerance.
MAX_ATTEMPTS = 4
# Infra circuit-breaker: total reservations a set may consume regardless of
# audio. Transport failures are refunded from ``MAX_ATTEMPTS`` above, so a box
# whose transport keeps failing could otherwise reserve forever; this caps the
# loop and gives a terminal distinct from acoustic insufficiency
# (``INFRA_RETRY_EXHAUSTED``) so the envelope can say "the speaker couldn't
# complete a pass" rather than blaming the room. With MAX_ATTEMPTS=4 audible
# attempts this tolerates up to four refunded infra retries. Chosen to match
# the relay's per-plan attempt ceiling (``capture_relay.spec
# .MAX_CAPTURE_PLAN_ATTEMPTS``) so the durable reservation attempt, which
# indexes the commissioning bundle's repeat captures, never exceeds that
# ceiling.
MAX_RESERVATIONS = 8
INFRA_RETRY_EXHAUSTED = "infra_retry_exhausted"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_repeat_admission.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_REPEAT_ADMISSION_STATE"
OWNER_ID = uuid.uuid4().hex
_THREAD_LOCK = threading.RLock()
_CLAIM_ERROR: str | None = None
logger = logging.getLogger(__name__)
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def failure_status(attempt: Any) -> str:
    """A transport/infra failure retries until the reservation circuit-breaker.

    Transport failures never play a tone, so they are refunded from the
    audible ``MAX_ATTEMPTS`` budget (see ``measurement_attempts``) and the set
    stays retryable — a box just needs to get its captures through. Only
    reaching ``MAX_RESERVATIONS`` total reservations makes an infra failure
    terminal so a box that can never complete a pass cannot loop forever.
    """

    try:
        number = int(attempt)
    except (TypeError, ValueError):
        number = MAX_RESERVATIONS
    return "refused" if number >= MAX_RESERVATIONS else "active"


def result_emitted_audio(result: Mapping[str, Any]) -> bool:
    """Whether one stored attempt consumed the audible measurement budget.

    A transport/infra failure that provably never played a tone records
    ``audio_emitted is False`` and is refunded from the budget. Every other
    attempt — a real acoustic capture (``audio_emitted is True``) OR one whose
    audio state is unknown/absent — consumes it, so an uncertain write fails
    closed with acoustic semantics rather than reopening the audio gate.
    """

    return result.get("audio_emitted") is not False


def measurement_attempts(results: Any) -> int:
    """Count durable results that consumed the audible measurement budget.

    A PURE projection of the durable ``results`` ledger — never a second
    mutable counter — so the audio-gate budget can never drift from the
    attempts actually recorded. Transport/infra results (``audio_emitted is
    False``) are excluded; unknown audio fails closed and is counted.
    """

    if not isinstance(results, (list, tuple)):
        return 0
    return sum(
        1
        for item in results
        if isinstance(item, Mapping) and result_emitted_audio(item)
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _base() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": STATE_KIND,
        "comparison": None,
        "targets": {},
        "updated_at": None,
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _base()
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("crossover repeat admission state is unreadable") from exc
    if not isinstance(raw, Mapping) or raw.get("kind") != STATE_KIND:
        raise RuntimeError("crossover repeat admission state is malformed")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("crossover repeat admission schema is unsupported")
    comparison_raw = raw.get("comparison")
    comparison = None
    if comparison_raw is not None:
        if not isinstance(comparison_raw, Mapping):
            raise RuntimeError("crossover repeat comparison binding is malformed")
        comparison = {
            "comparison_set_id": str(comparison_raw.get("comparison_set_id") or ""),
            "fingerprint": str(comparison_raw.get("fingerprint") or ""),
        }
        if not all(comparison.values()):
            raise RuntimeError("crossover repeat comparison binding is incomplete")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, Mapping):
        raise RuntimeError("crossover repeat targets are malformed")
    targets: dict[str, dict[str, Any]] = {}
    for key, value in targets_raw.items():
        target_id = str(key)
        if not target_id or not isinstance(value, Mapping):
            raise RuntimeError("crossover repeat target entry is malformed")
        attempts = value.get("attempts")
        status = value.get("status")
        inflight = value.get("inflight")
        results = value.get("results", [])
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 1 <= attempts <= MAX_RESERVATIONS
        ):
            raise RuntimeError("crossover repeat target state is invalid")
        if not isinstance(results, list):
            raise RuntimeError("crossover repeat target state is invalid")
        result_attempts: list[int] = []
        for item in results:
            if not isinstance(item, Mapping):
                raise RuntimeError("crossover repeat target state is invalid")
            result_attempt = item.get("attempt")
            if (
                isinstance(result_attempt, bool)
                or not isinstance(result_attempt, int)
                or not 1 <= result_attempt <= attempts
            ):
                raise RuntimeError("crossover repeat target state is invalid")
            emitted = item.get("audio_emitted")
            if emitted is not None and not isinstance(emitted, bool):
                raise RuntimeError("crossover repeat target state is invalid")
            result_attempts.append(result_attempt)
        result_attempts_ordered = result_attempts == sorted(set(result_attempts))
        if (
            status not in {"active", "ready", "completed", "refused", "aborted"}
            or (inflight is not None and (
                not isinstance(inflight, str) or _UUID_HEX_RE.fullmatch(inflight) is None
            ))
            or status != "active" and inflight is not None
            or len(results) > attempts
            or not result_attempts_ordered
            or str(value.get("target_id") or "") != target_id
            or not str(value.get("target_fingerprint") or "")
            or _UUID_HEX_RE.fullmatch(str(value.get("owner_id") or "")) is None
        ):
            raise RuntimeError("crossover repeat target state is invalid")
        targets[target_id] = {
            "target_id": target_id,
            "target_fingerprint": str(value["target_fingerprint"]),
            "owner_id": str(value["owner_id"]),
            "attempts": attempts,
            "status": status,
            "inflight": inflight,
            "results": [dict(item) for item in results],
            "reason": value.get("reason"),
            "updated_at": value.get("updated_at"),
        }
    out = _base()
    out["comparison"] = comparison
    out["targets"] = targets
    out["updated_at"] = raw.get("updated_at")
    return out


def _write(path: Path, state: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(dict(state), indent=2, sort_keys=True) + "\n",
        mode=0o640,
        group_from_parent=True,
    )


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with _THREAD_LOCK:
        with lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(lock_path, 0o640)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def invalidate(*, path: str | Path | None = None) -> None:
    global _CLAIM_ERROR
    target = state_path(path)
    with _locked(target):
        _write(target, _base())
    _CLAIM_ERROR = None


def activate(
    comparison_set: Mapping[str, Any], *, path: str | Path | None = None
) -> dict[str, Any]:
    global _CLAIM_ERROR
    comparison = {
        "comparison_set_id": str(comparison_set.get("comparison_set_id") or ""),
        "fingerprint": str(comparison_set.get("fingerprint") or ""),
    }
    if not all(comparison.values()):
        raise ValueError("repeat admission requires a complete comparison binding")
    target = state_path(path)
    with _locked(target):
        state = _base()
        state["comparison"] = comparison
        state["updated_at"] = _now()
        _write(target, state)
        _CLAIM_ERROR = None
        return state


def claim_owner(*, path: str | Path | None = None) -> dict[str, Any]:
    """At service start, close active work left by the previous process.

    The correction web socket uses ``Accept=no`` and one service ``ExecStart``;
    there is exactly one owner process.  Claiming is deliberately explicit at
    that lifecycle boundary. Ordinary reads remain pure and a second live
    process cannot destructively steal an inflight reservation.
    """

    global _CLAIM_ERROR
    target = state_path(path)
    try:
        with _locked(target):
            state = _load(target)
            targets = dict(state["targets"])
            aborted: list[tuple[str, dict[str, Any]]] = []
            for key, raw in targets.items():
                entry = dict(raw)
                prior_status = entry.get("status")
                if (
                    entry.get("owner_id") != OWNER_ID
                    and prior_status in {"active", "ready"}
                ):
                    reason = (
                        "service_restarted_during_finalization"
                        if prior_status == "ready"
                        else "service_restarted"
                    )
                    entry.update({
                        "status": "aborted",
                        "reason": reason,
                        "inflight": None,
                        "updated_at": _now(),
                    })
                    targets[key] = entry
                    aborted.append((key, entry))
            if aborted:
                state.update({"targets": targets, "updated_at": _now()})
                _write(target, state)
                for target_id, entry in aborted:
                    log_event(
                        logger,
                        "correction.crossover_repeat_aborted",
                        target=target_id,
                        attempts=entry.get("attempts"),
                        reason=entry.get("reason"),
                    )
            _CLAIM_ERROR = None
            return state
    except (OSError, RuntimeError, ValueError) as exc:
        _CLAIM_ERROR = type(exc).__name__
        raise


def _assert_comparison(state: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    actual = state.get("comparison")
    if not isinstance(actual, Mapping) or any(
        str(actual.get(key) or "") != str(expected.get(key) or "")
        for key in ("comparison_set_id", "fingerprint")
    ):
        raise ValueError("the crossover repeat comparison context changed")


def reserve(
    comparison_set: Mapping[str, Any],
    *,
    target_id: str,
    target_fingerprint: str,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Reserve one attempt before playback; reject any ambiguous state."""

    if not str(target_id or "") or not str(target_fingerprint or ""):
        raise ValueError("repeat admission requires a complete target binding")
    target = state_path(path)
    with _locked(target):
        state = _load(target)
        _assert_comparison(state, comparison_set)
        targets = dict(state["targets"])
        entry = dict(targets.get(target_id) or {})
        if entry and entry.get("target_fingerprint") != target_fingerprint:
            raise ValueError("the crossover repeat target changed")
        if (
            entry.get("owner_id") not in (None, OWNER_ID)
            and entry.get("status") == "active"
        ):
            raise ValueError("the crossover repeat set belongs to another service process")
        if entry.get("status") in {"ready", "completed", "refused", "aborted"}:
            raise ValueError(f"the crossover repeat set is {entry.get('status')}")
        if entry.get("inflight"):
            raise ValueError("a crossover repeat attempt is already in progress")
        attempts = int(entry.get("attempts") or 0)
        # Two independent gates. The audible budget is DERIVED from the durable
        # results, so a refunded transport failure never advances it; the raw
        # reservation cap is the infra circuit-breaker that stops an
        # always-failing box from reserving forever, with a distinct terminal
        # reason so the envelope blames the speaker, not the room.
        if measurement_attempts(entry.get("results")) >= MAX_ATTEMPTS:
            raise ValueError("the crossover repeat set already used four attempts")
        if attempts >= MAX_RESERVATIONS:
            raise ValueError(
                "the crossover repeat set could not complete a measurement pass "
                f"({INFRA_RETRY_EXHAUSTED})"
            )
        token = uuid.uuid4().hex
        entry.update({
            "target_id": target_id,
            "target_fingerprint": target_fingerprint,
            "owner_id": OWNER_ID,
            "attempts": attempts + 1,
            "status": "active",
            "inflight": token,
            "updated_at": _now(),
        })
        targets[target_id] = entry
        state.update({"targets": targets, "updated_at": entry["updated_at"]})
        _write(target, state)
        return {**entry, "token": token, "attempt": attempts + 1}


def finish(
    comparison_set: Mapping[str, Any],
    *,
    target_id: str,
    target_fingerprint: str,
    token: str,
    result: Mapping[str, Any],
    status: str,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Finish the exact inflight token as active, ready, or refused."""

    if status not in {"active", "ready", "refused"}:
        raise ValueError(f"unsupported repeat admission finish status: {status}")
    target = state_path(path)
    with _locked(target):
        state = _load(target)
        _assert_comparison(state, comparison_set)
        targets = dict(state["targets"])
        entry = dict(targets.get(target_id) or {})
        if (
            entry.get("target_fingerprint") != target_fingerprint
            or entry.get("owner_id") != OWNER_ID
            or entry.get("inflight") != token
        ):
            raise ValueError("repeat result has no matching inflight admission token")
        results = list(entry.get("results") or [])
        stored = {**dict(result), "attempt": entry["attempts"]}
        # Persist the audible-budget discriminator only when playback proved
        # its state: True (a tone played) or False (a transport/infra failure
        # that never played). Anything else stays UNSET and fails closed —
        # measurement_attempts() then counts it as budget-consuming (acoustic
        # semantics). Storing a strict bool (never a None sentinel) keeps
        # legacy results byte-identical.
        emitted = stored.get("audio_emitted")
        if emitted is True or emitted is False:
            stored["audio_emitted"] = emitted
        else:
            stored.pop("audio_emitted", None)
        results.append(stored)
        entry.update({
            "inflight": None,
            "results": results[-MAX_RESERVATIONS:],
            "status": status,
            "updated_at": _now(),
        })
        targets[target_id] = entry
        state.update({"targets": targets, "updated_at": entry["updated_at"]})
        _write(target, state)
        log_event(
            logger,
            "correction.crossover_repeat_attempt",
            comparison_set_id=str(comparison_set.get("comparison_set_id") or ""),
            target=target_id,
            attempt=entry["attempts"],
            accepted=result.get("accepted"),
            reject_reason=result.get("reject_reason"),
            snr_db=result.get("estimated_snr_db"),
            clipping=result.get("clipping"),
            failure_type=result.get("failure_type"),
            phase=result.get("phase") or "acoustic",
            audio_emitted=stored.get("audio_emitted"),
            measurement_attempts=measurement_attempts(entry["results"]),
        )
        return entry


def complete(
    comparison_set: Mapping[str, Any],
    *,
    target_id: str,
    target_fingerprint: str,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Move ready -> completed only after final measurement persistence."""

    target = state_path(path)
    with _locked(target):
        state = _load(target)
        _assert_comparison(state, comparison_set)
        targets = dict(state["targets"])
        entry = dict(targets.get(target_id) or {})
        if (
            entry.get("target_fingerprint") != target_fingerprint
            or entry.get("owner_id") != OWNER_ID
            or entry.get("status") != "ready"
            or entry.get("inflight") is not None
        ):
            raise ValueError("repeat set is not ready for completion")
        entry.update({"status": "completed", "updated_at": _now()})
        targets[target_id] = entry
        state.update({"targets": targets, "updated_at": entry["updated_at"]})
        _write(target, state)
        return entry


def abort_ready(
    comparison_set: Mapping[str, Any],
    *,
    target_id: str,
    target_fingerprint: str,
    reason: str,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Move ``ready`` to terminal ``aborted`` after finalization fails.

    ``ready`` has already consumed the audible attempt and deliberately blocks
    another reservation. If measurement persistence or the later completion
    ledger write raises, the caller uses this transition before propagating the
    original exception: attempts remain preserved, no fifth sweep can play,
    and a new comparison/level run is the only supported recovery.
    """

    reason_id = str(reason or "").strip()
    if not reason_id or len(reason_id) > 120:
        raise ValueError("repeat finalization abort requires a bounded reason")
    target = state_path(path)
    with _locked(target):
        state = _load(target)
        _assert_comparison(state, comparison_set)
        targets = dict(state["targets"])
        entry = dict(targets.get(target_id) or {})
        if (
            entry.get("target_fingerprint") != target_fingerprint
            or entry.get("owner_id") != OWNER_ID
            or entry.get("status") != "ready"
            or entry.get("inflight") is not None
        ):
            raise ValueError("repeat set is not ready for finalization abort")
        entry.update({
            "status": "aborted",
            "reason": reason_id,
            "updated_at": _now(),
        })
        targets[target_id] = entry
        state.update({"targets": targets, "updated_at": entry["updated_at"]})
        _write(target, state)
        log_event(
            logger,
            "correction.crossover_repeat_aborted",
            comparison_set_id=str(comparison_set.get("comparison_set_id") or ""),
            target=target_id,
            attempts=entry.get("attempts"),
            reason=reason_id,
        )
        return entry


def reservation_is_finished(
    comparison_set: Mapping[str, Any],
    *,
    target_id: str,
    target_fingerprint: str,
    attempt: int,
    path: str | Path | None = None,
) -> bool:
    """Return whether the exact audible reservation was already consumed.

    Request-boundary recovery can observe an exception after a deeper layer
    has durably finished or aborted the attempt.  Re-finishing that same token
    would replace the useful original error with a misleading persistence
    failure.  The authoritative ledger proves consumption with the target
    binding, attempt counter, no inflight token, and a matching result entry.
    """

    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return False
    target = state_path(path)
    with _locked(target):
        state = _load(target)
        _assert_comparison(state, comparison_set)
        entry = (state.get("targets") or {}).get(target_id)
        if not isinstance(entry, Mapping):
            return False
        results = entry.get("results") or ()
        latest = results[-1] if isinstance(results, list) and results else None
        return bool(
            entry.get("target_fingerprint") == target_fingerprint
            and entry.get("attempts") == attempt
            and entry.get("inflight") is None
            and isinstance(latest, Mapping)
            and latest.get("attempt") == attempt
        )


def snapshot(
    comparison_set: Mapping[str, Any] | None = None,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Read compact state without changing admission decisions."""

    if _CLAIM_ERROR is not None:
        raise RuntimeError(
            "crossover repeat admission ownership claim failed at service start"
        )
    target = state_path(path)
    with _locked(target):
        state = _load(target)
        if comparison_set is not None:
            _assert_comparison(state, comparison_set)
        return state
