# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The room, read off a banked round's seat-cube takes below the ceiling.

Three readers over the ungated seat-kind lateral takes a round banked
(``seat/cube`` in :mod:`~jasper.active_speaker.measurement_programs`; the
room is the measurement, ADR-0260 Wave 0b), each one artifact of
``jasper-round-views``:

* :func:`room_ceiling` — where the room layer stops: the applied candidate's
  trusted floor, clamped by :func:`~jasper.audio_measurement.room_boundary.room_ceiling_hz`,
  with the fallback disclosed (ADR-0256 rule 1).
* :func:`room_median` — the cube's common trend: per frequency the median
  across positions, the spread (population sigma) and each position's
  deviation, :data:`ROOM_FLOOR_HZ` to the ceiling. The contract a room
  candidate reads.
* :func:`room_persistence` — which peaks and dips hold across the cube:
  each position's features against its own robust local level, clustered by
  centre, with the fraction of positions carrying each at an agreeing depth.

Nothing here corrects anything.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.room_boundary import room_ceiling_hz

from ..measurement_programs import POSE_KIND_SEAT
from .evidence_packet import applied_profile_source
from .journey import PHASE_LATERAL
from .position_cycle import parse_curve_magnitude, take_artifact_path
from .record_index import bundle_measurements
from .round_captures import doc_pose_key
from .round_views import RoundViewsError, local_features
from .spatial import cloud_trusted_floor_hz

#: The room layer's floor: below it a seat take says little a cabinet can act on.
ROOM_FLOOR_HZ = 20.0

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

CEILING_SOURCE_APPLIED = "applied_candidate"
CEILING_SOURCE_FALLBACK = "fallback"


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
        from jasper.audio_measurement.room_boundary import (  # lazy: the two bounds only
            ROOM_BOUNDARY_MAX_HZ,
            ROOM_BOUNDARY_MIN_HZ,
        )

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


# --------------------------------------------------------------------------- #
# the seat takes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SeatTake:
    take_id: str
    pose_key: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    gating_applied: bool | None


def seat_takes(bundle_dir: Path) -> tuple[SeatTake, ...]:
    """The bundle's seat-kind takes, latest attempt per pose, in walk order.

    Walked newest-first so a retake speaks for its pose, as
    :func:`~.position_cycle.read_pose_curve_pair` does. A take with no
    readable summed curve is passed over rather than refused: what is
    MISSING is the caller's to say, from what came back.
    """
    latest: dict[str, SeatTake] = {}
    for row in reversed(bundle_measurements(bundle_dir, phase=PHASE_LATERAL)):
        path = take_artifact_path(bundle_dir, row.path)
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, Mapping) or record.get("pose_kind") != POSE_KIND_SEAT:
            continue
        pose_id = str(record.get("pose_id") or row.path)
        if pose_id in latest:
            continue
        raw_curves = record.get("curves")
        curves = raw_curves if isinstance(raw_curves, list) else []
        summed = next(
            (c for c in curves if isinstance(c, Mapping) and c.get("role") == "summed"), None,
        )
        parsed = parse_curve_magnitude(summed) if summed is not None else None
        if parsed is None:
            continue
        freqs, magnitude, _band = parsed
        gating = record.get("gating_applied")
        latest[pose_id] = SeatTake(
            take_id=str(record.get("take_id") or path.stem),
            pose_key=doc_pose_key(record),
            freqs_hz=freqs,
            magnitude_db=magnitude,
            gating_applied=gating if isinstance(gating, bool) else None,
        )
    return tuple(reversed(latest.values()))


def _window(takes: Sequence[SeatTake]) -> str:
    """What the takes say about their own window, never assumed."""
    applied = {take.gating_applied for take in takes}
    if applied == {False}:
        return "ungated"
    return "gated" if applied == {True} else "mixed"


def _stacked(
    takes: Sequence[SeatTake], lo_hz: float, hi_hz: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Every take on the first take's grid, cropped to ``[lo_hz, hi_hz]``."""
    grid = takes[0].freqs_hz
    keep = (grid >= lo_hz) & (grid <= hi_hz)
    freqs = grid[keep]
    if not freqs.size:
        raise RoundViewsError(
            f"no seat take carries a bin between {lo_hz:g} and {hi_hz:g} Hz "
            f"(the takes span {float(grid[0]):g}-{float(grid[-1]):g} Hz)"
        )
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
    return {
        "freqs_hz": freqs.tolist(),
        "median_db": median.tolist(),
        "spread_db": np.std(rows, axis=0).tolist(),
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
    """One position's excursions against its local level, wide enough to count."""
    found = []
    for centre, lo, hi in local_features(
        freqs, residual, lo_hz=ROOM_FLOOR_HZ, hi_hz=ceiling_hz, feature_db=FEATURE_DEPTH_DB,
    ):
        feature = _Feature(
            "peak" if residual[centre] > 0 else "dip",
            float(freqs[lo]), float(freqs[hi]), float(freqs[centre]), float(residual[centre]),
        )
        if feature.width_octaves >= FEATURE_MIN_WIDTH_OCTAVES:
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
        "ceiling_hz": ceiling.ceiling_hz,
        "ceiling_source": ceiling.source,
        "window": _window(takes),
        "thresholds": {
            "depth_db": FEATURE_DEPTH_DB,
            "min_width_octaves": FEATURE_MIN_WIDTH_OCTAVES,
            "agreement_db": FEATURE_AGREEMENT_DB,
            "trend_half_width_octaves": TREND_HALF_WIDTH_OCTAVES,
        },
        "features": features,
    }
