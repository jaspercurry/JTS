# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Renderer-neutral frequency-response contract for the web page and CLI."""

from __future__ import annotations

import math
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .frequency_display import prepare_frequency_curve

SCHEMA = "jts_frequency_view/1"
FREQUENCY_VIEW_FILENAME = "frequency_view.json"


class FrequencyViewError(ValueError):
    """A run cannot supply a valid frequency-response view."""


@dataclass(frozen=True)
class FrequencySeries:
    """One stored response curve plus the facts needed to draw it."""

    id: str
    label: str
    kind: str
    freqs_hz: tuple[float, ...]
    magnitude_db: tuple[float, ...]
    reference_db: float | None = None
    smoothing_fractional_octave: int | None = None
    visible_by_default: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise FrequencyViewError("a frequency series needs an id")
        if not self.freqs_hz or len(self.freqs_hz) != len(self.magnitude_db):
            raise FrequencyViewError(
                f"frequency series {self.id!r} needs matching non-empty arrays"
            )
        if not all(
            math.isfinite(value) for value in self.freqs_hz + self.magnitude_db
        ):
            raise FrequencyViewError(f"frequency series {self.id!r} must be finite")
        if self.reference_db is not None and not math.isfinite(self.reference_db):
            raise FrequencyViewError(
                f"frequency series {self.id!r} reference must be finite"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            **dict(self.details),
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "freqs_hz": list(self.freqs_hz),
            "magnitude_db": list(self.magnitude_db),
            "reference_db": self.reference_db,
            "smoothing_fractional_octave": self.smoothing_fractional_octave,
            "visible_by_default": self.visible_by_default,
        }


@dataclass(frozen=True)
class FrequencyRun:
    """One saved measurement or analysis, independent of its producer."""

    id: str
    measurement_family: str
    series: tuple[FrequencySeries, ...]
    label: str = ""
    started_at: Any = None
    state: str | None = None
    round_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise FrequencyViewError("a frequency run needs an id")
        ids = [series.id for series in self.series]
        if len(ids) != len(set(ids)):
            raise FrequencyViewError(f"frequency run {self.id!r} has duplicate series ids")


def frequency_series(
    *,
    series_id: str,
    label: str,
    kind: str,
    freqs_hz: Any,
    magnitude_db: Any,
    reference_db: Any = None,
    smoothing_fractional_octave: int | None = None,
    visible_by_default: bool = False,
    **details: Any,
) -> FrequencySeries | None:
    """Normalize one JSON-shaped curve, or return ``None`` when unusable."""

    if not isinstance(freqs_hz, Sequence) or isinstance(freqs_hz, (str, bytes)):
        return None
    if not isinstance(magnitude_db, Sequence) or isinstance(
        magnitude_db, (str, bytes)
    ):
        return None
    if not freqs_hz or len(freqs_hz) != len(magnitude_db):
        return None
    try:
        frequencies = tuple(float(value) for value in freqs_hz)
        magnitudes = tuple(float(value) for value in magnitude_db)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in frequencies + magnitudes):
        return None
    reference = (
        float(reference_db)
        if isinstance(reference_db, (int, float)) and not isinstance(reference_db, bool)
        else None
    )
    if reference is not None and not math.isfinite(reference):
        reference = None
    return FrequencySeries(
        id=series_id,
        label=label,
        kind=kind,
        freqs_hz=frequencies,
        magnitude_db=magnitudes,
        reference_db=reference,
        smoothing_fractional_octave=smoothing_fractional_octave,
        visible_by_default=visible_by_default,
        details=details,
    )


def build_frequency_view(
    run_a: FrequencyRun,
    run_b: FrequencyRun | None = None,
) -> dict[str, Any]:
    """Project one or two neutral runs into the shared web/LLM document."""

    runs = [run_a] + ([run_b] if run_b is not None else [])
    projected = []
    for index, run in enumerate(runs):
        slot = "ab"[index]
        projected.append({
            "slot": slot,
            "id": run.id,
            "label": run.label or f"Run {slot.upper()}",
            "measurement_family": run.measurement_family,
            "started_at": run.started_at,
            "round_id": run.round_id,
            "state": run.state,
            "metadata": dict(run.metadata),
            "series": [prepare_frequency_curve(series.to_dict(), run.metadata) for series in run.series],
        })
    return {
        "schema": SCHEMA,
        "normalization": {
            "kind": "series_reference",
            "note": (
                "Every series declares its display reference_db. Comparable "
                "curves in one direct run share a reference so measured level "
                "differences remain visible. An explicit baseline may use its "
                "own stored or evaluated reference."
            ),
        },
        "runs": projected,
    }


def frequency_run_from_view(document: Mapping[str, Any]) -> FrequencyRun:
    """Read one saved frequency view through the shared series contract."""
    if len(document["runs"]) != 1:
        raise FrequencyViewError("as an input, a frequency view must contain one run")
    raw = document["runs"][0]
    curves = []
    for item in raw["series"]:
        fields = dict(item)
        fields["series_id"] = fields.pop("id")
        if (curve := frequency_series(**fields)) is not None:
            curves.append(curve)
    return FrequencyRun(id=raw["id"], label=raw.get("label", ""),
                        measurement_family=raw["measurement_family"], series=tuple(curves),
                        metadata=raw.get("metadata", {}))


def manifest_frequency_run(manifest: Mapping[str, Any]) -> FrequencyRun:
    from .frequency_plot import prepare_plot_curve  # lazy: numerical display import cost

    series = []
    for group in manifest.get("sets", ()):
        for take in group["takes"]:
            if not take["selected"] or not take.get("curve"):
                continue
            curve, pose = take["curve"], take["pose"]
            item = frequency_series(
                **{key: value for key, value in curve.items() if key != "role"},
                series_id=f"{group['set_id']}:{take['take_id']}", label=take["role"], kind="measured",
                set_id=group["set_id"], take_id=take["take_id"], role=take["role"],
                base=group.get("base", False), candidate_id=group["capture_basis"].get("candidate_id"),
                graph_fingerprint=group["capture_basis"].get("graph_fingerprint"),
                position={**pose, "id": json.dumps(pose, sort_keys=True), "vertical_deg": pose.get("elevation_deg")},
                visible_by_default=True,
            )
            if item is not None:
                series.append(item)
    references: dict[str, float] = {}
    for curve in sorted(series, key=lambda curve: not curve.details["base"]):
        role = curve.details["role"]
        if role not in references:
            references[role] = prepare_plot_curve(curve.to_dict())["reference_db"]
    return FrequencyRun(id=manifest["run_id"], measurement_family="speaker",
                        series=tuple(replace(curve, reference_db=references[curve.details["role"]]) for curve in series))
