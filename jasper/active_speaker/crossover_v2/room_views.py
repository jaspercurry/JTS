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

from jasper.audio_measurement.room_boundary import (
    CEILING_SOURCE_APPLIED,
    CEILING_SOURCE_FALLBACK,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
    ROOM_FLOOR_HZ,
    ROOM_MEDIAN_WINDOW,
    room_ceiling_hz,
)
from jasper.audio_measurement.measurement_geometry import (
    WALL_FIELD_BY_KEY, boundary_prior, load_declared_geometry,
)
from jasper.audio_measurement.room_limits import (
    admit_boost, boost_cap_db, cloud_trusted_floor_hz, cut_floor_db, spatial_support,
)

from .evidence_packet import applied_profile_source
from .room_prescription import ROOM_MEDIAN_FIELD, read_room_median
from .room_selection import SeatTake
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


def band_edges(ceiling_hz: float) -> tuple[tuple[float, float], ...]:
    """The room bands, :data:`ROOM_FLOOR_HZ` to ``ceiling_hz``; the ceiling tops the last."""
    lows = (ROOM_FLOOR_HZ, *ROOM_BAND_SPLITS_HZ)
    highs = (*ROOM_BAND_SPLITS_HZ, ceiling_hz)
    return tuple(zip(lows, highs))


def band_masks(
    freqs_hz: Any, ceiling_hz: float,
) -> tuple[tuple[float, float, np.ndarray], ...]:
    """Each band's bins on ``freqs_hz``: half-open below a split and closed at
    the ceiling, so a bin sitting on a split is counted once."""
    freqs = np.asarray(freqs_hz, dtype=float)
    edges = band_edges(ceiling_hz)
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
    raw_floor_hz: float | None
    profile_path: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceiling_hz": self.ceiling_hz,
            "ceiling_source": self.source,
            "trusted_floor_hz": self.trusted_floor_hz,
            "raw_floor_hz": self.raw_floor_hz,
            "clamp_hz": [ROOM_BOUNDARY_MIN_HZ, ROOM_BOUNDARY_MAX_HZ],
            "profile_path": self.profile_path,
            "reason": self.reason,
        }


def room_ceiling(applied_profile_path: Path | None) -> Ceiling:
    """The applied candidate's trusted floor, clamped (ADR-0256 rule 1).

    ``exclusion_evidence.validity_floor_hz`` is the cloud's ``1/T`` floor, so
    the trusted floor is :func:`cloud_trusted_floor_hz`'s ``2.5/T`` of it. A
    profile that is missing, unreadable, or carries no floor falls back to the
    default and says why.
    """
    profile, reason = applied_profile_source(applied_profile_path)
    evidence = (profile or {}).get("exclusion_evidence")
    raw = evidence.get("validity_floor_hz") if isinstance(evidence, Mapping) else None
    raw_hz = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else None
    trusted = cloud_trusted_floor_hz(raw_hz)
    return Ceiling(
        ceiling_hz=room_ceiling_hz(trusted),
        source=CEILING_SOURCE_FALLBACK if trusted is None else CEILING_SOURCE_APPLIED,
        trusted_floor_hz=trusted,
        raw_floor_hz=raw_hz,
        profile_path=str(applied_profile_path) if applied_profile_path is not None else None,
        reason=(
            (reason or "the applied profile discloses no trusted floor")
            if trusted is None else ""
        ),
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


# --------------------------------------------------------------------------- #
# the median
# --------------------------------------------------------------------------- #


def room_median(takes: Sequence[SeatTake], ceiling: Ceiling) -> dict[str, Any]:
    """The contract a room candidate reads: median, spread, deviations."""
    freqs, rows = _stacked(takes, ROOM_FLOOR_HZ, ceiling.ceiling_hz)
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
        "coverage_hz": [max(ROOM_FLOOR_HZ, *(t.band_hz[0] for t in takes)),
                        min(ceiling.ceiling_hz, *(t.band_hz[1] for t in takes))],
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

    Searched over the margin the grid carries so an extremum on the band's
    own edge bin is judged against real neighbours; only centres inside the
    band count.
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
            ROOM_FLOOR_HZ <= feature.centre_hz <= ceiling_hz
            and feature.width_octaves >= FEATURE_MIN_WIDTH_OCTAVES
        ):
            found.append(feature)
    return found


def room_persistence(takes: Sequence[SeatTake], ceiling: Ceiling) -> dict[str, Any]:
    """Which features hold across the cube, and at how many positions."""
    # A half-octave margin either side so the trend window is whole at the edges.
    margin = 2.0 ** TREND_HALF_WIDTH_OCTAVES
    freqs, rows = _stacked(takes, ROOM_FLOOR_HZ / margin, ceiling.ceiling_hz * margin)
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
        "coverage_hz": [max(ROOM_FLOOR_HZ, *(t.band_hz[0] for t in takes)),
                        min(ceiling.ceiling_hz, *(t.band_hz[1] for t in takes))],
        "thresholds": {
            "depth_db": FEATURE_DEPTH_DB,
            "min_width_octaves": FEATURE_MIN_WIDTH_OCTAVES,
            "agreement_db": FEATURE_AGREEMENT_DB,
            "trend_half_width_octaves": TREND_HALF_WIDTH_OCTAVES,
        },
        "features": features,
    }


def incumbent_room(profile: Mapping[str, Any] | None, manifest: Mapping[str, Any]) -> dict[str, Any]:
    profile = profile or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    correction = snapshot.get("room_correction", profile.get("room_correction")) or {}
    basis = correction.get("basis") or {}
    fingerprint = (profile.get("source") or {}).get("measured_candidate_fingerprint")
    scopes = {"room_tune", "applied"} if correction else {"speaker_tune", "room_tune", "applied"}
    matches = [row["set_id"] for row in manifest["sets"] if (
        (fingerprint and row["capture_basis"].get("candidate_id") == fingerprint)
        or (not row["capture_basis"].get("candidate_id")
            and row["capture_basis"].get("graph_scope") in scopes)
    )]
    return {
        "round_id": basis.get("round_id"), ROOM_MEDIAN_FIELD: basis.get(ROOM_MEDIAN_FIELD),
        "set_id": matches[0] if len(matches) == 1 else None,
        "reason": "" if len(matches) == 1 else (
            "room_incumbent_set_ambiguous" if matches else "room_incumbent_set_unavailable"
        ),
    }


def room_document(
    takes: Sequence[SeatTake], *, set_id: str, evidence: Mapping[str, Any],
    applied_profile_path: Path | None, geometry_path: Path | None,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    ceiling = room_ceiling(applied_profile_path)
    median = {**room_median(takes, ceiling), "set_id": set_id, "evidence": dict(evidence)}
    value = read_room_median(median)
    persistence = room_persistence(takes, ceiling)
    for feature in persistence["features"]:
        feature["admission"] = admit_boost(
            feature["centre_hz"], freqs_hz=value.freqs_hz, median_db=value.median_db,
            deviations_db=value.deviations_db, n_positions=value.n_positions,
        ).to_dict()
    geometry = load_declared_geometry(geometry_path) if geometry_path is not None else None
    walls = {key: distance for key, field in WALL_FIELD_BY_KEY.items()
             if geometry is not None and (distance := getattr(geometry, field)) is not None}
    profile, _ = applied_profile_source(applied_profile_path)
    return {
        "ceiling": {"hz": ceiling.ceiling_hz, "provenance": ceiling.to_dict()},
        "median": median,
        "persistence": persistence,
        "limits": {
            "cut_floor_db": cut_floor_db(value.spread_db, value.freqs_hz, value.ceiling_hz).tolist(),
            "boost_cap_db": boost_cap_db(value.freqs_hz, value.ceiling_hz).tolist(),
        },
        "incumbent": incumbent_room(profile, manifest),
        "boundary": {"advisory": True, **boundary_prior(value.freqs_hz, walls=walls)} if walls else None,
        "boundary_reason": "" if walls else ("walls_undeclared" if geometry else "geometry_undeclared"),
    }
