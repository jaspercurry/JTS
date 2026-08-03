# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-speaker durable memory for the tuning loop: its floor, and its misses.

Two facts, one file, one owner each:

* ``floor`` — the :class:`~jasper.active_speaker.attempts_loop.FloorStats` this
  speaker's loop grades against. Adopted once from a repeat study (or declared
  as a policy bar) and then read, so the loop does not re-derive a threshold
  from whatever captures happen to be lying around.
* ``model_error`` — a bounded, newest-first history of ``realized − predicted``
  per verify. This is the **rung-P4 seam**: the crossover flow predicts a grade
  when it fits, and measures one when it verifies, and until now nothing kept
  the pair. The two banked commissioning sessions in
  ``captures/r11-loop-proof-corpus/`` are the shape of that gap — both carry a
  fitted candidate with predictions and neither kept a VERIFY analysis, so the
  model error they would have shown is simply gone.

This module is where the I/O lives. The decision kernel next door
(:mod:`jasper.active_speaker.attempts_loop`) imports nothing that touches a
disk, which is what makes "the kernel is pure" a property you can check by
reading its imports instead of a claim you have to trust. That split is the
whole reason this is a separate file.

The update API is deliberately two calls — :func:`adopt_floor` and
:func:`record_model_error` — each read-modify-write, so the live flow can call
whichever it has news for without assembling a whole state document.
``record_model_error`` is idempotent by its per-speaker stable observation
identity, closing the process-crash window between this file's atomic write and
the conductor journey's separate atomic write.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_json
from jasper.log_event import log_event

from .attempts_loop import FLOOR_BASES, FloorStats

SCHEMA_VERSION = 1
MODEL_ERROR_STATE_KIND = "jts_active_speaker_model_error"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_model_error.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_MODEL_ERROR_PATH"

#: Newest-first, so a truncated read keeps the records a reader wants. Sized to
#: outlive a commissioning campaign's worth of verifies without becoming a log:
#: the point of the history is a trend in the model's error, and a trend that
#: needs more than 32 points is one nobody is reading anyway.
MAX_MODEL_ERROR_RECORDS = 32

logger = logging.getLogger(__name__)


class ModelErrorConflictError(RuntimeError):
    """A stable observation identity was reused with different numbers."""


@dataclass(frozen=True)
class ModelErrorStoreSnapshot:
    """One coherent read of the store-owned facts used by the live host."""

    floor: FloorStats | None
    model_error_count: int


def model_error_state_path(path: str | Path | None = None) -> Path:
    """Explicit argument, then env override, then the production default."""

    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _base_state(path: Path) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": MODEL_ERROR_STATE_KIND,
        "state_path": str(path),
        "updated_at": None,
        "floor": None,
        "model_error": [],
    }


def _normalise_floor(raw: Any) -> dict[str, Any] | None:
    """Keep a stored floor only if it still parses as one.

    A half-written or hand-edited floor is dropped rather than half-trusted:
    the loop's alternative to a floor it can read is refusing to grade, which
    is safe, whereas a floor missing its threshold is not.
    """

    if not isinstance(raw, Mapping):
        return None
    metric = raw.get("metric")
    claim_floor_db = raw.get("claim_floor_db")
    basis = raw.get("basis")
    if not isinstance(metric, str) or not metric:
        return None
    if not isinstance(claim_floor_db, (int, float)) or isinstance(claim_floor_db, bool):
        return None
    if not (float(claim_floor_db) > 0.0):
        return None
    if basis not in FLOOR_BASES:
        return None
    return dict(raw)


def _normalise_state(raw: Any, path: Path) -> dict[str, Any]:
    state = _base_state(path)
    if not isinstance(raw, Mapping):
        return state
    state["updated_at"] = raw.get("updated_at")
    state["floor"] = _normalise_floor(raw.get("floor"))
    records = raw.get("model_error")
    if isinstance(records, list):
        state["model_error"] = [
            item for item in records if isinstance(item, Mapping)
        ][:MAX_MODEL_ERROR_RECORDS]
    return state


def load_state(path: str | Path | None = None) -> dict[str, Any]:
    """Read the store, normalised. A missing or corrupt file reads as empty.

    Corrupt-reads-as-empty is the right failure here and not a silent one: the
    caller sees ``floor is None`` and refuses to grade, which is the same
    behaviour as a speaker that has never adopted a floor. Losing the history
    costs a trend; trusting a torn file costs a wrong threshold.
    """

    resolved = model_error_state_path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _base_state(resolved)
    except (OSError, json.JSONDecodeError) as exc:
        log_event(
            logger,
            "active_speaker.model_error_store_unreadable",
            path=str(resolved),
            error=str(exc),
            level=logging.WARNING,
        )
        return _base_state(resolved)
    return _normalise_state(raw, resolved)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    state["artifact_schema_version"] = SCHEMA_VERSION
    state["kind"] = MODEL_ERROR_STATE_KIND
    state["state_path"] = str(path)
    state["updated_at"] = _utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state, mode=0o640)


def adopt_floor(
    floor: FloorStats, *, path: str | Path | None = None,
) -> dict[str, Any]:
    """Make ``floor`` the threshold this speaker's loop grades against.

    Replaces any previous floor outright rather than merging: a floor is one
    fact with one owner, and a floor assembled from two studies would belong to
    neither. Returns the written state.
    """

    resolved = model_error_state_path(path)
    state = load_state(resolved)
    state["floor"] = floor.to_dict()
    _write_state(resolved, state)
    log_event(
        logger,
        "active_speaker.model_error_floor_adopted",
        path=str(resolved),
        metric=floor.metric,
        basis=floor.basis,
        claim_floor_db=round(floor.claim_floor_db, 5),
    )
    return state


def _floor_from_state(state: Mapping[str, Any]) -> FloorStats | None:
    raw = state.get("floor")
    if not isinstance(raw, Mapping):
        return None
    return FloorStats(
        metric=str(raw["metric"]),
        claim_floor_db=float(raw["claim_floor_db"]),
        basis=str(raw["basis"]),
        source=str(raw.get("source") or "unrecorded"),
        median_db=_optional_float(raw.get("median_db")),
        p95_db=_optional_float(raw.get("p95_db")),
        measured_at=str(raw.get("measured_at") or ""),
    )


def store_snapshot(path: str | Path | None = None) -> ModelErrorStoreSnapshot:
    """Read the adopted floor and record count from one store revision."""

    state = load_state(path)
    records = state.get("model_error")
    return ModelErrorStoreSnapshot(
        floor=_floor_from_state(state),
        model_error_count=len(records) if isinstance(records, list) else 0,
    )


def stored_floor(path: str | Path | None = None) -> FloorStats | None:
    """The adopted floor, or ``None`` when this speaker has never adopted one."""

    return store_snapshot(path).floor


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def record_model_error(
    *,
    speaker_id: str,
    attempt_id: str,
    metric: str,
    predicted_db: float,
    realized_db: float,
    path: str | Path | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bank one ``realized − predicted`` observation, newest first.

    Sign convention, stated once so nothing has to re-derive it: ``error_db =
    realized_db - predicted_db``, on a lower-is-better grade. **Positive means
    the hardware came out worse than the model promised**, which is the
    direction that matters — it is the model over-claiming.

    ``speaker_id`` + ``attempt_id`` + ``metric`` is the stable observation
    identity. Replaying an identical write is an idempotent no-op; reusing
    that identity with different numbers raises :class:`ModelErrorConflictError`
    without changing the store. That closes the crash window between this
    write and the conductor's separate journey-state persist.

    ``context`` is free-form provenance (build sha, session id, band) stored
    verbatim under ``context``. It is never interpreted here; the store's job
    is to keep the pair, not to explain it.
    """

    speaker = str(speaker_id)
    attempt = str(attempt_id)
    metric_name = str(metric)
    if not speaker:
        raise ValueError("speaker_id must be non-empty")
    if not attempt:
        raise ValueError("attempt_id must be non-empty")
    if not metric_name:
        raise ValueError("metric must be non-empty")
    predicted = float(predicted_db)
    realized = float(realized_db)
    resolved = model_error_state_path(path)
    state = load_state(resolved)
    records = state["model_error"]
    for existing in records:
        if not (
            str(existing.get("speaker_id") or "") == speaker
            and str(existing.get("attempt_id") or "") == attempt
            and str(existing.get("metric") or "") == metric_name
        ):
            continue
        if (
            _optional_float(existing.get("predicted_db")) == predicted
            and _optional_float(existing.get("realized_db")) == realized
        ):
            log_event(
                logger,
                "active_speaker.model_error_duplicate_ignored",
                path=str(resolved),
                speaker_id=speaker,
                attempt_id=attempt,
                metric=metric_name,
            )
            return state
        log_event(
            logger,
            "active_speaker.model_error_identity_conflict",
            path=str(resolved),
            speaker_id=speaker,
            attempt_id=attempt,
            metric=metric_name,
            level=logging.WARNING,
        )
        raise ModelErrorConflictError(
            "model-error identity already exists with different values: "
            f"{speaker}/{attempt}/{metric_name}"
        )

    error_db = realized - predicted
    record = {
        "speaker_id": speaker,
        "attempt_id": attempt,
        "metric": metric_name,
        "predicted_db": predicted,
        "realized_db": realized,
        "error_db": error_db,
        "recorded_at": _utc_now(),
        "context": dict(context or {}),
    }
    state["model_error"] = (
        [record] + list(records)
    )[:MAX_MODEL_ERROR_RECORDS]
    _write_state(resolved, state)
    log_event(
        logger,
        "active_speaker.model_error_recorded",
        path=str(resolved),
        speaker_id=speaker,
        attempt_id=attempt,
        metric=metric_name,
        error_db=round(error_db, 5),
    )
    return state
