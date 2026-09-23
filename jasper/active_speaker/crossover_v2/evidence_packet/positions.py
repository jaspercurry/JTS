# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.audio_measurement.evidence_reasons import (
    REASON_CROSS_SEAT_SPREAD_OVERFLOW,
    REASON_NO_CURVE_GRID,
    REASON_TOO_FEW_SEATS,
)
from jasper.json_fields import finite_float

from ...commissioning_evidence_store import EVIDENCE_ROOT
from .. import position_cycle
from ..feature_classification import UNCERTAINTY_UNSEPARATED
from ..journey import PHASE_LATERAL
from ..record_index import Measurement
from .offline_reads import _copy_allowed, _mapping

#: Position fields copied verbatim. ``wav_path`` is deliberately absent — it is
#: an absolute path on the speaker's filesystem, and ``wav_sha256`` identifies
#: the same bytes without naming where they live.
_POSITION_FIELDS = (
    "position_id",
    "index",
    "attempt",
    "role",
    # WHERE the capture was taken, copied through from the same
    # ``_RECORD_FIELDS`` join every other per-position scalar rides;
    # :func:`_angle_deg_block` is conditional on what the rows actually carry.
    "position_deg",
    "position_axis",
    # The elevation half of the same WHERE, orthogonal to the bearing: a seat
    # raised above mark height states both.
    "vertical_deg",
    "mark_distance_m",
    "take_id",
    "wav_sha256",
    "validity_floor_hz",
    "gate_disclosure",
    "gate_floor_source",
    # The two NUMBERS ``gate_disclosure`` narrates, beside it rather than
    # instead of it: the sentence makes a small ``gate_moved_rms_db``
    # readable, the number makes the sentence usable without parsing English.
    "gate_moved_rms_db",
    "gate_reflection_delay_ms",
    # The ROOM's floor at this seat and where it came from — always a pair,
    # because the number is unreadable without its provenance.
    "gate_entanglement_floor_hz",
    "gate_entanglement_floor_source",
    "gate_window_ms",
    "gating_applied",
    "glitch_detected",
    "summed_ripple_db",
    "reverse_null_depth_db",
    "echo",
)

#: Where a round banks one JSON record per accepted take, INSIDE the round
#: directory :func:`round_artifact_dir` returns:
#: :meth:`~.record_store.BankedRecordStore.bank` publishes
#: ``crossover_v2/{capture}/positions/{take_id}.json`` under
#: ``{EVIDENCE_ROOT}/artifacts/``. :mod:`.position_cycle` reaches the same
#: files from the BANKED ROUND root, which is why the accept rule is imported
#: from there rather than restated here.
_POSITIONS_SUBDIR = "positions"


def _ordinal(value: Any) -> int:
    """A sort key from an identity field, or ``0`` when it is not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

#: Decimal places the cross-seat spread is published to. Four, matching the
#: member curves it is taken over
#: (:func:`~jasper.attribution.position_evidence._sample_onto`): more digits
#: than its own inputs would be false precision, and this document is
#: content-fingerprinted.
_SIGMA_DECIMALS = 4

#: The cross-seat spread, declared as the enrichment rule requires. Entry
#: shape is :data:`~.feature_classification.LAB_ROW_UNCERTAINTY`'s (``kind`` +
#: ``of``); what differs is the LIST it publishes under, because
#: :data:`~.feature_classification.UNCERTAINTY_UNSEPARATED` is deliberately not
#: a member of the closed kind set.
_CROSS_SEAT_SIGMA_UNCERTAINTY: dict[str, dict[str, str]] = {
    "per_bin_sigma_db": {
        "kind": UNCERTAINTY_UNSEPARATED,
        "of": (
            "how far the seats disagree at this bin — the sample standard "
            "deviation (ddof=1) across the member curves above, uncentred. It "
            "contains TWO spreads and separates neither: the sound field's real "
            "variation from seat to seat, which no amount of repeating at a "
            "fixed seat would reduce, and the measurement noise each member "
            "curve carries, which averaging repeats into each member would. "
            "Which of the two dominates cannot be read off this round — "
            "separating them needs a repeat spread "
            "measured at a FIXED pose, which is the banked repeat floor "
            "(accuracy_budget.in_capture_repeat_floor), available on a rig "
            "that banked one. So it is published as a spread whose kind is not "
            "yet separable, never as a random or a systematic one: calling it "
            "either would be exactly the pooling these labels exist to prevent"
        ),
    },
}

#: Fields of the cross-seat block that are not spreads. ``n_seats`` is the
#: load-bearing one: this block publishes n, so it has to say what the obvious
#: quotient would and would not mean.
_CROSS_SEAT_SIGMA_NOT_AN_UNCERTAINTY: dict[str, str] = {
    "n_seats": (
        "how many member curves the spread above was taken over. A count, not a "
        "spread — published because a standard deviation cannot be judged "
        "without its n, and a reader not given one counts the rows anyway. It is "
        "not a divisor to reach for while the spread's two halves are "
        "unseparated: per_bin_sigma_db/sqrt(n_seats) would be the standard error "
        "of the cross-seat MEAN, and only the random half falls that way, so "
        "until the halves are separated that quotient is the standard error of "
        "nothing"
    ),
    "n_seats_excluded": (
        "member curves this block could not use — a row with no magnitude_db, "
        "one whose length does not match the grid, one carrying a sample that "
        "is not a real number, or EVERY row when the positions block carried no "
        "curve grid at all, because with no bins to check a length against no "
        "row can be read as a curve on this grid. That fourth cause is why the "
        "count can equal the row total while nothing was individually rejected; "
        "the block's reason says which case produced it. Counted rather than "
        "dropped quietly, on the rule the capture_snr block keeps for the "
        "takes it does not publish. A count, not a spread"
    ),
}


def _member_curve(values: Any, n_bins: int) -> list[float] | None:
    """One member curve as ``n_bins`` real numbers, or ``None``.

    All-or-nothing per row: a curve admitted for only the bins it could supply
    would make ``n_seats`` not the count the spread was taken over in every
    bin. The row is counted, never dropped silently.
    """
    if not isinstance(values, list) or len(values) != n_bins:
        return None
    curve: list[float] = []
    for value in values:
        number = finite_float(value)
        if number is None:
            return None
        curve.append(number)
    return curve


def _cross_seat_sigma_block(
    freqs_hz: Any, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """How far the seats disagree, bin by bin — the packet's one computed number.

    The sample standard deviation (``ddof=1``) across the member curves, per
    bin, index-aligned with ``curve_grid.freqs_hz``. Taken over the rows THIS
    packet publishes, so a reader can reproduce the figure from the packet
    alone. Nothing upstream publishes it: the combiner forms the same array in
    :func:`~jasper.audio_measurement.spatial_combine._band_spread` and reduces
    it to two figures per octave band without letting the array out.

    It would be a different statistic if it did. The combiner's runs over
    ``per_position_db``, raw and unsmoothed on a LINEAR grid; the member curves
    here are ``per_position_diag_db``, smoothed at the diagnostic fraction and
    resampled onto a LOG 1/12-octave grid whose floor is the round's validity
    floor when it has a usable one and 20 Hz otherwise
    (``curve_grid.floor_source`` says which). Hence the name ``per_bin_sigma``
    rather than ``sigma_db`` or ``max_sigma_db``, which already mean the
    combiner's own reductions.

    It lives inside the positions block, beside the grid it is on: a reader
    must not be able to pair a spread with a grid it was not taken over.

    Below two usable seats it refuses and does not publish 0.0 — a sample
    standard deviation is undefined at n=1, and a zero would say the seats
    agreed.

    ``statistics.stdev`` rather than numpy: it RAISES at n < 2 instead of
    returning a silent ``NaN``, and computes in exact arithmetic, so a spread
    too large for a float is an ``OverflowError`` rather than an ``inf`` that
    would reach the fingerprint. Do not weaken the guard or the ``except``.
    Exact arithmetic costs 1.8 ms for the shipped shape (4 seats over an
    89-bin grid) and 54 ms for a pessimistic 12 x 2048; the packet is built by
    an offline CLI, never on an audio path.
    """
    n_bins = len(freqs_hz) if isinstance(freqs_hz, list) else 0
    if not n_bins:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": REASON_NO_CURVE_GRID,
            "n_seats": 0,
            "n_seats_excluded": len(rows),
        }
    curves: list[list[float]] = []
    excluded = 0
    for row in rows:
        curve = _member_curve(row.get("magnitude_db"), n_bins)
        if curve is None:
            excluded += 1
        else:
            curves.append(curve)
    if len(curves) < 2:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": REASON_TOO_FEW_SEATS,
            "n_seats": len(curves),
            "n_seats_excluded": excluded,
        }
    try:
        per_bin_sigma_db = [
            round(
                statistics.stdev(curve[index] for curve in curves), _SIGMA_DECIMALS
            )
            for index in range(n_bins)
        ]
    except OverflowError:
        # Reachable: ``statistics.stdev`` raises rather than returning ``inf``
        # when the exact result will not fit a float, which a hand-edited
        # member curve near the float ceiling does.
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": REASON_CROSS_SEAT_SPREAD_OVERFLOW,
            "n_seats": len(curves),
            "n_seats_excluded": excluded,
        }
    return {
        "available": True,
        "n_seats": len(curves),
        "n_seats_excluded": excluded,
        "per_bin_sigma_db": per_bin_sigma_db,
        "source": "positions[].magnitude_db, across seats, one value per grid bin",
        "uncertainty": {
            # Empty, and that is the answer rather than an omission — see note.
            "fields": {},
            "not_uncertainties": dict(
                sorted(_CROSS_SEAT_SIGMA_NOT_AN_UNCERTAINTY.items())
            ),
            # The third list, for the case neither of the first two describes: a
            # real spread about a reading whose kind this evidence cannot say.
            "unseparated": {
                field: dict(entry)
                for field, entry in sorted(_CROSS_SEAT_SIGMA_UNCERTAINTY.items())
            },
            "note": (
                "fields is empty because nothing here is a random OR a "
                "systematic uncertainty: the one spread published is a pooling "
                "of both, so it is declared under unseparated rather than filed "
                "as a kind it does not have. The rule that produces that "
                "answer: the two kinds are never pooled into one number, and "
                "where a measurement can only yield a pooled one it says so and "
                "names what would separate it — here, a repeat spread at a "
                "fixed pose, which is the banked repeat floor "
                "(accuracy_budget.in_capture_repeat_floor)"
            ),
        },
        "note": (
            "one value per curve_grid.freqs_hz bin, in that order, computed "
            "from the position rows THIS packet publishes — so it is "
            "reproducible from the packet alone. UNCENTRED: a seat that simply "
            "plays louder raises it, because a level difference between seats "
            "is part of what 'the seats disagree' means here. It is not the "
            "combiner's sigma_db/max_sigma_db under another name: those are "
            "taken over raw unsmoothed curves on a linear grid and reduced to "
            "two figures per octave band, they describe the cloud_measure "
            "group, and they reach only candidate.json's exclusion_evidence — "
            "not this document, and not the cloud evidence it is built from"
        ),
    }


def _positions_block(cloud: dict[str, Any]) -> dict[str, Any]:
    """Per-position curves and capture integrity, copied rather than derived.

    The grid, the curves and the flat reference all come from ONE artifact, so
    a reader cannot compare a curve from one evaluation against a reference
    from another. The one DERIVED field is ``cross_seat_sigma``, which sits
    here so a spread and the grid it was taken over are not separable.
    """
    positions = _mapping(cloud.get("positions"))
    grid = _mapping(positions.get("curve_grid"))
    rows: list[dict[str, Any]] = []
    withheld: set[str] = set()
    for entry in positions.get("positions") or []:
        if not isinstance(entry, dict):
            continue
        kept, dropped = _copy_allowed(entry, _POSITION_FIELDS + ("magnitude_db",))
        withheld.update(dropped)
        rows.append(kept)
    freqs_hz = grid.get("freqs_hz") or []
    return {
        "available": bool(rows),
        "schema": positions.get("schema"),
        "n_positions": len(rows),
        "curve_grid": {
            "freqs_hz": freqs_hz,
            "fractional_octave": grid.get("fractional_octave"),
            "smoothing_fraction": grid.get("smoothing_fraction"),
            "floor_hz": grid.get("floor_hz"),
            "floor_source": grid.get("floor_source"),
        },
        "positions": rows,
        # Derived from the two above and published beside them, so a reader
        # cannot pair the spread with a grid it was not taken over.
        "cross_seat_sigma": _cross_seat_sigma_block(freqs_hz, rows),
        "redacted_fields": sorted(withheld),
        # The bearings this round's own seats were prompted at — or, for a
        # round whose records predate the writer, why there are none.
        "angle_deg": _angle_deg_block(rows),
        "role_vocabulary": sorted({
            str(row.get("role")) for row in rows if row.get("role")
        }),
    }


def _angle_deg_block(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The cloud seats' own bearings, or the reason this round banks none.

    :func:`~.spatial.cloud_position_record` stamps ``position_deg`` /
    ``position_axis`` / ``mark_distance_m`` on every retained cloud position,
    so the absence branch states only the NARROW fact: THIS round's rows carry
    no bearing. It names ``position_axis`` and ``role`` as the fields that
    separate the ways that happens — a vertical seat and a geometry-retake seat
    both legitimately bank no degree.

    ``bool`` subclasses ``int``, so a ``true`` in the field would otherwise
    publish 1 as a bearing.
    """
    angles = sorted({
        row["position_deg"] for row in rows
        if isinstance(row.get("position_deg"), int)
        and not isinstance(row.get("position_deg"), bool)
    })
    if angles:
        return {
            "available": True,
            "angles_deg": angles,
            "note": (
                "position_deg is signed whole degrees, negative LEFT of the "
                "design axis, read off each seat's own record rather than "
                "parsed out of its prompt. A seat may carry none — a vertical "
                "pose commands no bearing, and neither does a geometry-locked "
                "retake, whose record declares no side — so this set can "
                "be shorter than n_positions, and positions[].position_axis "
                "with positions[].role says which seats are missing from it. "
                "These are cloud seats, not the lateral walk's poses in the "
                "lateral_poses block; the two are different captures and "
                "share no row."
            ),
        }
    return {
        "available": False,
        "status": "not_evaluated",
        "reason": (
            "no position row in this round carries position_deg, so no seat in "
            "it states a bearing. Different rounds look like this and "
            "positions[].position_axis with positions[].role separates them: "
            "one banked before the capture-time writer gained the field, and "
            "one whose seats commanded no bearing at all — a vertical pose, or "
            "a geometry-locked retake, whose record declares no side"
        ),
    }


def _banked_takes(
    session_dir: Path,
    rows: Sequence[Measurement],
    phase: str | None,
    read: Callable[[Path], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Every banked take of one ``phase``, narrowed by its own accept rule.

    ``rows`` is the bundle's measurement index, scanned ONCE per packet
    (:func:`~.record_index.bundle_measurements` rescans on every call).
    ``phase`` of ``None`` takes every take the round banked. ``read`` still
    OPENS each selected file and may reject it: the index narrows the
    candidates, the record decides.
    """
    artifacts = session_dir / EVIDENCE_ROOT / "artifacts"
    takes = [
        read(artifacts / row.path)
        for row in rows
        if phase is None or row.phase == phase
    ]
    return [take for take in takes if take is not None]


def _distinct_degrees(takes: list[Any], field: str) -> list[int]:
    """The sorted whole degrees ``field`` carries across ``takes``."""

    values = set()
    for take in takes:
        value = take.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            values.add(value)
    return sorted(values)


def _lateral_poses_block(
    session_dir: Path, rows: Sequence[Measurement],
) -> dict[str, Any]:
    """The signed bearings a lateral walk banked, one row per accepted take.

    Read through :func:`~.position_cycle.read_lateral_take`, the same accept
    rule :func:`~.position_cycle.position_cycle_document` uses for these files.

    Beside the ``positions`` block, never merged into it: a cloud position is a
    summed sweep at a floor-plan seat judged by gating and ripple, a lateral
    pose is a per-driver measurement at a bearing on the design axis.

    ``position_deg`` is the SIGNED whole-degree bearing, negative LEFT of the
    design axis, stamped by :func:`~.spatial.lateral_pose_record` at take time.
    A commanded pose recorded verbatim, not a measurement with a spread, so
    this block publishes no uncertainty.

    Both survivors and superseded takes are listed, because the speaker keeps
    both on disk deliberately.
    """
    takes = _banked_takes(
        session_dir, rows, PHASE_LATERAL, position_cycle.read_lateral_take,
    )
    if not takes:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": (
                f"this round banked no {PHASE_LATERAL} take records under "
                f"{_POSITIONS_SUBDIR}/ — its walk was refused at take time, its "
                "poses were never accepted, or the round ran no lateral walk "
                "at all"
            ),
            "n_takes": 0,
        }
    # Coerced rather than cast: a hand-edited sidecar with a non-numeric index
    # sorts first instead of raising.
    takes.sort(key=lambda take: (_ordinal(take["index"]), _ordinal(take["attempt"])))
    return {
        "available": True,
        "n_takes": len(takes),
        "takes": takes,
        # ``bool`` subclasses ``int``, so a ``true`` in either field would
        # otherwise publish 1 as a degree.
        "angles_deg": _distinct_degrees(takes, "position_deg"),
        "elevations_deg": _distinct_degrees(takes, "vertical_deg"),
        "source": f"{_POSITIONS_SUBDIR}/<take_id>.json",
        "note": (
            "position_deg is signed whole degrees, negative LEFT of the design "
            "axis. These are LATERAL walk poses, not the cloud seats in the "
            "positions block above; the two are different captures and share "
            "no row. Membership is every ACCEPTED take, a superseded attempt "
            "included — which is a different set from the conductor's live "
            "lateral_poses, where a retake replaces the attempt it supersedes "
            "and only the latest per index survives"
        ),
    }
