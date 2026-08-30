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
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.gating import TRUSTED_FLOOR_MULTIPLIER
from jasper.audio_measurement.program import (
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    RoleBand,
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


# The fewest RESOLVED positions a cloud group can close with and still produce a
# usable claim, so a position the flow gives up on degrades the group instead of
# ending the session (ruling item 3: "continue the phase if it can proceed with
# the positions it has").
#
# MEASURED, not chosen: the group close itself has no position floor at all
# (``_close_cloud_group`` never compares ``len(positions)`` to anything), and
# ``combine_cloud_positions`` tolerates any non-empty group. The binding
# constraint is downstream, in the fit —
# ``linearization_envelope.position_stability_limit`` raises ``ValueError`` for
# ``n_positions < 2``, because a cross-position spread across fewer than two
# positions is undefined. So two is where "can proceed" genuinely stops.
#
# Deliberately NOT ``MIN_CLOUD_MEASURE_POSITIONS`` / ``MIN_CLOUD_VERIFY_POSITIONS``
# (6 / 5): those are PLAN-DECLARATION floors — how many positions the household
# is asked to walk — enforced once by ``_validated_cloud_counts`` before any
# capture happens. Reusing them at runtime would have killed the 2026-08-03
# verify, which was running usefully at 4 positions of the 6 that tier declared
# then. Between this floor and the declared one the claim is degraded, and
# degradation is DISCLOSED (the geometry verdict's ``n_positions`` /
# ``thin_evidence`` already ride the envelope), not gated.
MIN_RESOLVED_CLOUD_POSITIONS = 2


def group_position_floor(phase: str) -> int:
    """How few resolved positions still lets a group stand.

    A cloud is an AVERAGE: below :data:`MIN_RESOLVED_CLOUD_POSITIONS` there is
    nothing to combine, so the session ends honestly.  The lateral walk is not —
    §4.4: "side evidence owns robustness, not the target".  The coefficients are
    the anchor's and already in hand, so a pose nobody could capture costs a
    robustness sample and nothing else.  Floor ZERO: drop it, record why, keep
    walking, and let the consumer disclose that it decided on fewer positions
    than planned.
    """
    return 0 if phase == PHASE_LATERAL else MIN_RESOLVED_CLOUD_POSITIONS


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


def _summed_sweep_band_hz(program: Any) -> tuple[float, float] | None:
    """The band a SUMMED sweep drove — the one segment the map above cannot key.

    Sibling of :func:`_primary_sweep_bands`, separate because it answers a
    different question: a summed sweep declares ``role=None`` (every driver is
    sounding), so there is no key to file it under and a caller identifies it
    by holding ``ProgramAnalysis.summed_response`` rather than by a role word.
    ``None`` for a program that plays no summed sweep — a MEASURE program is
    exactly that, and it is the reason this returns an option rather than
    raising.
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
#: rotates in elevation — the prompts ask for a raise or a lower, and a person
#: performs it — so a pose on this axis commands no horizontal bearing
#: (:attr:`PositionGeometry.degrees` is ``None``), which is a different fact
#: from "0°" and must never read as one.  WHERE it was raised to is
#: :attr:`PositionGeometry.vertical_deg`.
POSITION_AXIS_VERTICAL = "vertical"

#: Every axis a pose can be stated on, so a reader can CHECK the value rather
#: than trust it.
POSITION_AXES = (POSITION_AXIS_HORIZONTAL, POSITION_AXIS_VERTICAL)


@dataclass(frozen=True)
class PositionGeometry:
    """WHERE a prompted capture was taken, as numbers instead of a sentence.

    The facts a pose record owes a reader: **bearing, elevation, axis, and
    distance** — an owner ruling named the first, third and fourth as the
    minimum and a later one added elevation beside them.  Before it, a cloud
    position's only statement of place was the household ``prompt`` string, and
    the 2026-08 new-horn campaign read that prose as a mic being carried
    sideways when the rig had rotated — a misreading prose cannot rule out,
    cannot be diffed, and cannot be compared across rounds.

    **The frame, stated once so nothing downstream has to restate it.**
    ``degrees`` is the signed whole-degree HORIZONTAL bearing of the pose
    measured from the speaker, negative LEFT of the design axis as seen from the
    microphone looking at the speaker; ``vertical_deg`` is the signed
    whole-degree ELEVATION of the pose above mark height, in the same frame and
    derived against the same length, negative BELOW; ``axis`` is which of
    :data:`POSITION_AXES` the pose's stated move was on; ``mark_distance_m`` is
    the speaker-to-MARK distance both angles are DERIVED AGAINST.  That last one
    is a reference length, never a surveyed capsule distance: nothing in a round
    measures how far the microphone actually ended up, so a reader gets the
    angles and the length they were taken against, and neither is a claim about
    the other.

    **The two angles are ORTHOGONAL and default differently, which is a fact
    rather than an inconsistency to iron out.**  ``degrees`` is ``None``
    wherever no signed bearing was commanded — on
    :data:`POSITION_AXIS_VERTICAL`, where the operator raises or lowers the
    microphone rather than swinging it, and on the horizontal axis for a pose
    whose RECORD declares no side (both geometry-locked retake rungs).  ``None``
    is the honest answer in both; 0 would be a lie that reads as "on the design
    axis".  ``vertical_deg`` has no such case and so needs no ``None``: a pose
    nobody raised is genuinely AT mark height, and 0 states that truly.  A
    compound pose — sideways *and* raised — therefore states both numbers, which
    is the move a single axis-plus-value pair could only describe half of.

    Whole degrees, for the reason the derivation that produces them gives: the
    poses come from tape-measure offsets to a mark placed "about" 1 m out, and
    a tenth of a degree would claim a precision the placement never had.

    **No combination of axis and angle is refused here.**  A vertical walk is
    performed BY HAND, so the values this carries are the operator's to state
    and this type's to record faithfully; the automation that genuinely cannot
    swing in elevation refuses at its own seam
    (``capture_plan.position_angle_deg``), where the refusal is about a
    positioner rather than about a pose.

    Derived by ``crossover_v2_flow.position_geometry``, which owns the pose
    table and both sign conventions and names each ``None`` case; carried here
    because this module owns what a retained take RECORDS.
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
        # Whole degrees, as above. `bool` is an `int` and is never an elevation.
        if isinstance(self.vertical_deg, bool) or not isinstance(
            self.vertical_deg, int
        ):
            raise ValueError(
                "a pose elevation is a whole number of degrees above mark "
                f"height, got {self.vertical_deg!r}"
            )


def take_id_for(position_id: str, attempt: int) -> str:
    """One take's id, as every builder that mints one spells it.

    A geometry retake reuses the position id — same prompted spot, measured
    again from further out — so the id alone does not identify a take
    (attribution plan §6's "accepted-attempt <-> position mapping"). Zero-padded
    so a lexical sort of the bundle is also a chronological one.

    Written here once: this expression stood in every builder below and again
    at the storage seam, and a copy of an index convention per caller is a
    place per caller for it to drift. The seam mints nothing now — it reads
    ``take_id`` off the record and the record store names the artifact from it —
    because the seam and the record must name the same take or the bundle's
    path and the session's own evidence disagree.
    """
    return f"{position_id}_a{int(attempt):02d}"


#: The graph fingerprints that name no graph.  Both spellings reach a record —
#: ``""`` from a host that could not name its graph at all, and
#: :data:`~.contracts.ENTRY_GRAPH_FINGERPRINT_UNKNOWN` from
#: ``coordinator.entry_graph_fingerprint`` when no applied profile was found —
#: and neither can classify a take.
_UNNAMED_GRAPHS = frozenset({"", ENTRY_GRAPH_FINGERPRINT_UNKNOWN})

#: The phases whose captures are a re-measure AFTER an apply.  Used only to
#: separate ``verify`` from ``candidate`` — never to separate ``baseline`` from
#: either, which is the split :func:`take_kind` refuses to take from a phase.
_VERIFY_PHASES = frozenset({PHASE_VERIFY, PHASE_CLOUD_VERIFY})


def take_kind(
    *, graph_fingerprint: str, baseline_fingerprint: str, phase: str,
) -> str:
    """Which of :data:`~.contracts.MEASURE_KINDS` a take is, or ``""``.

    **Derived from the GRAPH, never from the phase.**  A phase → kind map is not
    merely imprecise, it is not well defined: :data:`~.journey.PHASE_LATERAL` is
    a per-driver walk that is a ``baseline`` **or** a ``candidate`` check
    depending on which candidate was applied under it, and the cloud phases
    split by which side of the apply they sit on.  Only the graph the take
    played through tells those apart, which is why #3130 put
    ``graph_fingerprint`` on the pose record in the first place.

    The rule: equal to the round's pre-apply fingerprint → ``baseline``; a
    post-apply re-measure phase → ``verify``; anything else played through a
    graph that is not the "before" → ``candidate``.

    ``graph_fingerprint`` is the applied profile's ``candidate_fingerprint``
    (:func:`~.coordinator.entry_graph_fingerprint`'s namespace), deliberately
    NOT the running-config hash ``provenance.graph.fingerprint`` carries — see
    :func:`lateral_pose_record` for why the running hash cannot separate two
    walks.  ``baseline_fingerprint`` is the same quantity for the round's own
    "before", so the two are comparable.

    **Unresolvable is ``""``, never a guess.**  Either fingerprint unnamed and
    this returns empty: an honest fact about the capture, exactly as
    ``baseline_record_id`` is ``""`` where the prior baselined no such pose.
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

    The engine's own fields (``session.TuningSession._record``), carried at the
    builders so a flow-banked take and an engine-banked take are one record
    shape rather than two.  Offline re-analysis (ruling S3) reads the bank, and
    a reader that had to ask which of two shapes it was holding could not.

    Every field defaults empty because most flow call sites do not state them
    yet — the unprompted-phase take states ``baseline_fingerprint`` once a
    round has a baseline to compare against, and the rest still do not — and an empty field here is an honest fact about the capture,
    never a refusal to bank it.  The wave that lifts retention states them.

    ``baseline_fingerprint`` is the round's pre-apply graph, the comparand
    :func:`take_kind` needs; the take's OWN graph stays a separate builder
    keyword because it is a fact about the take, not about the claim.

    ``level_db`` is the PROVEN fader level — the reading
    ``VolumeClaim.prove`` returned and the session re-checked against the level
    it declared — and ``stimulus_dbfs`` is the ladder rung the stimulus played
    at.  Two different quantities on purpose: a ladder moves the stimulus, never
    the claim, and the 8.712 dB incident is what one field saying both costs.

    **``level_db`` is optional HERE and never optional on an engine-banked
    record, and that difference is a fact rather than a mismatch to iron out.**
    The engine banks a stimulus only once its level proved, so its own field is
    always a number; the flow's retention sites hold no volume claim at all,
    so a take retained there has no proven level to state and ``None`` says
    exactly that.  Narrowing this to ``float`` would force those callers
    to invent a number, which is the failure ``_proven_level`` exists to stop.
    ``stimulus_dbfs`` is optional on BOTH sides for a different reason and
    needs no such note: ``None`` is the single stimulus a program declares when
    no ladder was asked for.

    ``wav_path`` is the record → capture pointer, bundle-relative.  It is NOT
    derivable from ``take_id``: ``bundles.capture_artifact_relpath`` appends a
    ``uuid4`` hex, and its caller mints the path BEFORE the write precisely so
    the record can carry it.  Without it a banked record names a capture nothing
    can reach, which is what leaves offline analysis with no inputs.
    """

    baseline_fingerprint: str = ""
    baseline_record_id: str = ""
    candidate_id: str = ""
    polarity: str = ""
    #: Whether the graph this take played through carried the box's own
    #: per-driver level match, and by how much.  Beside ``polarity`` and for
    #: its reason: both are facts about the measurement branch, and a
    #: reverse-null pair is only comparable to a reader who knows whether the
    #: branches were levelled before they were summed.  ``False``/``None`` on
    #: every take that declared none, which is what a record banked before
    #: this existed reads back as.
    level_matched: bool = False
    level_match_trims_db: Mapping[str, float] | None = None
    #: The delay coordinate this take's graph carried, as the executable
    #: ``(role, microseconds)`` pair that was installed. Beside ``polarity`` and
    #: ``level_matched`` because it is the same class of fact -- what the
    #: MEASUREMENT BRANCH was doing -- and it is the one that makes a
    #: confirmation gradeable: ``confirmation_verdict`` keys measured depths by
    #: the coordinate they were played at, so a take without this is a depth
    #: nothing can place. Unsigned: which branch counts as "positive" is
    #: ``NullWalkSpec``'s question, and a record that pre-signed it would be a
    #: second opinion about the sign frame.
    delayed_role: str = ""
    played_delay_us: float | None = None
    #: What a delay confirmation MEASURED, and against which reference. The
    #: depth is the notch at Fc below the shoulders either side;
    #: ``shoulder_summed`` says whether each shoulder had both branches open
    #: there, because on a real 2-way the lower one is the woofer alone and a
    #: reader of the depth cannot tell that unless it is said.
    null_depth_db: float | None = None
    shoulder_summed: Mapping[str, bool] | None = None
    #: Which composition the banked curves came through -- the configured path
    #: composed (``M*C/P``), or the protection phase retained. MEASURE divides
    #: protection out and multiplies the configured crossover in; LATERAL does
    #: not, and until now nothing on disk said which a curve had been through,
    #: so an offline consumer summing them could not tell.
    phase_provenance: str = ""
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

    The COMMON CORE of every builder below. What each of them adds on top
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
    take at all. ``claim.wav_path`` is its POINTER sibling: the digest says
    whether bytes are the right ones, the path says where they are.

    ``graph_fingerprint`` and the :class:`TakeClaim` fields are here rather than
    in the builders because they belong to EVERY take whatever its kind —
    one edit, every record. The measure kind is derived here for the same
    reason, and from the graph rather than from ``phase``: see :func:`take_kind`.

    **It is spelled ``measure_kind`` and not ``kind``, which the engine's own
    record uses, because :func:`take_kind` can honestly answer ``""``** — a
    take whose graph names neither fingerprint is unresolved, never guessed.
    :meth:`~.record_store.BankedRecordStore.bank` accepts the measurement kind
    under EITHER spelling and always writes it back under this one, so the two
    are interchangeable everywhere except at that empty value: ``kind`` is read
    by MEMBERSHIP in :data:`~.contracts.MEASURE_KINDS`, which ``""`` fails,
    while ``measure_kind`` is read by the KEY's PRESENCE, which carries an
    unresolved take through. A ``""`` written here as ``kind`` would leave the
    record with no route at all. The two words converge on one when an
    unresolved take stops being expressible; delete this spelling then.
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
        # The numbers only when there ARE numbers, exactly as the engine's own
        # record states them: an absent key reads as the un-matched take every
        # record banked before this existed was, so no schema version moves.
        **(
            {"level_match_trims_db": dict(claim.level_match_trims_db)}
            if claim.level_matched and claim.level_match_trims_db
            else {}
        ),
        # Every one of these four on the same terms: present only when the take
        # HAS the fact, so a record banked before they existed reads back
        # identically and no schema version moves. Absence is "not this kind of
        # take", never a zero -- a depth of 0 dB is a measured flat sum and a
        # coordinate of 0 us is a real coordinate, so defaulting either would
        # invent evidence.
        **(
            {
                "delayed_role": claim.delayed_role,
                "played_delay_us": float(claim.played_delay_us),
            }
            if claim.played_delay_us is not None and claim.delayed_role
            else {}
        ),
        **(
            {"null_depth_db": float(claim.null_depth_db)}
            if claim.null_depth_db is not None
            else {}
        ),
        **(
            {"shoulder_summed": dict(claim.shoulder_summed)}
            if claim.shoulder_summed is not None
            else {}
        ),
        **({"phase_provenance": claim.phase_provenance} if claim.phase_provenance else {}),
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

    The identity half — phase, index, attempt, ``take_id``, ``session_id``, the
    ``wav_sha256`` verifier and the engine's own :class:`TakeClaim` fields — is
    :func:`_take_identity`, shared with the other two builders.

    ``regime`` is WHAT PLAYED, in the walk seam's vocabulary
    (:data:`LATERAL_POSE_REGIME` is the other word in it), and is ``""`` here
    until a caller states it. **That vocabulary is not
    :data:`~.contracts.MEASURE_REGIMES`'** ``reference_axis``/``near_field``
    pair, which the engine's own record spells under the same key — two
    vocabularies, one key name, and converging them is the retention lift's,
    not this builder's. Stating a guessed word here would make the collision
    harder to find, not easier.

    ``geometry`` is WHERE the microphone was, as fields rather than as English
    (owner ruling, 2026-08-24).  Until it existed this record carried no
    geometry at all — the ``prompt`` sentence was the only statement of place,
    and the 2026-08 new-horn campaign read a rotation out of it as a sideways
    carry.  The four keys it lands (``position_deg``, ``position_axis``,
    ``vertical_deg``, ``mark_distance_m``) are stamped from the pose the
    operator was actually given; ``prompt`` stays beside them as the human
    instruction and stops being the source of truth.  ``position_deg``
    deliberately spells the same word :func:`lateral_pose_record` already does
    — one vocabulary for one question — and is ``None`` wherever no bearing was
    commanded.  ``vertical_deg`` is absent from records banked before it
    existed, and a reader takes that absence as 0 — the pose was at mark
    height, which is what every pose this record shape had until then was.  See
    :class:`PositionGeometry` for the frame all four sit in.

    ``curves`` is WHAT WAS MEASURED, in :func:`pose_curve_record`'s shape and
    under the key :func:`lateral_pose_record` already spells — see
    :func:`analysis_curve_records`.  Empty for a caller that supplied none, and
    absent entirely from records banked before it existed.
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
        # The position's named question (attribution-stage plan §5's promotion
        # queue item 1). The prompt string alone cannot be parsed back into a
        # role, so the label rides the record explicitly.
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
        "validity_floor_hz": validity_floor_hz,
        "gating_applied": gating_applied,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
        "curves": [dict(curve) for curve in curves],
    }


#: What every :data:`~.journey.PHASE_LATERAL` pose plays -- ``program_for_phase``
#: hands them all the anchor's interleaved per-driver MEASURE object.
#:
#: A literal copy of :data:`jasper.active_speaker.angle_capture.REGIME_PER_DRIVER`
#: because importing it would close a cycle (that module imports the flow, the
#: flow imports this one).  Pinned equal by
#: ``test_the_pose_record_states_the_seams_own_regime_word``.
LATERAL_POSE_REGIME = "per_driver"

#: Deep-null floor applied before the log, so a bin that cancelled to exactly
#: zero banks a number instead of ``-inf``, which is not JSON.  The same 1e-12
#: :func:`~jasper.audio_measurement.deconv.magnitude_response` applies, for the
#: same reason and at the same place in the arithmetic.
_POSE_MAGNITUDE_FLOOR = 1e-12


def pose_curve_record(curve: LateralPoseCurve) -> dict[str, Any]:
    """One measured curve, banked as magnitude AND phase (ruling S3).

    The ONE serializer ``complex_tf`` has, for every banked kind that measures
    one -- :func:`analysis_curve_records` is how the other three reach it.  The
    pair banked here reconstructs the transfer function exactly --
    ``10 ** (magnitude_db / 20) * exp(1j * radians(phase_deg))`` -- which is
    what makes the ruling's *"just save the information"* true of phase and not
    only of magnitude.  Without it every offline re-analysis re-derives phase
    from the WAVs and the forward model cannot run from the bank at all.

    ``phase_deg`` is WRAPPED to (-180, 180], the value :func:`numpy.angle`
    produces.  An unwrapped phase is a derived VIEW with a choice of branch in
    it; the wrapped value is what the transform computed, and a consumer that
    wants the unwrapped one can take it without this record having guessed.

    Absolute phase carries the microphone's own uncorrected response, since mic
    calibration here is magnitude-only -- common-mode across the roles of one
    capture, and so self-cancelling for the relative cross-driver work this
    curve exists for.  It is not a claim about the driver's absolute phase.
    """
    tf = np.asarray(curve.complex_tf, dtype=np.complex128)
    magnitude = np.maximum(np.abs(tf), _POSE_MAGNITUDE_FLOOR)
    return {
        "role": curve.role,
        "band_hz": [float(curve.band_hz[0]), float(curve.band_hz[1])],
        "freqs_hz": [float(hz) for hz in curve.freqs_hz],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(magnitude)],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
    }


def analysis_curve_records(analysis: Any, program: Any) -> list[dict[str, Any]]:
    """One analysis's PRIMARY complex responses, in the banked curve shape.

    Ruling S3's *"just save the information"* for the three retained kinds that
    are not a lateral pose.  The walk builds :class:`LateralPoseCurve` objects
    of its own because its candidate fit consumes them in-process; the other
    three kinds only ever needed the RECORD, so they take the same two steps
    (:func:`lateral_pose_curve` then :func:`pose_curve_record`) straight
    through and bank the identical shape under the identical key.  One SHAPE
    for all four kinds, so the reader the cutover's reader-flip row builds has
    one thing to parse rather than four.  Nothing in the tree reads ``curves``
    yet -- see :func:`lateral_pose_record`, which says so and says why.

    BOTH response fields are read, because
    :mod:`~jasper.audio_measurement.program_analysis` fills them on different
    paths: a per-driver analysis fills ``driver_responses`` (one curve per
    role) and a summed-sweep analysis fills ``summed_response`` (one curve,
    role ``"summed"``).  Written as a union rather than a branch so an analysis
    that grows the other half starts banking it instead of silently dropping
    it.  CHECK fills neither -- it solves gains off pilots and computes no
    transfer function at all.

    **PRIMARY responses only, which is fewer than the analysis computed.** A
    MEASURE analysis additionally deconvolves each role's repeat occurrences
    and hangs them off the primary as ``DriverResponse.repeat_responses``; they
    are diagnostic evidence for the primary and feed no candidate/trim/
    alignment math, so they are banked no more than the walk banks them.  The
    ``repeat_index`` filter below is the walk's own, kept verbatim: inert on
    today's ``driver_responses`` (a tuple of primaries) and correct if that
    tuple ever carries a repeat directly.

    A role whose band the program does not declare is SKIPPED rather than
    banked on a guessed band -- outside the driven band the samples are noise,
    and :func:`pose_curve_record`'s ``band_hz`` is what tells a consumer where
    to stop reading.  An empty list therefore means NO CURVE WAS BANKED, never
    "this capture was clean": CHECK reaches it by measuring none, and the
    caller's guard reaches it by failing loudly at WARN.
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
    lateral_consumer: str,
    session_id: str,
    graph_fingerprint: str,
    captured_at: str,
    wav_sha256: str | None,
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """One retained lateral pose, as the evidence bundle's sidecar carries it.

    Takes: the accepted :class:`LateralPose`, plus the six facts it does not
    carry.  ``position_deg`` is the SIGNED whole-degree bearing (negative LEFT
    of the design axis), derived by the flow's ``position_angle_deg`` and
    stated here rather than re-derived.  ``lateral_consumer`` is one of
    :data:`~.journey.LATERAL_CONSUMERS`.

    ``graph_fingerprint`` is WHICH CANDIDATE WAS APPLIED while this pose was
    taken, in :func:`~.coordinator.entry_graph_fingerprint`'s namespace (the
    applied profile record's ``candidate_fingerprint``, or
    :data:`~.contracts.ENTRY_GRAPH_FINGERPRINT_UNKNOWN`).  Deliberately NOT the
    running-config hash a capture's ``provenance.graph.fingerprint`` carries:
    a pose is a ``per_driver`` capture, played through the transient routing
    graph that omits crossover, delay and linearization, so the running hash is
    the SAME before and after a candidate is applied and cannot tell two walks
    apart.  The applied candidate can, which is what makes a banked walk
    classifiable as a baseline or a candidate check without reading the
    capture-retention ring.  :func:`take_kind` is that classification, and it
    is stamped on the record here rather than left for a reader to redo.

    ``position_axis`` is horizontal by construction: the lateral walk states
    every one of its poses as a sideways move at mark height.  Stated so a
    reader of the bank does not have to know that to place the microphone.

    ``captured_at`` is minted at retention, not carried on the pose, for the
    reason :func:`entry_baseline_record` mints its own: a
    :class:`LateralPose` holds no clock.

    Guarantees: WHERE the microphone was (``position_deg`` + ``offset_cm`` +
    ``at_mark``), WHAT played (``regime``), WHO the walk was for
    (``lateral_consumer``), the identity/verifier pair (``take_id``,
    ``wav_sha256``) a replay needs, and WHAT WAS MEASURED --- ``curves``, one
    :func:`pose_curve_record` per driver, on the shared log basis
    :func:`lateral_evidence_grid_hz` names.  Refuses nothing.

    ``curves`` is empty for a pose that carried none.  That shape is reachable
    only through a direct construction: the walk's own
    :func:`lateral_curves_sufficient` floor rejects a capture that produced
    fewer than two before any record is built.

    Nothing in the tree READS ``curves`` yet, and that is ruling S3's whole
    point --- *"right now, let's just save the information"*.  The forward
    model already consumes this exact quantity in-process every round; what it
    could never do was run from the bank.

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
        "vertical_deg": 0,
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

    These three play from wherever the microphone already is — there is no
    table row and no instruction — so what a take of one records is the
    CAPTURE: its bytes' digest, the identity that finds it again, and
    ``curves`` — WHAT WAS MEASURED, in :func:`pose_curve_record`'s shape and
    under the key :func:`lateral_pose_record` already spells (see
    :func:`analysis_curve_records`).

    **The curves are the only part of the analysis this record keeps**, and
    ruling S3 is why: a round's VERDICTS live where the phase puts them
    (``_measure_analysis``, ``_verify_analysis``, the gain plan CHECK
    publishes) and are rewritten inside the round, but the complex responses
    they were drawn from land in no file at all unless they land here. MEASURE
    is the kind that matters most — its per-driver phase is what cross-driver
    timing is measured from — and CHECK banks an empty list because it
    computes no transfer function at all. An empty list is "no curve banked"
    and never "this capture was clean"; :func:`analysis_curve_records` names
    the two ways it is reached. Absent entirely from records banked before
    this field existed.

    **The take id follows the entry baseline's convention rather than inventing
    a second one.** That phase had the same problem first — a retained capture
    with no prompted spot — and solved it by minting the position id from the
    phase and the index, so the position id IS the take id once
    :func:`take_id_for` has qualified it by attempt. One convention, four
    phases; a reader who can parse one banked take can parse all of them.

    **The pose is the design axis**, for the reason
    :func:`entry_baseline_record` already gives about its own unprompted
    capture: a capture with no prompted move is :data:`~.contracts.DESIGN_AXIS_DEG`
    on the horizontal axis, which is the same reading
    ``session.TuningSession._bearings`` gives a spec that names no position, so
    one pose is one record on both sides. Stating it is not inventing a
    bearing — it is declining to make this the one banked kind whose pose a
    reader has to special-case, and it keeps the four builders one shape for
    the index W1-d builds on ``_record()``'s fields.

    ``prompt`` is ``""`` because no instruction was issued, which is a
    different fact from an unknown one; ``regime`` defaults empty for the same
    reason it does on the entry baseline — the caller states it or it is
    honestly unstated, and no builder guesses it from the phase.
    """
    identity = _take_identity(
        position_id=f"{phase}_{index:02d}",
        phase=phase, index=index, attempt=attempt,
        session_id=session_id, wav_sha256=wav_sha256,
        graph_fingerprint=graph_fingerprint, claim=claim,
    )
    return {
        # Same coincidence the entry baseline records: no prompted spot of its
        # own, so this take's position id IS its take id.
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

    **``curves`` is a SECOND curve on a second basis, not a copy of that one.**
    The three arrays above are the GRADED side — decimated to the benefit
    curve's bins, magnitude only, carrying the ``excluded`` mask the round's
    before→after claim is drawn over.  ``curves`` is the MEASURED side, in
    :func:`pose_curve_record`'s shape on the shared log basis and with the
    phase that side has never carried (see :func:`analysis_curve_records`).
    Neither is derivable from the other, which is why both ride.  Absent
    entirely from records banked before it existed.

    **The pose is the design axis**, not a missing fact: this capture has no
    prompted spot, and a capture with no prompted move is
    :data:`~.contracts.DESIGN_AXIS_DEG` on the horizontal axis — the same
    reading ``session.TuningSession._bearings`` gives a spec that names no
    position, so one pose is one record on both sides. ``reference_mark`` still
    says WHERE that axis was measured from; ``prompt`` is ``""`` because no
    instruction was issued, which is a different fact from an unknown one.
    """
    identity = _take_identity(
        position_id=f"{PHASE_ENTRY_BASELINE}_{index:02d}",
        phase=PHASE_ENTRY_BASELINE, index=index, attempt=attempt,
        session_id=session_id, wav_sha256=wav_sha256,
        graph_fingerprint=graph_fingerprint, claim=claim,
    )
    return {
        # The entry baseline has no prompted spot of its own, so its position
        # id IS its take id — the one kind where the two coincide.
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


# --------------------------------------------------------------------------- #
# THE GROUP CLOSE — what a closed cloud is worth, once
# --------------------------------------------------------------------------- #
#
# Everything above answers "is this ONE take evidence".  This section answers
# the question the group close asks next: given every retained position, what
# did the cloud measure, what did the honesty instruments carve out of it, and
# what is the resulting spec verdict.  It lived in ``crossover_v2_flow`` until
# wave 3 rank 2; it sits here because its inputs are this module's own
# ``_CloudPosition`` group and :func:`combine_cloud_positions`' answer, and
# splitting a reduction from the objects it reduces is what made the flow a god
# file.
#
# **The "no household vocabulary" rule above is about REFUSALS and is intact.**
# A screen still leaves as a kind from :data:`SCREEN_KINDS` and something else
# maps it to household copy.  What this section carries is DISCLOSURE copy on a
# group result — sentences about what an instrument carved out, which have no
# refusal code to route through and no other owner; they arrived with
# ``carve_outs_by_band`` and ``assemble_cloud_group_result``, which are their
# only callers.

# --------------------------------------------------------------------------- #
# PR-4: contract-derived analysis bands + the live-flow honesty pipeline
# --------------------------------------------------------------------------- #
#
# docs/historical/linearization-campaign-2026-07.md, PR-4: "The echo/detector
# band and PR-2's signal_band_hz derive from the declared contract: the
# summed system's swept band (RoleBand.band as composed) for the passband;
# the tweeter's usable_frequency_range_hz / measurement_band_hz for the upper
# echo band -- replacing DEFAULT_ECHO_BAND_HZ's flat constant at the call
# site." This section is that derivation, plus the single result-assembly
# function issue #1742 item 4 asks for.

# Cloud curves decimated for persistence (bundle cloud.json + the durable v2
# state's compact cloud block) -- mirrors
# jasper.web.correction_crossover_v2.MAX_PERSISTED_SUM_POINTS (512), which
# this module cannot import without a circular dependency (that module
# imports THIS one). Kept as an independent constant rather than a shared one
# for that reason; if the two ever need to diverge, they now can.
CLOUD_CURVE_MAX_JSON_POINTS = 512


def _composed_swept_band_hz(roles: Sequence[RoleBand]) -> tuple[float, float]:
    """The summed system's swept band -- the union of every declared
    ``RoleBand.band`` -- PR-4's contract-derived ``signal_band_hz``.

    No existing function composes across roles (each ``RoleBand.band`` is one
    driver's own excitation-ceiling band, from
    ``excitation_safety_plan.resolve_driver_excitation_ceilings``); this is
    that composition, added here because it is session-owned wiring policy
    (which roles participate in the passband), not a pure-DSP concern that
    belongs in ``spatial_combine`` or ``program.py``.
    """
    lo = min(float(r.band.lower_hz) for r in roles)
    hi = max(float(r.band.upper_hz) for r in roles)
    return (lo, hi)


@dataclass(frozen=True)
class _CloudEchoBand:
    """The echo/null analysis band the pipeline will APPLY, plus how it was
    derived -- one value, so the band and its provenance cannot be carried
    (or persisted) apart from each other.

    ``band_hz`` is what the detector actually runs on. ``derived_lo_hz`` is
    the lower edge the declared contract produced BEFORE the HF-regime clamp
    (equal to ``band_hz[0]`` whenever no clamp happened), so a reader can
    always tell a contract-derived band from a clamped one **without** the
    journal -- the honesty rule issue #1763 turned into a requirement.
    ``source`` names WHICH derivation path produced the band, because
    "the module default" means something different when nothing was declared
    than when a clamp could not produce a usable band:

    * ``declared`` -- the tweeter's declared ``measurement_band_hz``,
      possibly narrowed by the passband containment clamp, possibly raised
      by the HF-regime clamp (``hf_regime_clamped`` tells which).
    * ``undeclared_default`` -- no measurement band was threaded through, so
      ``DEFAULT_ECHO_BAND_HZ`` stands in (pre-PR-4 behaviour, unchanged).
    * ``clamp_degenerate_default`` -- the HF clamp would have left a band too
      narrow for the detector to resolve anything in (see
      :func:`_min_clamped_echo_band_width_hz`), so ``DEFAULT_ECHO_BAND_HZ``
      stands in instead.
    * ``passband_fallback`` -- the declared band sits entirely outside the
      composed passband, so the passband itself stands in.

    ``diagnostics`` carries the journal fields the flow emits, and is ``None``
    on the ordinary path where there is nothing to say.  They travel as data
    rather than as a log call because this module is side-effect-free (see the
    module docstring); the event NAME and its level stay with the flow, which
    picks them off ``source`` and ``hf_regime_clamped``.  Same shape as
    :class:`BoostExclusion` and :class:`CloudCombine`, for the same reason.
    """

    band_hz: tuple[float, float]
    source: str
    hf_regime_clamped: bool
    derived_lo_hz: float
    diagnostics: dict[str, Any] | None = None

    def disclosure(self) -> dict[str, Any]:
        """The JSON-native provenance block the pipeline payload carries.

        Deliberately does NOT repeat ``band_hz``: the payload already
        publishes the applied band as ``echo_band_hz``, and two copies of one
        pair is how they come to disagree.
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

    ``detect_echo``'s quefrency step is ``resolution_us = 1e6 / bandwidth``,
    and two of its gates are multiples of that step: the searched window's
    edge margin (``WINDOW_EDGE_MARGIN_STEPS``, one step above
    ``search_us[0]``) and -- independently of the window --
    ``assess_geometry``'s refusal to cluster any estimate whose ``tau_us``
    is below ``GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``. The geometry
    floor is the binding one, and once it reaches the TOP of the searched
    window no delay the detector is allowed to look for can be clustered at
    all, so the band cannot produce a geometry lock however good the room is:

        GEOMETRY_MIN_RESOLUTION_STEPS * 1e6 / DEFAULT_ECHO_SEARCH_US[1]
          = 3.0 * 1e6 / 800 us
          = 3750 Hz

    (The edge margin's own bound is 1.0 * 1e6 / (800 - 120) us => 1470 Hz,
    i.e. slacker, which is why the geometry floor is the one to read.
    ``DEFAULT_ECHO_SEARCH_US`` is the right window to read because this
    program's ``combine_positions`` call passes no ``echo_search_us``, so the
    default window is the one actually searched.)

    This dominates the detector's other width constraint,
    ``MIN_ECHO_BAND_BINS`` (16 bins of ``detect_echo``'s own FFT): that FFT
    is floored at 4096 points, so at this program's 48 kHz the coarsest bin
    spacing is 11.72 Hz and 16 bins need only 15 * 11.72 = 175.8 Hz -- 21x
    narrower than the bound above. One rule is therefore enough: a band that
    clears this floor clears the bin-count refusal too.

    Derived rather than hard-coded so a change to either detector constant
    moves this bound with it instead of leaving a stale literal behind.
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
    """The contract-derived echo/null analysis band (PR-4): the tweeter's
    declared ``measurement_band_hz``, replacing ``DEFAULT_ECHO_BAND_HZ``'s
    flat constant at this call site -- returned WITH its provenance (see
    :class:`_CloudEchoBand`).

    Falls back to ``DEFAULT_ECHO_BAND_HZ`` when the tweeter's measurement
    band was not threaded through (an older/incomplete confirmed profile) --
    that constant is the module's own long-standing default, not a new
    invention, and every existing corpus test that validated
    ``identify_interference_nulls`` against the S0 corpus did so at exactly
    this band (``S0_BAND_HZ`` in ``tests/test_interference_nulls.py``).

    **Containment (inherited PR-2/PR-6a constraint):** clamped to sit INSIDE
    ``signal_band_hz`` (the derived passband), never wider. A band that
    neither contains nor sits clear of the analysis band leaves
    ``detect_echo``'s signal-presence screen uncalibrated
    (``spatial_combine.BAND_BELOW_PASSBAND_MARGIN_DB``'s docstring: "What is
    NOT calibrated: a passband narrower than the analysis band, or
    overlapping it"). Since ``signal_band_hz`` is the union of BOTH roles'
    excitation bands (always at least as wide as one driver's own
    measurement window in the ordinary 2-way case -- the woofer's lower edge
    sits well below the tweeter's, and the tweeter's own excitation ceiling
    upper edge is never narrower than its measurement band, per
    ``resolve_driver_excitation_ceilings``'s "Band-edge asymmetry" rule),
    this clamp is a no-op for every declared contract exercised by this
    program's tests and only bites a genuinely malformed one.

    **HF regime (issue #1763):** when the contained lower edge sits below
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ`, it is RAISED to that floor and the
    clamp is disclosed -- the returned provenance, plus the WARNING event the
    flow emits from ``diagnostics`` (slug suffix
    ``cloud_echo_band_clamped_to_hf_regime``), so neither a journal reader nor
    a payload reader has to infer it from the band alone. The contract's
    upper edge is kept: the floor is a statement about where the detector's
    calibrations hold, not about how wide the driver's window is. See
    :data:`ECHO_BAND_HF_REGIME_FLOOR_HZ`'s own comment for the six-band
    deficit table behind the number, and for why PR-4's disclose-and-proceed
    design was replaced.

    **When the clamp cannot produce a usable band** -- the surviving width
    ``upper - floor`` is below :func:`_min_clamped_echo_band_width_hz` -- the
    band falls back to ``DEFAULT_ECHO_BAND_HZ`` with its own disclosure
    rather than to a stub the detector would refuse everything in. That trade
    is stated rather than glossed: the default is NOT re-clamped into the
    passband, so in this corner the band can sit outside a pathologically low
    passband and leave the signal-presence screen's deficit statistic
    uncalibrated. That is the lesser loss -- an uncontained band still runs
    both estimators, whereas a band too narrow to resolve any delay in the
    searched window makes every number downstream meaningless. It is also
    unreachable from any plausible contract: it needs
    ``min(declared_upper, passband_upper)`` below 7750 Hz, i.e. a "tweeter"
    (or a whole 2-way system) that is not swept into the top three octaves --
    the same malformed-contract family as the passband fallback below.
    """
    from jasper.audio_measurement.spatial_combine import DEFAULT_ECHO_BAND_HZ

    declared = tweeter_measurement_band_hz is not None
    band = tweeter_measurement_band_hz or DEFAULT_ECHO_BAND_HZ
    lo = max(float(band[0]), float(signal_band_hz[0]))
    hi = min(float(band[1]), float(signal_band_hz[1]))
    if lo >= hi:
        # A genuinely malformed declared contract -- the tweeter's own
        # measurement band sits entirely outside the composed passband.
        # Fall back to the passband itself rather than hand a caller an
        # inverted/degenerate pair that would raise deep inside
        # combine_positions with no context about why.
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
        # logged so a journal reader does not have to know that to read the
        # line.
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

    **No longer the same shape as ``_decimate_sum`` (issue #1858).** Before
    that fix this mirrored ``jasper.web.correction_crossover_v2._decimate_sum``
    exactly (floor-division stride, identity when already short enough) so
    the two persisted curve payloads read the same way to a consumer.
    ``_decimate_sum`` now block-averages instead, because its input
    (``conductor.measure_predicted_sum``) is the RAW, unsmoothed prediction
    and a stride over that aliases below ~500 Hz. This function's input,
    ``combined.power_mean_spec_db``, has already been through
    ``smooth_fractional_octave`` inside :func:`combine_positions` before it
    ever reaches here, so a plain stride over an already-smoothed curve does
    not reintroduce that failure mode -- the two callers start from
    differently-prepared curves, which is why one still strides and the
    other no longer does. ``freqs_hz`` and ``magnitude_db`` remain
    identity-shaped (floor-division stride) either way.
    """
    n = len(freqs_hz)
    step = max(1, n // CLOUD_CURVE_MAX_JSON_POINTS)
    return {
        "freqs_hz": [float(f) for f in freqs_hz[::step]],
        "magnitude_db": [float(m) for m in magnitude_db[::step]],
    }


def _geometry_guidance_copy(geometry: Mapping[str, Any]) -> str:
    """Plain-language "spread the mic further" guidance from a geometry
    verdict dict (:func:`cloud_geometry_verdict`'s own shape) -- the
    household-facing surface issue #1742 item 2 asked for. Recorded since
    PR-3b (the durable v2 state's ``cloud`` block, ``GEOMETRY_RETRY_POSITIONS``'s
    own comment). PR-4 carries this copy onto the envelope and `/state`
    (`crossover_v2_status_block`'s compact projection); no household-facing
    surface renders it yet (zero JS/asset changes in PR-4) -- PR-7 renders
    it.

    Softened, never suppressed, when ``thin_evidence`` -- and the softened
    copy names the qualitative floor ("the bare minimum of positions"),
    never a discrete number or a percentage, because thin_evidence is a
    cliff at an exact confident-estimate count, not a gradient
    (spatial_combine.GeometryLock's own docstring) -- naming the actual
    count would read as a gradient the instrument does not claim. Empty
    string when not locked -- nothing to say.
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
# Carve-out disclosure (owner decision 1, 2026-07-25; plan PR-6b)
#
# The owner's decision of record: identified interference nulls are excluded
# from spec evaluation AND from correction, the band's tolerance applies to the
# SURVIVING envelope, and "the report discloses 'EQ cannot fill these' with the
# numbers." ``evaluate_flat_spec`` already does the excluding -- the masked bins
# leave both the reference level and every band's deviation. What it does not
# do, and must not, is say WHY: it is a pure evaluator that takes a bool mask
# and holds no product policy (its own module docstring). So the "why" is
# assembled here, in the wiring layer that already holds the registry and the
# spec report side by side, next to ``_geometry_guidance_copy`` -- the other
# household-facing copy derived from a pipeline verdict.
#
# **This module owns the carve-out copy strings; PR-7 renders them.** One
# owner, so a chart callout and the envelope's expert disclosure cannot say
# different things about the same carved range.
# --------------------------------------------------------------------------- #

# Which honesty instrument carved a range. Snake_case and self-identifying,
# mirroring the vocabulary rule interference_nulls.py states for its own slugs.
CARVE_OUT_SOURCE_IDENTIFIED_NULL = "identified_null"
CARVE_OUT_SOURCE_POSITION_SCREEN = "position_screen"


def _format_carve_out_hz(hz: float) -> str:
    """One frequency as household copy — kHz at and above 1 kHz, Hz below.

    Deliberately NOT the ``f"{hz:.0f} Hz"`` form the envelope's flatness lines
    use: those quote a single worst bin, while these copy strings list several
    frequencies in one sentence, where five-digit Hz figures read as noise.
    """
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
    """The classification's own household sentence, or ``""`` for a
    classification this copy does not cover.

    **The ``position_invariant`` wording is load-bearing and pre-registered**
    (plan PR-1's classification vocabulary, PR-7's callout copy): a single
    session cannot separate "travels with the speaker" from "a path in the room
    that did not change while measuring", and the output must not claim it can
    — S0 separated them only by MOVING the speaker. So the copy names both and
    names the experiment that would tell them apart.

    No hardware noun appears here, in either branch. The classification is
    evidence about how a null behaved across a mic cloud; it is not evidence
    about what part of a speaker or room produced it, and naming one would be
    the device-taxonomy guess this program forbids in shipped copy (the JTS3
    rim-wave attribution is session knowledge, not measured general truth).
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

    The two honesty instruments are listed SEPARATELY rather than merged: a
    merged interval loses which instrument found it, and the registry's rows
    are the only ones carrying τ/r — the exclusion *reason of record*. Ranges
    from the two sources may overlap each other; that is reported as two rows
    (one per instrument's own evidence), not silently collapsed, because "both
    instruments flagged this" is a stronger statement than either alone.
    ``merged_excluded_bands_hz`` remains the merged view for anyone counting.

    A registry row's interval is the null's OWN ``f_lo_hz``/``f_hi_hz`` (its
    half-depth width), unclipped to any spec band — τ and r describe the whole
    null, so clipping the interval to a band edge would attach the numbers to a
    fragment of what was measured.

    Ordered by lower edge, then by source, so two rows starting at the same
    frequency come out in a stable order rather than an input-order one.
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

    ``""`` when nothing was carved in the band, mirroring
    :func:`_geometry_guidance_copy`'s "empty string when not locked — nothing
    to say" rule rather than rendering a "no interference found" sentence a
    reader could mistake for a measurement.

    The delay is quoted in **milliseconds** here because it is the one number
    that makes the sentence mean something to a household ("a delayed copy
    arrives 0.32 ms later"); τ stays in microseconds in the structured record,
    which is the registry's own unit and the one owner of it.
    """
    nulls = [r for r in records if r["source"] == CARVE_OUT_SOURCE_IDENTIFIED_NULL]
    screened = [r for r in records if r["source"] == CARVE_OUT_SOURCE_POSITION_SCREEN]
    sentences: list[str] = []
    if nulls:
        where = _join_carve_out_phrases(
            [_format_carve_out_hz(float(r["f_center_hz"])) for r in nulls]
        )
        # One ladder, one τ (IdentifiedNull.tau_us is "the same value on every
        # rung of one report" — its own docstring), so the first row's delay
        # describes them all.
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
        # "One range" rather than "1 range": this is prose, and the frequency
        # figures are the numerals a reader should be counting in it.
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

    Separated from :func:`_carve_out_disclosure_copy` rather than folded into
    it because the two registers have different readers and the plan puts τ/r
    behind a disclosure ("τ/r vocabulary lives in an expert disclosure, not the
    headline"). Both are produced here so a chart callout and the envelope's
    ``<details>`` cannot drift into saying different things.

    ``r`` is reported as the pair the registry actually holds — the
    time-domain and frequency-domain estimates — rather than one averaged
    figure, because their AGREEMENT is what admitted the null in the first
    place, and an average would hide it.
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

    Owner decision 1 (2026-07-25) in payload form. One entry per band of
    ``spec_report``, **always all of them, in the report's own order**, so a
    consumer can join to ``spec["bands"]`` by index or by ``band_hz`` and can
    render "nothing carved here" without having to infer it from an absence.

    A record is included in a band when its interval OVERLAPS the band's
    ``[graded_lo_hz, graded_hi_hz)`` span — the span actually graded, not the
    nominal row — so a null straddling a band edge appears under both bands it
    actually carves, and one sitting entirely outside the session's trusted
    range appears under none, because it removed no bin any verdict was taken
    from.

    **What this does NOT include: the gate's trusted-floor clamp.** Bins
    below the group's ``trusted_floor_hz`` also leave the spec evaluation,
    but they are not an interference verdict and are deliberately kept out of
    the honesty instruments' own accounting — the same separation
    ``_compact_cloud_status`` carries for exactly this reason. Since #2551
    that separation is structural rather than a convention the reader has to
    hold: the clamp moves each band's graded EDGE, so a bin outside the
    trusted range is not in the band to be excluded FROM. A band's
    ``n_excluded`` is therefore exactly what these records cover, and the
    clamps show up as the spec report's ``graded_lo_hz``/``graded_hi_hz``
    beside the nominal ``band_hz`` here rather than hiding inside a count.
    """
    records = _carve_out_records(null_report, screen_bands_hz)
    out: list[dict[str, Any]] = []
    for band in spec_report.bands:
        f_lo, f_hi = float(band.f_lo_hz), float(band.f_hi_hz)
        # Overlap is tested against the edges this band was GRADED between,
        # not its nominal row: a null outside the trusted range carved nothing
        # out of this band's grading, because those bins were never in it. The
        # UPPER edge matters as much as the lower one now that the top band's
        # follows the microphone-trust ceiling -- testing the nominal 16 kHz
        # there would drop every carve-out a 20 kHz-trusted session found above
        # it, leaving `n_excluded` counting bins with no reason attached. That
        # is what makes the equality claimed above ("n_excluded is exactly
        # what these records cover") true rather than approximate. `band_hz`
        # below stays the nominal pair, since it is the join key a consumer
        # uses against ``spec["bands"]`` — which carries `graded_lo_hz`
        # itself, so this payload does not copy it and cannot drift from it.
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

    Why the worst rather than a mean or the anchor's: the combined curve is a
    power mean ACROSS these positions, so a bin below any one position's
    reflection-gate floor is contaminated in the average by that position's
    truncated-window artifact (``gating.f_valid_floor_hz`` — the same
    quantity ``_analyze_verify``'s tracking band already clamps up to, W6.9
    forensics). Taking the highest floor is the only choice under which every
    graded bin is inside every contributing capture's validity.

    ``None`` (no position carried a finite, positive floor) means the lower
    edge could not be verified — NOT that it is zero. Callers disclose it as
    unknown and clamp nothing; see :func:`assemble_cloud_group_result`.
    """
    floors = [
        float(getattr(p.response, "validity_floor_hz", None) or 0.0)
        for p in positions
    ]
    usable = [f for f in floors if math.isfinite(f) and f > 0.0]
    return max(usable) if usable else None


def cloud_trusted_floor_hz(validity_floor_hz: float | None) -> float | None:
    """The group's TRUSTED floor (``2.5/T``) from its validity floor
    (``1/T``) — the number the flat spec is graded above (issue #2551).

    ``1/T`` is where a reflection-free window of ``T`` has one full cycle of
    resolution; ``2.5/T`` is where the gated magnitude is actually
    trustworthy, and the E4 gate-stability sweep is why the distinction is
    not academic — the 1-4 kHz band moved **2.1 dB** across 3/5/7/10 ms
    gates purely because part of it sat below the shorter windows' trusted
    floor, while everything above it held to <=0.006 dB
    (:data:`~jasper.audio_measurement.gating.TRUSTED_FLOOR_MULTIPLIER`).
    The gate's own delta probe already prices itself over this floor and
    refuses to grade below it; before #2551 the spec evaluator did not, so
    one capture was read by two graders against two honesty floors.

    Derived rather than plumbed, deliberately. Both floors come from the
    same window — ``f_trusted = 2.5 * f_valid`` exactly
    (:func:`~jasper.audio_measurement.gating.f_trusted_floor_hz` is that
    multiply) — and the multiplier is monotonic, so the trusted floor of the
    group's WORST validity floor is the worst of the positions' trusted
    floors. One input, one owner, and no caller that passes a validity floor
    can forget to pass the trusted one and silently grade lower.

    ``None`` in, ``None`` out; likewise for a non-finite or non-positive
    floor, which is "no floor was established" and never "a floor of zero".
    Callers clamp nothing then, and say so — see
    :func:`assemble_cloud_group_result`.
    """
    if validity_floor_hz is None:
        return None
    floor = float(validity_floor_hz)
    if not math.isfinite(floor) or floor <= 0.0:
        return None
    return TRUSTED_FLOOR_MULTIPLIER * floor


@dataclass(frozen=True)
class CloudGroupResult:
    """:func:`assemble_cloud_group_result`'s payload, plus the line a failure earns.

    ``diagnostics`` carries the journal fields the flow emits under
    ``event=correction.crossover_v2_cloud_pipeline_failed``, and is ``None``
    when there was nothing to say.  They travel as data rather than as a log
    call because this module is side-effect-free (see the module docstring);
    the event NAME stays with the flow, which owns it.  Same shape as
    :class:`CloudCombine`, for the same reason.
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
    """The wiring contract (issue #1742 item 4) -- THE single function that
    consumes the exclusion mask, ``geometry.locked``, and the null registry
    TOGETHER. No other code in this program may read
    ``combined.excluded``/``combined.geometry.locked`` and treat that as the
    honesty verdict on its own; doing so is reading the mask alone, the hole
    this item exists to close (see the plan doc's "Architecture" table: "the
    mask alone is a hole").

    Runs :func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    on ``combined`` at ``echo_band_hz``, unions its excluded bins with the
    combiner's own power-vs-median screen (``combined.excluded``), and
    evaluates :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`
    against the merged mask -- the plan's "merged honesty mask = screen ∪
    identified nulls" line, made executable.

    ``combined`` may be ``None`` (the group could not be combined at all --
    :func:`combine_cloud_positions`'s own honest "unknown") or a
    :class:`~jasper.audio_measurement.spatial_combine.CombinedResponse`.

    **The spec-curve SSOT (plan PR-5).** The ``spec`` report this builds is
    the ONE construction every spec-facing surface reads -- the flatness
    gauge, the observe ledger's spec-facing summary, `/state`, and the
    envelope all render ``flatness`` (:func:`~jasper.active_speaker.flat_spec.spec_flatness_gauge`
    of that same report) rather than deriving a number of their own. Nothing
    downstream re-evaluates the curve.

    **The carve-out disclosure (plan PR-6b, owner decision 1).** ``carve_outs``
    is :func:`carve_outs_by_band` of the SAME registry and the SAME spec report
    — per band, which ranges left this band's grading, in plain language, with
    τ/r behind an expert string. It is a third reading of one evaluation, never
    a second one: the bins are already gone from ``spec`` by the time this runs,
    and no verdict here can move. The tolerance table is untouched — the 8-16 kHz
    row still reads ±2.5 dB, applied to whatever survives the carve-out (the
    owner's decision was to disclose the carve-out, not to re-spec the band).

    **``echo_band_provenance`` (issue #1763) is how a payload reader tells a
    contract-derived band from a clamped one.** ``echo_band_hz`` publishes the
    band the detector actually ran on, which is necessary but not sufficient:
    a reader seeing ``[4000, 18000]`` cannot tell whether the driver declared
    that window or whether the HF-regime clamp raised a declared 2 kHz edge
    into it, and the difference is exactly the asterisk issue #1763 exists to
    make visible. :meth:`_CloudEchoBand.disclosure` supplies the block (its
    ``source`` / ``hf_regime_clamped`` / ``derived_lo_hz`` / ``floor_hz``);
    the session passes it alongside the band it came from. ``None`` when a
    caller did not state one — "not stated", never "not clamped", the same
    unknown-vs-zero rule ``validity_floor_hz`` follows below.

    **The spec is graded above the group's TRUSTED floor, not its validity
    floor** (issue #2551). ``validity_floor_hz`` is the group's own gated
    ``1/T`` (:func:`cloud_validity_floor_hz`); :func:`cloud_trusted_floor_hz`
    turns it into the ``2.5/T`` the gate's delta probe already refuses to
    grade below, and THAT is what
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` intersects
    every band's lower edge with -- the reference band's included, since a
    bin the gate cannot support must not be able to re-centre the target
    either. Both floors are published: ``validity_floor_hz`` for provenance,
    ``trusted_floor_hz`` as the number the verdicts were actually taken
    above. Three properties this deliberately keeps:

    * **The intersection is a band EDGE, not a mask entry.** A sub-floor bin
      is not in the band at all, so ``spec.n_excluded`` stays exactly the
      honesty instruments' own count (screen union identified nulls) and a
      gate artifact can never inflate it. Which edge each band was graded
      from is disclosed per band as ``graded_lo_hz``, delta-probe style,
      its upper edge as ``graded_hi_hz``, and the report echoes
      ``trusted_floor_hz``/``trusted_ceiling_hz``, the clamped
      ``reference_band_hz`` and the whole ``graded_band_hz`` on its face.
      ``merged_excluded_bands_hz`` is
      likewise untouched: ``excluded_interval_count`` on `/state` remains the
      "how much interference did we find" number.
    * **A band left entirely outside the trusted range is ``evaluable=False``, never
      ``passed=False``.** There is no evidence there, which is not a
      failure; ``graded_lo_hz >= graded_hi_hz`` is the tell that distinguishes
      it from a band the axis never reached.
      :attr:`~jasper.active_speaker.flat_spec.FlatSpecReport.overall_passed`
      still treats unevaluable as not-passed, so nothing is flattered by the
      distinction.
    * **A ``None`` floor or ceiling clamps NOTHING and is reported as
      ``None``.** The alternative -- withholding the whole gauge, which is
      what the retired per-capture ``_flatness_tracking`` did when a capture
      had no floor -- would throw away the evidence above an unverified lower
      edge, or below an unverified upper one.

    Regime, measured on the S0 main leg 2026-07-27, re-derived 2026-08-02
    (#2045) and re-derived again for #2551: **all ten** of that session's
    positions gate to a 142.857 Hz validity floor, i.e. a **357.14 Hz**
    trusted floor, which sits ABOVE the spec table's 250 Hz edge and
    therefore clamps 987 bins out of the low band. Before #2551 the
    evaluator was handed the 142.857 Hz number instead, which sits below
    250 Hz and changed no graded figure at all -- a clamp in name only, on a
    corpus whose worst deviation bins were beneath the floor its own gate
    disclosure printed. ``test_flat_spec_ssot`` pins both halves: that the
    positions no longer collapse a gate, and what intersecting at the
    trusted floor costs.

    **Clamping is not free, and it moves the headline in the flattering
    direction.** That is the mechanism's own behaviour and it is stated here
    rather than discovered later; measured on the S0 corpus at a 1777.8 Hz
    trusted floor supplied explicitly
    (``test_flat_spec_ssot.CLAMP_TRUSTED_FLOOR_HZ``, pinned by
    ``test_the_trusted_floor_clamp_costs_the_low_band``), clamping:

    * moves **987 bins** out of the 250 Hz-2 kHz band;
    * **re-centres the reference** -24.6035 -> -29.1532 dB (-4.5498 dB),
      because the reference is a power mean over the non-excluded low-mid
      band and the clamp removed the loud low end of it;
    * moves the HEADLINE ``max_db`` -11.5741 -> -7.0243 dB, i.e.
      **+4.5498 dB in the FLATTERING direction** -- exactly the reference
      shift, because the worst bin survives the clamp, so its deviation
      moves one-for-one with the reference. This is the first number the
      ledger line prints and it moves FURTHER than the RMS does;
    * takes the pooled RMS 5.7705 -> 2.7474 dB (-3.0231 dB);
    * **flips the 250 Hz-2 kHz band verdict**, -4.9174 dB (fail) ->
      -0.3677 dB (pass), since ``BandResult.passed`` is
      ``abs(max_deviation_db) <= tolerance_db``. Overall stays False here
      only because the other two bands still fail on their own.

    **The cost grew ~4.3x with the low-mid frame** (ADR-0194), and the
    mechanism is worth stating: the clamped 250-1777.8 Hz sliver is the same
    sliver, but it is now a much larger share of a much smaller reference
    pool (``SPEC_BANDS[0]`` alone rather than 250 Hz-8 kHz), so removing it
    moves the pooled zero four times as far. Narrowing the frame bought
    attribution and made this clamp's price higher, not lower.

    Direction is **response-shape dependent, not a property of the clamp**:
    on THIS corpus the removed region sat above the surviving reference, so
    dropping it lowered the reference and flattered every surviving
    deviation. A speaker whose sub-floor region is quiet would move the other
    way. Do not generalize the sign.

    None of that is the speaker improving -- it is the same speaker graded on
    fewer bins, which is exactly what the gauge's ``n_bins`` exists to keep
    visible (``ConvergenceResidual``'s own "a residual that fell because the
    denominator shrank is not convergence" rule). Its sibling ``n_excluded``
    reports a different thing and deliberately does not move here: the clamp
    is an edge, not a mask entry. One short gate in a group is therefore
    expensive by design, and the group takes the WORST position's floor.

    **Deferred alternative, recorded rather than dismissed:** the honest
    third option is per-position, per-bin validity masking INSIDE
    ``combine_positions`` -- mask each position's contribution below that
    position's OWN floor and combine the survivors, so nine good captures
    keep contributing at 500 Hz instead of one bad one costing the band. It
    is strictly better than a group-wide clamp and is out of scope here only
    because it is a ``spatial_combine`` signature and estimator change (the
    power mean would need per-bin weights), not a wiring one. Revisit
    trigger: a real session where one short gate meaningfully shrinks the
    graded band -- the S0 corpus is that evidence already now that the floor
    is the trusted one (357.14 Hz clears the table's 250 Hz edge on every
    position), so this is queued on measured grounds, not speculation.

    **Fail-soft, named, not absolute** (S4 review finding, 2026-07-26 --
    corrected from an earlier "any exception is caught" overclaim). Catches
    exactly ``(ValueError, TypeError, IndexError, AttributeError)`` --
    the documented raise surface of every function this calls
    (:func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
    and :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` both
    raise only ``ValueError`` on malformed input;
    :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`
    raises ``ValueError`` via ``zip(strict=True)`` on a length mismatch or
    ``IndexError`` on an out-of-bounds index; a malformed/incomplete
    ``combined``-like object raises ``TypeError``/``AttributeError`` reading
    its fields; :func:`carve_outs_by_band` adds no new family -- it reads
    already-built records by keys it set itself, and its only external reads
    are attribute lookups on the two reports and indexing/``float()`` on the
    screen intervals, i.e. ``AttributeError``/``IndexError``/``TypeError``/
    ``ValueError``). ``_run_cloud_pipeline`` relies on exactly this bounded set --
    a downstream DSP failure inside it is diagnostic/disclosure machinery,
    never a capture-accept gate, so this bounded family is caught and
    reported as ``available: False`` rather than surfacing to the caller.
    Any OTHER exception -- ``KeyError``/``RuntimeError``/``OSError`` (none
    observed on this call surface today; would indicate a genuine bug in a
    callee) or ``MemoryError``/``KeyboardInterrupt`` -- propagates
    uncaught, by design: it should reach the caller rather than silently
    become an honest-looking "unavailable".
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
        # NOTE: ``crossover_registry`` is deliberately absent from this union.
        # See its builder for why classification there may never become
        # gating.
        # The mask handed to the evaluator is EXACTLY what the honesty
        # instruments found. The gate's floor rides beside it as a band-edge
        # intersection instead (#2551), so ``n_excluded`` cannot conflate an
        # interference verdict with a short window — see this function's
        # docstring.
        trusted_floor_hz = cloud_trusted_floor_hz(validity_floor_hz)
        # The ceiling rides beside the floor for the same reason and from the
        # same caller: the spec may not grade above where the microphone is
        # trusted, which is also where the fitter was allowed to command and
        # the delta probe allowed to grade (#2649).
        spec_report = evaluate_flat_spec(
            combined.freqs_hz, combined.power_mean_spec_db, merged_mask,
            trusted_floor_hz=trusted_floor_hz,
            trusted_ceiling_hz=trusted_ceiling_hz,
        )
        # #2291/#2160: hand the LIVE report to a caller that needs the object
        # rather than the serialized copy below. ``evaluate_spec`` reads
        # ``overall_passed`` and each band's ``evaluable``/``passed``, which
        # ``to_dict`` flattens away, and the round's spec verdict must be the
        # SAME report this function already built — re-evaluating it from
        # ``combined`` in the session would be a second owner of the merged
        # honesty mask, which is exactly what this function exists to prevent.
        # A sink rather than a second return value because every other caller
        # (and every test) reads the dict, and widening the return type would
        # change all of them to serve one consumer.
        if graded_spec_sink is not None:
            # The curve, the mask, and the verdict as ONE record: decision 10's
            # blend correction reads all three, and this is the only place all
            # three exist together. Handing them over separately would let a
            # consumer pair a curve with a mask from a different evaluation.
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
            # #1967/#1867: the crossover region, ASKED. Classification only —
            # never unioned into any mask above. ``None`` when there is no
            # committed crossover to name a region with, or when the gating
            # band already reached it. See
            # :func:`_crossover_region_null_registry`.
            "null_registry_crossover_region": crossover_registry,
            "spec": spec_report.to_dict(),
            # PR-6b: owner decision 1's disclosure half — the SAME registry
            # and the SAME spec report above, re-read per band as "what was
            # carved out of this band's grading, and why". Not a second
            # evaluation: `evaluate_flat_spec` already removed these bins, and
            # nothing here can change a verdict.
            "carve_outs": carve_outs_by_band(
                spec_report, null_report, combined.excluded_bands_hz,
            ),
            # PR-5: the spec-facing gauge — a pure reduction of the SAME
            # ``spec`` report above, carried here so no downstream surface
            # has to (or may) derive its own. Byte-identical wherever it is
            # rendered, because there is one number, copied.
            "flatness": spec_flatness_gauge(spec_report).to_dict(),
            "validity_floor_hz": (
                float(validity_floor_hz)
                if validity_floor_hz is not None and math.isfinite(validity_floor_hz)
                else None
            ),
            # #2551: the floor the spec was actually graded above — 2.5x the
            # one directly above, and the same number the gate's own delta
            # probe prices itself over. Published beside its input rather
            # than in place of it, so a reader can see both the window's
            # resolution limit and its trust limit.
            "trusted_floor_hz": trusted_floor_hz,
            # The ceiling beside the floor, at the same level, because the two
            # are not symmetric in how reliably they are KNOWN: the floor comes
            # from this group's own gate and is always available, while the
            # ceiling is read off the bound candidate's mic tier and is
            # ``None`` on a pre-apply close that has no candidate yet. A
            # session can therefore grade its MEASURE group to 16 kHz and its
            # VERIFY group to 20 kHz, and a reader comparing the two `spec`
            # blocks across phases is comparing different spans. Publishing it
            # here makes that visible rather than leaving it to be inferred
            # from inside `spec.trusted_ceiling_hz`.
            "trusted_ceiling_hz": spec_report.trusted_ceiling_hz,
            "echo_band_hz": list(echo_band_hz),
            "echo_band_provenance": (
                dict(echo_band_provenance)
                if isinstance(echo_band_provenance, Mapping)
                else None
            ),
            # WHICH INSTRUMENT measured this group (flow-simplification §1.2).
            # ``None`` means unknown, never a guessed default — same
            # discipline as ``echo_band_provenance`` directly above, and for
            # the same reason: the two tiers make materially different claims,
            # so a reader that cannot tell them apart must say so.
            "tier": str(tier) or None,
            "curve": _decimate_curve_for_json(
                combined.freqs_hz, combined.power_mean_spec_db,
            ),
            # WO-1 (attribution plan §6, §11.1 A7): the MEMBERS behind every
            # aggregate above. The combiner has computed each position's
            # curve and echo diagnostic all along and this function used to
            # drop them, which is why P2 — the position-variance classifier
            # §5 calls a free probe — was not actually free, and why
            # ``clustered_fraction`` was the summary of a distribution nobody
            # could inspect. Serialization only: no new signal, no threshold,
            # no verdict. Never raises (see ``position_evidence_block``), so
            # it cannot turn a good group into a failed one.
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
