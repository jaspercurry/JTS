# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Select one compatible set of seat records before computing room views."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.json_fields import finite_float

from ..measurement_programs import (
    POSE_KIND_BEARING, PURPOSE_ROOM, resolved_measurement_purpose, validated_pose,
)
from .journey import PHASE_LATERAL
from .position_cycle import parse_curve_magnitude
from .record_index import Measurement, measurement_documents
from .measurement_context import capture_basis
from .round_captures import RoundCapturesRefused, doc_pose_key

REFUSE_ROOM_SELECTION = "room_capture_selection_required"
REFUSE_ROOM_CAPTURE = "room_capture_not_found"


@dataclass(frozen=True)
class SeatTake:
    take_id: str
    pose_key: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    gating_applied: bool | None
    band_hz: tuple[float, float]


@dataclass(frozen=True)
class SeatSelection:
    takes: tuple[SeatTake, ...]
    evidence: Mapping[str, Any]


def _take(row: Measurement, record: Mapping[str, Any]) -> SeatTake | None:
    kind = record.get("pose_kind") or POSE_KIND_BEARING
    try:
        offset, _ = validated_pose(kind, record.get("seat_offset_m"), record.get("mark_distance_m"))
    except (ValueError, TypeError):
        return None
    if offset is None and finite_float(record.get("position_deg")) is None:
        return None
    if record.get("incident") or record.get("measurement_status") == "incomplete":
        return None
    curves = record.get("curves") or []
    summed = next((c for c in curves if isinstance(c, Mapping) and c.get("role") == "summed"), None)
    parsed = parse_curve_magnitude(summed) if summed is not None else None
    if parsed is None:
        return None
    freqs, magnitude, band = parsed
    if not np.all(np.isfinite(magnitude)) or not np.all(np.diff(freqs) > 0) or freqs[0] <= 0:
        return None
    floor = finite_float(summed.get("validity_floor_hz")) if summed else None
    lo, hi = max(band[0], float(freqs[0]), floor or 0.0), min(band[1], float(freqs[-1]))
    if not np.isfinite([lo, hi]).all() or lo >= hi:
        return None
    gating = record.get("gating_applied")
    return SeatTake(
        str(record.get("take_id") or row.path), doc_pose_key(record), freqs, magnitude,
        gating if isinstance(gating, bool) else None, (lo, hi),
    )

def select_seat_takes(
    bundle_dir: Path, *, capture_id: str | None = None,
    take_ids: tuple[str, ...] | None = None, basis: Mapping[str, Any] | None = None,
) -> SeatSelection:
    """A capture id selects its whole compatible set; no selector may mix sets.

    Unknown identity stays unknown. Within a set, use the newest readable
    take per physical pose and disclose older and unusable records.
    """
    groups: dict[str, list[tuple[Measurement, Mapping[str, Any]]]] = {}
    bases: dict[str, dict[str, Any]] = {}
    for row, record in measurement_documents(bundle_dir):
        if take_ids is not None and record.get("take_id") not in take_ids:
            continue
        try:
            purpose = resolved_measurement_purpose(
                record.get("measurement_purpose"), record.get("pose_kind") or POSE_KIND_BEARING,
            )
        except ValueError:
            continue
        if row.phase != PHASE_LATERAL or purpose != PURPOSE_ROOM:
            continue
        row_basis = dict(basis) if basis is not None else capture_basis(record)
        key = "manifest" if take_ids is not None else json_fingerprint(row_basis)
        bases[key] = row_basis
        groups.setdefault(key, []).append((row, record))
    matches = [
        key for key, rows in groups.items()
        if capture_id is None or any(record.get("take_id") == capture_id for _, record in rows)
    ]
    if (capture_id is not None and not matches) or len(matches) > 1:
        raise RoundCapturesRefused(
            REFUSE_ROOM_CAPTURE if not matches else REFUSE_ROOM_SELECTION,
            {"capture_id": capture_id, "groups": [
                {"basis": bases[key], "capture_ids": [record.get("take_id") for _, record in rows]}
                for key, rows in groups.items()
            ]},
        )
    if not matches:
        return SeatSelection((), {})
    key, = matches
    rows = sorted(groups[key], key=lambda pair: (
        pair[0].captured_at or "", finite_float(pair[1].get("attempt")) or 0, pair[0].path,
    ), reverse=True)
    latest: dict[str, SeatTake] = {}
    omitted, repeats = [], []
    for row, record in rows:
        take = _take(row, record)
        take_id = str(record.get("take_id") or row.path)
        if take is None:
            omitted.append({"take_id": take_id, "record": row.path, "reason": "seat_curve_or_pose_unusable"})
        elif take.pose_key in latest:
            repeats.append(take_id)
        else:
            latest[take.pose_key] = take
    takes = tuple(reversed(latest.values()))
    selected_ids = {take.take_id for take in takes}
    return SeatSelection(takes, {
        "basis": bases[key],
        "unknown_fields": [name for name, value in bases[key].items() if value is None],
        "take_ids": [take.take_id for take in takes],
        "pose_keys": sorted(take.pose_key for take in takes),
        "omitted_takes": omitted,
        "superseded_take_ids": repeats,
        "observed_levels_db": [
            {"take_id": record.get("take_id"), "level_db": record.get("level_db"),
             "main_volume_db": (record.get("provenance") or {}).get("main_volume_db")}
            for _, record in rows if record.get("take_id") in selected_ids
        ],
    })
