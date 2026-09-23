# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.program import KIND_SUMMED_SWEEP, KIND_SWEEP, PROGRAM_PHASE_MEASURE

from ...measurement_programs import POSE_KIND_BEARING, validated_pose
from ..contracts import DESIGN_AXIS_DEG, POSITION_AXES, POSITION_AXIS_HORIZONTAL
from ..journey import PHASE_ENTRY_BASELINE, PHASE_LATERAL
from ..pose_curve import LateralPoseCurve, lateral_pose_curve, pose_curve_record


@dataclass(frozen=True)
class LateralPose:
    """One accepted pose in the lateral walk.

    Carries NO trim, delay, polarity or fit, structurally: re-solving any of
    them per pose is forbidden, and there is no field here to write one to.

    ``pose_id`` is the canonical key for a POSE on every surface.
    ``position_id`` / ``position_index`` answer a different question — which
    slot of a walk — and joining takes on ``position_id`` mixes poses into the
    seat table.
    """

    pose_id: str
    index: int
    attempt: int
    prompt: str
    role: str
    offset_cm: float
    at_mark: bool
    curves: tuple[LateralPoseCurve, ...]

    def curve(self, role: str) -> LateralPoseCurve | None:
        for curve in self.curves:
            if curve.role == role:
                return curve
        return None


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


def pose_kind_fields(
    geometry: PositionGeometry, *, gating_applied: bool | None = None,
) -> dict[str, Any]:
    """Measured geometry and analysis facts shared by retained takes."""
    return {
        "mark_distance_m": geometry.mark_distance_m,
        **({"gating_applied": gating_applied} if gating_applied is not None else {}),
        **({
            "pose_kind": geometry.kind,
            "seat_offset_m": list(geometry.seat_offset_m) if geometry.seat_offset_m is not None else None,
        } if geometry.kind != POSE_KIND_BEARING else {}),
    }


def take_id_for(position_id: str, attempt: int) -> str:
    """One take's id, as every builder that mints one spells it.

    A geometry retake reuses the position id, so the position id alone does not
    identify a take. Zero-padded so a lexical sort of the bundle is also a
    chronological one.
    """
    return f"{position_id}_a{int(attempt):02d}"


_ATTEMPT_SUFFIX = re.compile(r"_a\d+$")


def take_stop_id(take_id: str) -> str:
    """The prompted stop a take measured: its id with the attempt struck.

    :func:`take_id_for`'s inverse. Takes sharing a stop id are attempts at one
    prompted spot and only the newest speaks for it, which is the key "latest
    attempt wins" supersedes across. An id carrying no attempt suffix is its own
    stop.
    """
    return _ATTEMPT_SUFFIX.sub("", take_id)


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


@dataclass(frozen=True)
class TakeClaim:
    """What the SESSION claimed around one take, on every record it banks.

    Carried at the builders so a flow-banked take and an engine-banked take are
    one record shape. Every field defaults empty because an unstated field is an
    honest fact about the capture, never a refusal to bank it.

    ``level_db`` is the PROVEN fader level and ``stimulus_dbfs`` is the ladder
    rung the stimulus played at — two quantities on purpose, since a ladder
    moves the stimulus and never the claim. ``level_db`` is optional here where
    an engine-banked record's is not: the flow's retention sites hold no volume
    claim, so ``None`` says exactly that rather than inviting an invented
    number. ``stimulus_dbfs`` is ``None`` when no ladder was asked for.

    ``wav_path`` is the record → capture pointer, bundle-relative, and is NOT
    derivable from ``take_id`` (``bundles.capture_artifact_relpath`` appends a
    ``uuid4`` hex).
    """

    measure_kind: str = ""
    baseline_record_id: str = ""
    candidate_id: str = ""
    polarity: str = ""
    #: Whether the graph this take played through carried the box's own
    #: per-driver level match, and by how much: a reverse-null pair is only
    #: comparable to a reader who knows whether the branches were levelled
    #: before they were summed. ``False``/``None`` on a take that declared none.
    level_matched: bool = False
    level_match_trims_db: Mapping[str, float] | None = None
    level_db: float | None = None
    stimulus_dbfs: float | None = None
    incident: str = ""
    wav_path: str = ""
    #: Which of :func:`phase_composition`'s two words the banked curves carry.
    #: ``""`` where it says neither, and ABSENT from the record there: an
    #: unstated composition must not read as either one.
    phase_composition: str = ""


def _take_identity(
    *,
    position_id: str,
    phase: str,
    index: int,
    attempt: int,
    session_id: str,
    wav_sha256: str | None,
    graph_fingerprint: str = "",
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """The identity block every retained take carries, whatever kind it is.

    The common core; each builder adds its own role-tagged extension rather than
    sharing one shape with half its columns null. Deliberately NOT emitted here:
    the id key itself — a cloud position calls it ``position_id`` and a pose
    calls it ``pose_id``, which are two questions.

    ``wav_sha256`` is the capture's content digest: the VERIFIER for a replay,
    never the index. Recorded whether or not any store retained the bytes.
    ``claim.wav_path`` is its pointer sibling.

    """
    return {
        "phase": phase,
        "index": index,
        "attempt": attempt,
        "take_id": take_id_for(position_id, attempt),
        "session_id": session_id,
        "wav_sha256": wav_sha256,
        "measure_kind": claim.measure_kind,
        "graph_fingerprint": graph_fingerprint,
        "baseline_record_id": claim.baseline_record_id,
        "candidate_id": claim.candidate_id,
        "polarity": claim.polarity,
        "level_matched": claim.level_matched,
        # The numbers only when there ARE numbers: an absent key reads as an
        # un-matched take, so no schema version moves.
        **(
            {"level_match_trims_db": dict(claim.level_match_trims_db)}
            if claim.level_matched and claim.level_match_trims_db
            else {}
        ),
        # Stated or absent, never a guessed default: see TakeClaim.
        **(
            {"phase_composition": claim.phase_composition}
            if claim.phase_composition
            else {}
        ),
        "level_db": claim.level_db,
        "stimulus_dbfs": claim.stimulus_dbfs,
        "incident": claim.incident,
        "wav_path": claim.wav_path,
    }


def cloud_position_record(
    *,
    position_id: str,
    phase: str,
    index: int,
    attempt: int,
    prompt: str,
    wide: bool,
    role: str,
    geometry: PositionGeometry,
    captured_at: float,
    session_id: str,
    gate_window_ms: float | None,
    gate_floor_source: str | None,
    gate_disclosure: str | None,
    gate_moved_rms_db: float | None,
    gate_reflection_delay_ms: float | None,
    gate_entanglement_floor_hz: float | None,
    gate_entanglement_floor_source: str,
    validity_floor_hz: float | None,
    gating_applied: bool,
    summed_ripple_db: float | None,
    glitch_detected: bool,
    wav_sha256: str | None,
    graph_fingerprint: str = "",
    regime: str = "",
    curves: Sequence[Mapping[str, Any]] = (),
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """One retained cloud position, as a banked record.

    ``take_id`` is minted here so the session's evidence and the bundle's
    sidecar path name the same take.

    ``gate_floor_source`` records WHY the gate window is what it is (#1966);
    ``gating_applied`` alone cannot distinguish a window that stops at a found
    reflection from one capped at the search bound. ``gate_disclosure`` is the
    same fact as a sentence.

    ``gate_moved_rms_db`` and ``gate_reflection_delay_ms`` are the two numbers
    that sentence narrates, from the same
    :mod:`~jasper.audio_measurement.gate_disclosure` record, so digits and
    prose share a derivation. Both are ``None`` on an ungateable capture, and
    the delay is ``None`` — never 0.0 — on a window capped at the search
    ceiling. The delay is RELATIVE to the direct arrival, not the gating block's
    absolute ``first_reflection_ms``.

    ``gate_entanglement_floor_hz`` is the ROOM's floor at THIS seat and
    ``gate_entanglement_floor_source`` says which of
    :data:`~jasper.audio_measurement.gating.ENTANGLEMENT_SOURCES` timed it —
    never one without the other (#3502). Banked per SEAT because it is derived
    at the seat's own ``mark_distance_m``. ``unknown`` with a null floor is
    ordinary on a rig whose first bounce lands while the direct sound is still
    decaying.

    ``regime`` is WHAT PLAYED, in the walk seam's vocabulary
    (:data:`LATERAL_POSE_REGIME` is the other word in it), ``""`` until a caller
    states it. That vocabulary is NOT :data:`~.contracts.MEASURE_REGIMES`',
    which the engine's record spells under the same key — two vocabularies, one
    key name.

    ``geometry`` is WHERE the microphone was, as fields rather than English:
    ``position_deg`` (``None`` where no bearing was commanded),
    ``position_axis``, ``vertical_deg`` and ``mark_distance_m``, stamped from
    the pose the operator was given, with ``prompt`` beside them as the human
    instruction rather than the source of truth. ``vertical_deg`` is absent from
    older records and a reader takes that absence as 0. See
    :class:`PositionGeometry` for the frame.

    ``curves`` is WHAT WAS MEASURED, in :func:`pose_curve_record`'s shape.
    """
    return {
        "position_id": position_id,
        **_take_identity(
            position_id=position_id, phase=phase, index=index, attempt=attempt,
            session_id=session_id, wav_sha256=wav_sha256,
            graph_fingerprint=graph_fingerprint, claim=claim,
        ),
        "prompt": prompt,
        "regime": regime,
        "wide": wide,
        # The position's named question: the prompt string alone cannot be
        # parsed back into a role, so the label rides the record explicitly.
        "role": role,
        "position_deg": geometry.degrees,
        "position_axis": geometry.axis,
        "vertical_deg": geometry.vertical_deg,
        "captured_at": captured_at,
        "gate_window_ms": gate_window_ms,
        "gate_floor_source": gate_floor_source,
        "gate_disclosure": gate_disclosure,
        "gate_moved_rms_db": gate_moved_rms_db,
        "gate_reflection_delay_ms": gate_reflection_delay_ms,
        "gate_entanglement_floor_hz": gate_entanglement_floor_hz,
        "gate_entanglement_floor_source": gate_entanglement_floor_source,
        "validity_floor_hz": validity_floor_hz,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
        "curves": [dict(curve) for curve in curves],
        **pose_kind_fields(geometry, gating_applied=gating_applied),
    }


#: What every :data:`~.journey.PHASE_LATERAL` pose plays: the anchor's
#: interleaved per-driver MEASURE object. A literal copy of
#: :data:`jasper.active_speaker.angle_capture.REGIME_PER_DRIVER` because
#: importing it would close a cycle; pinned equal by test.
LATERAL_POSE_REGIME = "per_driver"

def analysis_curve_records(analysis: Any, program: Any) -> list[dict[str, Any]]:
    """One analysis's PRIMARY complex responses, in the banked curve shape.

    One shape for every retained kind, so a reader has one thing to parse.

    BOTH response fields are read, because
    :mod:`~jasper.audio_measurement.program_analysis` fills them on different
    paths: a per-driver analysis fills ``driver_responses``, a summed-sweep
    analysis fills ``summed_response``. A union rather than a branch, so an
    analysis that grows the other half starts banking it. CHECK fills neither.

    One record per PRIMARY response; a role's repeat occurrences ride nested on
    their own primary (:func:`pose_curve_record`) rather than as rows of their
    own, so a reader counting curves still counts roles. They remain diagnostic
    and feed no candidate/trim/alignment math. A role whose band the
    program does not declare is SKIPPED rather than banked on a guessed band,
    since outside the driven band the samples are noise. An empty list
    therefore means NO CURVE WAS BANKED, never "this capture was clean".
    """
    bands = _primary_sweep_bands(program)
    records = [
        pose_curve_record(lateral_pose_curve(response, bands[response.role]))
        for response in analysis.driver_responses
        if response.repeat_index is None and response.role in bands
    ]
    summed = analysis.summed_response
    summed_band = _summed_sweep_band_hz(program)
    if summed is not None and summed_band is not None:
        records.append(pose_curve_record(lateral_pose_curve(summed, summed_band)))
    return records


def lateral_pose_record(
    pose: LateralPose,
    *,
    geometry: PositionGeometry,
    lateral_consumer: str,
    session_id: str,
    graph_fingerprint: str,
    captured_at: str,
    wav_sha256: str | None,
    claim: TakeClaim = TakeClaim(),
    gating_applied: bool | None = None,
) -> dict[str, Any]:
    """One lateral capture with its actual pose, purpose and analyzed curves."""
    if geometry.degrees is None:
        raise ValueError("a lateral pose commands a horizontal bearing; this geometry declares none")
    return {
        "pose_id": pose.pose_id,
        **_take_identity(
            position_id=pose.pose_id, phase=PHASE_LATERAL, index=pose.index,
            attempt=pose.attempt, session_id=session_id, wav_sha256=wav_sha256,
            graph_fingerprint=graph_fingerprint, claim=claim,
        ),
        "prompt": pose.prompt,
        "role": pose.role,
        "position_deg": int(geometry.degrees),
        "position_axis": POSITION_AXIS_HORIZONTAL,
        "vertical_deg": int(geometry.vertical_deg),
        "offset_cm": float(pose.offset_cm),
        "at_mark": bool(pose.at_mark),
        "regime": LATERAL_POSE_REGIME,
        "lateral_consumer": lateral_consumer,
        "captured_at": captured_at,
        "curves": [pose_curve_record(curve) for curve in pose.curves],
        **pose_kind_fields(geometry, gating_applied=gating_applied),
    }


def phase_capture_record(
    *,
    phase: str,
    index: int,
    attempt: int,
    session_id: str,
    graph_fingerprint: str,
    captured_at: str,
    wav_sha256: str | None,
    prompt: str = "",
    regime: str = "",
    curves: Sequence[Mapping[str, Any]] = (),
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """One banked take for a phase that prompts no spot: CHECK, MEASURE, VERIFY.

    These play from wherever the microphone already is, so a take records the
    CAPTURE: its digest, the identity that finds it again, and ``curves``.

    The curves are the only part of the analysis this record keeps: a round's
    verdicts are rewritten inside the round, but the complex responses they were
    drawn from land in no file unless they land here. CHECK banks an empty list
    because it computes no transfer function; an empty list is "no curve banked"
    and never "this capture was clean".

    The take id follows the entry baseline's convention — the position id is
    minted from phase and index, so it IS the take id once :func:`take_id_for`
    qualifies it by attempt.

    The pose is :data:`~.contracts.DESIGN_AXIS_DEG` on the horizontal axis,
    which is the reading ``session.TuningSession._bearings`` gives a spec naming
    no position, so one pose is one record on both sides. ``prompt`` is ``""``
    because no instruction was issued, a different fact from an unknown one;
    ``regime`` is the caller's to state and is never guessed from the phase.
    """
    identity = _take_identity(
        position_id=f"{phase}_{index:02d}",
        phase=phase, index=index, attempt=attempt,
        session_id=session_id, wav_sha256=wav_sha256,
        graph_fingerprint=graph_fingerprint, claim=claim,
    )
    return {
        # No prompted spot of its own, so the position id IS the take id.
        "position_id": identity["take_id"],
        **identity,
        "captured_at": captured_at,
        "prompt": prompt,
        "regime": regime,
        "position_deg": DESIGN_AXIS_DEG,
        "position_axis": POSITION_AXIS_HORIZONTAL,
        "vertical_deg": 0,
        "curves": [dict(curve) for curve in curves],
    }


def entry_baseline_record(
    *,
    index: int,
    attempt: int,
    session_id: str,
    program_id: str,
    reference_mark: str,
    graph_fingerprint: str,
    captured_at: str,
    freqs_hz: Sequence[float],
    magnitude_db: Sequence[float],
    excluded: Sequence[bool],
    validity_floor_hz: float | None,
    gate_window_ms: float | None,
    summed_ripple_db: float | None,
    glitch_detected: bool,
    wav_sha256: str | None,
    prompt: str = "",
    regime: str = "",
    curves: Sequence[Mapping[str, Any]] = (),
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """The entry baseline's retained record — a cloud position's shape, minus
    the group, plus the curve.

    Three fields a cloud position has no use for make THIS capture comparable to
    the post-apply one, and are why it is a separate builder: WHAT was played
    (``program_id``), WHERE from (``reference_mark``), and WHICH graph it went
    through (``graph_fingerprint``).

    The reduced curve rides here, which is what makes this the DURABLE copy:
    a retained take is write-once and keyed by ``take_id``, while the flow state
    file holding the same arrays is rewritten on every persist. Bounded at
    ``round_evidence.BENEFIT_CURVE_MAX_BINS`` upstream. Same three arrays and
    names as ``round_evidence.EntryBaseline.to_dict``, so one reader covers both.

    ``curves`` is a SECOND curve on a second basis, not a copy: the three arrays
    are the GRADED side (decimated, magnitude only, carrying the ``excluded``
    mask), ``curves`` is the MEASURED side on the shared log basis with phase.
    Neither is derivable from the other.

    The pose is :data:`~.contracts.DESIGN_AXIS_DEG` on the horizontal axis, as
    for every capture with no prompted move. ``reference_mark`` says where that
    axis was measured from; ``prompt`` is ``""`` because no instruction was
    issued.
    """
    identity = _take_identity(
        position_id=f"{PHASE_ENTRY_BASELINE}_{index:02d}",
        phase=PHASE_ENTRY_BASELINE, index=index, attempt=attempt,
        session_id=session_id, wav_sha256=wav_sha256,
        graph_fingerprint=graph_fingerprint, claim=claim,
    )
    return {
        # No prompted spot of its own, so the position id IS the take id.
        "position_id": identity["take_id"],
        **identity,
        "program_id": program_id,
        "reference_mark": reference_mark,
        "prompt": prompt,
        "position_deg": DESIGN_AXIS_DEG,
        "position_axis": POSITION_AXIS_HORIZONTAL,
        "vertical_deg": 0,
        "regime": regime,
        "captured_at": captured_at,
        "freqs_hz": [float(hz) for hz in freqs_hz],
        "magnitude_db": [float(db) for db in magnitude_db],
        "excluded": [bool(flag) for flag in excluded],
        "validity_floor_hz": validity_floor_hz,
        "gate_window_ms": gate_window_ms,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
        "curves": [dict(curve) for curve in curves],
    }


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

#: The pose a capture with no prompted move of its own was taken at.
_DESIGN_AXIS_GEOMETRY = PositionGeometry(
    axis=POSITION_AXIS_HORIZONTAL,
    degrees=0,
    mark_distance_m=MARK_DISTANCE_M,
)
