# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Repeat spread arithmetic and the floor record read by the evidence builder."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Mapping, Sequence

from jasper.atomic_io import atomic_write_json
from jasper.json_fields import finite_float

from .attempts_loop import FloorStats, percentile
from jasper.json_fields import utc_now_iso as _utc_now

SCHEMA_VERSION = 1
REPEAT_FLOOR_KIND = "jts_active_speaker_repeat_floor"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_repeat_floor.json")

#: The metric a floor is ABOUT: ``spec_convergence_residual``'s own pooled
#: number, which is what the tournament reads. Named here rather than in the
#: round views so the writer, the CLI and the packet all spell it once.
SHIPPED_POOL_METRIC = "shipped_linear_pool_db"


def _state_path(path: str | Path | None) -> Path:
    return Path(path) if path is not None else DEFAULT_STATE_PATH


def pairwise_abs_deltas(values: Sequence[float]) -> list[float]:
    vs = [float(v) for v in values]
    if len(vs) < 2:
        return []
    return [abs(a - b) for i, a in enumerate(vs) for b in vs[i + 1:]]


def sample_spread(values: Sequence[float]) -> dict[str, float] | None:
    if len(values) < 2:
        return None
    return {"n": float(len(values)), "mean": mean(values), "sd": stdev(values),
            "range": max(values) - min(values), "min": min(values), "max": max(values)}


def derive_repeat_floor(
    result: Any = None, *, rounds: Sequence[Mapping[str, Any]],
    samples: Mapping[str, Sequence[float]] | None = None,
    units: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Round metrics or per-take samples, with one provenance row per observation.

    Existing dB columns stay unchanged; non-dB metrics carry their native unit.
    """
    round_axis = samples is None
    if samples is None:
        samples = {metric.name: list(metric.values.values()) for metric in result.metrics}
    metrics = {}
    for name, values in samples.items():
        if any(finite_float(value) is None for value in values):
            raise ValueError("repeat samples must be finite")
        spread = sample_spread(values)
        if spread is None:
            continue
        unit = (units or {}).get(name, "db")
        deltas = pairwise_abs_deltas(values)
        metrics[name] = {
            "n": int(spread["n"]),
            **{f"{key}_{unit}": spread[key] for key in ("mean", "sd", "range", "min", "max")},
            f"pairwise_abs_delta_p95_{unit}": percentile(deltas, 95.0),
            f"pairwise_abs_delta_median_{unit}": percentile(deltas, 50.0),
            **({"unit": unit} if unit != "db" else {}),
        }
    if not metrics or (round_axis and SHIPPED_POOL_METRIC not in metrics):
        raise ValueError("a repeat floor needs at least two observations of its metric")
    return {
        "artifact_schema_version": SCHEMA_VERSION, "kind": REPEAT_FLOOR_KIND,
        "measured_at": _utc_now(),
        "n_repeats": len(result.round_labels) if round_axis else len(rounds),
        "aggregate_metric": SHIPPED_POOL_METRIC if round_axis else None,
        "rounds": [dict(row) for row in rounds], "metrics": metrics,
        "note": "touched-nothing fixed-pose repeats; random error only (ADR-0202)",
    }


def repeat_pair(
    take: Mapping[str, Sequence[float]], trims: Mapping[str, Sequence[float]], floor: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare the first two takes against a previously banked floor (ADR-0302)."""
    disagreements, unmeasured = [], []
    limits: dict[str, float] = {}
    metrics = [(name, values, {"scope": "take", "metric": name}) for name, values in take.items()]
    metrics += [(f"{role}_trim_db", values, {"role": role, "metric": "trim_db"}) for role, values in trims.items()]
    for name, values, finding in metrics:
        delta = abs(values[0] - values[1])
        if name == "polarity":
            if delta:
                disagreements.append(finding)
            continue
        thresholds = stopping_thresholds({**(floor or {}), "aggregate_metric": name})
        unit = "us" if name == "delay_us" else "db"
        threshold = thresholds.get(f"margin_{unit}") if thresholds else None
        if threshold is None:
            unmeasured.append(finding)
        else:
            limits[name] = threshold
            if delta > threshold:
                disagreements.append(finding)
    return {"pair": "disagrees" if disagreements else "unmeasured" if unmeasured else "agrees",
            "disagreements": disagreements, "unmeasured": unmeasured, "pair_limits": limits}


def write_repeat_floor(
    payload: Mapping[str, Any], *, state_path: str | Path | None = None
) -> dict[str, Any]:
    record = dict(payload)
    atomic_write_json(_state_path(state_path), record)
    return record


def load_repeat_floor(
    *, state_path: str | Path | None = None
) -> dict[str, Any] | None:
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
    aggregate = record.get("metrics")
    if not isinstance(aggregate, Mapping):
        return None
    row = aggregate.get(record.get("aggregate_metric"))
    if not isinstance(row, Mapping):
        return None
    unit = row.get("unit", "db")
    p95 = finite_float(row.get(f"pairwise_abs_delta_p95_{unit}"))
    median = finite_float(row.get(f"pairwise_abs_delta_median_{unit}"))
    if p95 is None or median is None:
        return None
    try:
        floor = FloorStats.from_repeat_study(
            metric=str(record.get("aggregate_metric") or ""),
            median_db=median,
            p95_db=p95,
            source=REPEAT_FLOOR_KIND,
            measured_at=str(record.get("measured_at") or ""),
        )
    except ValueError:  # from_repeat_study refuses p95 <= 0 and an empty metric name
        return None
    return {
        f"plateau_{unit}": floor.p95_db,
        f"margin_{unit}": floor.claim_floor_db,
        "formula": (
            f"plateau_{unit} = p95(|delta| between two touched-nothing repeats of "
            f"the aggregate metric); margin_{unit} = CLAIM_FLOOR_P95_MULTIPLE * "
            f"plateau_{unit}"
        ),
    }
