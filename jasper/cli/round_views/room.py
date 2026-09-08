# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The room, read off a round's seat-cube takes below the ceiling.

* ``room-ceiling <round-dir>`` — where the room layer stops: the applied
  candidate's trusted floor, clamped to the room boundary's bounds, or the
  shipped default with the fallback disclosed (ADR-0256). Writes
  ``room_ceiling.json``.
* ``room-median <round-dir>`` — the cube's common trend: per frequency the
  median magnitude across the seat takes, the spread (population sigma) and
  each position's deviation, 20 Hz to the ceiling. Writes ``room_median.json``.
* ``room-persistence <round-dir>`` — which peaks and dips hold across the
  cube: each position's features against its own robust local level, clustered
  by centre, with the fraction of positions where the feature appears at an
  agreeing depth. Writes ``room_persistence.json``.

All three read the seat-kind lateral takes a round banked
(:mod:`~jasper.active_speaker.measurement_programs`, ``seat/cube``), which are
analyzed ungated: the room is the measurement (ADR-0260, Wave 0b). Nothing
here corrects anything; a candidate kind reads these artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.crossover_v2.evidence_packet import applied_profile_source
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.position_cycle import (
    parse_curve_magnitude,
    read_take_curves,
    take_artifact_path,
)
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, round_inputs
from jasper.active_speaker.crossover_v2.round_views import RoundViewsError
from jasper.active_speaker.crossover_v2.spatial import cloud_trusted_floor_hz
from jasper.active_speaker.measurement_programs import POSE_KIND_SEAT
from jasper.audio_measurement.room_boundary import (
    ROOM_BOUNDARY_DEFAULT_HZ,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
)
from jasper.cli._refusal import EXIT_UNREADABLE, stage

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _ROUND_TOOL_ERRORS,
    _write,
    answer,
    default_out,
    refused_by_name,
)

#: The room layer's floor: below it a seat take says little a cabinet can act on.
ROOM_FLOOR_HZ = 20.0

#: A feature is a run of at least this depth and width against the local
#: level; positions agree on it when their depths sit within the same span.
FEATURE_DEPTH_DB = 3.0
FEATURE_MIN_WIDTH_OCTAVES = 1.0 / 6.0
FEATURE_AGREEMENT_DB = 3.0

#: The robust local level a feature is read against: a running median over
#: this many octaves either side, in dB, so a mode neither lifts its own
#: baseline the way a power mean would nor hides in a dip.
TREND_HALF_WIDTH_OCTAVES = 0.5

CEILING_SOURCE_APPLIED = "applied_candidate"
CEILING_SOURCE_FALLBACK = "fallback"

REFUSE_NO_SEAT_TAKES = "room_no_seat_takes"


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
    profile that is missing, unreadable, or carries no floor falls back to
    :data:`ROOM_BOUNDARY_DEFAULT_HZ` and says why.
    """
    profile, reason = applied_profile_source(applied_profile_path)
    evidence = (profile or {}).get("exclusion_evidence")
    raw = evidence.get("validity_floor_hz") if isinstance(evidence, Mapping) else None
    raw_hz = float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else None
    trusted = cloud_trusted_floor_hz(raw_hz)
    path = str(applied_profile_path) if applied_profile_path is not None else None
    if trusted is None:
        return Ceiling(
            ceiling_hz=ROOM_BOUNDARY_DEFAULT_HZ,
            source=CEILING_SOURCE_FALLBACK,
            trusted_floor_hz=None,
            raw_floor_hz=raw_hz,
            profile_path=path,
            reason=reason or "the applied profile discloses no trusted floor",
        )
    return Ceiling(
        ceiling_hz=min(max(trusted, ROOM_BOUNDARY_MIN_HZ), ROOM_BOUNDARY_MAX_HZ),
        source=CEILING_SOURCE_APPLIED,
        trusted_floor_hz=trusted,
        raw_floor_hz=raw_hz,
        profile_path=path,
        reason="",
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


def seat_takes(inputs: RoundInputs) -> tuple[SeatTake, ...]:
    """The round's seat-kind takes, latest attempt per pose, in walk order.

    A take with no readable summed curve is passed over rather than refused:
    what is MISSING is the caller's to say, from what came back.
    """
    latest: dict[str, tuple[int, SeatTake]] = {}
    for row in bundle_measurements(inputs.session_dir, phase=PHASE_LATERAL):
        path = take_artifact_path(inputs.session_dir, row.path)
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, Mapping) or record.get("pose_kind") != POSE_KIND_SEAT:
            continue
        curves = read_take_curves(path, phase=PHASE_LATERAL) or []
        summed = next(
            (
                parsed for curve in curves
                if curve.get("role") == "summed"
                for parsed in (parse_curve_magnitude(curve),)
                if parsed is not None
            ),
            None,
        )
        if summed is None:
            continue
        freqs, magnitude, _band = summed
        pose_id = str(record.get("pose_id") or record.get("position_id") or row.path)
        attempt = int(record.get("attempt") or 0)
        take = SeatTake(
            take_id=str(record.get("take_id") or path.stem),
            pose_key=doc_pose_key(record),
            freqs_hz=freqs,
            magnitude_db=magnitude,
            gating_applied=(
                record["gating_applied"] if isinstance(record.get("gating_applied"), bool) else None
            ),
        )
        if pose_id not in latest or attempt >= latest[pose_id][0]:
            latest[pose_id] = (attempt, take)
    return tuple(take for _attempt, take in latest.values())


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

#: The bands the answer summarizes the spread over; the last runs to the ceiling.
SPREAD_BANDS_HZ: tuple[tuple[float, float | None], ...] = (
    (ROOM_FLOOR_HZ, 60.0), (60.0, 120.0), (120.0, None),
)


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


def _band_means(freqs: np.ndarray, values: np.ndarray, ceiling_hz: float) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for lo, hi in SPREAD_BANDS_HZ:
        top = ceiling_hz if hi is None else min(hi, ceiling_hz)
        mask = (freqs >= lo) & (freqs <= top)
        out[f"{lo:g}-{top:g}"] = float(np.mean(values[mask])) if np.any(mask) else None
    return out


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def _running_median_db(freqs: np.ndarray, magnitude_db: np.ndarray) -> np.ndarray:
    factor = 2.0 ** TREND_HALF_WIDTH_OCTAVES
    lo = np.searchsorted(freqs, freqs / factor, side="left")
    hi = np.searchsorted(freqs, freqs * factor, side="right")
    return np.asarray([
        float(np.median(magnitude_db[a:b])) for a, b in zip(lo, hi)
    ])


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


def _features(freqs: np.ndarray, residual: np.ndarray) -> list[_Feature]:
    """Runs of ``residual`` at least :data:`FEATURE_DEPTH_DB` deep, one sign."""
    found: list[_Feature] = []
    for kind, sign in (("peak", 1.0), ("dip", -1.0)):
        inside = sign * residual >= FEATURE_DEPTH_DB
        edges = np.flatnonzero(np.diff(np.concatenate(([False], inside, [False]))))
        for start, stop in zip(edges[::2], edges[1::2]):
            f_lo, f_hi = float(freqs[start]), float(freqs[stop - 1])
            if f_lo <= 0 or math.log2(f_hi / f_lo) < FEATURE_MIN_WIDTH_OCTAVES:
                continue
            at = start + int(np.argmax(sign * residual[start:stop]))
            found.append(_Feature(kind, f_lo, f_hi, float(freqs[at]), float(residual[at])))
    return found


def room_persistence(takes: Sequence[SeatTake], ceiling: Ceiling) -> dict[str, Any]:
    """Which features hold across the cube, and at how many positions."""
    # A half-octave margin above the ceiling so the trend window is whole there.
    freqs, rows = _stacked(
        takes, ROOM_FLOOR_HZ / 2.0 ** TREND_HALF_WIDTH_OCTAVES,
        ceiling.ceiling_hz * 2.0 ** TREND_HALF_WIDTH_OCTAVES,
    )
    band = (freqs >= ROOM_FLOOR_HZ) & (freqs <= ceiling.ceiling_hz)
    per_position = [
        _features(freqs[band], (row - _running_median_db(freqs, row))[band])
        for row in rows
    ]
    candidates = sorted(
        ((position, feature) for position, found in enumerate(per_position) for feature in found),
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
    features = []
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


# --------------------------------------------------------------------------- #
# the verbs
# --------------------------------------------------------------------------- #

#: A feature this many positions share, as a fraction, is what the answer counts.
PERSISTENT_FRACTION = 0.7


def _inputs(args: argparse.Namespace) -> RoundInputs:
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, Path(args.round_dir))
    if args.applied_profile:
        inputs = replace(inputs, applied_profile_path=Path(args.applied_profile))
    return inputs


def _seat_takes_or_refuse(inputs: RoundInputs) -> tuple[SeatTake, ...] | int:
    takes = seat_takes(inputs)
    if takes:
        return takes
    return refused_by_name(
        REFUSE_NO_SEAT_TAKES,
        {"round_dir": str(inputs.session_dir), "looked_for": "lateral takes with pose_kind=seat"},
    )


def _out(args: argparse.Namespace, inputs: RoundInputs) -> Path:
    return default_out(inputs, Path(args.round_dir), ARTIFACT_BY_VIEW[args.command].artifact)


def _cmd_room_ceiling(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    ceiling = room_ceiling(inputs.applied_profile_path)
    written = _write(ceiling.to_dict(), args.out, _out(args, inputs))
    origin = (
        f"trusted floor {ceiling.trusted_floor_hz:g} Hz clamped to "
        f"[{ROOM_BOUNDARY_MIN_HZ:g}, {ROOM_BOUNDARY_MAX_HZ:g}]"
        if ceiling.source == CEILING_SOURCE_APPLIED else ceiling.reason
    )
    return answer(
        args.command, out=written, **ceiling.to_dict(),
        line=(
            f"room-ceiling: {ceiling.ceiling_hz:g} Hz ({ceiling.source}: {origin})"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def _cmd_room_median(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    takes = _seat_takes_or_refuse(inputs)
    if isinstance(takes, int):
        return takes
    ceiling = room_ceiling(inputs.applied_profile_path)
    payload = room_median(takes, ceiling)
    written = _write(payload, args.out, _out(args, inputs))
    spread = _band_means(
        np.asarray(payload["freqs_hz"]), np.asarray(payload["spread_db"]), ceiling.ceiling_hz,
    )
    return answer(
        args.command, out=written, ceiling_hz=ceiling.ceiling_hz,
        ceiling_source=ceiling.source, n_positions=len(takes),
        window=payload["window"], mean_spread_db=spread,
        line=(
            f"room-median: {len(takes)} position(s), ceiling {ceiling.ceiling_hz:g} Hz "
            f"({ceiling.source}), {payload['window']}; mean spread "
            + ", ".join(
                f"{band} Hz {value:.1f} dB" if value is not None else f"{band} Hz n/a"
                for band, value in spread.items()
            )
            + (f" -> {written}" if written else "")
        ),
    )


def _cmd_room_persistence(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    takes = _seat_takes_or_refuse(inputs)
    if isinstance(takes, int):
        return takes
    ceiling = room_ceiling(inputs.applied_profile_path)
    payload = room_persistence(takes, ceiling)
    written = _write(payload, args.out, _out(args, inputs))
    features = payload["features"]
    persistent = sum(1 for f in features if f["presence_fraction"] >= PERSISTENT_FRACTION)
    top = [
        {k: f[k] for k in ("kind", "centre_hz", "median_depth_db", "presence_fraction")}
        for f in sorted(features, key=lambda f: -abs(f["median_depth_db"]))[:3]
    ]
    return answer(
        args.command, out=written, ceiling_hz=ceiling.ceiling_hz,
        n_positions=len(takes), features=len(features),
        persistent=persistent, persistent_fraction=PERSISTENT_FRACTION, top=top,
        line=(
            f"room-persistence: {persistent} of {len(features)} feature(s) at >= "
            f"{PERSISTENT_FRACTION:g} presence over {len(takes)} position(s)"
            + (
                "; top: " + ", ".join(
                    f"{f['kind']} {f['centre_hz']:.0f} Hz {f['median_depth_db']:+.1f} dB "
                    f"({f['presence_fraction']:.2f})"
                    for f in top
                )
                if top else ""
            )
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    for name, func, help_ in (
        ("room-ceiling", _cmd_room_ceiling,
         "where the room layer stops: the applied candidate's trusted floor, clamped, or the disclosed default"),
        ("room-median", _cmd_room_median,
         "the seat cube's median, spread and per-position deviation below the ceiling"),
        ("room-persistence", _cmd_room_persistence,
         "which peaks and dips hold across the seat cube, and at what fraction of positions"),
    ):
        parser = sub.add_parser(name, help=help_)
        parser.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
        parser.add_argument(
            "--applied-profile", default=None, metavar="PATH",
            help="read the ceiling from this applied profile instead of the round's own",
        )
        parser.add_argument("--out", default=None, help="write the result here (- for stdout)")
        parser.set_defaults(func=func)
