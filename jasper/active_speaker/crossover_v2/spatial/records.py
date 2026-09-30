# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jasper.audio_measurement.program import KIND_SUMMED_SWEEP, KIND_SWEEP, PROGRAM_PHASE_MEASURE

from ...measurement_programs import POSE_KIND_BEARING, validated_pose
from ..contracts import POSITION_AXES
from ..pose_curve import lateral_pose_curve, pose_curve_record


def _primary_sweep_bands(program: Any) -> dict[str, tuple[float, float]]:
    """Each role's PRIMARY sweep band, read off the program that played.

    ``kind == KIND_SWEEP`` matters because a MEASURE program opens with a
    leading pilot pair that carries a role and a band too, so a role-only match
    would take the pilot's. The two bands are equal today; this names which
    segment the retained curve's band describes if that coupling moves.
    """
    bands: dict[str, tuple[float, float]] = {}
    for segment in program.segments:
        if segment.kind != KIND_SWEEP or segment.role is None:
            continue
        if segment.f1_hz is None or segment.f2_hz is None:
            continue
        bands.setdefault(segment.role, (float(segment.f1_hz), float(segment.f2_hz)))
    return bands


def _summed_sweep_band_hz(program: Any) -> tuple[float, float] | None:
    """The band a SUMMED sweep drove — the one segment the map above cannot key.

    A summed sweep declares ``role=None``, so there is no key to file it under
    in :func:`_primary_sweep_bands`. ``None`` for a program that plays no summed
    sweep, which a MEASURE program is.
    """
    for segment in program.segments:
        if segment.kind != KIND_SUMMED_SWEEP:
            continue
        if segment.f1_hz is None or segment.f2_hz is None:
            continue
        return (float(segment.f1_hz), float(segment.f2_hz))
    return None


# --------------------------------------------------------------------------- #
# what a retained take records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionGeometry:
    """WHERE a prompted capture was taken, as numbers instead of a sentence.

    The frame, stated once: ``degrees`` is the signed whole-degree HORIZONTAL
    bearing measured from the speaker, negative LEFT of the design axis as seen
    from the microphone looking at the speaker; ``vertical_deg`` is the signed
    whole-degree ELEVATION above mark height, negative BELOW; ``axis`` is which
    of :data:`POSITION_AXES` the stated move was on; ``mark_distance_m`` is the
    speaker-to-MARK distance both angles are DERIVED AGAINST — a reference
    length, never a surveyed capsule distance.

    The two angles default differently. ``degrees`` is ``None`` wherever no
    signed bearing was commanded (a vertical pose, or a horizontal one whose
    record declares no side), because ``0`` would read as "on the design axis".
    ``vertical_deg`` has no such case: a pose nobody raised is genuinely at mark
    height. A compound pose states both.

    Whole degrees, because the poses come from tape-measure offsets to a mark
    placed "about" 1 m out. No combination of axis and angle is refused here —
    a vertical walk is performed by hand, and the automation that cannot swing
    in elevation refuses at ``capture_plan.position_angle_deg``.

    ``kind`` categorizes the pose (ADR-0260). A ``seat`` pose is
    stated from the listener's head as ``seat_offset_m`` ``(right, forward,
    up)``, not from the mark, so its ``mark_distance_m`` is ``None``; a
    ``close`` pose states its own standoff there.
    """

    axis: str
    degrees: int | None
    mark_distance_m: float | None
    vertical_deg: int = 0
    kind: str = POSE_KIND_BEARING
    seat_offset_m: tuple[float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.axis not in POSITION_AXES:
            raise ValueError(
                f"a pose axis must be one of {POSITION_AXES}, got {self.axis!r}"
            )
        object.__setattr__(
            self, "seat_offset_m", validated_pose(self.kind, self.seat_offset_m)[0],
        )
        # `bool` is an `int` and is never an elevation.
        if isinstance(self.vertical_deg, bool) or not isinstance(
            self.vertical_deg, int
        ):
            raise ValueError(
                "a pose elevation is a whole number of degrees above mark "
                f"height, got {self.vertical_deg!r}"
            )


def phase_composition(analysis: Any, *, protection_emitted: bool) -> str:
    """Which composition the curves on this analysis carry, or ``""``.

    ``crossover_composed``: §4.2's ``M*C/P`` ran, so the curve carries the
    configured crossover's phase rather than the emitted protection's.
    ``protection_retained``: protection WAS emitted and no composition removed
    it, which is the lateral walk's deliberate case.

    ``""`` — unstated, never either word — wherever the question was not put:
    a CHECK or VERIFY capture, whose analyzer never composes and whose
    ``configured_path_composed`` is therefore a default rather than an answer,
    and a box that emitted no protection for a curve to retain. Both of those
    would otherwise read as ``protection_retained``, which is a claim about
    contamination that is not true of either (docs/tuning-methodology.md §4
    step 1).

    ``protection_emitted`` is the SESSION's fact — whether the capture graph
    carried a protective high-pass at all — and is not derivable from the
    analysis, which sees only whether it was handed priors to divide out.
    """
    if getattr(analysis, "branch_diagnostic", None):
        return "complete_tune_measured"
    if analysis.phase != PROGRAM_PHASE_MEASURE:
        return ""
    if analysis.configured_path_composed:
        return "crossover_composed"
    return "protection_retained" if protection_emitted else ""


def analysis_curve_records(analysis: Any, program: Any) -> list[dict[str, Any]]:
    """One analysis's PRIMARY complex responses, in the banked curve shape.

    One shape for every retained kind, so a reader has one thing to parse.

    BOTH response fields are read, because
    :mod:`~jasper.audio_measurement.program_analysis` fills them on different
    paths: a per-driver analysis fills ``driver_responses``, a summed-sweep
    analysis fills ``summed_response``. A union rather than a branch, so an
    analysis that grows the other half starts banking it. CHECK fills neither.

    One record per PRIMARY response and window: each response's own curve,
    then, for each response whose gate applied, that arrival read ungated
    (ADR-0383 §2), so a reader picks a role's curve by its window. A role's
    repeat occurrences ride nested on their own primary (:func:`pose_curve_record`)
    rather than as rows of their own. They remain diagnostic and feed no
    candidate/trim/alignment math. A role whose band the program does not
    declare is SKIPPED rather than banked on a guessed band, since outside the
    driven band the samples are noise. An empty list therefore means NO CURVE
    WAS BANKED, never "this capture was clean".
    """
    bands = _primary_sweep_bands(program)
    primaries = [
        (response, bands[response.role]) for response in analysis.driver_responses
        if response.repeat_index is None and response.role in bands
    ]
    summed = analysis.summed_response
    summed_band = _summed_sweep_band_hz(program)
    if summed is not None and summed_band is not None:
        primaries.append((summed, summed_band))
    return [
        pose_curve_record(lateral_pose_curve(response, band, ungated=ungated))
        for ungated in (False, True) for response, band in primaries
        if not ungated or (response.gating or {}).get("applied")
    ]


# The named question each prompted position answers. Persisted with the
# position so the attribution stage consumes a labelled sample rather than an
# anonymous member of an average; profile-independent.
#
#   ONAX  — inside the design-axis window (lateral offset < WIDE_OFFSET_MIN_CM)
#   OFFAX — out at the coverage edge (lateral offset >= WIDE_OFFSET_MIN_CM)
#   XOVR  — vertical offset: the axis the woofer/tweeter crossover lobes on
#
# A CONSUMER MUST NOT ASSUME a cloud carries every role: roles come from the
# walked PREFIX of the table, and a short walk stops before the first vertical
# move. An absent role is unsampled, never null evidence.
POSITION_ROLE_ONAX = "onax"
POSITION_ROLE_OFFAX = "offax"
POSITION_ROLE_XOVR = "xovr"
POSITION_ROLES = (POSITION_ROLE_ONAX, POSITION_ROLE_OFFAX, POSITION_ROLE_XOVR)

# The mark distance the CHECK screen asks for ("about 1 m in front of the
# speaker") — the reference length that turns this flow's lateral OFFSETS into
# the BEARINGS a positioner can act on. A default, not a pin: a categorized
# pose states its own distance (ADR-0260).
MARK_DISTANCE_M = 1.0
