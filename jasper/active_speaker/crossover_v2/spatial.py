# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a capture-consuming phase DECIDES about one take: is it evidence, what
does it record, and does the group want another one.

Serves the phases that consume a take without prescribing anything from it —
the two position clouds, the lateral walk, and the entry baseline. Each ladder's
content is its ORDER and the gates it deliberately DROPS, both stated at the
ladder.

Three rules this module keeps: inputs are STATED, never reached for (the caller
evaluates the shared predicates into a :class:`CaptureScreens`); no household
vocabulary — a refusal leaves as a kind from :data:`SCREEN_KINDS` and
:mod:`.refusal_copy` maps it; and side-effect-free, so
:func:`boost_excluded_bands_hz` returns its log fields as data
(:attr:`BoostExclusion.diagnostics`) rather than journalling them. No
``jasper.web`` import and nothing from :mod:`..crossover_v2_flow`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.gating import (
    ENTANGLEMENT_SOURCE_DECLARED,
    ENTANGLEMENT_SOURCE_MEASURED,
    TRUSTED_FLOOR_MULTIPLIER,
    EntanglementFloor,
)
from jasper.audio_measurement.program import (
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
)
from jasper.audio_measurement.program_analysis import INTEGRITY_CHECK_SWEEP_HEARD

from .contracts import (
    DESIGN_AXIS_DEG,
    ENTRY_GRAPH_FINGERPRINT_UNKNOWN,
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
    CaptureValidity,
)
from .journey import (
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_VERIFY,
)
from .round_evidence import MeasuredResponse, measured_response_from_analysis
from .verification import (
    ECHO_BAND_HF_REGIME_FLOOR_HZ,
    _crossover_region_null_registry,
    _null_registry_to_dict,
    evaluate_capture_validity,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

__all__ = [
    "CARVE_OUT_SOURCE_IDENTIFIED_NULL",
    "CARVE_OUT_SOURCE_POSITION_SCREEN",
    "CLOUD_CLOSE_NONE",
    "CLOUD_CLOSE_AWAITING_CONFIRM",
    "CLOUD_CLOSE_RUNNING",
    "CLOUD_CURVE_MAX_JSON_POINTS",
    "GEOMETRY_RETRY_POSITIONS",
    "LATERAL_EVIDENCE_BAND_HZ",
    "LATERAL_EVIDENCE_POINTS_PER_OCTAVE",
    "LATERAL_POSE_REGIME",
    "MARK_DISTANCE_M",
    "POSITION_AXES",
    "POSITION_AXIS_HORIZONTAL",
    "POSITION_AXIS_VERTICAL",
    "POSITION_ROLES",
    "POSITION_ROLE_OFFAX",
    "POSITION_ROLE_ONAX",
    "POSITION_ROLE_XOVR",
    "PositionGeometry",
    "SCREEN_LOCATE_FAILED",
    "SCREEN_PILOT_LEVEL_COLLAPSE",
    "SCREEN_LINEARITY_FAILED",
    "SCREEN_CAPTURE_GLITCH",
    "SCREEN_CLIPPED",
    "SCREEN_KINDS",
    "CaptureScreens",
    "EntryBaselineScreen",
    "GeometryRetake",
    "BoostExclusion",
    "CloudCombine",
    "CloudGroupResult",
    "CloudVerdict",
    "LateralPose",
    "LateralPoseCurve",
    "assemble_cloud_group_result",
    "carve_outs_by_band",
    "cloud_entanglement_floor_hz",
    "cloud_position_capture",
    "cloud_trusted_floor_hz",
    "cloud_validity_floor_hz",
    "combine_cloud_positions",
    "cloud_geometry_verdict",
    "cloud_position_screens",
    "lateral_pose_screens",
    "lateral_curves_sufficient",
    "lateral_evidence_grid_hz",
    "lateral_pose_curve",
    "entry_baseline_screens",
    "MIN_RESOLVED_CLOUD_POSITIONS",
    "group_position_floor",
    "geometry_retake",
    "take_id_for",
    "TakeClaim",
    "take_kind",
    "cloud_position_record",
    "pose_curve_record",
    "analysis_curve_records",
    "lateral_pose_record",
    "entry_baseline_record",
    "phase_capture_record",
    "boost_excluded_bands_hz",
]


# --------------------------------------------------------------------------- #
# cloud close state, and the geometry-retry ceiling (#2291 Phase 5c-ii)
# --------------------------------------------------------------------------- #

# Where the pre-apply cloud's close has got to. Read by the wizard through
# durable state; see :attr:`V2ConductorSnapshot.cloud_close`.
CLOUD_CLOSE_NONE = ""
CLOUD_CLOSE_AWAITING_CONFIRM = "awaiting_confirm"
CLOUD_CLOSE_RUNNING = "running"

# How many wider-spread RETAKES of the group's last position the
# geometry-locked check may ask for, once per group.
#
# Retakes rather than appended positions because of the PROTOCOL, not the
# physics: the relay runner completes a set at exactly ``capture_target``
# accepted captures with ``index == accepted_count + 1``, so rejecting a capture
# is the only lever that keeps a plan alive at the same index. Appending is the
# better estimator if the runner ever grows variable-length sets.
#
# Bounded on purpose: `geometry.locked` is a "spread the mic further" hint, not
# a failure, and no amount of mic movement decorrelates a source-fixed null, so
# an unbounded loop would never terminate. Two retakes, then proceed and RECORD
# the verdict.
GEOMETRY_RETRY_POSITIONS = 2


# --------------------------------------------------------------------------- #
# the refusal vocabulary — kinds, not household copy
# --------------------------------------------------------------------------- #

#: The stimulus was never located, or located but carried no usable curve.
SCREEN_LOCATE_FAILED = "locate_failed"
#: The two-level pilot pair never cleared the room floor (#1810).
SCREEN_PILOT_LEVEL_COLLAPSE = "pilot_level_collapse"
#: AGC in the recording chain bent the curve being measured.
SCREEN_LINEARITY_FAILED = "linearity_failed"
#: A spliced or otherwise glitched timeline — the transient capture class.
SCREEN_CAPTURE_GLITCH = "capture_glitch"
#: A sweep clipped.
SCREEN_CLIPPED = "clipped"

#: Every kind above, so the flow's mapping can be CHECKED for completeness
#: rather than trusted: a kind added here without an arm there is a wiring
#: defect.
SCREEN_KINDS = frozenset({
    SCREEN_LOCATE_FAILED,
    SCREEN_PILOT_LEVEL_COLLAPSE,
    SCREEN_LINEARITY_FAILED,
    SCREEN_CAPTURE_GLITCH,
    SCREEN_CLIPPED,
})


@dataclass(frozen=True)
class CaptureScreens:
    """The shipped capture-integrity predicates, EVALUATED, for one take.

    Every field is computed by the caller with the shared predicates in
    :mod:`.capture_dispatch`, which are total and side-effect-free, so stating
    them eagerly is exact even though the ladders below short-circuit.

    ``pilot_snr_ok`` and ``linearity_ok`` are tri-state (``None`` = not
    evaluated), and the ladders branch on ``is False``: an unevaluated screen is
    not a failed one.

    Every field is required, including the four a shorter ladder does not read:
    a permissive default would silently answer for a capture the caller never
    looked at the day a rung is added that reads it.
    """

    stimulus_located: bool
    pilot_snr_ok: bool | None
    linearity_ok: bool | None
    glitch_detected: bool
    sweep_locate_confidence_ok: bool
    sweep_schedule_ok: bool
    any_sweep_clipped: bool


# --------------------------------------------------------------------------- #
# the three ladders
# --------------------------------------------------------------------------- #


def cloud_position_screens(
    screens: CaptureScreens, *, has_summed_response: bool,
) -> str | None:
    """One prompted cloud position: the light per-capture QC, or a refusal kind.

    Per-position work is deliberately light — the group analyses (combine, null
    identification, spec evaluation) run ONCE per group. On the S0 ten-position
    corpus the combine is 2.7-2.8 s and everything layered on it totals
    0.02-0.04 s, so running the set per position would multiply the dominant
    cost by N.

    Two VERIFY gates are deliberately NOT applied, because both assume a
    stationary mic replaying the identical program: gate-comparability (a cloud
    position's gate legitimately differs from the anchor's, since the nearest
    boundary moves with the mic) and the G3 pilot-transfer step (moving the mic
    changes the acoustic transfer by design, so it says nothing about chain
    drift).

    ``has_summed_response`` is the last screen: the stimulus located but no
    summed response came back, so there is no curve to combine.
    """
    if not screens.stimulus_located:
        return SCREEN_LOCATE_FAILED
    if screens.pilot_snr_ok is False:
        # The room/level discriminator runs before the linearity branch so a
        # collapsed pilot pair is never reported as the phone's fault (#1810).
        return SCREEN_PILOT_LEVEL_COLLAPSE
    if screens.linearity_ok is False:
        return SCREEN_LINEARITY_FAILED
    if not has_summed_response:
        return SCREEN_LOCATE_FAILED
    return None


def lateral_pose_screens(screens: CaptureScreens) -> str | None:
    """One pose of the lateral walk, or a refusal kind.

    MEASURE's own capture-integrity gates, in MEASURE's order, because a pose
    replays MEASURE's program. Three MEASURE gates are deliberately NOT applied
    — the delay-search status, the GCC trust floor and the plausibility backstop
    — because all three judge the ALIGNMENT SOLVE, whose search window is a
    geometry prior about the MARK: a microphone 40 cm to the side legitimately
    fails it, and refusing there would keep only the poses that align like the
    anchor.

    A rejected pose does not re-arm MEASURE with a level backoff either: the
    pose must be measured at the ANCHOR'S level or its curve is not comparable.

    The walk's last rung is :func:`lateral_curves_sufficient`.
    """
    if not screens.stimulus_located:
        return SCREEN_LOCATE_FAILED
    if screens.pilot_snr_ok is False:
        return SCREEN_PILOT_LEVEL_COLLAPSE
    if not screens.sweep_locate_confidence_ok:
        return SCREEN_LOCATE_FAILED
    if screens.glitch_detected:
        return SCREEN_CAPTURE_GLITCH
    if not screens.sweep_schedule_ok:
        return SCREEN_CAPTURE_GLITCH
    if screens.any_sweep_clipped:
        return SCREEN_CLIPPED
    if screens.linearity_ok is False:
        return SCREEN_LINEARITY_FAILED
    return None


def lateral_curves_sufficient(n_curves: int) -> str | None:
    """The lateral walk's last rung: did this pose yield BOTH branches?

    Fewer than two curves cannot answer a woofer-versus-HF question. Reuses the
    locate kind because the household action is identical.

    A second call rather than an argument to :func:`lateral_pose_screens`
    because counting the curves means BUILDING them, and ``lateral_pose_curve``
    raises ``IndexError`` on a degenerate response with an empty frequency axis.
    Two calls keep the ladder's short-circuit ahead of the builder.
    """
    return SCREEN_LOCATE_FAILED if n_curves < 2 else None


@dataclass(frozen=True)
class EntryBaselineScreen:
    """The entry baseline's verdict: a refusal kind, or the reduced side.

    ``integrity_payload`` is set only on the capture-integrity arm, and is the
    fact the household screen needs beside the code — ``{"capture_integrity":
    ...}``, with an explicit ``None`` inside when the record was ABSENT rather
    than failed.  The other arms carry no payload because the code alone is the
    whole finding.
    """

    kind: str | None
    measured: MeasuredResponse | None = None
    integrity_payload: Mapping[str, Any] | None = None


def entry_baseline_screens(
    analysis: "ProgramAnalysis",
    *,
    stimulus_located: bool,
    reference_mark: str,
) -> EntryBaselineScreen:
    """The "before" capture: screen it, and reduce it when it passes.

    Reuses VERIFY's shipped gates — stimulus locate, ``pilot_snr_ok`` (ahead of
    everything but locate, so a room/level problem is never reported as
    something else, #1810), capture integrity through
    :func:`~.verification.evaluate_capture_validity`, and ``linearity_ok``. One
    deliberate difference: an ABSENT integrity record is UNUSABLE here where
    VERIFY treats it as no-evidence-and-continue, because a before-side nobody
    graded cannot carry a before→after claim.

    Three VERIFY gates are dropped: gate-comparability (it protects an overlay
    this capture never makes), the G3 pilot-transfer step (it protects a
    tracking comparison that does not exist here, and stage 2 may not inherit
    its reference, #1927), and the tracking-max comparison, structurally —
    :func:`~.priors.entry_baseline_priors` withholds ``predicted_sum``.

    One refusal is this phase's own: the reduction
    (:func:`~.round_evidence.measured_response_from_analysis`) must produce a
    side, and ``None`` reuses the locate kind.

    ``analysis`` arrives whole because two steps CONSUME it rather than test it;
    ``stimulus_located`` stays a separate argument because it is a flow-side
    predicate's answer, not an attribute of the analysis.
    """
    if not stimulus_located:
        return EntryBaselineScreen(SCREEN_LOCATE_FAILED)
    if analysis.pilot_snr_ok is False:
        return EntryBaselineScreen(SCREEN_PILOT_LEVEL_COLLAPSE)
    integrity = analysis.capture_integrity
    validity = evaluate_capture_validity(integrity)
    if validity.status is CaptureValidity.UNUSABLE:
        payload = (
            {"capture_integrity": integrity.to_dict()}
            if integrity is not None else {"capture_integrity": None}
        )
        # The same two-code split VERIFY's verdict makes: a sweep nobody could
        # hear is a level/mic problem, a spliced or clipped timeline is the
        # transient glitch class. An ABSENT record takes the glitch kind's
        # silent auto-retry.
        if integrity is not None and INTEGRITY_CHECK_SWEEP_HEARD in integrity.failed:
            return EntryBaselineScreen(SCREEN_LOCATE_FAILED, integrity_payload=payload)
        return EntryBaselineScreen(SCREEN_CAPTURE_GLITCH, integrity_payload=payload)
    if analysis.linearity_ok is False:
        return EntryBaselineScreen(SCREEN_LINEARITY_FAILED)
    measured = measured_response_from_analysis(
        analysis, reference_mark=reference_mark,
    )
    if measured is None:
        return EntryBaselineScreen(SCREEN_LOCATE_FAILED)
    return EntryBaselineScreen(None, measured=measured)


# --------------------------------------------------------------------------- #
# what a group will stand on, and when it asks for another take
# --------------------------------------------------------------------------- #


# The fewest RESOLVED positions a cloud group can close with and still produce a
# usable claim, so a position the flow gives up on degrades the group instead of
# ending the session.
#
# DERIVED, not chosen: ``linearization_envelope.position_stability_limit``
# raises ``ValueError`` for ``n_positions < 2``, because a cross-position spread
# across fewer than two positions is undefined.
#
# Deliberately NOT ``MIN_CLOUD_MEASURE_POSITIONS`` /
# ``MIN_CLOUD_VERIFY_POSITIONS``: those are PLAN-DECLARATION floors, enforced
# before any capture happens. Between this floor and the declared one the claim
# is degraded, and degradation is DISCLOSED (the geometry verdict's
# ``n_positions`` / ``thin_evidence``), not gated.
MIN_RESOLVED_CLOUD_POSITIONS = 2


def group_position_floor(phase: str) -> int:
    """How few resolved positions still lets a group stand.

    A cloud is an AVERAGE, so below :data:`MIN_RESOLVED_CLOUD_POSITIONS` there
    is nothing to combine. The lateral walk is not: its coefficients are the
    anchor's, so a pose nobody could capture costs a robustness sample and
    nothing else — floor ZERO, and the consumer discloses the shortfall.
    """
    return 0 if phase == PHASE_LATERAL else MIN_RESOLVED_CLOUD_POSITIONS


@dataclass(frozen=True)
class GeometryRetake:
    """A warranted geometry retake: which rung to show, and which take it drops.

    ``rung`` indexes the caller's prompt ladder; ``retries_after`` is the
    counter's new value. Both are computed here so the count spent and the
    sentence shown cannot drift apart.
    """

    rung: int
    retries_after: int


def geometry_retake(
    *,
    locked: bool | None,
    thin_evidence: bool | None,
    retries_used: int,
    budget: int,
    group_already_closed: bool,
    have_take_to_replace: bool,
) -> GeometryRetake | None:
    """Whether this group close asks the household to walk two more positions.

    Five conjuncts, none obvious:

    * ``locked`` — the combine could not separate the room's arrivals, the only
      condition a retake can improve.
    * ``thin_evidence`` is NOT set — it marks a verdict resting on the bare
      minimum of usable echo estimates, which the instrument already qualifies;
      a thin lock is disclosed and accepted rather than retried.
    * the budget is not spent.
    * ``group_already_closed`` is False. A voluntary retake re-enters the close
      with the group closed, and the retry branch DROPS the take at this index —
      on a voluntary retake the only copy, since the retention replaced the
      original in place.
    * ``have_take_to_replace``, which is why this returns an object rather than
      a bool: a group can close with its last position SETTLED without a curve,
      and rejecting nothing would re-open the slot whose tries just ran out.
    """
    warranted = (
        locked is True
        and thin_evidence is not True
        and retries_used < budget
        and not group_already_closed
    )
    if not (warranted and have_take_to_replace):
        return None
    return GeometryRetake(rung=retries_used, retries_after=retries_used + 1)


# --- lateral evidence ------------------------------------------------------- #
#
# One fixed log-spaced basis for every retained pose curve: fixed rather than
# per-role so both branches land on the SAME frequencies and a consumer can sum
# them without resampling; log-spaced because a crossover argument is a
# per-octave one. 1/12 octave is ~118 Hz at 2 kHz — a COARSE gate, never a polar
# measurement (#1968).
LATERAL_EVIDENCE_BAND_HZ = (20.0, 20_000.0)
LATERAL_EVIDENCE_POINTS_PER_OCTAVE = 12


@dataclass(frozen=True)
class LateralPoseCurve:
    """One driver's NEUTRAL response at one pose, on the shared log basis.

    ``complex_tf`` holds ``M = plant * P`` — polarity-free, with NO
    configured-crossover composition applied; ``S_c = sign_c * M * C_c / P`` is
    the consumer's step, once per candidate.

    Values are SAMPLED at the nearest native bin, never interpolated or
    averaged: a phase interpolated across a wrap is simply wrong. The
    frequencies actually sampled ride along. ``band_hz`` is the role's driven
    sweep band — outside it the samples are noise and a consumer must bound
    itself with this.

    ``repeat_curves`` holds this driver's other located occurrences, in
    occurrence order; a repeat's own ``repeat_curves`` is empty, mirroring
    ``DriverResponse.repeat_responses``.
    """

    role: str
    freqs_hz: np.ndarray
    complex_tf: np.ndarray
    band_hz: tuple[float, float]
    #: The gate's trusted floor for THIS occurrence, Hz. ``None`` is "no floor
    #: was resolved", never 0 Hz.
    validity_floor_hz: float | None = None
    repeat_curves: tuple["LateralPoseCurve", ...] = ()


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


def lateral_evidence_grid_hz() -> np.ndarray:
    """The shared log basis every retained pose curve is sampled onto."""
    lo, hi = LATERAL_EVIDENCE_BAND_HZ
    octaves = math.log2(hi / lo)
    return np.geomspace(
        lo, hi, num=int(round(octaves * LATERAL_EVIDENCE_POINTS_PER_OCTAVE)) + 1,
    )


def lateral_pose_curve(
    response: Any, band_hz: tuple[float, float],
) -> LateralPoseCurve:
    """Sample one analyzed driver response onto the shared basis."""
    freqs = np.asarray(response.freqs_hz, dtype=np.float64)
    tf = np.asarray(response.complex_tf, dtype=np.complex128)
    # ``searchsorted`` + a one-step comparison is the nearest native bin on a
    # monotonically increasing rfft grid, without materialising an N x M
    # distance matrix: the analysis grid is hundreds of thousands of bins.
    grid = lateral_evidence_grid_hz()
    right = np.searchsorted(freqs, grid).clip(1, freqs.size - 1)
    left = right - 1
    take = np.where(
        np.abs(grid - freqs[left]) <= np.abs(freqs[right] - grid), left, right
    )
    return LateralPoseCurve(
        role=str(response.role),
        freqs_hz=freqs[take],
        complex_tf=tf[take],
        band_hz=(float(band_hz[0]), float(band_hz[1])),
        validity_floor_hz=response.validity_floor_hz,
        repeat_curves=tuple(
            lateral_pose_curve(occurrence, band_hz)
            for occurrence in response.repeat_responses
        ),
    )


# --------------------------------------------------------------------------- #
# what a retained take records
# --------------------------------------------------------------------------- #


#: A pose whose stated displacement from the mark lies in the HORIZONTAL plane.
#: It names where the pose's STATED offset lies, not a promise that nothing else
#: moved: the second geometry-retake rung asks for a sideways move AND a rise,
#: and records that rise only in its ``prompt``.
POSITION_AXIS_HORIZONTAL = "horizontal"

#: A pose stated as a move ABOVE or BELOW mark height. Nothing rotates in
#: elevation, so such a pose commands no horizontal bearing
#: (:attr:`PositionGeometry.degrees` is ``None``), which is a different fact
#: from "0°". Where it was raised to is
#: :attr:`PositionGeometry.vertical_deg`.
POSITION_AXIS_VERTICAL = "vertical"

#: Every axis a pose can be stated on, so a reader can CHECK the value.
POSITION_AXES = (POSITION_AXIS_HORIZONTAL, POSITION_AXIS_VERTICAL)


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
    """

    axis: str
    degrees: int | None
    mark_distance_m: float
    vertical_deg: int = 0

    def __post_init__(self) -> None:
        if self.axis not in POSITION_AXES:
            raise ValueError(
                f"a pose axis must be one of {POSITION_AXES}, got {self.axis!r}"
            )
        # `bool` is an `int` and is never an elevation.
        if isinstance(self.vertical_deg, bool) or not isinstance(
            self.vertical_deg, int
        ):
            raise ValueError(
                "a pose elevation is a whole number of degrees above mark "
                f"height, got {self.vertical_deg!r}"
            )


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


#: The graph fingerprints that name no graph: ``""`` from a host that could not
#: name its graph, and :data:`~.contracts.ENTRY_GRAPH_FINGERPRINT_UNKNOWN` when
#: no applied profile was found. Neither can classify a take.
_UNNAMED_GRAPHS = frozenset({"", ENTRY_GRAPH_FINGERPRINT_UNKNOWN})

#: The phases whose captures are a re-measure AFTER an apply.  Used only to
#: separate ``verify`` from ``candidate`` — never to separate ``baseline`` from
#: either, which is the split :func:`take_kind` refuses to take from a phase.
_VERIFY_PHASES = frozenset({PHASE_VERIFY, PHASE_CLOUD_VERIFY})


def take_kind(
    *, graph_fingerprint: str, baseline_fingerprint: str, phase: str,
) -> str:
    """Which of :data:`~.contracts.MEASURE_KINDS` a take is, or ``""``.

    Derived from the GRAPH, never from the phase: a phase → kind map is not well
    defined, since a lateral walk is a ``baseline`` or a ``candidate`` check
    depending on what was applied under it (#3130). The rule: equal to the
    round's pre-apply fingerprint → ``baseline``; a post-apply re-measure phase
    → ``verify``; otherwise ``candidate``.

    ``graph_fingerprint`` is the applied profile's ``candidate_fingerprint``
    (:func:`~.coordinator.entry_graph_fingerprint`'s namespace), deliberately
    NOT the running-config hash. ``baseline_fingerprint`` is the same quantity
    for the round's "before", so the two are comparable. Either one unnamed
    returns ``""`` rather than a guess.
    """
    if graph_fingerprint in _UNNAMED_GRAPHS or baseline_fingerprint in _UNNAMED_GRAPHS:
        return ""
    if graph_fingerprint == baseline_fingerprint:
        return MEASURE_KIND_BASELINE
    if phase in _VERIFY_PHASES:
        return MEASURE_KIND_VERIFY
    return MEASURE_KIND_CANDIDATE


@dataclass(frozen=True)
class TakeClaim:
    """What the SESSION claimed around one take, on every record it banks.

    Carried at the builders so a flow-banked take and an engine-banked take are
    one record shape. Every field defaults empty because an unstated field is an
    honest fact about the capture, never a refusal to bank it.

    ``baseline_fingerprint`` is the round's pre-apply graph, the comparand
    :func:`take_kind` needs; the take's OWN graph is a separate builder keyword.

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

    baseline_fingerprint: str = ""
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

    Spelled ``measure_kind`` rather than the engine record's ``kind`` because
    :func:`take_kind` can honestly answer ``""``: ``kind`` is read by MEMBERSHIP
    in :data:`~.contracts.MEASURE_KINDS`, which ``""`` fails, while
    ``measure_kind`` is read by the key's PRESENCE, which carries an unresolved
    take through. :meth:`~.record_store.BankedRecordStore.bank` accepts either
    spelling and writes back this one.
    """
    return {
        "phase": phase,
        "index": index,
        "attempt": attempt,
        "take_id": take_id_for(position_id, attempt),
        "session_id": session_id,
        "wav_sha256": wav_sha256,
        "measure_kind": take_kind(
            graph_fingerprint=graph_fingerprint,
            baseline_fingerprint=claim.baseline_fingerprint,
            phase=phase,
        ),
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
    """One retained cloud position, as the record two consumers read.

    Built whether or not a retention seam is bound: the group close reads these
    records to serialize the per-position members, and that happens on every
    session including the ones that bind no seam. ``take_id`` is minted here so
    the session's evidence and the bundle's sidecar path name the same take.

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
    at the seat's own ``mark_distance_m``, which is why
    :func:`cloud_entanglement_floor_hz` pools the seats. ``unknown`` with a null
    floor is ordinary on a rig whose first bounce lands while the direct sound
    is still decaying.

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
        "mark_distance_m": geometry.mark_distance_m,
        "captured_at": captured_at,
        "gate_window_ms": gate_window_ms,
        "gate_floor_source": gate_floor_source,
        "gate_disclosure": gate_disclosure,
        "gate_moved_rms_db": gate_moved_rms_db,
        "gate_reflection_delay_ms": gate_reflection_delay_ms,
        "gate_entanglement_floor_hz": gate_entanglement_floor_hz,
        "gate_entanglement_floor_source": gate_entanglement_floor_source,
        "validity_floor_hz": validity_floor_hz,
        "gating_applied": gating_applied,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
        "curves": [dict(curve) for curve in curves],
    }


#: What every :data:`~.journey.PHASE_LATERAL` pose plays: the anchor's
#: interleaved per-driver MEASURE object. A literal copy of
#: :data:`jasper.active_speaker.angle_capture.REGIME_PER_DRIVER` because
#: importing it would close a cycle; pinned equal by test.
LATERAL_POSE_REGIME = "per_driver"

#: Deep-null floor applied before the log, so a bin that cancelled to exactly
#: zero banks a number instead of ``-inf``, which is not JSON. The same 1e-12
#: :func:`~jasper.audio_measurement.deconv.magnitude_response` applies.
_POSE_MAGNITUDE_FLOOR = 1e-12


def pose_curve_record(curve: LateralPoseCurve) -> dict[str, Any]:
    """One measured curve, banked as magnitude AND phase.

    The ONE serializer ``complex_tf`` has. The pair reconstructs the transfer
    function exactly: ``10 ** (magnitude_db / 20) * exp(1j * radians(phase_deg))``.

    ``phase_deg`` is WRAPPED to (-180, 180], the value :func:`numpy.angle`
    produces — unwrapping is a derived view with a branch choice in it, left to
    the consumer.

    Absolute phase carries the microphone's own uncorrected response, since mic
    calibration here is magnitude-only: common-mode across the roles of one
    capture, so self-cancelling for relative cross-driver work. Not a claim
    about the driver's absolute phase.

    ``repeat_curves`` carries each sibling occurrence in this same shape, so a
    reader has one thing to parse at either level. Its ``validity_floor_hz`` is
    that OCCURRENCE's own gate floor, which is a narrower quantity than the
    take-level field of the same name (:func:`lateral_pose_record`, one
    response for the whole take).
    """
    tf = np.asarray(curve.complex_tf, dtype=np.complex128)
    magnitude = np.maximum(np.abs(tf), _POSE_MAGNITUDE_FLOOR)
    return {
        "role": curve.role,
        "band_hz": [float(curve.band_hz[0]), float(curve.band_hz[1])],
        "freqs_hz": [float(hz) for hz in curve.freqs_hz],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(magnitude)],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
        # Ruling S3 one field further (ADR-0228 entry 2): the
        # linearization envelope reads the conservative floor ACROSS
        # occurrences and its sigma term reads the repeats, so a round banked
        # without these two cannot be re-fitted offline. Additive: a round
        # banked before this carries neither key.
        "validity_floor_hz": curve.validity_floor_hz,
        "repeat_curves": [
            pose_curve_record(repeat) for repeat in curve.repeat_curves
        ],
    }


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
    position_deg: int,
    vertical_deg: int = 0,
    lateral_consumer: str,
    session_id: str,
    graph_fingerprint: str,
    captured_at: str,
    wav_sha256: str | None,
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """One retained lateral pose, as the evidence bundle's sidecar carries it.

    ``position_deg`` is the SIGNED whole-degree bearing (negative LEFT of the
    design axis), derived by ``capture_plan.position_angle_deg`` and stated
    rather than re-derived. ``lateral_consumer`` is one of
    :data:`~.journey.LATERAL_CONSUMERS`.

    ``graph_fingerprint`` is WHICH CANDIDATE WAS APPLIED while this pose was
    taken, in :func:`~.coordinator.entry_graph_fingerprint`'s namespace —
    deliberately NOT the running-config hash, because a pose plays through the
    transient routing graph that omits crossover, delay and linearization, so
    that hash is the same before and after an apply and cannot tell two walks
    apart. :func:`take_kind` is the classification that buys, stamped here.

    ``position_axis`` is horizontal by construction, even for a RAISED pose:
    :data:`POSITION_AXIS_VERTICAL` is the pose commanding no horizontal bearing,
    and every pose reaching this builder commands one. ``vertical_deg`` is the
    signed elevation above mark height against the same :data:`MARK_DISTANCE_M`;
    0 is true of a pose nobody raised. ``captured_at`` is minted at retention
    because a :class:`LateralPose` holds no clock.

    Refuses nothing. ``curves`` is empty only for a directly constructed pose —
    :func:`lateral_curves_sufficient` rejects a thin capture before any record
    is built. A curve inside ``curves`` carries its OWN ``validity_floor_hz``,
    per role and per occurrence; a take-level field of that name is one
    response's floor for the whole take, so the two must not be flattened
    together.

    Separate from :func:`cloud_position_record` rather than a widened one: a
    cloud position is a summed sweep judged by gating and ripple, and those
    columns are never meaningful for a pose.
    """
    return {
        "pose_id": pose.pose_id,
        **_take_identity(
            position_id=pose.pose_id, phase=PHASE_LATERAL, index=pose.index,
            attempt=pose.attempt, session_id=session_id, wav_sha256=wav_sha256,
            graph_fingerprint=graph_fingerprint, claim=claim,
        ),
        "prompt": pose.prompt,
        "role": pose.role,
        "position_deg": int(position_deg),
        "position_axis": POSITION_AXIS_HORIZONTAL,
        "vertical_deg": int(vertical_deg),
        "offset_cm": float(pose.offset_cm),
        "at_mark": bool(pose.at_mark),
        "regime": LATERAL_POSE_REGIME,
        "lateral_consumer": lateral_consumer,
        "captured_at": captured_at,
        "curves": [pose_curve_record(curve) for curve in pose.curves],
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

    Structurally a cloud-position record, handed to the same retention seam so
    it lands in ``refs["position_artifacts"]`` beside every other retained take.
    It is NOT a group member, which is why the retention call is explicit at its
    call site.

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


# --------------------------------------------------------------------------- #
# the blind span below the null registry's floor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoostExclusion:
    """:func:`boost_excluded_bands_hz`'s answer, plus the line it justifies.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_boost_evidence``, as data rather than a log
    call because this module is side-effect-free.
    """

    bands: tuple[tuple[float, float], ...]
    diagnostics: dict[str, Any]


def boost_excluded_bands_hz(
    combined: Any,
    result: Mapping[str, Any],
    *,
    echo_band_hz: Sequence[float],
) -> BoostExclusion:
    """Bands BELOW the null registry's own floor where this cloud's positions
    disagree about a dip — so boosting one corrects nothing any listener hears
    (#1967).

    The registry's analysis band is floored at 4 kHz, so below that edge it
    contributes no exclusions — not because it was uncertain but because it was
    never asked — while a round's largest prescribed boost can sit under that
    floor. This runs
    :func:`~jasper.audio_measurement.interference_nulls.classify_dip_position_variance`
    over the blind span and hands the dips the positions DISAGREE about to the
    fit vocabulary, which refuses a lift whose realized cascade would put
    significant gain in one.

    It cannot GRANT boost anywhere: the bound is monotone by construction, and a
    ``position_invariant`` dip is left exactly as the gate had it, because
    position-invariance says a dip is real and not that it is correctable. The
    residual is named: such a dip may still be a source-fixed interference null
    rather than a driver deficit, and separating those needs the post-apply arm
    (#1868).

    Each band costs a real correction — the fit drops any boost filter whose
    action region overlaps one, per filter, with a whole-lift refusal
    (``lift_suppressed_reason="boost_excluded_band"``) only when every boost was
    aimed — which is why this returns only the positively-contradicted class and
    never a "we were unsure here" list.

    Fails OPEN, disclosed: a span too narrow to analyse, a cloud with no
    per-position curves, or a numeric failure all yield no exclusions.
    ``variance_check_failed`` is reported in
    :attr:`BoostExclusion.diagnostics` so the flow can raise the journal line's
    level.
    """
    from jasper.audio_measurement.interference_nulls import (
        CLASSIFICATION_POSITION_DEPENDENT,
        classify_dip_position_variance,
    )

    registry = result.get("null_registry") or {}
    n_dependent = 0
    floor_hz = float(echo_band_hz[0])
    grid = np.asarray(getattr(combined, "freqs_hz", ()), dtype=float)
    # The cloud's gated validity floor is the honest lower edge: below it every
    # position's curve is a truncated-window artifact.
    validity_floor_hz = result.get("validity_floor_hz")
    lo_hz = float(validity_floor_hz) if validity_floor_hz else 0.0
    if grid.size:
        lo_hz = max(lo_hz, float(grid[0]))
    span = (lo_hz, floor_hz)
    bands: tuple[tuple[float, float], ...] = ()
    reason = ""
    n_dips = 0
    variance_check_failed = False
    if not (0.0 < lo_hz < floor_hz) or int(
        np.count_nonzero((grid >= lo_hz) & (grid <= floor_hz))
    ) < 3:
        reason = "no_blind_span"
    else:
        try:
            report = classify_dip_position_variance(combined, band_hz=span)
        except Exception:  # noqa: BLE001 - see "Fails OPEN" above.
            reason = "variance_check_failed"
            variance_check_failed = True
        else:
            reason = report.reason
            n_dips = len(report.dips)
            n_dependent = sum(
                dip.classification == CLASSIFICATION_POSITION_DEPENDENT
                for dip in report.dips
            )
            bands = report.position_dependent_bands_hz
    return BoostExclusion(
        bands=bands,
        diagnostics={
            # The band the registry adjudicated, and the span below it where
            # it structurally could not.
            "registry_band_hz": [round(v, 3) for v in echo_band_hz],
            "registry_classification": str(registry.get("classification") or ""),
            "registry_reason": str(registry.get("reason") or ""),
            "unadjudicated_span_hz": [round(v, 3) for v in span],
            "variance_reason": reason,
            "n_dips": n_dips,
            # How many of those dips the cloud's positions DISAGREED about — the
            # only class this bound acts on. ``n_dips - n_position_dependent`` is
            # the invariant remainder, which keeps its boost and is exactly the
            # residual #1868 has to close.
            "n_position_dependent": n_dependent,
            "boost_excluded_bands_hz": [
                [round(lo, 3), round(hi, 3)] for lo, hi in bands
            ],
            "variance_check_failed": variance_check_failed,
        },
    )


# --------------------------------------------------------------------------- #
# the cloud group: one position, and the capture it becomes
# --------------------------------------------------------------------------- #

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
# the BEARINGS a positioner can act on.
MARK_DISTANCE_M = 1.0

#: The pose a capture with no prompted move of its own was taken at.
_DESIGN_AXIS_GEOMETRY = PositionGeometry(
    axis=POSITION_AXIS_HORIZONTAL,
    degrees=0,
    mark_distance_m=MARK_DISTANCE_M,
)


@dataclass(frozen=True)
class _CloudPosition:
    """One accepted position inside a group, retained for the group-end combine.

    ``response`` is the capture's ``ProgramAnalysis.summed_response``, carrying
    the calibrated, reflection-gated magnitude on a linear (rfftfreq) grid plus
    the matching complex TF. Held as the response rather than a pre-built
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture` because
    the per-position work the null gate and the spec curve do needs the same
    object, and re-deriving it from a lossy intermediate would drift.
    """

    position_id: str
    index: int
    attempt: int
    prompt: str
    wide: bool
    captured_at: float
    response: Any
    sample_rate_hz: int
    # The named question this position answers (:data:`POSITION_ROLES`), copied
    # off the prompt the operator was given. Defaulted so construction sites
    # that predate roles stay valid.
    role: str = POSITION_ROLE_ONAX
    # WHERE the microphone was, carried off the SAME prompt ``role`` and
    # ``wide`` come from. Held on the position rather than re-derived at
    # retention: a geometry retake shows a different prompt than the table's,
    # and a derivation from the index would state the spot the operator was
    # told to abandon. Defaulted to the design axis, which is honest for a
    # fixture: a position built without a pose is one nobody moved.
    geometry: PositionGeometry = _DESIGN_AXIS_GEOMETRY
    # The contract-derived analysis bands this position's GROUP is
    # combined/searched with — ``spatial_combine.combine_positions``' own
    # kwargs. Every position in one group shares the same session-derived
    # values, so carrying them here lets :func:`combine_cloud_positions` derive
    # the bands from ``positions`` alone and no two call sites can drift.
    # ``None`` means the module defaults.
    echo_band_hz: tuple[float, float] | None = None
    signal_band_hz: tuple[float, float] | None = None


def cloud_position_capture(position: _CloudPosition) -> Any:
    """One retained position → a :class:`spatial_combine.PositionCapture`.

    Regime of the ``ir`` field, stated exactly because ``detect_echo``'s answer
    depends on it: it is the inverse rFFT of the response's GATED, CALIBRATED
    complex transfer function — the impulse response after
    ``deconv.direct_arrival_window`` and the adaptive reflection gate, not the
    raw deconvolved IR. The direct arrival is present and early secondary
    arrivals inside the gate survive; late room reflections beyond the gate are
    gone by construction. ``tests/test_crossover_v2_cloud_geometry_corpus.py``
    measures that this agrees with the ungated IR on the S0 corpus's verdict.
    """
    from jasper.audio_measurement.spatial_combine import PositionCapture

    response = position.response
    freqs = np.asarray(response.freqs_hz, dtype=float)
    magnitude = np.asarray(response.magnitude_db, dtype=float)
    complex_tf = np.asarray(response.complex_tf)
    # ``program_analysis._n_fft_for`` always returns a power of two (>= 8192),
    # so the analysis grid is an even-length rfft and ``n = 2*(bins-1)`` inverts
    # it exactly.
    ir = np.fft.irfft(complex_tf, n=2 * (complex_tf.size - 1))
    return PositionCapture(
        position_id=position.position_id,
        freqs_hz=freqs,
        magnitude_db=magnitude,
        sample_rate=int(position.sample_rate_hz),
        ir=ir,
        # Carrying the role changes no combination (the reduction stays
        # unweighted) and is what lets the per-position residual say "on-axis"
        # rather than "position 3".
        role=str(position.role or ""),
    )


def _geometry_verdict_from_combined(
    combined: Any, n_positions: int,
) -> dict[str, Any]:
    """The geometry-verdict dict from an ALREADY-COMBINED result.

    Separate from :func:`cloud_geometry_verdict` so a caller can combine a
    group exactly ONCE and derive both the retry gate and the pipeline from that
    one object. A plain JSON-native dict, because the host persists it verbatim.
    ``locked`` is ``False`` on every degraded path, and ``reason`` says which.
    """
    if combined is None:
        return {
            "locked": False,
            "reason": "combine_failed",
            "n_positions": n_positions,
        }
    geometry = combined.geometry
    return {
        "locked": bool(geometry.locked),
        "reason": str(geometry.reason),
        "n_confident": int(geometry.n_confident),
        "n_positions": int(geometry.n_positions),
        "median_tau_us": float(geometry.median_tau_us),
        "clustered_fraction": float(geometry.clustered_fraction),
        "thin_evidence": bool(geometry.thin_evidence),
    }


@dataclass(frozen=True)
class CloudCombine:
    """:func:`combine_cloud_positions`'s answer, plus the line a failure earns.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_cloud_combine_failed``, ``None`` when there
    was nothing to say, as data rather than a log call because this module is
    side-effect-free.
    """

    combined: Any | None
    diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True)
class CloudVerdict:
    """:func:`cloud_geometry_verdict`'s answer, carrying the same line.

    ``verdict`` is the plain JSON-native dict the host persists verbatim into
    the durable v2 state; ``diagnostics`` is whatever the combine underneath
    it would have journalled.
    """

    verdict: dict[str, Any]
    diagnostics: dict[str, Any] | None = None


def combine_cloud_positions(positions: Sequence[_CloudPosition]) -> CloudCombine:
    """Assemble a closed group and combine it.

    ``CloudCombine.combined`` is a
    :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`, or
    ``None`` when the group cannot be combined. Call it exactly ONCE per
    group-close event and derive both the geometry verdict and the pipeline from
    that one object: the combine is 3-6 s across runs and hosts on the S0
    ten-position corpus (interpreter-bound ``smooth_fractional_octave``, worse
    on a Pi 5), and :data:`GEOMETRY_RETRY_POSITIONS` allows up to three close
    attempts per group.

    Never raises: a group's captures are already-accepted evidence, so an
    unusable cloud is a ``None`` the caller turns into an honest "unknown"
    rather than an exception that would strand the session.
    """
    from jasper.audio_measurement.spatial_combine import (
        DEFAULT_ECHO_BAND_HZ,
        combine_positions,
    )

    if not positions:
        return CloudCombine(None)
    # Every position in one group carries the SAME session-derived bands, so
    # reading them off the first is reading the group's own. ``None`` (a caller
    # that declared no driver contract) falls back to the module default.
    echo_band_hz = positions[0].echo_band_hz or DEFAULT_ECHO_BAND_HZ
    signal_band_hz = positions[0].signal_band_hz
    try:
        return CloudCombine(combine_positions(
            [cloud_position_capture(p) for p in positions],
            echo_band_hz=echo_band_hz,
            signal_band_hz=signal_band_hz,
        ))
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        return CloudCombine(
            None, {"positions": len(positions), "error": str(exc)},
        )


def cloud_geometry_verdict(positions: Sequence[_CloudPosition]) -> CloudVerdict:
    """Combine, then read ``.geometry``.

    A convenience wrapper for callers that only have ``positions``; the session
    does NOT call this, because it combines once and derives both answers.

    Reason-string divergence, disclosed: an empty ``positions`` short-circuits
    here with ``reason="no_positions"``, while
    :func:`_geometry_verdict_from_combined` called directly with
    ``combined=None`` and ``n_positions=0`` reports ``combine_failed`` for the
    same fact.
    """
    if not positions:
        return CloudVerdict(
            {"locked": False, "reason": "no_positions", "n_positions": 0}
        )
    result = combine_cloud_positions(positions)
    return CloudVerdict(
        _geometry_verdict_from_combined(result.combined, len(positions)),
        result.diagnostics,
    )


# --------------------------------------------------------------------------- #
# THE GROUP CLOSE — what a closed cloud is worth, once
# --------------------------------------------------------------------------- #
#
# Everything above answers "is this ONE take evidence". This section answers
# what the group close asks next: given every retained position, what did the
# cloud measure, what did the honesty instruments carve out of it, and what is
# the resulting spec verdict.
#
# The "no household vocabulary" rule is about REFUSALS and is intact. What this
# section carries is DISCLOSURE copy on a group result — sentences about what an
# instrument carved out, which have no refusal code to route through.
#
# The echo/detector band and ``signal_band_hz`` derive from the declared
# contract: the summed system's swept band for the passband, the tweeter's
# measurement band for the upper echo band.

# Cloud curves decimated for persistence. Mirrors
# :data:`~.durable_state.MAX_PERSISTED_SUM_POINTS` as an independent constant,
# so the two may diverge.
CLOUD_CURVE_MAX_JSON_POINTS = 512


@dataclass(frozen=True)
class _CloudEchoBand:
    """The echo/null analysis band the pipeline will APPLY, plus how it was
    derived — one value, so band and provenance cannot be carried apart.

    ``band_hz`` is what the detector runs on. ``derived_lo_hz`` is the lower
    edge the declared contract produced BEFORE the HF-regime clamp, so a reader
    can tell a contract-derived band from a clamped one without the journal
    (#1763). ``source`` names which derivation path produced the band:

    * ``declared`` — the tweeter's ``measurement_band_hz``, possibly narrowed by
      the passband containment clamp or raised by the HF-regime clamp
      (``hf_regime_clamped`` tells which).
    * ``undeclared_default`` — no measurement band was threaded through, so
      ``DEFAULT_ECHO_BAND_HZ`` stands in.
    * ``clamp_degenerate_default`` — the HF clamp would have left a band too
      narrow to resolve anything in (:func:`_min_clamped_echo_band_width_hz`).
    * ``passband_fallback`` — the declared band sits entirely outside the
      composed passband, so the passband stands in.

    ``diagnostics`` carries the journal fields the flow emits, ``None`` when
    there is nothing to say, as data because this module is side-effect-free.
    """

    band_hz: tuple[float, float]
    source: str
    hf_regime_clamped: bool
    derived_lo_hz: float
    diagnostics: dict[str, Any] | None = None

    def disclosure(self) -> dict[str, Any]:
        """The JSON-native provenance block the pipeline payload carries.

        Deliberately does NOT repeat ``band_hz``: the payload already publishes
        the applied band as ``echo_band_hz``.
        """
        return {
            "source": self.source,
            "hf_regime_clamped": self.hf_regime_clamped,
            "derived_lo_hz": float(self.derived_lo_hz),
            "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
        }


def _min_clamped_echo_band_width_hz() -> float:
    """The narrowest band the HF-regime clamp may hand the detector, derived
    from the DETECTOR's own constants rather than picked.

    ``detect_echo``'s quefrency step is ``resolution_us = 1e6 / bandwidth``, and
    ``assess_geometry`` refuses to cluster any estimate whose ``tau_us`` is
    below ``GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``. Once that floor
    reaches the TOP of the searched window no delay can be clustered at all:

        GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / DEFAULT_ECHO_SEARCH_US[1]
          = 3.0 * 1e6 / 800 us = 3750 Hz

    The searched window's own edge margin bounds at ~1470 Hz, i.e. slacker, and
    ``MIN_ECHO_BAND_BINS`` needs only ~176 Hz at 48 kHz, so this floor is the
    binding one and clearing it clears both.
    """
    from jasper.audio_measurement.spatial_combine import (
        DEFAULT_ECHO_SEARCH_US,
        GEOMETRY_MIN_RESOLUTION_STEPS,
    )

    return GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / float(DEFAULT_ECHO_SEARCH_US[1])


def _derive_cloud_echo_band_hz(
    signal_band_hz: tuple[float, float],
    tweeter_measurement_band_hz: tuple[float, float] | None,
) -> _CloudEchoBand:
    """The contract-derived echo/null analysis band: the tweeter's declared
    ``measurement_band_hz``, returned WITH its provenance
    (:class:`_CloudEchoBand`).

    Falls back to ``DEFAULT_ECHO_BAND_HZ`` when no tweeter measurement band was
    threaded through — the band every corpus test validated
    ``identify_interference_nulls`` at.

    Containment: clamped to sit INSIDE ``signal_band_hz``, never wider. A band
    that neither contains nor sits clear of the analysis band leaves
    ``detect_echo``'s signal-presence screen uncalibrated
    (``spatial_combine.BAND_BELOW_PASSBAND_MARGIN_DB``). Since
    ``signal_band_hz`` is the union of both roles' excitation bands, the clamp
    is a no-op for any well-formed 2-way contract.

    HF regime (#1763): a contained lower edge below
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ` is RAISED to that floor and the clamp
    disclosed, in the provenance and in the WARNING event the flow emits from
    ``diagnostics``. The contract's upper edge is kept: the floor says where the
    detector's calibrations hold, not how wide the driver's window is.

    When the clamp cannot produce a usable band — surviving width below
    :func:`_min_clamped_echo_band_width_hz` — the band falls back to
    ``DEFAULT_ECHO_BAND_HZ`` with its own disclosure. That fallback is NOT
    re-clamped into the passband, so it can leave the signal-presence screen
    uncalibrated; that is the lesser loss against a band too narrow to resolve
    any delay, and it needs ``min(declared_upper, passband_upper)`` below
    7750 Hz to reach at all.
    """
    from jasper.audio_measurement.spatial_combine import DEFAULT_ECHO_BAND_HZ

    declared = tweeter_measurement_band_hz is not None
    band = tweeter_measurement_band_hz or DEFAULT_ECHO_BAND_HZ
    lo = max(float(band[0]), float(signal_band_hz[0]))
    hi = min(float(band[1]), float(signal_band_hz[1]))
    if lo >= hi:
        # A malformed declared contract: the tweeter's measurement band sits
        # entirely outside the composed passband. Fall back to the passband
        # rather than hand back an inverted pair that would raise deep inside
        # combine_positions with no context.
        return _CloudEchoBand(
            band_hz=(float(signal_band_hz[0]), float(signal_band_hz[1])),
            source="passband_fallback",
            hf_regime_clamped=False,
            derived_lo_hz=lo,
            diagnostics={
                "declared_measurement_band_hz": list(band),
                "signal_band_hz": list(signal_band_hz),
            },
        )
    if lo < ECHO_BAND_HF_REGIME_FLOOR_HZ:
        min_width_hz = _min_clamped_echo_band_width_hz()
        if hi - ECHO_BAND_HF_REGIME_FLOOR_HZ < min_width_hz:
            return _CloudEchoBand(
                band_hz=(float(DEFAULT_ECHO_BAND_HZ[0]), float(DEFAULT_ECHO_BAND_HZ[1])),
                source="clamp_degenerate_default",
                hf_regime_clamped=False,
                derived_lo_hz=lo,
                diagnostics={
                    "derived_lo_hz": lo, "upper_hz": hi,
                    "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
                    "min_width_hz": min_width_hz,
                    "fallback_band_hz": list(DEFAULT_ECHO_BAND_HZ),
                },
            )
        # ``clamped_lo_hz`` equals ``floor_hz`` by construction; both are
        # logged so a journal reader need not know that.
        return _CloudEchoBand(
            band_hz=(ECHO_BAND_HF_REGIME_FLOOR_HZ, hi),
            source="declared" if declared else "undeclared_default",
            hf_regime_clamped=True,
            derived_lo_hz=lo,
            diagnostics={
                "derived_lo_hz": lo,
                "clamped_lo_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ,
                "floor_hz": ECHO_BAND_HF_REGIME_FLOOR_HZ, "upper_hz": hi,
            },
        )
    return _CloudEchoBand(
        band_hz=(lo, hi),
        source="declared" if declared else "undeclared_default",
        hf_regime_clamped=False,
        derived_lo_hz=lo,
    )


def _decimate_curve_for_json(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray,
) -> dict[str, list[float]]:
    """Stride-decimate one combined curve to at most
    :data:`CLOUD_CURVE_MAX_JSON_POINTS`, for disclosure only.

    A plain stride is safe here, unlike in ``durable_state._decimate_sum``,
    because this input (``combined.power_mean_spec_db``) has already been
    through ``smooth_fractional_octave`` inside :func:`combine_positions`; a
    stride over a raw unsmoothed prediction aliases below ~500 Hz (#1858).
    """
    n = len(freqs_hz)
    step = max(1, n // CLOUD_CURVE_MAX_JSON_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs_hz[::step]],
        "magnitude_db": [float(m) for m in magnitude_db[::step]],
    }


def _geometry_guidance_copy(geometry: Mapping[str, Any]) -> str:
    """Plain-language "spread the mic further" guidance from a geometry verdict
    dict (:func:`cloud_geometry_verdict`'s shape).

    Softened, never suppressed, when ``thin_evidence``, and the softened copy
    names the qualitative floor rather than a count: ``thin_evidence`` is a
    cliff at an exact confident-estimate count, so naming it would read as a
    gradient the instrument does not claim. ``""`` when not locked.
    """
    if not geometry.get("locked"):
        return ""
    if geometry.get("thin_evidence"):
        return (
            "The measured echo pattern looks the same at every microphone "
            "position, but only the bare minimum of positions gave a "
            "confident enough reading to tell. Spreading the microphone "
            "further apart next time would make this more certain."
        )
    return (
        "The measured echo pattern did not change between microphone "
        "positions. Spreading the microphone further apart next time may "
        "help JTS tell the speaker's own sound apart from the room's."
    )


# --------------------------------------------------------------------------- #
# Carve-out disclosure
#
# Identified interference nulls are excluded from spec evaluation AND from
# correction, the band's tolerance applies to the SURVIVING envelope, and the
# report discloses "EQ cannot fill these" with the numbers.
# ``evaluate_flat_spec`` does the excluding but must not say WHY — it is a pure
# evaluator holding no product policy — so the "why" is assembled here. This
# module is the one owner of the carve-out copy strings, so a chart callout and
# the envelope's expert disclosure cannot disagree about one carved range.
# --------------------------------------------------------------------------- #

# Which honesty instrument carved a range.
CARVE_OUT_SOURCE_IDENTIFIED_NULL = "identified_null"
CARVE_OUT_SOURCE_POSITION_SCREEN = "position_screen"


def _format_carve_out_hz(hz: float) -> str:
    """One frequency as household copy — kHz at and above 1 kHz, Hz below."""
    return f"{hz / 1000.0:.1f} kHz" if hz >= 1000.0 else f"{hz:.0f} Hz"


def _join_carve_out_phrases(parts: Sequence[str]) -> str:
    """``["a", "b", "c"]`` -> ``"a, b and c"``. No serial comma, matching the
    house copy elsewhere in this flow."""
    parts = tuple(parts)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _null_classification_copy(classification: str) -> str:
    """The classification's own household sentence, or ``""`` for one this copy
    does not cover.

    The ``position_invariant`` wording is load-bearing: a single session cannot
    separate "travels with the speaker" from "a path in the room that did not
    change while measuring", so the copy names both and names the experiment
    that would tell them apart.

    No hardware noun appears in either branch. The classification is evidence
    about how a null behaved across a mic cloud, not about what produced it.
    """
    from jasper.audio_measurement.interference_nulls import (
        CLASSIFICATION_POSITION_DEPENDENT,
        CLASSIFICATION_POSITION_INVARIANT,
    )

    if classification == CLASSIFICATION_POSITION_INVARIANT:
        return (
            " It sat at the same frequencies at every microphone position — "
            "consistent with something that travels with the speaker, or with "
            "a path that did not change while measuring; moving the speaker "
            "and measuring again would tell those apart."
        )
    if classification == CLASSIFICATION_POSITION_DEPENDENT:
        return (
            " It appeared at some microphone positions and not others, so "
            "whatever causes it does not travel with the speaker."
        )
    return ""


def _carve_out_records(
    null_report: Any, screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Every carved range, tagged with the instrument that carved it.

    The two instruments are listed SEPARATELY: only the registry's rows carry
    τ/r, and overlapping ranges are two rows rather than one, because "both
    instruments flagged this" is a stronger statement than either alone.
    ``merged_excluded_bands_hz`` remains the merged view.

    A registry row's interval is the null's own ``f_lo_hz``/``f_hi_hz``,
    unclipped to any spec band, because τ and r describe the whole null.

    Ordered by lower edge then source, so rows starting at the same frequency
    come out stable rather than in input order.
    """
    records: list[dict[str, Any]] = []
    for null in null_report.nulls:
        records.append(
            {
                "f_lo_hz": float(null.f_lo_hz),
                "f_hi_hz": float(null.f_hi_hz),
                "source": CARVE_OUT_SOURCE_IDENTIFIED_NULL,
                "f_center_hz": float(null.f_center_hz),
                "n": int(null.n),
                "tau_us": float(null.tau_us),
                "r_time": float(null.r_time),
                "r_freq": float(null.r_freq),
                "depth_db": float(null.depth_db),
                "classification": str(null.classification),
                "reason": (
                    "A delayed copy of the sound cancels this range, and EQ "
                    "cannot fill a cancellation, so it is left out of "
                    "correction and out of grading."
                    + _null_classification_copy(str(null.classification))
                ),
            }
        )
    for band in screen_bands_hz:
        records.append(
            {
                "f_lo_hz": float(band[0]),
                "f_hi_hz": float(band[1]),
                "source": CARVE_OUT_SOURCE_POSITION_SCREEN,
                "reason": (
                    "The microphone positions disagreed about this range much "
                    "more than about the rest of the spectrum, so it reads as "
                    "interference rather than the speaker's own response and "
                    "is left out of correction and out of grading."
                ),
            }
        )
    records.sort(key=lambda record: (record["f_lo_hz"], record["source"]))
    return records


def _carve_out_disclosure_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The band's household-facing headline — plain language, no τ/r.

    ``""`` when nothing was carved, rather than a "no interference found"
    sentence a reader could mistake for a measurement. The delay is quoted in
    MILLISECONDS here; τ stays in microseconds in the structured record, which
    is the registry's own unit.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    screened = [r for r in records if r["source"] == CARVE_OUT_SOURCE_POSITION_SCREEN]
    sentences: list[str] = []
    if nulls:
        where = _join_carve_out_phrases(
            [_format_carve_out_hz(float(r["f_center_hz"])) for r in nulls]
        )
        # One ladder, one τ: ``IdentifiedNull.tau_us`` is the same value on
        # every rung of one report, so the first row's delay describes them all.
        delay_ms = float(nulls[0]["tau_us"]) / 1000.0
        plural = len(nulls) > 1
        sentences.append(
            f"{'Interference nulls at' if plural else 'An interference null at'} "
            f"{where} — a delayed copy of the sound arrives {delay_ms:.2f} ms "
            f"later. EQ cannot fill {'these' if plural else 'this'}, so "
            f"{'they are' if plural else 'it is'} left out of correction and "
            "out of this band's grading."
        )
    if screened:
        plural = len(screened) > 1
        # "One range" rather than "1 range": the frequency figures are the
        # numerals a reader should be counting in this sentence.
        count = f"{len(screened)}" if plural else "One"
        subject = f"{count} {'further ' if nulls else ''}"
        subject += "ranges are" if plural else "range is"
        tail = (
            "left out because the microphone positions disagreed about "
            if nulls
            else (
                "left out of correction and out of this band's grading "
                "because the microphone positions disagreed about "
            )
        )
        sentences.append(
            f"{subject} {tail}{'them' if plural else 'it'} too much to grade."
        )
    return " ".join(sentences)


def _carve_out_expert_copy(records: Sequence[Mapping[str, Any]]) -> str:
    """The expert-layer line — the same carve-outs WITH τ and r.

    Separate from :func:`_carve_out_disclosure_copy` because τ/r belong behind
    a disclosure rather than in the headline. ``r`` is reported as the pair the
    registry holds — time-domain and frequency-domain — rather than an average,
    because their AGREEMENT is what admitted the null.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    if not nulls:
        return ""
    where = _join_carve_out_phrases(
        [
            f"{_format_carve_out_hz(float(r['f_center_hz']))} (rung {int(r['n'])}, "
            f"{float(r['depth_db']):.1f} dB deep)"
            for r in nulls
        ]
    )
    first = nulls[0]
    return (
        f"carved out of grading: {where}; delay τ {float(first['tau_us']):.0f} µs, "
        f"reflection ratio r {float(first['r_time']):.3f} measured in time / "
        f"{float(first['r_freq']):.3f} implied by null depth"
    )


def carve_outs_by_band(
    spec_report: Any,
    null_report: Any,
    screen_bands_hz: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Per spec band: which ranges were carved out, why, and with what numbers.

    One entry per band of ``spec_report``, always all of them in the report's
    own order, so a consumer joins by index or ``band_hz`` and can render
    "nothing carved here" without inferring it from an absence.

    A record is included when its interval OVERLAPS the band's
    ``[graded_lo_hz, graded_hi_hz)`` span — the span actually graded, not the
    nominal row — so a null straddling an edge appears under both bands it
    carves and one outside the trusted range appears under none.

    Deliberately EXCLUDES the gate's trusted-floor clamp: that clamp moves each
    band's graded EDGE, so a sub-floor bin is not in the band to be excluded
    from. A band's ``n_excluded`` is therefore exactly what these records cover
    (#2551).
    """
    records = _carve_out_records(null_report, screen_bands_hz)
    out: list[dict[str, Any]] = []
    for band in spec_report.bands:
        f_lo, f_hi = float(band.f_lo_hz), float(band.f_hi_hz)
        # Overlap is tested against the GRADED edges, not the nominal row: a
        # null outside the trusted range carved nothing out of this band. The
        # upper edge matters as much as the lower, since the top band's follows
        # the microphone-trust ceiling. ``band_hz`` below stays the nominal
        # pair, since it is the join key against ``spec["bands"]``.
        graded_lo = f_lo if band.graded_lo_hz is None else float(band.graded_lo_hz)
        graded_hi = f_hi if band.graded_hi_hz is None else float(band.graded_hi_hz)
        in_band = [
            record
            for record in records
            if record["f_lo_hz"] < graded_hi and record["f_hi_hz"] > graded_lo
        ]
        out.append(
            {
                "band_hz": [f_lo, f_hi],
                "intervals": [dict(record) for record in in_band],
                "disclosure": _carve_out_disclosure_copy(in_band),
                "expert": _carve_out_expert_copy(in_band),
            }
        )
    return out


def cloud_validity_floor_hz(positions: Sequence[_CloudPosition]) -> float | None:
    """The group's own gated validity floor — the WORST (highest) of its
    positions' floors, or ``None`` when no position reported a usable one.

    The worst rather than a mean: the combined curve is a power mean ACROSS
    these positions, so a bin below any one position's reflection-gate floor is
    contaminated by that position's truncated-window artifact. The highest floor
    is the only choice under which every graded bin is inside every contributing
    capture's validity.

    ``None`` means the lower edge could not be verified, NOT that it is zero;
    callers disclose it as unknown and clamp nothing.
    """
    floors = [
        float(getattr(p.response, "validity_floor_hz", None) or 0.0)
        for p in positions
    ]
    usable = [f for f in floors if math.isfinite(f) and f > 0.0]
    return max(usable) if usable else None


def cloud_trusted_floor_hz(validity_floor_hz: float | None) -> float | None:
    """The group's TRUSTED floor (``2.5/T``) from its validity floor (``1/T``)
    — the number the flat spec is graded above (#2551).

    ``1/T`` is where a reflection-free window of ``T`` has one full cycle of
    resolution; ``2.5/T`` is where the gated magnitude is actually trustworthy.
    The E4 gate-stability sweep is why the distinction is not academic: the
    1-4 kHz band moved 2.1 dB across 3/5/7/10 ms gates purely because part of it
    sat below the shorter windows' trusted floor, while everything above held to
    <=0.006 dB (:data:`~jasper.audio_measurement.gating.TRUSTED_FLOOR_MULTIPLIER`).

    Derived rather than plumbed: the multiplier is monotonic, so the trusted
    floor of the group's worst validity floor is the worst of the positions'
    trusted floors, and no caller passing one floor can forget the other.

    ``None`` in, ``None`` out, likewise for a non-finite or non-positive floor,
    which is "no floor was established" and never "a floor of zero".
    """
    if validity_floor_hz is None:
        return None
    floor = float(validity_floor_hz)
    if not math.isfinite(floor) or floor <= 0.0:
        return None
    return TRUSTED_FLOOR_MULTIPLIER * floor


def cloud_entanglement_floor_hz(
    per_position: Sequence[tuple[Any, Any]],
) -> EntanglementFloor:
    """The group's ROOM floor and its provenance — the WORST of its positions'.

    :func:`cloud_trusted_floor_hz`'s argument applied to the floor no window
    choice can lower (#3495): the MAX is the only floor under which every marked
    bin is marked at every contributing capture.

    One position that does not know its floor un-knows the group's — a max over
    the seats that DID know would claim the silent seat is cleaner. Empty in,
    unknown out.

    The source is the WEAKEST of the pooled provenances: a group is ``measured``
    only when every seat's floor was timed off its own reflection, and one
    declared seat makes the aggregate ``declared``. Anything outside
    :data:`~jasper.audio_measurement.gating.ENTANGLEMENT_SOURCES` is unknown.
    Each seat is read through the lenient door, because a seat's pair comes off
    a persisted position row.
    """
    seats = [
        EntanglementFloor.coerce(floor_hz, source) for floor_hz, source in per_position
    ]
    known = [seat.hz for seat in seats if seat.hz is not None]
    if not seats or len(known) != len(seats):
        return EntanglementFloor.unknown()
    return EntanglementFloor(
        max(known),
        ENTANGLEMENT_SOURCE_MEASURED
        if all(seat.source == ENTANGLEMENT_SOURCE_MEASURED for seat in seats)
        else ENTANGLEMENT_SOURCE_DECLARED,
    )


@dataclass(frozen=True)
class CloudGroupResult:
    """:func:`assemble_cloud_group_result`'s payload, plus the line a failure earns.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_cloud_pipeline_failed``, ``None`` when there
    was nothing to say, as data because this module is side-effect-free.
    """

    result: dict[str, Any]
    diagnostics: dict[str, Any] | None = None


def assemble_cloud_group_result(
    combined: Any,
    *,
    echo_band_hz: tuple[float, float],
    echo_band_provenance: Mapping[str, Any] | None = None,
    validity_floor_hz: float | None = None,
    trusted_ceiling_hz: float | None = None,
    tier: str = "",
    position_records: Sequence[Mapping[str, Any]] = (),
    crossover_region_hz: tuple[float, float] | None = None,
    graded_spec_sink: Callable[[Any], None] | None = None,
) -> CloudGroupResult:
    """THE single function that consumes the exclusion mask,
    ``geometry.locked`` and the null registry TOGETHER.

    No other code may read ``combined.excluded`` / ``combined.geometry.locked``
    and treat that as the honesty verdict: the mask alone is a hole. This runs
    :func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    at ``echo_band_hz``, unions its excluded bins with the combiner's own
    power-vs-median screen, and evaluates
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` against the
    merged mask. ``combined`` may be ``None`` — the group could not be combined.

    The ``spec`` report built here is the SSOT: every spec-facing surface renders
    :func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge` of this report
    rather than deriving a number, and nothing downstream re-evaluates the
    curve. ``carve_outs`` is a third reading of that same evaluation, never a
    second one — the bins are already gone from ``spec`` and no verdict here can
    move. The tolerance table is untouched: the decision was to disclose the
    carve-out, not to re-spec the band.

    ``echo_band_provenance`` is how a payload reader tells a contract-derived
    band from a clamped one (#1763), since the published ``echo_band_hz`` alone
    cannot say which. :meth:`_CloudEchoBand.disclosure` supplies the block;
    ``None`` means "not stated", never "not clamped".

    The spec is graded above the group's TRUSTED floor, not its validity floor
    (#2551): :func:`cloud_trusted_floor_hz` turns the group's ``1/T`` into the
    ``2.5/T`` the gate's delta probe already refuses to grade below, and
    ``evaluate_flat_spec`` intersects every band's lower edge with it — the
    reference band included, since a bin the gate cannot support must not
    re-centre the target. Both floors are published. Three properties this
    keeps:

    * The intersection is a band EDGE, not a mask entry, so ``spec.n_excluded``
      stays exactly the honesty instruments' count and a gate artifact cannot
      inflate it. Each band discloses ``graded_lo_hz``/``graded_hi_hz`` beside
      its nominal row.
    * A band left entirely outside the trusted range is ``evaluable=False``,
      never ``passed=False`` — there is no evidence there, which is not a
      failure. ``overall_passed`` still treats unevaluable as not-passed.
    * A ``None`` floor or ceiling clamps NOTHING and is reported as ``None``,
      rather than withholding the evidence above an unverified edge.

    Clamping is not free and moves the headline in the FLATTERING direction on
    a corpus whose sub-floor region is loud — on S0 it re-centres the reference
    by -4.55 dB and flips the 250 Hz-2 kHz band verdict
    (``test_the_trusted_floor_clamp_costs_the_low_band`` pins the figures). The
    direction is response-shape dependent, not a property of the clamp: do not
    generalize the sign. None of it is the speaker improving — it is the same
    speaker graded on fewer bins, which is what ``n_bins`` keeps visible. One
    short gate in a group is therefore expensive by design, since the group
    takes the WORST position's floor; per-position per-bin masking inside
    ``combine_positions`` would be strictly better and is a
    ``spatial_combine`` estimator change rather than a wiring one.

    Fail-soft over a NAMED family: exactly
    ``(ValueError, TypeError, IndexError, AttributeError)``, the documented
    raise surface of every callee. A downstream DSP failure here is
    diagnostic machinery, never a capture-accept gate, so it is reported as
    ``available: False``. Any other exception propagates uncaught.
    """
    if combined is None:
        return CloudGroupResult({"available": False, "reason": "combine_failed"})
    try:
        from jasper.active_speaker.flat_spec import (
            GradedSpec,
            evaluate_flat_spec,
            spec_flatness_gauge,
        )
        from jasper.audio_measurement.interference_nulls import (
            identify_interference_nulls,
        )
        from jasper.attribution.position_evidence import position_evidence_block
        from jasper.audio_measurement.spatial_combine import merged_true_intervals

        null_report = identify_interference_nulls(combined, band_hz=echo_band_hz)
        crossover_registry = _crossover_region_null_registry(
            combined, echo_band_hz=echo_band_hz,
            crossover_region_hz=crossover_region_hz,
            identify=identify_interference_nulls,
        )
        merged_mask = np.asarray(combined.excluded, dtype=bool) | np.asarray(
            null_report.excluded, dtype=bool
        )
        # ``crossover_registry`` is deliberately absent from this union: see
        # its builder for why classification there may never become gating. The
        # mask handed to the evaluator is EXACTLY what the honesty instruments
        # found; the gate's floor rides beside it as a band-edge intersection
        # instead (#2551).
        trusted_floor_hz = cloud_trusted_floor_hz(validity_floor_hz)
        # The ROOM's floor is pooled from the same seats the curve was pooled
        # from (#3502). It CLAMPS NOTHING and changes no grade — it only lets
        # every band say which of its bins no window could have separated from
        # the room.
        entanglement = cloud_entanglement_floor_hz(
            [
                (
                    row.get("gate_entanglement_floor_hz"),
                    row.get("gate_entanglement_floor_source"),
                )
                for row in position_records
            ]
        )
        spec_report = evaluate_flat_spec(
            combined.freqs_hz, combined.power_mean_spec_db, merged_mask,
            trusted_floor_hz=trusted_floor_hz,
            trusted_ceiling_hz=trusted_ceiling_hz,
            entanglement_floor_hz=entanglement.hz,
            entanglement_floor_source=entanglement.source,
        )
        # Hand the LIVE report to a caller that needs the object rather than
        # the serialized copy below: ``to_dict`` flattens away
        # ``overall_passed`` and each band's ``evaluable``/``passed``, and
        # re-evaluating from ``combined`` would be a second owner of the merged
        # honesty mask. A sink rather than a second return value, because every
        # other caller reads the dict.
        if graded_spec_sink is not None:
            # The curve, the mask and the verdict as ONE record: this is the
            # only place all three exist together, and handing them over
            # separately would let a consumer pair a curve with a mask from a
            # different evaluation.
            graded_spec_sink(GradedSpec(
                combined.freqs_hz, combined.power_mean_spec_db, merged_mask,
                spec_report,
            ))
        geometry_dict = {
            "locked": bool(combined.geometry.locked),
            "reason": str(combined.geometry.reason),
            "n_confident": int(combined.geometry.n_confident),
            "n_positions": int(combined.geometry.n_positions),
            "median_tau_us": float(combined.geometry.median_tau_us),
            "clustered_fraction": float(combined.geometry.clustered_fraction),
            "thin_evidence": bool(combined.geometry.thin_evidence),
        }
        return CloudGroupResult({
            "available": True,
            "geometry": geometry_dict,
            "geometry_guidance": _geometry_guidance_copy(geometry_dict),
            "screen_excluded_bands_hz": [
                list(b) for b in combined.excluded_bands_hz
            ],
            "merged_excluded_bands_hz": [
                list(b) for b in merged_true_intervals(combined.freqs_hz, merged_mask)
            ],
            "null_registry": _null_registry_to_dict(null_report),
            # The crossover region, ASKED. Classification only, never unioned
            # into any mask above. ``None`` when there is no committed crossover
            # to name a region with, or when the gating band already reached it.
            "null_registry_crossover_region": crossover_registry,
            "spec": spec_report.to_dict(),
            # The SAME registry and spec report above, re-read per band. Not a
            # second evaluation: those bins are already gone.
            "carve_outs": carve_outs_by_band(
                spec_report, null_report, combined.excluded_bands_hz,
            ),
            # A pure reduction of the SAME ``spec`` report above, carried here
            # so no downstream surface derives its own.
            "flatness": spec_flatness_gauge(spec_report).to_dict(),
            "validity_floor_hz": (
                float(validity_floor_hz)
                if validity_floor_hz is not None and math.isfinite(validity_floor_hz)
                else None
            ),
            # The floor the spec was graded above — 2.5x the one directly
            # above. Published beside its input so a reader sees both the
            # window's resolution limit and its trust limit (#2551).
            "trusted_floor_hz": trusted_floor_hz,
            # The floor is always available; the ceiling is read off the bound
            # candidate's mic tier and is ``None`` on a pre-apply close with no
            # candidate. A session can therefore grade its MEASURE group and its
            # VERIFY group over different spans, which publishing this here
            # makes visible.
            "trusted_ceiling_hz": spec_report.trusted_ceiling_hz,
            "echo_band_hz": list(echo_band_hz),
            "echo_band_provenance": (
                dict(echo_band_provenance)
                if isinstance(echo_band_provenance, Mapping)
                else None
            ),
            # WHICH INSTRUMENT measured this group. ``None`` means unknown,
            # never a guessed default: the tiers make materially different
            # claims.
            "tier": str(tier) or None,
            "curve": _decimate_curve_for_json(
                combined.freqs_hz, combined.power_mean_spec_db,
            ),
            # The MEMBERS behind every aggregate above — serialization only:
            # no new signal, no threshold, no verdict. Never raises, so it
            # cannot turn a good group into a failed one.
            "positions": position_evidence_block(
                combined,
                position_records=position_records,
                validity_floor_hz=validity_floor_hz,
            ),
        })
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        return CloudGroupResult(
            {"available": False, "reason": "pipeline_failed"},
            {"error": str(exc)},
        )
