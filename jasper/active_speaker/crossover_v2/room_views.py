# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room statistics over one selected set of banked seat measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.room_boundary import (
    CEILING_SOURCE_FALLBACK,
    CEILING_SOURCE_ROUND_GATE,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
    ROOM_FLOOR_HZ,
    ROOM_MEDIAN_WINDOW,
    room_ceiling_hz,
)
from jasper.audio_measurement.measurement_geometry import (
    boundary_prior, load_declared_geometry,
)
from jasper.audio_measurement.room_limits import spatial_support
from jasper.json_fields import finite_float
from jasper.active_speaker.run_manifest import room_sets, view_sets

from .evidence_packet import applied_profile_source
from .prescription_contract import room_analysis_bounds
from .room_prescription import ROOM_MEDIAN_FIELD, read_room_median
from .room_selection import SeatTake
from .record_index import measurement_documents
from .round_views import RoundViewsError, local_features

#: A feature is a local excursion at least this deep against the local level,
#: at least this wide between its half-depth edges; positions agree on it
#: when their depths sit within the same span.
FEATURE_DEPTH_DB = 3.0
FEATURE_MIN_WIDTH_OCTAVES = 1.0 / 6.0
FEATURE_AGREEMENT_DB = 3.0

#: The robust local level a feature is read against: a running median over
#: this many octaves either side, in dB, so a mode neither lifts its own
#: baseline the way a power mean would nor hides in a dip.
TREND_HALF_WIDTH_OCTAVES = 0.5

#: Where the room band splits: modes below 60 Hz, the modal-to-transition
#: region to 120 Hz, the rest to the ceiling.
ROOM_BAND_SPLITS_HZ = (60.0, 120.0)


def band_edges(ceiling_hz: float, floor_hz: float = ROOM_FLOOR_HZ) -> tuple[tuple[float, float], ...]:
    edges = (floor_hz, *(split for split in ROOM_BAND_SPLITS_HZ if floor_hz < split < ceiling_hz), ceiling_hz)
    return tuple(zip(edges, edges[1:]))


def band_masks(
    freqs_hz: Any, ceiling_hz: float,
) -> tuple[tuple[float, float, np.ndarray], ...]:
    """Each band's bins on ``freqs_hz``: half-open below a split and closed at
    the ceiling, so a bin sitting on a split is counted once."""
    freqs = np.asarray(freqs_hz, dtype=float)
    edges = band_edges(ceiling_hz, float(freqs[0]))
    return tuple(
        (lo, hi, (freqs >= lo) & ((freqs <= hi) if index == len(edges) - 1 else (freqs < hi)))
        for index, (lo, hi) in enumerate(edges)
    )


@dataclass(frozen=True)
class Ceiling:
    """Where the room layer stops, and where that number came from."""

    ceiling_hz: float
    source: str
    trusted_floor_hz: float | None
    source_take_id: str | None
    source_curve_role: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceiling_hz": self.ceiling_hz,
            "ceiling_source": self.source,
            "trusted_floor_hz": self.trusted_floor_hz,
            "source_take_id": self.source_take_id,
            "source_curve_role": self.source_curve_role,
            "clamp_hz": [ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ],
            "reason": self.reason,
        }


def room_ceiling(bundle_dir: Path) -> Ceiling:
    """The highest trusted floor disclosed by a gated take in this round."""
    floors = []
    for row, record in measurement_documents(bundle_dir):
        gating_applied = record.get("gating_applied")
        if gating_applied is False:
            continue
        take_id = str(record.get("take_id") or row.path)
        for curve in record.get("curves") or ():
            if not isinstance(curve, Mapping):
                continue
            gate_window_ms = finite_float(curve.get("gate_window_ms"))
            if gating_applied is not True and not (gate_window_ms is not None and gate_window_ms > 0):
                continue
            role = curve.get("role")
            if not isinstance(role, str) or not role:
                continue
            trusted = finite_float(curve.get("trusted_floor_hz"))
            if trusted is not None and trusted > 0:
                floors.append((trusted, take_id, role))
    source = max(floors, default=None)
    trusted = source[0] if source is not None else None
    return Ceiling(
        ceiling_hz=room_ceiling_hz(trusted),
        source=CEILING_SOURCE_FALLBACK if source is None else CEILING_SOURCE_ROUND_GATE,
        trusted_floor_hz=trusted,
        source_take_id=source[1] if source is not None else None,
        source_curve_role=source[2] if source is not None else None,
        reason="the round has no gated summed or driver take" if source is None else "",
    )


def _window(takes: Sequence[SeatTake]) -> str:
    """What the takes say about their own window, never assumed."""
    applied = {take.gating_applied for take in takes}
    if applied == {False}:
        return ROOM_MEDIAN_WINDOW
    return "gated" if applied == {True} else "mixed"


def _stacked(
    takes: Sequence[SeatTake], lo_hz: float, hi_hz: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate only within the band every selected take measured."""
    lo_hz = max(lo_hz, *(take.band_hz[0] for take in takes))
    hi_hz = min(hi_hz, *(take.band_hz[1] for take in takes))
    grid = takes[0].freqs_hz
    keep = (grid >= lo_hz) & (grid <= hi_hz)
    freqs = grid[keep]
    if lo_hz >= hi_hz:
        raise RoundViewsError(
            f"no seat take carries a bin between {lo_hz:g} and {hi_hz:g} Hz "
            f"(the takes span {float(grid[0]):g}-{float(grid[-1]):g} Hz)"
        )
    freqs = np.unique(np.concatenate(([lo_hz], freqs, [hi_hz])))
    rows = np.vstack([
        np.interp(freqs, take.freqs_hz, take.magnitude_db) for take in takes
    ])
    return freqs, rows


def _coverage_hz(takes: Sequence[SeatTake], ceiling: Ceiling) -> list[float]:
    return [max(ROOM_FLOOR_HZ, *(take.band_hz[0] for take in takes)),
            min(ceiling.ceiling_hz, *(take.band_hz[1] for take in takes))]


# --------------------------------------------------------------------------- #
# the median
# --------------------------------------------------------------------------- #


def room_median(takes: Sequence[SeatTake], ceiling: Ceiling) -> dict[str, Any]:
    """The contract a room candidate reads: median, spread, deviations."""
    coverage_hz = _coverage_hz(takes, ceiling)
    freqs, rows = _stacked(takes, coverage_hz[0], ceiling.ceiling_hz)
    median = np.median(rows, axis=0)
    support = spatial_support(len(takes))
    return {
        "freqs_hz": freqs.tolist(),
        "median_db": median.tolist(),
        "spread_db": np.std(rows, axis=0).tolist() if support["sufficient"] else None,
        "spatial_support": support,
        "n_positions": len(takes),
        "positions": [
            {
                "id": take.take_id,
                "pose_key": take.pose_key,
                "deviation_db": (row - median).tolist(),
            }
            for take, row in zip(takes, rows)
        ],
        "ceiling_hz": ceiling.ceiling_hz,
        "ceiling_source": ceiling.source,
        "window": _window(takes),
        "coverage_hz": coverage_hz,
    }


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def _running_median_db(freqs: np.ndarray, magnitude_db: np.ndarray) -> np.ndarray:
    factor = 2.0 ** TREND_HALF_WIDTH_OCTAVES
    lo = np.searchsorted(freqs, freqs / factor, side="left")
    hi = np.searchsorted(freqs, freqs * factor, side="right")
    return np.asarray([float(np.median(magnitude_db[a:b])) for a, b in zip(lo, hi)])


@dataclass(frozen=True)
class _Feature:
    kind: str
    f_lo: float
    f_hi: float
    centre_hz: float
    depth_db: float

    @property
    def width_octaves(self) -> float:
        return math.log2(self.f_hi / self.f_lo)


def _features(freqs: np.ndarray, residual: np.ndarray, ceiling_hz: float) -> list[_Feature]:
    """One position's excursions against its local level, wide enough to count.

    Searched over the margin the grid carries above the ceiling, so an extremum
    on the ceiling's edge bin is judged against real neighbours; nothing is
    measured below the coverage floor, so the trend there is one-sided. Only
    centres inside the band count.
    """
    found = []
    for centre, lo, hi in local_features(
        freqs, residual, lo_hz=float(freqs[0]), hi_hz=float(freqs[-1]), feature_db=FEATURE_DEPTH_DB,
    ):
        feature = _Feature(
            "peak" if residual[centre] > 0 else "dip",
            float(freqs[lo]), float(freqs[hi]), float(freqs[centre]), float(residual[centre]),
        )
        if (
            feature.centre_hz <= ceiling_hz
            and feature.width_octaves >= FEATURE_MIN_WIDTH_OCTAVES
        ):
            found.append(feature)
    return found


def room_persistence(takes: Sequence[SeatTake], ceiling: Ceiling) -> dict[str, Any]:
    """Which features hold across the cube, and at how many positions."""
    # A half-octave margin above the ceiling keeps the trend window whole there.
    margin = 2.0 ** TREND_HALF_WIDTH_OCTAVES
    coverage_hz = _coverage_hz(takes, ceiling)
    freqs, rows = _stacked(takes, coverage_hz[0], ceiling.ceiling_hz * margin)
    candidates = sorted(
        (
            (position, feature)
            for position, row in enumerate(rows)
            for feature in _features(freqs, row - _running_median_db(freqs, row), ceiling.ceiling_hz)
        ),
        key=lambda item: -abs(item[1].depth_db),
    )
    clusters: list[tuple[_Feature, dict[int, _Feature]]] = []
    for position, feature in candidates:
        for seed, members in clusters:
            if (
                seed.kind == feature.kind and position not in members
                and seed.f_lo <= feature.centre_hz <= seed.f_hi
            ):
                members[position] = feature
                break
        else:
            clusters.append((feature, {position: feature}))
    features: list[dict[str, Any]] = []
    for seed, members in clusters:
        depths = np.asarray([member.depth_db for member in members.values()])
        median_depth = float(np.median(depths))
        present = int(np.count_nonzero(np.abs(depths - median_depth) <= FEATURE_AGREEMENT_DB))
        features.append({
            "kind": seed.kind,
            "centre_hz": float(np.exp(np.mean([math.log(m.centre_hz) for m in members.values()]))),
            "band_hz": [seed.f_lo, seed.f_hi],
            "width_octaves": float(np.median([m.width_octaves for m in members.values()])),
            "median_depth_db": median_depth,
            "n_present": present,
            "presence_fraction": present / len(takes),
        })
    features.sort(key=lambda f: (-f["presence_fraction"], -abs(f["median_depth_db"])))
    return {
        "n_positions": len(takes),
        "spatial_support": spatial_support(len(takes)),
        "ceiling_hz": ceiling.ceiling_hz,
        "ceiling_source": ceiling.source,
        "window": _window(takes),
        "coverage_hz": coverage_hz,
        "thresholds": {
            "depth_db": FEATURE_DEPTH_DB,
            "min_width_octaves": FEATURE_MIN_WIDTH_OCTAVES,
            "agreement_db": FEATURE_AGREEMENT_DB,
            "trend_half_width_octaves": TREND_HALF_WIDTH_OCTAVES,
        },
        "features": features,
    }


def incumbent_room(
    profile: Mapping[str, Any] | None, manifest: Mapping[str, Any], *, set_id: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    profile = profile or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    correction = snapshot.get("room_correction", profile.get("room_correction")) or {}
    basis = correction.get("basis") or {}
    matches = [row["set_id"] for row in (room_sets(manifest) or view_sets(manifest)) if row.get("base")]
    if len(matches) != 1:
        return None, "room_incumbent_set_ambiguous" if matches else "room_incumbent_set_unavailable"
    return {
        "round_id": basis.get("round_id"), ROOM_MEDIAN_FIELD: basis.get(ROOM_MEDIAN_FIELD),
        "set_id": matches[0],
    }, ""


def room_median_sha256(median: Mapping[str, Any]) -> str:
    """Bind the measured median independently of the document's current incumbent."""
    return json_fingerprint(median, field_name="room median")


def room_document(
    takes: Sequence[SeatTake], *, set_id: str, evidence: Mapping[str, Any],
    bundle_dir: Path, applied_profile_path: Path | None, geometry_path: Path | None,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    profile, _ = applied_profile_source(applied_profile_path)
    ceiling = room_ceiling(bundle_dir)
    median = {**room_median(takes, ceiling), "set_id": set_id, "evidence": dict(evidence)}
    value = read_room_median(median)
    persistence = room_persistence(takes, ceiling)
    limits = room_analysis_bounds(value, persistence)
    geometry = load_declared_geometry(geometry_path) if geometry_path is not None else None
    walls, boundary_reason = geometry.boundary_walls() if geometry else ({}, "geometry_undeclared")
    boundary = {"advisory": True, **boundary_prior(value.freqs_hz, walls=walls)} if walls else None
    if boundary is not None and geometry is not None and geometry.cabinet_back_wall_m is not None:
        boundary["geometry"] = geometry.to_dict()
        boundary["front_reference"] = "front_panel_centre" if "front" in walls else None
    incumbent, incumbent_reason = incumbent_room(profile, manifest, set_id=set_id)
    return {
        "ceiling": {"hz": ceiling.ceiling_hz, "provenance": ceiling.to_dict()},
        "median": median,
        ROOM_MEDIAN_FIELD: room_median_sha256(median),
        "persistence": persistence,
        "admit_boost": limits.pop("admit_boost"),
        "limits": limits,
        "incumbent": incumbent,
        "incumbent_reason": incumbent_reason,
        "boundary": boundary,
        "boundary_reason": boundary_reason,
    }
