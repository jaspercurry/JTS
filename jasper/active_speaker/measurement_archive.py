# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read saved speaker measurements into the neutral frequency-view model.

The archive is an adapter over files, not part of a tuning flow.  It accepts
the current banked ``curves`` records, direct response documents, and retained
round packets from before curves rode on every take.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .frequency_view import FrequencyRun, FrequencySeries
from .measurement_document import frequency_run_from_documents


@dataclass(frozen=True)
class ArchivedMeasurement:
    """The catalog facts needed to select one bundle."""

    id: str
    bundle_dir: Path
    started_at: Any = None
    state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "started_at": self.started_at, "state": self.state}


def _position_key(series: FrequencySeries) -> tuple[Any, Any] | None:
    position = series.details.get("position")
    if not isinstance(position, Mapping):
        return None
    return position.get("deg"), position.get("vertical_deg") or 0


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
        has_location = degrees is not None or (vertical is not None and vertical != 0)
        if item.kind != "position" and not has_location:
            continue
        if has_location:
            positions.add((degrees, vertical or 0))
        elif item.details.get("take_id"):
            positions.add(("take", item.details["take_id"]))
        else:
            positions.add(("series", item.id))
        if degrees is not None:
            angles.add(degrees)
    return len(positions), sorted(angles)


def _measurement_documents(bundle_dir: Path) -> list[Mapping[str, Any]]:
    from .commissioning_evidence_store import EVIDENCE_ROOT
    from .crossover_v2.record_index import bundle_measurements

    artifacts = bundle_dir / EVIDENCE_ROOT / "artifacts"
    documents = []
    for row in bundle_measurements(bundle_dir):
        try:
            value = json.loads((artifacts / row.path).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, Mapping):
            documents.append(value)
    return documents


def list_measurements(sessions_dir: Path) -> tuple[ArchivedMeasurement, ...]:
    """List every bundle that carries measurement records or old round evidence."""

    from . import bundles
    from .crossover_v2.evidence_packet import round_artifact_dir
    from .crossover_v2.record_index import bundle_measurements

    runs = []
    for entry in bundles.list_bundles(sessions_dir):
        run_id = str(entry.get("session_id") or "")
        bundle_dir = Path(str(entry.get("bundle_dir") or ""))
        round_dir, _ = round_artifact_dir(bundle_dir)
        if not run_id or (round_dir is None and not bundle_measurements(bundle_dir)):
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

    documents = _measurement_documents(run.bundle_dir)
    from .crossover_v2.evidence_packet import (
        CrossoverEvidencePacketError,
        build_crossover_evidence_packet,
    )
    from .crossover_v2.frequency_view import frequency_run as packet_frequency_run

    try:
        retained = packet_frequency_run(build_crossover_evidence_packet(run.bundle_dir))
    except (CrossoverEvidencePacketError, OSError, TypeError, ValueError):
        retained = None

    retained_reference = (
        retained.metadata.get("reference_db") if retained is not None else None
    )
    fallback_reference = (
        float(retained_reference)
        if isinstance(retained_reference, (int, float))
        and not isinstance(retained_reference, bool)
        and math.isfinite(retained_reference)
        else None
    )
    direct = frequency_run_from_documents(
        run_id=run.id,
        documents=documents,
        started_at=run.started_at,
        state=run.state,
        run_reference_db=fallback_reference,
    )

    if retained is None:
        return direct
    if not direct.series:
        return replace(retained, started_at=run.started_at, state=run.state)

    # The packet owns the stored aggregate. Direct records own individual
    # curves. Packet positions remain only where no direct record replaces them.
    summary = tuple(
        series for series in retained.series
        if series.kind in {"average", "entry_baseline"}
    )
    direct_series = direct.series
    if any(series.kind == "entry_baseline" for series in summary):
        direct_series = tuple(
            series for series in direct_series
            if series.details.get("phase") != "entry_baseline"
        )
    direct_take_ids = {
        str(series.details.get("take_id"))
        for series in direct_series
        if series.details.get("take_id")
    }
    direct_positions = {
        key
        for series in direct_series
        if series.details.get("role") == "summed"
        and (key := _position_key(series)) is not None
    }
    retained_positions = tuple(
        series for series in retained.series
        if series.kind == "position"
        and str(series.details.get("take_id") or "") not in direct_take_ids
        and (
            (key := _position_key(series)) is None
            or key not in direct_positions
        )
    )
    combined = summary + retained_positions + direct_series
    position_count, angles_deg = _combined_position_metadata(combined)
    return replace(
        direct,
        series=tuple(
            replace(series, visible_by_default=(index == 0))
            for index, series in enumerate(combined)
        ),
        round_id=retained.round_id,
        metadata={
            **dict(direct.metadata),
            **dict(retained.metadata),
            "position_count": position_count,
            "angles_deg": angles_deg,
        },
    )
