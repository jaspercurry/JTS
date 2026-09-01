# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured repeat floor — one writer, one durable file the packet reads.

Calibration experiment E2 asks one question: repeat a program touching
nothing, and how far does the aggregate metric move? That spread is the
instrument's own random noise (ADR-0202), and until it is banked somewhere a
reader can find, every threshold derived from it is an assumption
(``round_evidence.MEASURED_BENEFIT_MARGIN_DB`` and ``ITERATION_PLATEAU_DB``
both say so about themselves).

Ownership, deliberately narrow:

* **one writer** — ``jasper-round-views repeat-floor``, over N banked
  touched-nothing repeat rounds;
* **one reader** — the evidence packet's
  ``accuracy_budget.components.in_capture_repeat_floor``;
* **absent is normal.** A rig that never ran the repeats has no floor, and
  the packet reports that rather than defaulting one.

This module imports nothing from ``crossover_v2``: the packet imports it, and
the CLI hands it a duck-typed ``RepeatabilityResult`` the round views own.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.atomic_io import atomic_write_json

from .attempts_loop import CLAIM_FLOOR_P95_MULTIPLE

SCHEMA_VERSION = 1
REPEAT_FLOOR_KIND = "jts_active_speaker_repeat_floor"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_repeat_floor.json")

#: The metric a floor is ABOUT: ``spec_convergence_residual``'s own pooled
#: number, which is what the tournament reads. Named here rather than in the
#: round views so the writer, the CLI and the packet all spell it once.
SHIPPED_POOL_METRIC = "shipped_linear_pool_db"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_path(path: str | Path | None) -> Path:
    return Path(path) if path is not None else DEFAULT_STATE_PATH


def pairwise_abs_delta_p95(values: Sequence[float]) -> float | None:
    """The 95th percentile of every pair's absolute difference.

    ``MEASURED_BENEFIT_MARGIN_DB``'s own recipe: difference the two pooled
    values, repeat, take that distribution's p95. ``None`` below two values —
    one measurement has no difference to take.
    """
    vs = [float(v) for v in values]
    if len(vs) < 2:
        return None
    deltas = [abs(a - b) for i, a in enumerate(vs) for b in vs[i + 1:]]
    return float(np.percentile(deltas, 95))


def derive_repeat_floor(
    result: Any, *, rounds: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Turn one repeatability view into the durable record.

    ``result`` is duck-typed on the round views' ``RepeatabilityResult``
    (``round_labels``, and ``metrics`` each with ``name``/``values``/
    ``spread()``); ``rounds`` is one provenance row per repeat, built by the
    caller from each round's packet.

    Raises :class:`ValueError` when the aggregate metric has no spread: a
    floor with no aggregate row is not a floor.
    """
    metrics: dict[str, dict[str, float | None]] = {}
    for metric in result.metrics:
        spread = metric.spread()
        if spread is None:
            continue
        metrics[metric.name] = {
            "n": int(spread["n"]),
            "mean_db": spread["mean"],
            "sd_db": spread["sd"],
            "range_db": spread["range"],
            "min_db": spread["min"],
            "max_db": spread["max"],
            "pairwise_abs_delta_p95_db": pairwise_abs_delta_p95(
                list(metric.values.values())
            ),
        }
    if SHIPPED_POOL_METRIC not in metrics:
        raise ValueError(
            f"no spread for {SHIPPED_POOL_METRIC}: a repeat floor needs at "
            "least two rounds that graded the aggregate metric"
        )
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": REPEAT_FLOOR_KIND,
        "measured_at": _utc_now(),
        "n_repeats": len(result.round_labels),
        "aggregate_metric": SHIPPED_POOL_METRIC,
        "rounds": [dict(row) for row in rounds],
        "metrics": metrics,
        "note": (
            "touched-nothing fixed-pose repeats; the spread is the RANDOM "
            "term only (ADR-0202) and the systematic bounds — mic-calibration "
            "tier, gate leakage — live elsewhere in the accuracy budget"
        ),
    }


def write_repeat_floor(
    payload: Mapping[str, Any], *, state_path: str | Path | None = None
) -> dict[str, Any]:
    """Publish one floor. Returns the payload as written, ``state_path``
    included, so a caller can report where it landed."""
    path = _state_path(state_path)
    record = {**dict(payload), "state_path": str(path)}
    atomic_write_json(path, record, mode=0o640, group_from_parent=True)
    return record


def load_repeat_floor(
    *, state_path: str | Path | None = None
) -> dict[str, Any] | None:
    """The persisted floor, or ``None`` when there is none.

    Absent-tolerant and never raises: unreadable, malformed, wrong-kind and
    wrong-schema are all indistinguishable from no file, because the reader's
    fallback — report the floor unmeasured — is the honest answer in every one
    of those cases.
    """
    path = _state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("kind") != REPEAT_FLOOR_KIND
        or raw.get("artifact_schema_version") != SCHEMA_VERSION
    ):
        return None
    return raw


def stopping_thresholds(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The plateau and benefit margin this floor implies, or ``None``.

    The plateau is ``ITERATION_PLATEAU_DB``'s own TODO ("the movement that
    study cannot distinguish from noise") and the margin is
    ``MEASURED_BENEFIT_MARGIN_DB``'s own house rule
    (:data:`~jasper.active_speaker.attempts_loop.CLAIM_FLOOR_P95_MULTIPLE`).
    The same 2× keeps plateau = margin/2, the relationship ``round_evidence``
    calls load-bearing.
    """
    aggregate = record.get("metrics")
    if not isinstance(aggregate, Mapping):
        return None
    row = aggregate.get(record.get("aggregate_metric"))
    if not isinstance(row, Mapping):
        return None
    noise = row.get("pairwise_abs_delta_p95_db")
    if isinstance(noise, bool) or not isinstance(noise, (int, float)):
        return None
    noise = float(noise)
    if not math.isfinite(noise):
        return None
    return {
        "noise_p95_db": noise,
        "plateau_db": noise,
        "margin_db": CLAIM_FLOOR_P95_MULTIPLE * noise,
        "n_repeats": record.get("n_repeats"),
        "formula": (
            "plateau_db = p95(|delta| between two touched-nothing repeats of "
            "the aggregate metric); margin_db = CLAIM_FLOOR_P95_MULTIPLE * "
            "plateau_db"
        ),
    }
