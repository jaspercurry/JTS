# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read saved speaker measurements into the neutral frequency-view model.

The archive is an adapter over files, not part of a tuning flow. It reads a
bundle's banked take records and its entry baseline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED

from . import bundles
from .frequency_view import FrequencyRun, FrequencySeries
from .measurement_document import frequency_run_from_documents
from .crossover_v2.evidence_packet import CrossoverEvidencePacketError, entry_evidence
from .crossover_v2.record_index import has_banked_take, measurement_documents
from .crossover_v2.round_frequency_view import frequency_run as packet_frequency_run
from .crossover_v2.round_inputs import capture_identity


@dataclass(frozen=True)
class ArchivedMeasurement:
    """The catalog facts needed to select one bundle."""

    id: str
    bundle_dir: Path
    started_at: Any = None
    state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "started_at": self.started_at, "state": self.state}


def _combined_position_metadata(
    series: tuple[FrequencySeries, ...],
) -> tuple[int, list[int]]:
    positions: set[tuple[Any, ...]] = set()
    angles: set[int] = set()
    for item in series:
        position = item.details.get("position")
        if not isinstance(position, Mapping):
            continue
        raw_degrees = position.get("deg")
        raw_vertical = position.get("vertical_deg")
        degrees = (
            raw_degrees
            if isinstance(raw_degrees, int) and not isinstance(raw_degrees, bool)
            else None
        )
        vertical = (
            raw_vertical
            if isinstance(raw_vertical, int) and not isinstance(raw_vertical, bool)
            else None
        )
        if degrees is None and not vertical:
            continue
        positions.add((degrees, vertical or 0))
        if degrees is not None:
            angles.add(degrees)
    return len(positions), sorted(angles)


def list_measurements(sessions_dir: Path) -> tuple[ArchivedMeasurement, ...]:
    """List every bundle that banked a take."""

    runs = []
    for entry in bundles.list_bundles(sessions_dir):
        run_id = str(entry.get("session_id") or "")
        bundle_dir = Path(str(entry.get("bundle_dir") or ""))
        if not run_id or not has_banked_take(bundle_dir):
            continue
        runs.append(ArchivedMeasurement(
            id=run_id,
            bundle_dir=bundle_dir,
            started_at=entry.get("started_at"),
            state=str(entry.get("state") or "") or None,
        ))
    return tuple(runs)


def load_measurement(run: ArchivedMeasurement) -> FrequencyRun:
    """Load one archive entry, preferring its direct measurement records."""

    takes = tuple(measurement_documents(run.bundle_dir))
    try:
        retained = packet_frequency_run(entry_evidence(run.bundle_dir, tuple(row for row, _ in takes)))
    except (CrossoverEvidencePacketError, OSError, TypeError, ValueError):
        retained = None

    direct = frequency_run_from_documents(
        run_id=run.id,
        documents=[document for _, document in takes],
        started_at=run.started_at,
        state=run.state,
    )

    if retained is None or not (direct.series or retained.series):
        # A run with no curve says why instead of drawing nothing (ADR-0373).
        return direct if direct.series else replace(direct, metadata={
            **direct.metadata, "curves": {"status": "unavailable", "reason": TAKE_CURVES_NOT_BANKED}})
    if not direct.series:
        return replace(retained, started_at=run.started_at, state=run.state)
    identities = {capture_identity(curve.details, set_id=curve.details.get("set_id") or curve.id)
                  for curve in direct.series if curve.details.get("role") == "summed"}
    if len(identities) > 1:
        return direct

    # The packet owns the entry baseline; direct records own every take's curve.
    summary = tuple(series for series in retained.series if series.kind == "entry_baseline")
    direct_series = direct.series
    if summary:
        direct_series = tuple(
            series for series in direct_series
            if series.details.get("phase") != "entry_baseline"
        )
    combined = summary + direct_series
    position_count, angles_deg = _combined_position_metadata(combined)
    return replace(
        direct,
        series=tuple(
            replace(series, visible_by_default=(index == 0))
            for index, series in enumerate(combined)
        ),
        metadata={
            **dict(direct.metadata),
            **dict(retained.metadata),
            "position_count": position_count,
            "angles_deg": angles_deg,
        },
    )
