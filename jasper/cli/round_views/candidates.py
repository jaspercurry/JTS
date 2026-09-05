# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A/B/C at one held pose: what each config measured, and where they differ.

* ``candidates <round-dir>`` — this round's lateral takes grouped by
  ``candidate_id`` at each pose that played more than one, per candidate the
  deviation of its own curve from its own median level, and per pair the delta
  between them over the band both were swept across. Writes
  ``candidates.json``. The ladder is WITHIN one round — a pose is held and the
  graph swaps under it — so the comparison never crosses a microphone move. A
  round no pose of which played two candidates is refused by name: one config
  at a pose is a repeat, and ``repeat`` is the instrument that measures those,
  as spread.
"""

from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.position_cycle import (
    parse_curve_magnitude,
    read_take_curves,
    take_artifact_path,
)
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
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

#: Published when no pose in the round played two candidates — including a
#: round that walked none at all, which the detail's own counts tell apart.
REFUSE_NO_LADDER = "candidates_no_ladder"


class _Curve(NamedTuple):
    """One candidate's banked curve for one role at one pose."""

    take_path: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    band_hz: tuple[float, float]


#: ``{(position_deg, vertical_deg): {role: {candidate_id: curve}}}``.
_Poses = dict[tuple[int, int], dict[str, dict[str, _Curve]]]


def _read_poses(session_dir: Path) -> tuple[_Poses, int]:
    """Every lateral take's curves, keyed pose -> role -> candidate.

    **Latest attempt wins**, on :func:`~.position_cycle.read_pose_curve_pair`'s
    rule and for its reason: a superseded take stays on disk as the honest walk
    record and the index's path order is chronological, so a later row
    overwrites the one it replaced. A take naming NO candidate is counted and
    dropped rather than folded in under a blank id, which would put two
    configs' curves in one row.
    """
    poses: _Poses = {}
    unattributed = 0
    for row in bundle_measurements(session_dir, phase=PHASE_LATERAL):
        if row.position_deg is None:
            continue
        by_role = poses.setdefault((row.position_deg, row.vertical_deg), {})
        if not row.candidate_id:
            unattributed += 1
            continue
        curves = read_take_curves(
            take_artifact_path(session_dir, row.path), phase=PHASE_LATERAL
        )
        if curves is None:
            continue
        for curve in curves:
            role = str(curve.get("role") or "")
            parsed = parse_curve_magnitude(curve)
            if not role or parsed is None:
                continue
            freqs_hz, magnitude_db, swept_hz = parsed
            by_role.setdefault(role, {})[row.candidate_id] = _Curve(
                row.path, freqs_hz, magnitude_db,
                # The DECLARED sweep clamped to the grid actually banked. What
                # the intersection below spans is then covered by every
                # curve's own bins, so resampling one onto another can never
                # reach past its measured span -- where ``np.interp`` holds
                # the endpoint value and would publish an invented difference.
                (max(swept_hz[0], float(freqs_hz.min())),
                 min(swept_hz[1], float(freqs_hz.max()))),
            )
    return poses, unattributed


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
    is the basis ``per-seat`` puts curves on and for the same reason: a level
    difference between two applied graphs must not read as a shape one."""
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
    was removed.
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


def _named(by_role: dict[str, dict[str, _Curve]]) -> list[str]:
    """Every candidate one pose named, whatever role it was read through."""
    return sorted({
        candidate_id
        for by_candidate in by_role.values()
        for candidate_id in by_candidate
    })


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
                {"role": role, **table}
                for role in sorted(by_role)
                if (table := _role_table(by_role[role])) is not None
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
        (delta, table, role["role"])
        for table in tables
        for role in table["roles"]
        for delta in role["deltas"]
    ]
    nothing: dict[str, Any] = {}
    delta, table, role_name = max(
        deltas, key=lambda row: row[0]["max_abs_db"],
        default=(nothing, nothing, None),
    )
    return {
        "pairs": len(deltas),
        "max_abs_delta_db": delta.get("max_abs_db"),
        "max_abs_delta_hz": delta.get("max_abs_hz"),
        "max_abs_delta_between": [delta["a"], delta["b"]] if delta else [],
        "max_abs_delta_role": role_name,
        "max_abs_delta_position_deg": table.get("position_deg"),
        "max_abs_delta_vertical_deg": table.get("vertical_deg"),
    }


def _cmd_candidates(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    poses, unattributed = stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _read_poses, inputs.session_dir
    )
    tables = _tables(poses)
    if not tables:
        return refused_by_name(REFUSE_NO_LADDER, {
            "round_dir": str(round_dir),
            "poses_walked": len(poses),
            "candidates_named": sorted(
                {c for by_role in poses.values() for c in _named(by_role)}
            ),
            "takes_naming_no_candidate": unattributed,
        })
    named = sorted({c for table in tables for c in table["played"]})
    worst = _worst(tables)
    summary = {
        "round_dir": str(round_dir),
        "banked": inputs.banked,
        "poses": len(tables),
        "candidates": named,
        "takes_naming_no_candidate": unattributed,
        **worst,
    }
    written = _write(
        {"summary": summary, "tables": tables}, args.out,
        default_out(inputs, round_dir, ARTIFACT_BY_VIEW[args.command].artifact),
    )
    worst_line = (
        "no pair shared a role"
        if worst["max_abs_delta_db"] is None else
        f"widest gap {worst['max_abs_delta_between'][0]} vs "
        f"{worst['max_abs_delta_between'][1]} {worst['max_abs_delta_db']:.2f} dB "
        f"@ {worst['max_abs_delta_hz']:.0f} Hz "
        f"({worst['max_abs_delta_role']} at {worst['max_abs_delta_position_deg']}deg)"
    )
    return answer(
        args.command, out=written, **summary,
        line=(
            f"candidates: {len(named)} candidate(s) over {len(tables)} held "
            f"pose(s), {worst['pairs']} pair(s); {worst_line}"
            f"{f' -> {written}' if written else ''}"
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    candidates = sub.add_parser(
        "candidates",
        help="compare the configs this round played at each held pose, pair by pair",
    )
    candidates.add_argument(
        "round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP
    )
    candidates.add_argument("--out", default=None, help="write the result here (- for stdout)")
    candidates.set_defaults(func=_cmd_candidates)
