# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a capture-consuming phase DECIDES about one take (#2291 Phase 5a-iv).

A sibling of :mod:`.programs` and :mod:`.priors`.  Those two answer
"what does this phase play, and how loud" and "what is the analyzer told about
what came back"; this one answers the question immediately after — **is this
take evidence, what does it record, and does the group want another one**.  In
``consume_capture`` the three are consecutive steps of one capture.

The phases it serves are the ones that consume a take without prescribing
anything from it: the two position clouds, the R16 lateral walk, and #2291's
entry baseline.  They were three separate method families on the conductor with
one shape between them — *run shipped gates in a documented order, and if they
all pass, reduce the take to a record* — and the interesting content of all
three is the ORDER and the DROPPED GATES, not the arithmetic.

**Every screen here is a decision about which shipped gate does NOT apply, and
that is why they are worth their own module.**  A cloud position drops VERIFY's
gate-comparability and G3 pilot-transfer steps because the mic MOVED; a lateral
pose drops the three gates that judge the alignment solve because §4.4 forbids
re-running it; an entry baseline drops the tracking comparison because nothing
is applied yet.  Each of those is a sentence that must survive refactoring, and
each is a sentence that dies silently if the ladder it describes gets reordered
by someone reading only the code.  Having one owner is what keeps the sentence
and the ladder in the same place.

**Inputs are stated, never reached for** — the rule :mod:`.priors` established
and the behavioural difference between this module and the methods it replaced.
The conductor's screens called module-level predicates
(``_stimulus_locate_ok`` and friends) that also serve MEASURE and VERIFY, whose
verdicts stay on the session; rather than give those predicates a second
owner, the caller EVALUATES them and states the results in a
:class:`CaptureScreens`.  The predicates are total and side-effect-free, so
stating them eagerly is exact — see :class:`CaptureScreens` for the one
consequence that has (a rejected take now evaluates every predicate rather than
short-circuiting) and why it is safe.

**No household vocabulary**, the rule :mod:`.coordinator` established: a refusal
leaves as a *kind* from :data:`SCREEN_KINDS`, and something else maps it to the
``REASON_REGISTRY`` code whose copy the household reads.  That registry is
:mod:`.refusal_copy`'s since #2291 Phase 5c-ii; the rule here is unchanged, only
the owner's name is.  This module still does not import it — the mapping is
built there, over the kinds declared here.

**Side-effect-free**, unlike :mod:`.coordinator` — deliberately, because that
module's docstring asserts it is the package's only exception and an assertion
is worth keeping true.  :func:`boost_excluded_bands_hz` has a journal line in
the shipped flow, so it returns its log fields as data
(:attr:`BoostExclusion.diagnostics`) and the flow emits them under the event
name it already owns.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.program import KIND_SWEEP
from jasper.audio_measurement.program_analysis import INTEGRITY_CHECK_SWEEP_HEARD

from .contracts import CaptureValidity
from .journey import PHASE_ENTRY_BASELINE, PHASE_LATERAL
from .round_evidence import MeasuredResponse, measured_response_from_analysis
from .verification import evaluate_capture_validity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

__all__ = [
    "CLOUD_CLOSE_NONE",
    "CLOUD_CLOSE_AWAITING_CONFIRM",
    "CLOUD_CLOSE_RUNNING",
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
    "CloudVerdict",
    "LateralPose",
    "LateralPoseCurve",
    "cloud_position_capture",
    "combine_cloud_positions",
    "cloud_geometry_verdict",
    "cloud_position_screens",
    "lateral_pose_screens",
    "lateral_curves_sufficient",
    "lateral_evidence_grid_hz",
    "lateral_pose_curve",
    "entry_baseline_screens",
    "group_position_floor",
    "geometry_retake",
    "take_id_for",
    "cloud_position_record",
    "lateral_pose_record",
    "entry_baseline_record",
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
# Retakes rather than appended positions for ONE reason, and it is the protocol
# rather than the physics: the relay runner completes a set at exactly
# ``capture_target`` accepted captures with ``index == accepted_count + 1``, so
# rejecting a capture is the only lever that keeps a plan alive at the same
# index — appending would need a variable-length plan the shipped runner cannot
# express.
#
# A "replacing is better physics" argument was made and WITHDRAWN under review
# (2026-07-26): the reviewer computed the power-mean counterexample, where
# APPENDING a wide position to a clustered cloud fills a −15 dB null further
# than replacing does (−6.1 dB vs −7.7 dB) and lowers ``clustered_fraction``
# more besides. Replacing is what the protocol permits, not what the estimator
# prefers; if the runner ever grows variable-length sets, appending is the
# better answer.
#
# Bounded on purpose: `geometry.locked` is a "spread the mic further" hint, not
# a failure, and an unbounded loop against a genuinely position-invariant
# defect (S0's source-fixed horn-rim comb — see the plan doc's "S0 executed"
# §b) would never terminate, because no amount of mic movement decorrelates a
# source-fixed null. Two retakes, then proceed and RECORD the verdict — it
# lands in the journal and the durable v2 state's `cloud` block. PR-4 carries
# it further: `_geometry_guidance_copy`'s plain-language guidance rides the
# envelope's own `cloud` key and `/state`'s compact projection
# (`crossover_v2_status_block`) — but no household-facing surface renders it
# yet (zero JS/asset changes in PR-4). PR-7 renders it.
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
#: rather than trusted.  The same discipline :data:`.coordinator.REFUSAL_KINDS`
#: keeps, and for the same reason: a kind added here without an arm there is a
#: wiring defect, and a silent ``else`` is how a new kind ships wearing another
#: kind's household sentence.
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

    Every field is a fact the caller computed with a shared module-level
    predicate — the same ones MEASURE's and VERIFY's verdicts use.  Those
    predicates live in :mod:`.capture_dispatch` since #2291 Phase 5c-ii, and
    still do not live *here* on purpose: they serve verdicts this module does
    not own, and a shared predicate with two owners is worse than one stated
    argument.  They arrive as arguments either way.

    **Stating them eagerly is exact, and that is checkable rather than
    asserted.**  The ladders below short-circuit, so on a rejected take the
    shipped code never called the later predicates.  Each of them
    (``_stimulus_locate_ok``, ``_sweep_locate_confidence_ok``,
    ``_sweep_schedule_ok``, ``_any_sweep_clipped``) reads only
    ``analysis.locations``, returns a bool, and **never raises on no input** —
    they are total and side-effect-free — so evaluating one whose verdict is
    then discarded costs a list comprehension and changes nothing observable.
    What it buys is that the ORDER lives in exactly one place, as data rather
    than as control flow spread across a call site.

    ``pilot_snr_ok`` and ``linearity_ok`` are tri-state (``None`` = not
    evaluated) exactly as they are on ``ProgramAnalysis``, because the ladders
    below branch on ``is False`` rather than on falsiness: an unevaluated screen
    is not a failed one.

    **Every field is required, and the four that a shorter ladder does not read
    are required for the same reason as the three it does.**  An earlier
    revision defaulted ``glitch_detected``/``sweep_locate_confidence_ok``/
    ``sweep_schedule_ok``/``any_sweep_clipped`` permissively, so
    :func:`cloud_position_screens`' call site stated three of seven and inherited
    four passes.  That reads as covered from either end while being a promise
    nobody made: the day a rung is added to the cloud ladder reading one of
    them, it would silently never fire — the caller was never asked, so the
    default answers for a capture it has not looked at.  A default that is only
    correct because the current ladder ignores the field is a defect waiting for
    the ladder to change.  Requiring all seven costs each caller four pure,
    total predicate calls and makes "what this capture was" a stated fact rather
    than a partly-assumed one.
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

    **Per-position work is deliberately light** (the PR-3b design contract): the
    same locate/linearity screens every phase runs, plus "did this capture yield
    a usable summed response".  The group analyses — combine, null
    identification, spec evaluation — run ONCE per group, not once per position,
    so their cost is paid once instead of N times.

    Measured 2026-07-27 on the S0 ten-position corpus (a laptop; a Pi 5 is
    slower — ``combine_cloud_positions`` states the 3-6 s across-hosts regime):
    the **combine is 2.7-2.8 s and dominates completely**, while everything
    layered on it — the null gate, the spec evaluation, the carve-out assembly —
    totals **0.02-0.04 s**.  Running the set per position would multiply that by
    N instead of paying it once.

    Two VERIFY gates are deliberately NOT applied here, because both assume a
    stationary mic replaying the identical program:

    * gate-comparability (a shorter gate than MEASURE's ⇒ inconclusive) — a
      cloud position's gate legitimately differs from the anchor's, since the
      nearest boundary changes when the mic moves.  That is the measurement, not
      a defect.
    * the G3 pilot-transfer step — the reference it compares against is "the
      same chain measuring the same thing"; moving the mic changes the acoustic
      transfer by design, so a step here carries no information about the
      recording chain drifting.

    ``has_summed_response`` is the last screen and its own kind of refusal: the
    stimulus located but no summed response came back, so the capture carries no
    curve to combine and is not evidence.
    """
    if not screens.stimulus_located:
        return SCREEN_LOCATE_FAILED
    if screens.pilot_snr_ok is False:
        # Issue #1810, the same ordering rule as the other two ladders: the
        # room/level discriminator runs before the linearity branch so a
        # collapsed pilot pair is never reported as the phone's fault.
        return SCREEN_PILOT_LEVEL_COLLAPSE
    if screens.linearity_ok is False:
        return SCREEN_LINEARITY_FAILED
    if not has_summed_response:
        return SCREEN_LOCATE_FAILED
    return None


def lateral_pose_screens(screens: CaptureScreens) -> str | None:
    """One pose of the R16 lateral walk (plan §4.4), or a refusal kind.

    The screens are MEASURE's own capture-integrity gates, in MEASURE's order,
    because a pose replays MEASURE's program: a pose that did not record cleanly
    is not evidence, wherever the microphone was standing.

    Three MEASURE gates are deliberately NOT applied — the delay-search status,
    the GCC trust floor and the plausibility backstop — because all three judge
    the ALIGNMENT SOLVE, which §4.4 forbids re-running here.  The search window
    is a geometry prior about the MARK, so a microphone 40 cm to the side
    legitimately fails it; refusing on those would quietly keep only the poses
    that happen to align like the anchor, which is precisely the off-axis
    consequence these samples exist to expose.

    Nor does a rejected pose re-arm MEASURE with a level backoff: the pose must
    be measured at the ANCHOR'S level or its curve is not comparable to the
    anchor's, and a quieter retake would answer a different question.  That is
    the caller's concern, stated here because it is the reason this ladder ends
    in a plain refusal rather than in a re-arm.

    The walk's LAST rung is :func:`lateral_curves_sufficient`, and it is a
    separate call for a stated reason — see that function.
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

    A pose that yielded fewer than two curves cannot answer any of §4.4's
    questions — every one of them is a woofer-versus-HF comparison.  It reuses
    the locate kind rather than minting one, because the household action
    ("measure this spot again") is identical.

    **Why this is a second call rather than an ``n_curves`` argument to
    :func:`lateral_pose_screens`.**  Counting the curves means BUILDING them,
    and the builder (``lateral_pose_curve``) resamples a driver response onto a
    fixed grid by indexing its own frequency axis — on a degenerate response
    with an empty axis that is an ``IndexError``, not a zero-length curve.  The
    shipped ladder never reaches the builder on a capture the screens rejected,
    and folding the count into the ladder above would have inverted that: the
    build would run first, so a capture whose stimulus was never located could
    raise where it used to refuse cleanly, turning a household retry screen into
    a terminal internal error.  Two calls keep the shipped short-circuit exact.
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
    """#2291's "before" capture: screen it, and reduce it when it passes.

    **The accept rule reuses shipped gates; it invents none.**  Which of
    VERIFY's this mirrors, and which it drops:

    * stimulus locate — **reused**.  A capture whose stimulus was never located
      is not evidence about the speaker, whatever is being asked of it.
    * ``pilot_snr_ok is False`` — **reused**, and ahead of everything but the
      locate check, for issue #1810's reason: a pilot pair that never cleared
      the room floor is a room/level problem, and reporting it as anything else
      sends the household to fix the wrong thing.
    * capture integrity — **reused**, through
      :func:`~.verification.evaluate_capture_validity`, which is the shipped
      comparability rule and the same function the post-apply side is graded by.
      One difference from VERIFY's verdict, deliberate: an **absent** integrity
      record is UNUSABLE here where VERIFY treats it as
      no-evidence-and-continue.  That evaluator owns the "``None`` means no
      evidence, never clean" convention, and this capture exists only to be
      compared — a before-side nobody graded cannot carry a before→after claim,
      so it fails closed rather than seeding one.
    * ``linearity_ok is False`` — **reused**.  AGC in the recording chain bends
      the curve this capture exists to measure.
    * gate-comparability, §5.2 (VERIFY's gate shorter than MEASURE's ⇒
      inconclusive) — **dropped**.  It protects an OVERLAY of the measured
      summed response against MEASURE's per-driver model, and this capture makes
      no such overlay.  Its partner is stage 2's VERIFY, which is still graded
      against MEASURE's window by that rule; adding a second,
      differently-motivated refusal here would cost retakes without protecting a
      claim.
    * the G3 pilot-transfer step — **dropped**.  G3 asks whether the recording
      chain moved BETWEEN two replays of the identical program within one
      session's lifetime, and it exists to protect the tracking comparison it
      immediately precedes.  There is no tracking comparison here, and stage 1
      runs exactly one summed capture, so the gate could only ever record a
      reference that stage 2's own fresh session is forbidden to inherit
      (#1927).
    * the tracking-max tolerance comparison — **dropped**, structurally:
      :func:`~.priors.entry_baseline_priors` withholds ``predicted_sum``, so
      ``analysis.verify_tracking`` is ``None`` and there is nothing to grade.

    One more refusal that is this phase's own: the reduction
    (:func:`~.round_evidence.measured_response_from_analysis`) must produce a
    side.  It returns ``None`` for a missing summed response or a degenerate
    curve — the same "located, but carries no curve" condition
    :func:`cloud_position_screens` already answers with the locate kind, and
    this reuses that kind rather than minting one.

    Every refusal is an ordinary kind the flow renders with a shipped
    ``REASON_*``, so the slot's normal retry budget and household copy apply
    with no new machinery.

    ``analysis`` arrives whole here, unlike the other two ladders' stated
    screens, because two of this ladder's steps CONSUME it rather than test it:
    the shipped comparability evaluator reads the integrity record, and the
    reduction reads the summed response.  Handing over a
    :class:`CaptureScreens` and then the analysis as well would state one fact
    twice.

    ``stimulus_located`` is nonetheless a separate argument, and the split is
    the same one :class:`CaptureScreens` makes: it is the answer of a flow-side
    PREDICATE (``_stimulus_locate_ok``, which also serves MEASURE's and
    VERIFY's verdicts and so keeps its owner there), whereas ``pilot_snr_ok``
    and ``linearity_ok`` are plain attributes of the analysis this function was
    handed.  Reading a field off an argument is not reaching; importing the
    predicate would be.
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
        # The same two-code split VERIFY's verdict makes from the same evidence:
        # a sweep nobody could hear is a level/mic problem, and a spliced or
        # clipped timeline is the transient capture-glitch class (#1838 §5.2).
        # An ABSENT record takes the glitch kind's silent auto-retry, which is
        # the right household action for "we could not tell" — nothing for them
        # to fix, worth one more try.
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


def group_position_floor(phase: str, *, min_resolved_cloud_positions: int) -> int:
    """How few resolved positions still lets a group stand.

    A cloud is an AVERAGE: below ``min_resolved_cloud_positions`` there is
    nothing to combine, so the session ends honestly.  The lateral walk is not —
    §4.4: "side evidence owns robustness, not the target".  The coefficients are
    the anchor's and already in hand, so a pose nobody could capture costs a
    robustness sample and nothing else.  Floor ZERO: drop it, record why, keep
    walking, and let the consumer disclose that it decided on fewer positions
    than planned.
    """
    return 0 if phase == PHASE_LATERAL else min_resolved_cloud_positions


@dataclass(frozen=True)
class GeometryRetake:
    """A warranted geometry retake: which rung to show, and which take it drops.

    ``rung`` indexes the caller's prompt ladder; ``retries_after`` is the
    counter's new value.  Both are computed here rather than at the call site so
    "how many have been spent" and "which sentence the household reads" cannot
    drift apart.
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

    Four conjuncts, and none of them is obvious:

    * ``locked`` — the combine could not separate the room's arrivals, which is
      the only condition a retake can improve.
    * ``thin_evidence`` is NOT set — the flag marks a verdict resting on the
      bare minimum number of usable echo estimates (a cliff, not a gradient).
      Asking an operator to walk two more positions on that basis spends real
      session minutes on a verdict the instrument itself qualifies, so a thin
      lock is disclosed and accepted rather than retried.
    * the budget is not spent.
    * ``group_already_closed`` is False.  A VOLUNTARY retake (§2.6) re-enters
      the close with the group already closed, and the retry branch DROPS the
      take at this index — which, on a voluntary retake, is the only copy (the
      retention replaced the original in place).  Dropping it would leave the
      household with LESS evidence than before they chose to redo a spot, which
      is the one thing the retake contract promises can never happen.  So:
      re-combine, keep the verdict honest with the new take, and accept.

    ``have_take_to_replace`` is the fifth condition and the reason this returns
    an object rather than a bool.  A group can close because its last position
    was SETTLED without a curve, and then there is no take: the retake lever
    works by REJECTING the take at this index, so asking would re-open the slot
    whose tries are exactly what just ran out.  Carrying the narrowing in the
    return value rather than in a second conjunct at the call site keeps "which
    take gets dropped" and "is a drop warranted" the same fact.
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


# --- R16 lateral evidence (plan §4.4) --------------------------------------- #
#
# One fixed log-spaced basis for every retained pose curve. Fixed rather than
# per-role so both branches land on the SAME frequencies and a consumer can sum
# them without resampling either; log-spaced because a crossover argument is a
# per-octave one. 1/12 octave is ~118 Hz at 2 kHz, which resolves a handoff
# region the plan itself calls a COARSE gate ("lateral samples remain a coarse
# gate", #1968) — this is not a polar measurement and must not be read as one.
LATERAL_EVIDENCE_BAND_HZ = (20.0, 20_000.0)
LATERAL_EVIDENCE_POINTS_PER_OCTAVE = 12


@dataclass(frozen=True)
class LateralPoseCurve:
    """One driver's NEUTRAL response at one pose, on the shared log basis.

    ``complex_tf`` holds ``M = plant * P`` — polarity-free, with NO
    configured-crossover composition applied (see
    ``CrossoverV2Session._lateral_priors``). §4.2's
    ``S_c = sign_c * M * C_c / P`` is the consumer's step, once per candidate.

    Values are SAMPLED at the nearest native bin, never interpolated or
    averaged: an interpolated complex value is a number no microphone produced,
    and a phase interpolated across a wrap is simply wrong. The frequencies
    actually sampled ride along for the same reason. ``band_hz`` is the role's
    driven sweep band — outside it there was no stimulus, so the samples are
    noise and a consumer must bound itself with this.
    """

    role: str
    freqs_hz: np.ndarray
    complex_tf: np.ndarray
    band_hz: tuple[float, float]


@dataclass(frozen=True)
class LateralPose:
    """One accepted pose in the lateral walk.

    Carries NO trim, delay, polarity or fit. That absence is the §4.4 contract
    ("re-solve trim or delay independently at every pose" is forbidden), and it
    is structural rather than a convention: there is no field here for a second
    solution to be written to.
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

    ``kind == KIND_SWEEP`` matters because a v2 MEASURE program OPENS with a
    leading pilot pair, and a pilot carries a role and a band too — so a
    role-only match would take the pilot's, not the sweep's. Today those two
    bands are EQUAL (both derive from the same intersected ``RoleBand``), so
    this is not a live bug; it names which segment the retained curve's band
    describes, so the answer stays right if that coupling ever moves. Pinned by
    ``test_the_retained_band_reads_the_sweep_segment_not_a_pilot``.
    """
    bands: dict[str, tuple[float, float]] = {}
    for segment in program.segments:
        if segment.kind != KIND_SWEEP or segment.role is None:
            continue
        if segment.f1_hz is None or segment.f2_hz is None:
            continue
        bands.setdefault(segment.role, (float(segment.f1_hz), float(segment.f2_hz)))
    return bands


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
    # distance matrix (the analysis grid is hundreds of thousands of bins).
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
    )


# --------------------------------------------------------------------------- #
# what a retained take records
# --------------------------------------------------------------------------- #


#: A pose whose stated displacement from the mark lies in the HORIZONTAL plane
#: — the microphone is moved around the speaker rather than raised or lowered.
#: On a rig with a measurement arm that motion is the arm's own: a ROTATION
#: about the rig's vertical axis.  The word is
#: :data:`~jasper.active_speaker.crossover_v2_flow.REMOTE_VERTICAL_DISCLOSURE`'s
#: own ("Measured on the horizontal axis only"), reused rather than re-minted.
#:
#: It names where the pose's STATED offset lies, not a promise that nothing
#: else moved: the second geometry-retake rung asks for a sideways move AND a
#: rise, and records that rise only in its ``prompt``.
POSITION_AXIS_HORIZONTAL = "horizontal"

#: A pose stated as a move ABOVE or BELOW mark height.  Nothing on this rig
#: rotates in elevation — the prompts ask for a raise or a lower — so a pose on
#: this axis carries no bearing at all
#: (:attr:`PositionGeometry.degrees` is ``None``), which is a different fact
#: from "0°" and must never read as one.
POSITION_AXIS_VERTICAL = "vertical"

#: Every axis a pose can be stated on, so a reader can CHECK the value rather
#: than trust it.
POSITION_AXES = (POSITION_AXIS_HORIZONTAL, POSITION_AXIS_VERTICAL)


@dataclass(frozen=True)
class PositionGeometry:
    """WHERE a prompted capture was taken, as three numbers instead of a sentence.

    The three facts an owner ruling (2026-08-24) named as the minimum a pose
    record owes a reader: **angle, axis, and distance**.  Before it, a cloud
    position's only statement of place was the household ``prompt`` string, and
    the 2026-08 new-horn campaign read that prose as a mic being carried
    sideways when the rig had rotated — a misreading prose cannot rule out,
    cannot be diffed, and cannot be compared across rounds.

    **The frame, stated once so nothing downstream has to restate it.**
    ``degrees`` is the signed whole-degree bearing of the pose measured from the
    speaker, negative LEFT of the design axis as seen from the microphone
    looking at the speaker; ``axis`` is which of :data:`POSITION_AXES` that
    bearing lives on; ``mark_distance_m`` is the speaker-to-MARK distance the
    bearing is DERIVED AGAINST.  That last one is a reference length, never a
    surveyed capsule distance: nothing in a round measures how far the
    microphone actually ended up, so a reader gets the bearing and the length
    it was taken against, and neither is a claim about the other.

    ``degrees`` is ``None`` wherever no signed bearing was commanded — always on
    :data:`POSITION_AXIS_VERTICAL`, where the rig raises and lowers the
    microphone rather than swinging it, and on the horizontal axis for a pose
    whose RECORD declares no side (both geometry-locked retake rungs).  ``None``
    is the honest answer in both; 0 would be a lie that reads as "on the design
    axis".

    Whole degrees, for the reason the derivation that produces them gives: the
    poses come from tape-measure offsets to a mark placed "about" 1 m out, and
    a tenth of a degree would claim a precision the placement never had.

    Derived by ``crossover_v2_flow.position_geometry``, which owns the pose
    table and the sign convention and names each ``None`` case; carried here
    because this module owns what a retained take RECORDS.
    """

    axis: str
    degrees: int | None
    mark_distance_m: float

    def __post_init__(self) -> None:
        if self.axis not in POSITION_AXES:
            raise ValueError(
                f"a pose axis must be one of {POSITION_AXES}, got {self.axis!r}"
            )
        if self.axis == POSITION_AXIS_VERTICAL and self.degrees is not None:
            # Loud rather than silently banked: a bearing on the vertical axis
            # is a number nothing on this rig can have commanded, and a reader
            # who trusted it would place the microphone somewhere it never was.
            raise ValueError(
                "a vertical pose carries no bearing — this rig raises and "
                f"lowers the microphone rather than swinging it, got "
                f"{self.degrees!r} degrees"
            )


def take_id_for(position_id: str, attempt: int) -> str:
    """One take's id, as every builder AND the storage seam spell it.

    A geometry retake reuses the position id — same prompted spot, measured
    again from further out — so the id alone does not identify a take
    (attribution plan §6's "accepted-attempt <-> position mapping"). Zero-padded
    so a lexical sort of the bundle is also a chronological one.

    Written here once: this expression stood in all three builders below and a
    fourth time at ``correction_crossover_v2.bind_position_retention``, and four
    copies of an index convention is four places for it to drift. The seam and
    the record must name the same take or the bundle's sidecar path and the
    session's own evidence disagree.
    """
    return f"{position_id}_a{int(attempt):02d}"


def _take_identity(
    *,
    position_id: str,
    phase: str,
    index: int,
    attempt: int,
    session_id: str,
    wav_sha256: str | None,
) -> dict[str, Any]:
    """The identity block every retained take carries, whatever kind it is.

    The COMMON CORE of the three builders below. What each of them adds on top
    is its own — a graded seat and a walk pose are different captures and their
    grading columns are never meaningful for each other
    (see :func:`lateral_pose_record`) — so this is a shared core plus a
    role-tagged extension, never one shape with half its columns null.

    Deliberately NOT emitted here: the id key itself. A cloud position and an
    entry baseline call it ``position_id``, a pose calls it ``pose_id``, and
    collapsing those two words would be minting one vocabulary for two
    questions.

    ``wav_sha256`` is the capture's content digest — the VERIFIER for a replay,
    never the index (§6's rule that "content hashing stays the verifier; it must
    stop being the index"). Recorded whether or not any store retained the
    bytes, because it is what lets a laptop-side WAV be matched back to this
    take at all.
    """
    return {
        "phase": phase,
        "index": index,
        "attempt": attempt,
        "take_id": take_id_for(position_id, attempt),
        "session_id": session_id,
        "wav_sha256": wav_sha256,
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
    validity_floor_hz: float | None,
    gating_applied: bool,
    summed_ripple_db: float | None,
    glitch_detected: bool,
    wav_sha256: str | None,
) -> dict[str, Any]:
    """One retained cloud position, as the record two consumers read.

    **WO-1 moved this assembly ahead of the retention seam's own early return**,
    so it is built whether or not a retention seam is bound.  That is
    deliberate: the metadata has two consumers.  The seam is one; the group
    close is the other — the cloud pipeline reads these records to serialize the
    per-position members — and the close happens on every session, including the
    offline/test configurations that bind no retention seam at all.  Building it
    only when a storage seam existed would have made the per-position evidence
    silently depend on operator retention being wired.

    ``take_id`` is minted HERE rather than only at the storage seam, so the
    session's own evidence and the bundle's sidecar path name the same take.
    A geometry retake reuses the position id, so the id alone does not identify
    a take (attribution plan §6's "accepted-attempt ↔ position mapping").

    ``gate_floor_source`` records WHY the gate window is what it is (issue
    #1966).  ``gating_applied`` only says a window was applied at all; it cannot
    distinguish a window that stops at a found reflection from one capped at the
    search bound because none was found.  Every position of the 2026-07-30
    corpus was the second, and this record could not say so.
    ``gate_disclosure`` is the same fact as a sentence, so a reader does not
    have to know the enum's vocabulary to read the record honestly.

    ``gate_moved_rms_db`` and ``gate_reflection_delay_ms`` are the two NUMBERS
    that sentence narrates (ticket 1.5), banked beside it because prose was
    their only copy: the evidence packet's ``not_evaluated`` block used to say
    the reflection time "is narrated inside verify.gate.disclosure prose and is
    not banked as a number anywhere in a round's artifacts", and a reader is
    owed the figure without regex over English.  Both come from
    :mod:`~jasper.audio_measurement.gate_disclosure`'s one typed record, so the
    digits here and the digits in the sentence are the same derivation.  Both
    are ``None`` on an ungateable capture, and the delay is ``None`` — never
    0.0 — on a window capped at the search ceiling, where no reflection was
    found to time.  The delay is RELATIVE to the direct arrival, not the gating
    block's absolute ``first_reflection_ms``: see
    :attr:`~jasper.audio_measurement.gate_disclosure.GateDisclosure.reflection_delay_ms`
    for why the absolute time is meaningless to a reader.

    The identity half — phase, index, attempt, ``take_id``, ``session_id`` and
    the ``wav_sha256`` verifier — is :func:`_take_identity`, shared with the
    other two builders.

    ``geometry`` is WHERE the microphone was, as fields rather than as English
    (owner ruling, 2026-08-24).  Until it existed this record carried no
    geometry at all — the ``prompt`` sentence was the only statement of place,
    and the 2026-08 new-horn campaign read a rotation out of it as a sideways
    carry.  The three keys it lands (``position_deg``, ``position_axis``,
    ``mark_distance_m``) are stamped from the pose the operator was actually
    given; ``prompt`` stays beside them as the human instruction and stops
    being the source of truth.  ``position_deg`` deliberately spells the same
    word :func:`lateral_pose_record` already does — one vocabulary for one
    question — and is ``None`` wherever no bearing was commanded.  See
    :class:`PositionGeometry` for the frame all three sit in.
    """
    return {
        "position_id": position_id,
        **_take_identity(
            position_id=position_id, phase=phase, index=index, attempt=attempt,
            session_id=session_id, wav_sha256=wav_sha256,
        ),
        "prompt": prompt,
        "wide": wide,
        # The position's named question (attribution-stage plan §5's promotion
        # queue item 1). The prompt string alone cannot be parsed back into a
        # role, so the label rides the record explicitly.
        "role": role,
        "position_deg": geometry.degrees,
        "position_axis": geometry.axis,
        "mark_distance_m": geometry.mark_distance_m,
        "captured_at": captured_at,
        "gate_window_ms": gate_window_ms,
        "gate_floor_source": gate_floor_source,
        "gate_disclosure": gate_disclosure,
        "gate_moved_rms_db": gate_moved_rms_db,
        "gate_reflection_delay_ms": gate_reflection_delay_ms,
        "validity_floor_hz": validity_floor_hz,
        "gating_applied": gating_applied,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
    }


#: What every :data:`~.journey.PHASE_LATERAL` pose plays -- ``program_for_phase``
#: hands them all the anchor's interleaved per-driver MEASURE object.
#:
#: A literal copy of :data:`jasper.active_speaker.angle_capture.REGIME_PER_DRIVER`
#: because importing it would close a cycle (that module imports the flow, the
#: flow imports this one).  Pinned equal by
#: ``test_the_pose_record_states_the_seams_own_regime_word``.
LATERAL_POSE_REGIME = "per_driver"


def lateral_pose_record(
    pose: LateralPose,
    *,
    position_deg: int,
    lateral_consumer: str,
    session_id: str,
    wav_sha256: str | None,
) -> dict[str, Any]:
    """One retained lateral pose, as the evidence bundle's sidecar carries it.

    Takes: the accepted :class:`LateralPose`, plus the four facts it does not
    carry.  ``position_deg`` is the SIGNED whole-degree bearing (negative LEFT
    of the design axis), derived by the flow's ``position_angle_deg`` and
    stated here rather than re-derived.  ``lateral_consumer`` is one of
    :data:`~.journey.LATERAL_CONSUMERS`.

    Guarantees: WHERE the microphone was (``position_deg`` + ``offset_cm`` +
    ``at_mark``), WHAT played (``regime``), WHO the walk was for
    (``lateral_consumer``), and the identity/verifier pair
    (``take_id``, ``wav_sha256``) a replay needs.  Refuses nothing.

    Separate from :func:`cloud_position_record` rather than a widened one: a
    cloud position is a summed sweep judged by gating and ripple, and those
    columns are never meaningful for a pose.
    """
    return {
        "pose_id": pose.pose_id,
        **_take_identity(
            position_id=pose.pose_id, phase=PHASE_LATERAL, index=pose.index,
            attempt=pose.attempt, session_id=session_id, wav_sha256=wav_sha256,
        ),
        "prompt": pose.prompt,
        "role": pose.role,
        "position_deg": int(position_deg),
        "offset_cm": float(pose.offset_cm),
        "at_mark": bool(pose.at_mark),
        "regime": LATERAL_POSE_REGIME,
        "lateral_consumer": lateral_consumer,
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
) -> dict[str, Any]:
    """The entry baseline's retained record — a cloud position's shape, minus
    the group, plus the curve.

    Structurally a cloud-position record: same take-id convention, same
    gate/ripple/digest scalars, handed to the same retention seam so an entry
    baseline lands in ``refs["position_artifacts"]`` beside every other retained
    take and one replay path covers both.  It is NOT a group member, which is
    exactly why the retention call is explicit at its call site — nothing in the
    group bookkeeping would make it.

    The three fields a cloud position has no use for are the three that make
    THIS capture comparable to the post-apply one, and they are the reason it is
    a separate builder rather than a keyword on the other: WHAT was played
    (``program_id``), WHERE it was played from (``reference_mark``), and WHICH
    graph it went through (``graph_fingerprint``).  A before→after claim is only
    as good as those three matching on both sides.

    **The reduced curve rides here, and that is what makes this the DURABLE
    copy** (fragment ``02``'s duplication #2, plan row 2a).  It is bounded at
    ``round_evidence.BENEFIT_CURVE_MAX_BINS`` upstream, so a take carries a few
    KB of JSON beside a WAV.  A retained take is write-once and keyed by
    ``take_id``; the flow state file that also holds these arrays is rewritten
    on every persist, which is why a banked round could never be re-graded once
    the next round started.  Same three arrays as
    ``round_evidence.EntryBaseline.to_dict``, under the same names, so one
    reader covers both — see
    :func:`~.position_cycle.read_entry_baseline_take`.
    """
    identity = _take_identity(
        position_id=f"{PHASE_ENTRY_BASELINE}_{index:02d}",
        phase=PHASE_ENTRY_BASELINE, index=index, attempt=attempt,
        session_id=session_id, wav_sha256=wav_sha256,
    )
    return {
        # The entry baseline has no prompted spot of its own, so its position
        # id IS its take id — the one kind where the two coincide.
        "position_id": identity["take_id"],
        **identity,
        "program_id": program_id,
        "reference_mark": reference_mark,
        "graph_fingerprint": graph_fingerprint,
        "captured_at": captured_at,
        "freqs_hz": [float(hz) for hz in freqs_hz],
        "magnitude_db": [float(db) for db in magnitude_db],
        "excluded": [bool(flag) for flag in excluded],
        "validity_floor_hz": validity_floor_hz,
        "gate_window_ms": gate_window_ms,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
    }


# --------------------------------------------------------------------------- #
# the blind span below the null registry's floor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoostExclusion:
    """:func:`boost_excluded_bands_hz`'s answer, plus the line it justifies.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_boost_evidence``.  They travel as data
    rather than as a log call because this module is side-effect-free (see the
    module docstring); the event NAME and the ``session_id`` stay with the flow,
    which owns both.
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

    **The hole this fills.**  Boost permission is granted on ``cloud is not
    None`` (see the boost gate in
    :func:`~.intervention.plan_linearization`), whose stated meaning is that
    "null-exclusion stays a measured, registry-gated fact".  The registry's
    analysis band is floored at 4 kHz, so below that edge the registry
    contributes no exclusions — not because it was uncertain but because it was
    never asked — and the gate's claim is satisfied in form without being
    satisfied in substance.  On the 2026-07-30 JTS3 session the registry
    returned ``insufficient_evidence`` / ``no_corroborating_arrivals`` with zero
    exclusions (re-derived from that session's own ``cloud_measure.json``),
    while its largest prescribed boost was **+8.06 dB at 3633.6 Hz** — 366 Hz
    under the floor.  That boost figure is the owner's, from the offline replay
    recorded on issue #1967; it is quoted here rather than re-derived, and no
    test pins it.

    **What this does and, more importantly, what it does not.**  It runs
    :func:`~jasper.audio_measurement.interference_nulls.classify_dip_position_variance`
    over the blind span and hands the dips the cloud's own positions DISAGREE
    about to the fit vocabulary, which refuses a lift whose realized cascade
    would put significant gain in one.  It cannot grant boost anywhere — the
    bound is monotone by construction (see
    :func:`~jasper.active_speaker.linearization_fit._lift_stage`) and a
    ``position_invariant`` dip is left exactly as the gate already had it.  That
    asymmetry is deliberate and is not this module's call to make —
    ``interference_nulls``' module docstring ("position-invariance says *this is
    real*; it does not say *this is correctable*"),
    ``docs/historical/attribution-stage-plan.md`` §5 (a finding supported only by position
    variance stays ``unsure``, adjudicated by rotating the speaker), and
    :mod:`jasper.attribution.promotion` (which routes ``position_invariant`` to
    ``carve``) all refuse to read stationarity as a driver property.  So this
    narrows the gate where the evidence decides against a boost and leaves it
    open otherwise, which is the owner's ruling ("do not trade a blind gate for
    a blunt one") applied rather than overridden.

    The residual is therefore real and named: a dip that every position sees may
    still be a source-fixed interference null rather than a driver deficit, and
    separating those needs the post-apply arm to ask "did this help" rather than
    "did this match the model" (#1868).

    **Know what one band costs before adding to this list.**  The fit drops any
    boost filter whose own action region overlaps a band here — per filter, so
    siblings working elsewhere survive, and a whole-lift refusal
    (``lift_suppressed_reason="boost_excluded_band"``) only when EVERY boost was
    aimed.  Skirt spill from a surviving filter is kept and disclosed rather
    than refused.  Even so, each band here can cost a real correction, which is
    why this returns only the positively-contradicted class and never a "we were
    unsure here" list.

    **Fails OPEN**, disclosed.  A span too narrow to analyse, a cloud that never
    retained per-position curves, or an unexpected numeric failure all yield no
    exclusions — i.e. exactly the permission the gate grants today.  Failing
    closed would blanket-ban boost below 4 kHz on a computation hiccup, which is
    the blunt outcome this whole function exists to avoid.

    ``variance_check_failed`` is the one arm the caller must still act on: it is
    reported in :attr:`BoostExclusion.diagnostics` so the flow can raise the
    journal line's level, because an unexpected numeric failure is worth seeing
    even though it is answered by failing open.
    """
    from jasper.audio_measurement.interference_nulls import (
        CLASSIFICATION_POSITION_DEPENDENT,
        classify_dip_position_variance,
    )

    registry = result.get("null_registry") or {}
    n_dependent = 0
    floor_hz = float(echo_band_hz[0])
    grid = np.asarray(getattr(combined, "freqs_hz", ()), dtype=float)
    # The cloud's own gated validity floor is the honest lower edge: below it
    # every position's curve is a truncated-window artifact, which is the same
    # bound the spec band already clamps to.
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
            # The band the corroborating registry actually adjudicated, and the
            # span below it where it structurally could not.
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

# The named question each prompted position answers (McCarthy's mic-position
# vocabulary, attribution-stage plan §5 promotion queue item 1). Persisted with
# the position so the attribution stage can consume a labelled sample instead
# of an anonymous member of an average; profile-independent, so both listening
# profiles read the same labels.
#
#   ONAX  — inside the design-axis window (lateral offset < WIDE_OFFSET_MIN_CM)
#   OFFAX — out at the coverage edge (lateral offset >= WIDE_OFFSET_MIN_CM)
#   XOVR  — vertical offset: the axis the woofer/tweeter crossover lobes on,
#           which is the mechanism M8 needs a labelled sample of
#
# WHAT A CONSUMER MUST NOT ASSUME: a cloud carries every role. Roles come from
# the walked PREFIX of the table, so the Full tier's 8 prompted positions
# sample all three, but EXPRESS's 4 sample {onax, offax} ONLY — its walk stops
# before the first vertical move. That is by design (express is the shorter
# instrument, §1.3), so an attribution consumer reads the roles a group
# actually has and reports the absent one as unsampled, never as null evidence.
POSITION_ROLE_ONAX = "onax"
POSITION_ROLE_OFFAX = "offax"
POSITION_ROLE_XOVR = "xovr"
POSITION_ROLES = (POSITION_ROLE_ONAX, POSITION_ROLE_OFFAX, POSITION_ROLE_XOVR)
#
# The mark distance the CHECK screen asks for ("about 1 m in front of the
# speaker"). It is the reference length that turns this flow's lateral OFFSETS
# into the BEARINGS a positioner can act on, so it lives beside them rather than
# only inside that sentence.
MARK_DISTANCE_M = 1.0
#: The pose a capture with no prompted move of its own was taken at — every
#: one of them (CHECK, MEASURE, the entry baseline, stage 2's anchor) is a
#: design-axis capture, which is the same fact :func:`_entry_policy` states to
#: the position gate when it is handed no prompt.
_DESIGN_AXIS_GEOMETRY = PositionGeometry(
    axis=POSITION_AXIS_HORIZONTAL,
    degrees=0,
    mark_distance_m=MARK_DISTANCE_M,
)
@dataclass(frozen=True)
class _CloudPosition:
    """One accepted position inside a group, retained for the group-end combine.

    ``response`` is the capture's ``ProgramAnalysis.summed_response`` — a
    ``program_analysis.DriverResponse`` carrying the calibrated, reflection-gated
    magnitude on a linear (rfftfreq) grid plus the matching complex TF. Holding
    the response rather than a pre-built
    :class:`~jasper.audio_measurement.spatial_combine.PositionCapture` is
    deliberate: PR-4 needs the same object for the per-position work the null
    gate and the spec curve do, and re-deriving it from a lossy intermediate
    would be the drift this seam exists to prevent.
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
    # off the prompt the operator was actually given. Persisted with the
    # position so the attribution stage reads a labelled sample rather than an
    # anonymous member of an average (attribution-stage plan §5 promotion queue
    # item 1). Defaulted so every construction site that predates roles — the
    # corpus and unit fixtures — stays valid unchanged.
    role: str = POSITION_ROLE_ONAX
    # WHERE the microphone was, carried off the SAME prompt ``role`` and
    # ``wide`` come from (owner ruling, 2026-08-24). Held on the position
    # rather than re-derived at retention: a geometry retake shows a different
    # prompt than the table's, and a second derivation from the index would
    # state the spot the operator was told to abandon.
    #
    # Defaulted to the design axis so every construction site that predates it
    # — the corpus and unit fixtures — stays valid unchanged, exactly as
    # ``role`` above is. That default is the honest one for a fixture: a
    # position built without a pose is one nobody moved.
    geometry: PositionGeometry = _DESIGN_AXIS_GEOMETRY
    # PR-4: the contract-derived analysis bands this position's GROUP should be
    # combined/searched with — spatial_combine.combine_positions's own
    # ``echo_band_hz`` / ``signal_band_hz`` kwargs, echoed here rather than
    # threaded as a separate call-site argument. Carrying them on the position
    # (every position in one group shares the same session-derived values —
    # see ``CrossoverV2Session.__init__``) is what lets
    # :func:`combine_cloud_positions` derive the right bands from
    # ``positions`` alone, with no caller (``_close_cloud_group``'s single
    # combine, ``cloud_geometry_verdict``'s convenience wrapper) needing to
    # pass them explicitly or risk two call sites drifting apart.
    # ``None`` means "use the module defaults" — the pre-PR-4 behaviour, still
    # exercised by every corpus/unit test that builds a ``_CloudPosition``
    # without these two kwargs.
    echo_band_hz: tuple[float, float] | None = None
    signal_band_hz: tuple[float, float] | None = None
def cloud_position_capture(position: _CloudPosition) -> Any:
    """One retained position → a :class:`spatial_combine.PositionCapture`.

    **The PR-4 seam.** PR-3b calls the combiner for one thing — the geometry
    verdict — but the input assembly is the whole assembly, so PR-4's wider
    pipeline (``identify_interference_nulls`` → ``evaluate_flat_spec``) extends
    the consumer, never this builder.

    Regime of the ``ir`` field, stated exactly because ``detect_echo``'s answer
    depends on it: it is the inverse rFFT of the response's **gated, calibrated**
    complex transfer function — i.e. the impulse response AFTER
    ``deconv.direct_arrival_window`` and the adaptive reflection gate that
    ``program_analysis._driver_response`` applies, not the raw deconvolved IR.
    The direct arrival is therefore present (the window places it at a fixed
    pre-offset) and early secondary arrivals inside the gate survive, which is
    the region ``detect_echo`` windows itself down to; LATE room reflections
    beyond the gate are gone by construction. The S0 forensics ran the detector
    on the ungated IR instead — ``tests/test_crossover_v2_cloud_geometry_corpus.py``
    is the measurement that the two agree on the S0 corpus's geometry verdict,
    rather than an assumption that they must.
    """
    from jasper.audio_measurement.spatial_combine import PositionCapture

    response = position.response
    freqs = np.asarray(response.freqs_hz, dtype=float)
    magnitude = np.asarray(response.magnitude_db, dtype=float)
    complex_tf = np.asarray(response.complex_tf)
    # ``program_analysis._n_fft_for`` always returns a power of two (>= 8192),
    # so the analysis grid is an even-length rfft and ``n = 2*(bins-1)``
    # inverts it exactly rather than approximately.
    ir = np.fft.irfft(complex_tf, n=2 * (complex_tf.size - 1))
    return PositionCapture(
        position_id=position.position_id,
        freqs_hz=freqs,
        magnitude_db=magnitude,
        sample_rate=int(position.sample_rate_hz),
        ir=ir,
        # §4.2's one line. The role was written to the position RECORD and the
        # persisted row and read by nothing analytical — the combiner's only
        # per-position struct dropped it here, so nothing that decides or
        # remembers a round ever saw a position's KIND. Carrying it changes no
        # combination (the reduction stays unweighted; see
        # ``PositionCapture.role``) and is what lets the per-position residual
        # say "on-axis" rather than "position 3".
        role=str(position.role or ""),
    )


def _geometry_verdict_from_combined(
    combined: Any, n_positions: int,
) -> dict[str, Any]:
    """The geometry-verdict dict from an ALREADY-COMBINED result.

    Split out of :func:`cloud_geometry_verdict` (S3 review finding,
    2026-07-26) so :meth:`CrossoverV2Session._close_cloud_group` can
    combine a group's positions exactly ONCE and derive both the retry-gating
    verdict and the honest-instrument pipeline from that ONE object, rather
    than each deriving its own combine. A plain JSON-native dict, because the
    host persists it verbatim into the durable v2 state. ``locked`` is
    ``False`` on every degraded path — but the ``reason`` says WHICH degraded
    path, so "no credible echo estimates" never reads the same as "the cloud
    combined and its nulls move".
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
    ``event=correction.crossover_v2_cloud_combine_failed``, and is ``None``
    when there was nothing to say.  They travel as data rather than as a log
    call because this module is side-effect-free (see the module docstring);
    the event NAME and the ``session_id`` stay with the flow, which owns both.
    Same shape as :class:`BoostExclusion`, for the same reason.
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
    """Assemble a closed group and combine it — the whole PR-4 seam.

    ``CloudCombine.combined`` is a
    :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`, or
    ``None`` when the group cannot be combined (no positions, or a malformed
    one).  Called exactly ONCE per group-close event, from
    ``CrossoverV2Session._close_cloud_group``: PR-3b reads one field off the
    result (``geometry``, via :func:`_geometry_verdict_from_combined`); PR-4's
    pipeline (``assemble_cloud_group_result``) reads the rest of the SAME
    object.  Never a second combine — see S3 review finding (2026-07-26): an
    earlier revision of this wiring called this function TWICE per close
    attempt (once through :func:`cloud_geometry_verdict` for the retry gate,
    once more from the pipeline) — measured seconds-per-combine (3-6 s across
    runs/hosts on the S0 ten-position corpus; interpreter-bound
    ``smooth_fractional_octave``, worse on a Pi 5 — N2 review finding,
    2026-07-27: an earlier "5.6-6.2 s" point figure did not reproduce across
    hosts, so this states the regime instead of a false-precision number).
    :data:`GEOMETRY_RETRY_POSITIONS` allows up to 3 close attempts per group
    (2 retries + the accepting close), so the pre-fix worst case was 3 × 2 =
    6 combines, not the earlier "4x" claim — real operator seconds for a
    claim (byte-for-byte determinism) that was true but not worth paying for.

    Never raises.  A group's captures are already-accepted evidence and a
    combiner failure must not retroactively fail them, so an unusable cloud is
    a ``None`` the caller turns into an honest "unknown" rather than an
    exception that would strand the session.
    """
    from jasper.audio_measurement.spatial_combine import (
        DEFAULT_ECHO_BAND_HZ,
        combine_positions,
    )

    if not positions:
        return CloudCombine(None)
    # Every position in one group carries the SAME session-derived bands
    # (set once at construction — see ``_CloudPosition``'s docstring), so
    # reading them off the first position is reading the group's own bands,
    # not an arbitrary one.  ``None`` (a position built before PR-4, or by a
    # caller that never declared a driver contract) falls back to the
    # module's own long-standing default, unchanged from pre-PR-4 behaviour.
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
    """PR-3b's one use of the combiner: combine, then read ``.geometry``.

    A convenience wrapper around :func:`combine_cloud_positions` +
    :func:`_geometry_verdict_from_combined` for callers that only have
    ``positions`` (the corpus acceptance test; any future direct caller) —
    the session itself does NOT call this (see
    ``CrossoverV2Session._close_cloud_group``'s own single combine).

    **Reason-string divergence, documented not silently left (N4 review
    finding, 2026-07-27).**  An empty ``positions`` short-circuits HERE with
    ``reason="no_positions"`` before ever reaching the combiner, while
    :func:`_geometry_verdict_from_combined` called directly with a
    ``combined=None`` and ``n_positions=0`` (e.g. because
    :func:`combine_cloud_positions` was handed an empty group some other way)
    reports ``reason="combine_failed"`` for the exact same "there were zero
    positions" fact.  Unreachable through the session today (a group only
    closes with at least its just-captured position already retained), but
    the two functions disagree on naming WHICH degraded path a caller hit —
    the entire point of a ``reason`` field — so this wrapper owns disclosing
    the split rather than leaving a future reader to discover it by diffing
    the two bodies.
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
