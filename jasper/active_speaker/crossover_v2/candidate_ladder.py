# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compare candidates within one held pose and window, never across rounds."""

from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple

import numpy as np

from .journey import PHASE_LATERAL
from .position_cycle import (
    parse_curve_magnitude,
    read_take_curves,
    take_artifact_path,
)
from .record_index import bundle_measurements
from .round_inputs import RoundInputs
from ..frequency_view import FREQUENCY_VIEW_FILENAME, frequency_run_from_view

__all__ = [
    "REFUSE_NO_LADDER",
    "CandidateLadderRefused",
    "candidate_ladder",
]

#: Published when no pose in the round played two candidates — including a
#: round that walked none at all, which the detail's own counts tell apart.
REFUSE_NO_LADDER = "candidates_no_ladder"


class CandidateLadderRefused(Exception):
    """A named refusal with the evidence behind it. Never a bare failure."""

    def __init__(self, reason: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{reason}: {json.dumps(detail, sort_keys=True, default=str)}")
        self.reason = reason
        self.detail = dict(detail)


class _Curve(NamedTuple):
    """One candidate's banked curve for one role at one pose."""

    take_path: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    band_hz: tuple[float, float]


_Roles = dict[tuple[str, str], dict[str, _Curve]]
_Poses = dict[tuple[int, int], _Roles]


def _pose_curves(session_dir: Path, frequency_path: Path) -> Iterator[
    tuple[int | None, int, str | None, str, list[Mapping[str, Any]] | None]
]:
    if frequency_path.is_file():
        run = frequency_run_from_view(json.loads(frequency_path.read_text()))
        for curve in run.series:
            fields = curve.details
            if fields.get("phase") == PHASE_LATERAL:
                pose = fields["position"]
                yield (pose.get("deg"), pose.get("vertical_deg") or 0,
                       fields.get("candidate_id"), f"{frequency_path}#{fields['take_id']}",
                       [curve.to_dict()])
    else:
        for row in bundle_measurements(session_dir, phase=PHASE_LATERAL):
            yield (row.position_deg, row.vertical_deg, row.candidate_id, row.path,
                   read_take_curves(take_artifact_path(session_dir, row.path), phase=PHASE_LATERAL))


def _read_poses(session_dir: Path, frequency_path: Path) -> tuple[_Poses, int]:
    """Latest retained curve per pose, role, window and named candidate."""
    poses: _Poses = {}
    unattributed = set()
    for position, vertical, config_id, take_path, curves in _pose_curves(session_dir, frequency_path):
        if position is None:
            continue
        by_role = poses.setdefault((position, vertical), {})
        if not config_id:
            unattributed.add(take_path)
            continue
        if curves is None:
            continue
        for curve in curves:
            role = str(curve.get("role") or "")
            parsed = parse_curve_magnitude(curve)
            if not role or parsed is None:
                continue
            freqs_hz, magnitude_db, swept_hz = parsed
            # ``parse_curve_magnitude`` proves the FREQUENCIES finite, never
            # the levels: a bin at a perfect cancellation banks -inf, and one
            # of those would make every scalar below NaN and then fail the
            # strict writer -- costing the operator the whole round for one
            # bin. Dropped here, once, so ``bins`` reports what was compared.
            finite = np.isfinite(magnitude_db)
            if not np.any(finite):
                continue
            freqs_hz, magnitude_db = freqs_hz[finite], magnitude_db[finite]
            by_role.setdefault((role, str(curve.get("window") or "")), {})[config_id] = _Curve(
                take_path, freqs_hz, magnitude_db,
                # The DECLARED sweep clamped to the grid actually banked. What
                # the intersection below spans is then covered by every
                # curve's own bins, so resampling one onto another can never
                # reach past its measured span -- where ``np.interp`` holds
                # the endpoint value and would publish an invented difference.
                (max(swept_hz[0], float(freqs_hz.min())),
                 min(swept_hz[1], float(freqs_hz.max()))),
            )
    return poses, len(unattributed)


def _deviation(freqs_hz: np.ndarray, deviation_db: np.ndarray) -> dict[str, Any]:
    """One deviation reduced to scalars, and the frequency the worst bin sits
    at. The shared reduction, so a candidate's own departure from level and a
    pair's departure from each other are the same numbers."""
    worst = int(np.argmax(np.abs(deviation_db)))
    return {
        "bins": int(deviation_db.size),
        "mean_abs_db": float(np.mean(np.abs(deviation_db))),
        "max_abs_db": float(abs(deviation_db[worst])),
        "max_abs_hz": float(freqs_hz[worst]),
        "rms_db": float(np.sqrt(np.mean(deviation_db**2))),
    }


def _own_deviation(curve: _Curve, band_hz: tuple[float, float]) -> dict[str, Any] | None:
    """This candidate's curve as its deviation from its OWN median level, which
    is the basis :func:`~.round_views.per_seat_curves` puts curves on and for
    the same reason: a level difference between two applied graphs must not
    read as a shape one."""
    mask = (curve.freqs_hz >= band_hz[0]) & (curve.freqs_hz <= band_hz[1])
    if not np.any(mask):
        return None
    freqs_hz, magnitude_db = curve.freqs_hz[mask], curve.magnitude_db[mask]
    median_db = float(np.median(magnitude_db))
    return {
        "take_path": curve.take_path,
        "median_db": median_db,
        **_deviation(freqs_hz, magnitude_db - median_db),
    }


def _pair_delta(
    a: _Curve, b: _Curve, band_hz: tuple[float, float]
) -> dict[str, Any] | None:
    """``a`` minus ``b`` on ``a``'s grid, level offset removed and published.

    Both halves are level-normalised before subtracting, exactly as
    :func:`~.forward_model.predicted_minus_measured_db` normalises its pair:
    the raw offset between two graphs is a level difference, and the shape
    difference is what a ladder is asking about. ``level_offset_db`` is what
    was removed. It is taken over ``a``'s grid for BOTH halves, so on two
    curves banked at different resolutions it is not the two published
    ``median_db`` values differenced -- those are each over that curve's own
    bins, and the delta needs one grid.
    """
    mask = (a.freqs_hz >= band_hz[0]) & (a.freqs_hz <= band_hz[1])
    if not np.any(mask):
        return None
    freqs_hz = a.freqs_hz[mask]
    a_db = a.magnitude_db[mask]
    b_db = np.interp(freqs_hz, b.freqs_hz, b.magnitude_db)
    offset_db = float(np.median(a_db) - np.median(b_db))
    return {
        "level_offset_db": offset_db,
        **_deviation(freqs_hz, (a_db - offset_db) - b_db),
    }


def _named(by_role: _Roles) -> list[str]:
    """Every candidate one pose named, whatever role it was read through."""
    return sorted({
        candidate_id
        for by_candidate in by_role.values()
        for candidate_id in by_candidate
    })


def _role_table(by_candidate: dict[str, _Curve]) -> dict[str, Any] | None:
    """One role at one pose: the shared band, each candidate on it, each pair.

    The band is every participating candidate's swept span intersected, so the
    per-candidate scalars and the pairwise ones are read over ONE span rather
    than each over its own.
    """
    band_hz = (
        max(curve.band_hz[0] for curve in by_candidate.values()),
        min(curve.band_hz[1] for curve in by_candidate.values()),
    )
    rows = {
        candidate_id: row
        for candidate_id in sorted(by_candidate)
        if (row := _own_deviation(by_candidate[candidate_id], band_hz)) is not None
    }
    if not rows:
        return None
    return {
        "band_hz": list(band_hz),
        "candidates": [
            {"candidate_id": candidate_id, **row} for candidate_id, row in rows.items()
        ],
        "deltas": [
            {"a": a, "b": b, **delta}
            for a, b in combinations(rows, 2)
            if (delta := _pair_delta(by_candidate[a], by_candidate[b], band_hz))
            is not None
        ],
    }


def _tables(poses: _Poses) -> list[dict[str, Any]]:
    """One table per pose that played two or more candidates, in walk order."""
    tables = []
    for (position_deg, vertical_deg), by_role in sorted(poses.items()):
        if len(_named(by_role)) < 2:
            continue
        tables.append({
            "position_deg": position_deg,
            "vertical_deg": vertical_deg,
            "played": _named(by_role),
            # A role whose candidates share no measured band yields nothing to
            # difference and is absent; the pose still publishes, because two
            # configs WERE played here and refusing that as "no ladder" would
            # send the operator to an instrument for a different question.
            "roles": [
                {"role": role, **({"window": window} if window else {}), **table}
                for role, window in sorted(by_role)
                if (table := _role_table(by_role[role, window])) is not None
            ],
        })
    return tables


def _worst(tables: list[dict[str, Any]]) -> dict[str, Any]:
    """The largest pairwise departure anywhere in the round, and where.

    Empty values when no pair shared a role: two candidates measured at one
    pose through different roles have nothing to difference, which is an
    answer rather than a refusal.
    """
    deltas = [
        (delta, table, role)
        for table in tables
        for role in table["roles"]
        for delta in role["deltas"]
    ]
    nothing: dict[str, Any] = {}
    delta, table, role = max(
        deltas, key=lambda row: row[0]["max_abs_db"],
        default=(nothing, nothing, nothing),
    )
    return {
        "pairs": len(deltas),
        "max_abs_delta_db": delta.get("max_abs_db"),
        "max_abs_delta_hz": delta.get("max_abs_hz"),
        "max_abs_delta_between": [delta["a"], delta["b"]] if delta else [],
        "max_abs_delta_role": role.get("role"),
        "max_abs_delta_window": role.get("window"),
        "max_abs_delta_position_deg": table.get("position_deg"),
        "max_abs_delta_vertical_deg": table.get("vertical_deg"),
    }


def candidate_ladder(round_dir: Path, inputs: RoundInputs) -> dict[str, Any]:
    """The round's ladder as one publishable document: ``summary``, ``tables``.

    ``summary`` carries only scalars and run-bounded lists, so a caller can
    print it whole; the curves it was reduced from stay in the round.

    Raises :class:`CandidateLadderRefused` when no pose played two candidates,
    carrying the counts that tell a round which walked no ladder apart from one
    whose takes named no config.
    """
    poses, unattributed = _read_poses(inputs.session_dir, round_dir / FREQUENCY_VIEW_FILENAME)
    tables = _tables(poses)
    if not tables:
        raise CandidateLadderRefused(REFUSE_NO_LADDER, {
            "round_dir": str(round_dir),
            "poses_walked": len(poses),
            "candidates_named": sorted(
                {c for by_role in poses.values() for c in _named(by_role)}
            ),
            "takes_naming_no_candidate": unattributed,
        })
    return {
        "summary": {
            "round_dir": str(round_dir),
            "banked": inputs.banked,
            "poses": len(tables),
            "candidates": sorted({c for table in tables for c in table["played"]}),
            "takes_naming_no_candidate": unattributed,
            **_worst(tables),
        },
        "tables": tables,
    }
