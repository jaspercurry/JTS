# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compare candidates within one held pose and window, never across rounds."""

from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple, Sequence

import numpy as np

from jasper.audio_measurement.evidence_reasons import REASON_NO_COMPARISON
from jasper.audio_measurement.series_stats import curve_difference, deviation_summary
from jasper.json_fields import finite_float

from .journey import PHASE_LATERAL
from .position_cycle import (
    measured_curve_band,
    read_take_curves,
    take_artifact_path,
)
from .record_index import Measurement, measurement_documents
from .round_captures import REFUSE_CAPTURE_UNREADABLE, doc_pose_key, document_capture_id
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
    """One candidate's banked curve for one role at one pose; ``trusted``
    when its own gate windowed it."""

    take_id: str
    take_path: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    band_hz: tuple[float, float]
    trusted: bool


_Roles = dict[tuple[str, str], dict[str, _Curve]]
_Poses = dict[str, tuple[Mapping[str, Any], _Roles]]


class _Take(NamedTuple):
    """One lateral take: its own record, and the curves read for it."""

    take_id: str
    row: Measurement
    record: Mapping[str, Any]
    take_path: str
    curves: Sequence[Mapping[str, Any]] | None


class _Read(NamedTuple):
    """The retained curves, and the takes that gave none, by why."""

    poses: _Poses
    usable: list[str]
    unreadable: list[str]
    unattributed: list[str]


def _lateral_takes(session_dir: Path, frequency_path: Path) -> Iterator[_Take]:
    """Every lateral take by its own record, with the banked view's curves
    when the view holds any, else the record's. A take the view lacks reads
    as having no curve. A view curve is matched by its take id alone: the
    speaker program's view carries no ``phase`` (round_packet.write_round_packet)."""
    records = {
        document_capture_id(record) or Path(row.path).stem: (row, record)
        for row, record in measurement_documents(session_dir) if row.phase == PHASE_LATERAL
    }
    if frequency_path.is_file():
        run = frequency_run_from_view(json.loads(frequency_path.read_text()))
        viewed: dict[str, list[Mapping[str, Any]]] = {}
        for curve in run.series:
            take_id = str(curve.details.get("take_id") or "")
            if take_id in records:
                viewed.setdefault(take_id, []).append(curve.to_dict())
        if viewed:
            for take_id, (row, record) in records.items():
                yield _Take(take_id, row, record, f"{frequency_path}#{take_id}", viewed.get(take_id))
            return
    for take_id, (row, record) in records.items():
        yield _Take(take_id, row, record, row.path,
                    read_take_curves(take_artifact_path(session_dir, row.path), phase=PHASE_LATERAL))


def _read_poses(session_dir: Path, frequency_path: Path) -> _Read:
    """Latest retained curve per pose, role, window and named candidate.

    A take's pose is keyed from its own record by :func:`doc_pose_key`, the
    rear views' key, so seats at one bearing stay apart.
    """
    read = _Read({}, [], [], [])
    for take in _lateral_takes(session_dir, frequency_path):
        row = take.row
        if row.position_deg is None:
            read.unreadable.append(take.take_id)
            continue
        _, by_role = read.poses.setdefault(doc_pose_key(take.record), ({
            "position_deg": row.position_deg, "vertical_deg": row.vertical_deg,
            "pose_kind": row.pose_kind, "seat_offset_m": row.seat_offset_m,
            "mark_distance_m": row.mark_distance_m,
        }, {}))
        if not row.candidate_id:
            read.unattributed.append(take.take_id)
            continue
        usable = False
        for curve in take.curves or ():
            role = str(curve.get("role") or "")
            measured = measured_curve_band(curve)
            if not role or measured is None:
                continue
            freqs_hz, magnitude_db, band_hz = measured
            # In-record curves can carry -inf at a perfect cancellation.
            finite = np.isfinite(magnitude_db)
            if not np.any(finite):
                continue
            usable = True
            freqs_hz, magnitude_db = freqs_hz[finite], magnitude_db[finite]
            by_role.setdefault((role, str(curve.get("window") or "")), {})[row.candidate_id] = _Curve(
                take.take_id, take.take_path, freqs_hz, magnitude_db,
                # The band the take can speak for, above its trusted floor,
                # clamped to the bins actually banked. What the intersection
                # below spans is then covered by every curve's own bins, so
                # resampling one onto another can never reach past its
                # measured span -- where ``np.interp`` holds the endpoint
                # value and would publish an invented difference.
                (max(band_hz[0], float(freqs_hz.min())),
                 min(band_hz[1], float(freqs_hz.max()))),
                finite_float(curve.get("gate_window_ms")) is not None,
            )
        (read.usable if usable else read.unreadable).append(take.take_id)
    return read


def _own_deviation(curve: _Curve, band_hz: tuple[float, float]) -> dict[str, Any] | None:
    """This candidate's curve as its deviation from its OWN median level: a
    level difference between two applied graphs must not read as a shape one."""
    mask = (curve.freqs_hz >= band_hz[0]) & (curve.freqs_hz <= band_hz[1])
    if not np.any(mask):
        return None
    freqs_hz, magnitude_db = curve.freqs_hz[mask], curve.magnitude_db[mask]
    median_db = float(np.median(magnitude_db))
    return {
        "take_path": curve.take_path,
        "median_db": median_db,
        **deviation_summary(freqs_hz, magnitude_db - median_db),
    }


def _pair_delta(
    a: _Curve, b: _Curve, band_hz: tuple[float, float]
) -> dict[str, Any] | None:
    """``a`` minus ``b`` on ``a``'s grid, level offset removed and published.

    The level comes off as :func:`~.forward_model.predicted_minus_measured_db`
    takes it off (ADR-0358): the raw offset between two graphs is a level
    difference, and the shape difference is what a ladder is asking about.
    ``level_offset_db`` is what was removed, read on ``a``'s grid, so it is not
    the two published ``median_db`` values differenced.
    """
    difference = curve_difference(a.freqs_hz, a.magnitude_db, b.freqs_hz, b.magnitude_db, band_hz=band_hz)
    if difference is None:
        return None
    return {
        "level_offset_db": difference.level_offset_db,
        **deviation_summary(difference.freqs_hz, difference.delta_db),
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
        "trusted": all(curve.trusted for curve in by_candidate.values()),
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
    """One table per pose that played two or more candidates."""
    tables = []
    for key, (pose, by_role) in sorted(poses.items(), key=lambda item: (
        item[1][0]["position_deg"], item[1][0]["vertical_deg"], item[0],
    )):
        if len(_named(by_role)) < 2:
            continue
        tables.append({
            "pose_key": key,
            "deg": pose["position_deg"],
            "vertical_deg": pose["vertical_deg"],
            "kind": pose["pose_kind"],
            "seat_offset_m": pose["seat_offset_m"],
            "distance_m": pose["mark_distance_m"],
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
    """The largest pairwise departure in a trusted window, and where.

    A pair with a curve its gate did not window keeps the room and the sweep's
    edges, so it is counted in ``pairs`` but never headlines. Empty values
    when no trusted pair shared a role: two candidates measured at one pose
    through different roles have nothing to difference, which is an answer
    rather than a refusal.
    """
    deltas = [
        (delta, table, role)
        for table in tables
        for role in table["roles"]
        for delta in role["deltas"]
    ]
    nothing: dict[str, Any] = {}
    delta, table, role = max(
        (row for row in deltas if row[2]["trusted"]), key=lambda row: row[0]["max_abs_db"],
        default=(nothing, nothing, nothing),
    )
    return {
        "pairs": len(deltas),
        "max_abs_delta_db": delta.get("max_abs_db"),
        "max_abs_delta_hz": delta.get("max_abs_hz"),
        "max_abs_delta_between": [delta["a"], delta["b"]] if delta else [],
        "max_abs_delta_role": role.get("role"),
        "max_abs_delta_window": role.get("window"),
        "max_abs_delta_band_hz": role.get("band_hz"),
        "max_abs_delta_pose_key": table.get("pose_key"),
        "max_abs_delta_position_deg": table.get("deg"),
        "max_abs_delta_vertical_deg": table.get("vertical_deg"),
    }


def _left_out(read: _Read, tables: list[dict[str, Any]]) -> dict[str, Any]:
    """Every lateral take no table compares, each under why it is out."""
    retained = {
        curve.take_id: key for key, (_, by_role) in read.poses.items()
        for by_candidate in by_role.values() for curve in by_candidate.values()
    }
    compared = {table["pose_key"] for table in tables}
    omitted = [(take_id, REFUSE_CAPTURE_UNREADABLE) for take_id in read.unreadable] + [
        (take_id, REASON_NO_COMPARISON) for take_id, key in retained.items() if key not in compared
    ]
    return {
        "omitted": [{"capture_id": take_id, "reason": reason} for take_id, reason in sorted(omitted)],
        "superseded_take_ids": [take_id for take_id in read.usable if take_id not in retained],
        "takes_naming_no_candidate": read.unattributed,
    }


def candidate_ladder(round_dir: Path, inputs: RoundInputs) -> dict[str, Any]:
    """The round's ladder as one publishable document: ``summary``, ``tables``.

    ``summary`` carries only scalars and run-bounded lists, so a caller can
    print it whole; the curves it was reduced from stay in the round. A take
    no table compares is listed under why: ``omitted`` with a reason,
    ``superseded_take_ids`` for an earlier take of one candidate at one pose,
    ``takes_naming_no_candidate`` for a take that names none.

    Raises :class:`CandidateLadderRefused` when no pose played two candidates,
    carrying the counts that tell a round which walked no ladder apart from one
    whose takes named no config.
    """
    read = _read_poses(inputs.session_dir, round_dir / FREQUENCY_VIEW_FILENAME)
    tables = _tables(read.poses)
    if not tables:
        raise CandidateLadderRefused(REFUSE_NO_LADDER, {
            "round_dir": str(round_dir),
            "poses_walked": len(read.poses),
            "candidates_named": sorted(
                {c for _, by_role in read.poses.values() for c in _named(by_role)}
            ),
            "takes_naming_no_candidate": read.unattributed,
        })
    return {
        "summary": {
            "round_dir": str(round_dir),
            "banked": inputs.banked,
            "poses": len(tables),
            "candidates": sorted({c for table in tables for c in table["played"]}),
            **_left_out(read, tables),
            **_worst(tables),
        },
        "tables": tables,
    }
