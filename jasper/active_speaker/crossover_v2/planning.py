# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ONE candidate, assembled — and what its linearization produced (#2291
Phase 5a-v(c)).

A sibling of :mod:`.programs`, :mod:`.priors`, :mod:`.spatial`,
:mod:`.candidates` and :mod:`.fc_sweep`.  :mod:`.candidates` owns the *values*
one build returns; :mod:`.fc_sweep` owns which corners are worth building at
all.  This one owns the build itself: the eligibility gate, the planner request
this candidate's own sections imply, the cloud evidence its envelope consumed,
and the assembly of the emitted
:class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverCandidate`.

**One corner, and it is the candidate's.**  :func:`plan_for_candidate` derives
the crossover corner from the sections the candidate is realized with and reads
no session Fc — the 2026-08-10 defect made unrepresentable rather than merely
fixed.  Nothing here can reach a conductor field, so there is no second corner
in scope to read even by accident.

**Ports, not patches.**  The two seams that are *behaviour the host owns* — the
per-candidate linearization plan and the cloud exclusion evidence — are injected
into :func:`build_candidate` as callables rather than called through a sibling
here, so a class- or instance-level substitution of
``CrossoverV2Session._plan_linearization`` (nine such sites across two suites
— six on the class, three on an instance) or ``_exclusion_evidence_json`` still
binds on production instead of addressing a name production no longer routes
through (issue #2354).
:func:`ineligible_reason` is the counter-case and follows :mod:`.fc_sweep`'s own
rule: nothing substitutes it, so :func:`build_candidate` calls it directly and
the conductor's delegate calls the same function — one derivation either way.

``exclusion_evidence`` is a port for the second reason that module names: it is
read AFTER the plan has run, and its answer must describe the group's CURRENT
cloud result (a retake's re-close refreshes it — issue #1872).  A dict captured
at wiring time would file an earlier close's registry beside this candidate.

**Disclosure goes back through the host, at the moment it happens** — and this
module's one disclosure needs a traceback, which is why it carries a third
record type.  See :class:`FailureRecord`.

**One exception: the port's own failure (#2361).**  A ``journal`` that raises
being handed a record cannot also be asked to carry the news that it raised —
:func:`build_candidate` guards that one call and says the drop through this
module's OWN logger instead, the one channel a broken port cannot also take
down.  Everywhere else this module writes nothing and logs nothing itself,
exactly like :mod:`.intervention` and :mod:`.fc_sweep`; :mod:`.coordinator`
states the identical exception for its own seams.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.  Two consequences
are visible in the signatures below.  The conductor keeps the preconditions
whose refusal vocabulary is its own — a declared protection map on an uncomposed
capture, and an analysis carrying no candidate, which raises
``CrossoverV2FlowError`` — because those are facts about the *session*, not
about the candidate being built.  And the host keeps the
``journal_dropped`` notice that follows :func:`plan_for_candidate`: it reports
on the journal port itself, so it cannot be said through it, and
``test_crossover_v2_planner_wiring`` pins that it still reaches the flow's own
logger.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from jasper.audio_measurement.program_analysis import ALIGNMENT_OK, ProgramAnalysis
from jasper.log_event import log_event

from ..branch_chain import CrossoverSection, sections_by_role
from .candidates import CloudFitEvidence, LinearizationState
from .contracts import CandidateAcousticContext
from .intervention import (
    LINEARIZATION_MIN_PAIRED_OCCURRENCES,
    LinearizationPlan,
    driver_response_by_role,
    request_from_analysis,
)
from .journey import PHASE_CLOUD_MEASURE, PHASE_MEASURE

#: This module's own logger, reached for in exactly one place — see the
#: module docstring's "One exception" paragraph and :func:`build_candidate`'s
#: guard. Not a general-purpose capability: every other line this module
#: could say still goes back through an injected port.
logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_FIT_FAILED",
    "EVENT_FIT_FAILED_JOURNAL_DROPPED",
    "FailureRecord",
    "alignment_to_candidate_fields",
    "analysis_json",
    "build_candidate",
    "exclusion_evidence_json",
    "ineligible_reason",
    "plan_for_candidate",
]

#: The one event name this module discloses through its ``journal`` port.  A
#: named constant rather than a literal for the reason :mod:`.accountability`
#: gives: a journal name is a grep contract, so a rename should be visible as
#: one.
EVENT_FIT_FAILED = "correction.crossover_v2_linearization_fit_failed"

#: Said INSTEAD of :data:`EVENT_FIT_FAILED` when the ``journal`` port itself
#: raises trying to carry that record (#2361) — see :func:`build_candidate`'s
#: guard. A second, independent event name rather than a field on the
#: dropped record: the broken port could not have carried that field either,
#: so this one goes out the module's own logger (above), not the port.
#: Deliberately NOT ``EVENT_FIT_FAILED`` plus a ``"_journal_dropped"`` suffix
#: applied naively: that reads as ``..._fit_failed_journal_dropped``, which
#: contains :data:`EVENT_FIT_FAILED`'s own value as a literal substring —
#: three pre-existing consumers match that event by bare substring
#: (``"event=correction..." in caplog.text``), and one pairs it with an
#: adjacent ``reason=ValueError`` check that a same-shaped drop line could
#: satisfy too. This value keeps the ``journal_dropped`` token (so
#: ``grep journal_dropped`` still catches the whole vocabulary) while staying
#: substring-clean of :data:`EVENT_FIT_FAILED` in both directions.
EVENT_FIT_FAILED_JOURNAL_DROPPED = (
    "correction.crossover_v2_linearization_fit_journal_dropped"
)

#: The exceptions :func:`build_candidate`'s own ``journal`` call is guarded
#: against — the same 8-exception family as ``intervention._PORT_ERRORS``
#: and ``fc_sweep._SWEEP_ERRORS``, and a superset of
#: ``coordinator._SEAM_ERRORS`` (whose 6 omit ``ArithmeticError`` and
#: ``IndexError``). Enumerated rather than a blind ``except Exception``
#: (ruff BLE, and the repository's frozen broad-except budget). ``OSError``
#: is in the set for the same reason it is in theirs: the port is a logging
#: delegate, and a handler with nowhere to write raises exactly that.
_JOURNAL_ERRORS = (
    ArithmeticError,
    AttributeError,
    IndexError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


@dataclass(frozen=True)
class FailureRecord:
    """One log line this module would have emitted from inside an ``except``.

    The third disclosure record type, beside
    :class:`~jasper.active_speaker.crossover_v2.intervention.JournalRecord`
    (the planner's, whose payload becomes JSON) and
    :class:`~jasper.active_speaker.crossover_v2.accountability.GateRecord`
    (the gate's, whose logfmt bytes are the contract).  All three expose
    ``event`` / ``level`` / ``fields``, which is all the host's journal
    delegate needs; this one adds a fourth attribute, and the fourth attribute
    is the whole reason it exists.

    **A traceback cannot be replayed from a plain record.**  The SF2 degrade
    line is emitted with the failure's stack, and ``logging`` resolves
    ``exc_info=True`` against ``sys.exc_info()`` *at the moment the LogRecord
    is created*.  A record carrying only ``(event, fields, level)`` and logged
    after the handler exits therefore renders no traceback at all — the
    disclosure survives and the diagnosis does not.  Carrying the caught
    exception itself is what makes the deferred emission byte-identical:
    ``logging`` expands a ``BaseException`` to
    ``(type(e), e, e.__traceback__)``, which is exactly what ``sys.exc_info()``
    returns inside the handler.

    :attr:`exc_info` is therefore the live exception, and it is held rather
    than copied — an exception is not a payload and detaching one would throw
    away the traceback that is the point.  The sibling record types have no
    such attribute and need none; the host's delegate reads it structurally and
    passes ``False`` when it is absent, which keeps every non-exception line
    exactly the plain ``logger.log`` call it was.

    ``fields`` is held EXACTLY as built, for :class:`GateRecord`'s reason: this
    payload's logfmt bytes are the contract and every value in it is a scalar
    computed here.
    """

    event: str
    fields: Mapping[str, Any]
    level: int = logging.WARNING
    #: The caught exception, or ``None`` for a record with no stack to carry.
    #: ``repr=False`` because a traceback in a dataclass repr is noise in every
    #: log line and test failure that renders one.
    exc_info: BaseException | None = field(default=None, repr=False)


def alignment_to_candidate_fields(
    analysis: ProgramAnalysis, *, woofer_role: str, tweeter_role: str,
) -> tuple[float | None, str | None, str | None]:
    """Map a MEASURE ``AlignmentEstimate`` to ``(delay_us, delay_role, polarity)``.

    Honours the analysis sign contract (design §5.6.5): its ``delay_us`` is
    ``(D_woofer − D_tweeter)``, so **positive ⇒ the tweeter arrived earlier and
    the tweeter branch is delayed**; negative ⇒ the woofer is delayed. The W4
    :class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverAlignment`
    wants a non-negative magnitude + the delayed role, so the sign is folded into
    the role choice. Returns ``(None, None, None)`` when there is no trustworthy
    alignment (missing, or the estimator clamped at the search-window edge), so
    the candidate falls back to a trims-only apply.
    """
    from jasper.active_speaker.crossover_alignment import (
        POLARITY_INVERT,
        POLARITY_KEEP,
    )

    est = analysis.alignment
    if est is None or est.status != ALIGNMENT_OK:
        return None, None, None
    delay_us = float(est.delay_us)
    if delay_us >= 0.0:
        role, magnitude = tweeter_role, delay_us
    else:
        role, magnitude = woofer_role, -delay_us
    polarity = POLARITY_INVERT if est.polarity == "inverted" else POLARITY_KEEP
    return magnitude, role, polarity


def analysis_json(analysis: ProgramAnalysis) -> dict[str, Any]:
    """Compact JSON-safe evidence core for the measured candidate fingerprint.

    The W4 candidate freezes ``analysis`` as exact JSON data, so only the
    scalar verdicts travel — never the numpy response arrays. Enough to identify
    the exact measurement that authorized the candidate (§5.6/§5.8).
    """
    drift = analysis.drift
    align = analysis.alignment
    cand = analysis.candidate
    return {
        "schema_version": 1,
        "kind": "jts_program_analysis_evidence",
        "program_id": analysis.program_id,
        "epsilon_ppm": round(float(drift.epsilon_ppm), 3) if drift else None,
        "glitch_detected": bool(analysis.glitch_detected),
        "delay_us": round(float(align.delay_us), 3) if align else None,
        "alignment_seed_delay_us": (
            round(float(align.seed_delay_us), 3)
            if align and align.seed_delay_us is not None else None
        ),
        "polarity": align.polarity if align else None,
        "alignment_confidence": round(float(align.confidence), 4) if align else None,
        "alignment_confidence_source": align.confidence_source if align else None,
        "trim_db": (
            {k: round(float(v), 4) for k, v in cand.trim_db.items()} if cand else None
        ),
        # #1667: the band-average seed trim_db's ripple-optimal solve started
        # from — evidence only, so replay/forensics can always see both even
        # when the applied trim_db above coincides with it (the sanity-guard
        # fallback path).
        "trim_band_average_db": (
            {k: round(float(v), 4) for k, v in cand.trim_band_average_db.items()}
            if cand and cand.trim_band_average_db is not None else None
        ),
        "predicted_ripple_db": (
            round(float(cand.predicted_ripple_db), 4) if cand else None
        ),
        "alignment_seed_ripple_db": (
            round(float(cand.alignment_seed_ripple_db), 4)
            if cand and cand.alignment_seed_ripple_db is not None else None
        ),
        "flatness_improvement_db": (
            round(float(cand.flatness_improvement_db), 4)
            if cand and cand.flatness_improvement_db is not None else None
        ),
        "anchor_delay_us": (
            round(float(cand.anchor_delay_us), 3)
            if cand and cand.anchor_delay_us is not None else None
        ),
        "snap_delta_us": (
            round(float(cand.snap_delta_us), 3)
            if cand and cand.snap_delta_us is not None else None
        ),
        "snap_found": bool(cand.snap_found) if cand else None,
    }


def ineligible_reason(
    analysis: ProgramAnalysis, *, woofer_role: str, tweeter_role: str,
) -> str | None:
    """HARD GATE for the Layer-1a fit path, as a named reason or ``None``.

    Eligible means a reference-tier mic AND both drivers paired
    N >= :data:`~jasper.active_speaker.crossover_v2.intervention.LINEARIZATION_MIN_PAIRED_OCCURRENCES`
    in-capture occurrences. Anything else falls back to the plain trims-only
    candidate, byte-identical to the pre-PR-C path.

    **Returns the reason rather than stamping it** (#2291 Phase 2b). It used to
    answer ``bool`` and write ``self._last_linearization_outcome`` on the way
    out, which made the caller's own outcome depend on a field it never named —
    the same implicit-return shape the planner extraction removed one layer up.
    The caller now holds the answer as a value and puts it on the candidate
    itself.
    """
    if analysis.mic_tier != "reference":
        return "ineligible_mic_tier"
    woofer_resp = driver_response_by_role(analysis, woofer_role)
    tweeter_resp = driver_response_by_role(analysis, tweeter_role)
    if woofer_resp is None or tweeter_resp is None:
        return "ineligible_repeats"
    woofer_n = 1 + len(woofer_resp.repeat_responses)
    tweeter_n = 1 + len(tweeter_resp.repeat_responses)
    if (
        woofer_n >= LINEARIZATION_MIN_PAIRED_OCCURRENCES
        and tweeter_n >= LINEARIZATION_MIN_PAIRED_OCCURRENCES
    ):
        return None
    return "ineligible_repeats"


def exclusion_evidence_json(
    cloud: CloudFitEvidence, *, cloud_result: Mapping[str, Any],
) -> dict[str, Any]:
    """The fit's cloud inputs, as the candidate's exclusion reason of record.

    Everything the two cloud envelope terms actually consumed, plus the
    registry that justifies the intervals — enough that a reader holding
    only ``candidate.json`` can re-derive ``spatial_exclusion_limit`` and
    ``position_stability_limit`` and see WHY a band went uncorrected.
    ``cloud_result`` is this group's own pipeline result, which the caller must
    read at CALL time — always its CURRENT value, refreshed on every close
    including a retake's re-close (issue #1872) — and the registry inside it is
    serialized by the host's ``_null_registry_to_dict``, the one owner of that
    shape, so the candidate's copy always describes the cloud actually retained
    at confirm time. ``cloud_measure.json``'s own copy can lag it: the
    evidence store's ``publish_cloud`` write is a per-phase SINGLETON
    (see ``_run_cloud_pipeline``'s own guard), so a retake landing
    between the group's first close and the household's confirm leaves
    the PERSISTED file describing an earlier cloud than the candidate
    filed beside it. Accepted scope, not a defect this function owns: the
    evidence artifact is forensic, the candidate is the product, and
    #1872 is explicit that the store's write-once behavior — first
    artifact stands — is by design.

    ``band_spread`` is carried as the plain per-band numbers rather than
    the dataclass: this is persisted JSON, and the two fields the term
    reads (``sigma_db`` and the band edges it applies over) are the two a
    reader needs to check it. ``max_sigma_db`` rides along because
    ``position_stability_limit``'s docstring turns on the distinction
    between the two spreads, and a reader auditing the choice needs to see
    the number that was NOT used.

    ``validity_floor_hz`` and ``gated_spec_curve`` (room-correction regime
    plan RC1, issue #1787) ride here for a different consumer: the room
    layer. Both previously existed ONLY in the retention-prunable session
    bundle and the clearable v2 flow state, so once a bundle aged out the
    room layer could not tell where this speaker's gated measurement stops
    being trustworthy, nor what the speaker's own gated response is —
    which is exactly what Tier B residual correction must subtract to
    avoid re-flattening voicing the speaker layer already set. Carrying
    them on the candidate makes them travel with the correction they
    justify, and (because a non-empty ``exclusion_evidence`` is
    fingerprinted — see :meth:`MeasuredCrossoverCandidate._core`) makes
    them tamper-evident for free. Both are copied verbatim from this
    group's own CURRENT pipeline result — the same field
    ``cloud_measure.json`` was seeded from at publish time, but see the
    null-registry paragraph above for why a retake after that publish can
    leave the persisted file a close behind the candidate, and why that
    gap is accepted rather than fixed here.

    Cost, stated plainly: ``gated_spec_curve`` duplicates the already-
    decimated cloud curve (<=512 points, two float arrays), which adds
    roughly **15-20 KB of JSON per candidate**. That is a deliberate
    trade — the curve is small, bounded, and written once per commission,
    whereas the alternative (re-reading it from the session bundle) is
    exactly the retention-prunable dependency this extension exists to
    remove. If the curve ever grows unbounded, decimate at this boundary
    rather than dropping the field.
    """
    registry = cloud_result.get("null_registry")
    floor = cloud_result.get("validity_floor_hz")
    curve = cloud_result.get("curve")
    return {
        "phase": PHASE_CLOUD_MEASURE,
        "excluded_bands_hz": [list(band) for band in cloud.excluded_bands_hz],
        "n_positions": cloud.n_positions,
        "band_spread": [
            {
                "center_hz": float(band.center_hz),
                "f_lo": float(band.f_lo),
                "f_hi": float(band.f_hi),
                "sigma_db": float(band.sigma_db),
                "max_sigma_db": float(band.max_sigma_db),
                "n_bins": int(band.n_bins),
            }
            for band in cloud.band_spread
        ],
        "null_registry": dict(registry) if isinstance(registry, Mapping) else {},
        # None is a real, load-bearing value here: "the floor is
        # unverified", never "the floor is 0 Hz" (see
        # cloud_validity_floor_hz). The reader must treat it as absent.
        "validity_floor_hz": (
            float(floor)
            if isinstance(floor, (int, float)) and math.isfinite(float(floor))
            else None
        ),
        "gated_spec_curve": (
            {
                "freqs_hz": [float(v) for v in curve.get("freqs_hz", ())],
                "magnitude_db": [float(v) for v in curve.get("magnitude_db", ())],
            }
            if isinstance(curve, Mapping)
            else {}
        ),
    }


def plan_for_candidate(
    analysis: ProgramAnalysis,
    cand: Any,
    cloud: CloudFitEvidence | None,
    *,
    candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
    preset: Any,
    program_for_phase: Callable[[str], Any],
    woofer_role: str,
    tweeter_role: str,
    driver_class_by_role: Mapping[str, str],
    post_apply_verifies: bool,
    cloud_phase_planned: bool,
    plan_linearization: Callable[..., LinearizationPlan],
    journal: Callable[[Any], None] | None = None,
) -> LinearizationPlan:
    """Assemble ONE candidate's planner request and run the pure planner.

    This function is the whole of what the host still owns about
    linearization: which measurement objects become which named planner
    input. The prescription itself — the σ policy, the fit, the anchored
    give-back, the ripple re-solve, the trim decision and the headroom
    charge — lives in
    :func:`~jasper.active_speaker.crossover_v2.intervention.plan_linearization`
    and is reached identically from both candidate paths (#2291 Phase 2b).

    **One corner, and it is the candidate's.** The context is built from
    the sections this candidate is realized with — ``candidate_sections``
    for a swept corner, ``preset``'s own crossover regions for the configured
    one — and
    :class:`~jasper.active_speaker.crossover_v2.contracts.CandidateAcousticContext`
    derives the corner FROM them. The conductor's ``_fc_hz`` is not read, and
    there is no second corner in scope for the planner to read either — this
    module cannot reach one: that is the 2026-08-10 defect made
    unrepresentable rather than merely fixed.

    A split or empty section set raises
    (``CandidateFcDisagreementError`` / ``NoCrossoverSectionsError``, both
    ``ValueError`` subclasses), which :func:`build_candidate`'s SF2 arm
    degrades to the trims-only lane. Fail-closed on purpose: a candidate
    whose own preset names no crossover has no crossover to plan for, and
    guessing one from the session would be the defect wearing a new hat.

    Only called after :func:`ineligible_reason` returns ``None`` — the planner
    assumes eligibility and does not re-check.

    ``program_for_phase`` is the host's own "what does this session play"
    seam, injected rather than resolved to a program by the caller because it
    can raise (before the CHECK gain solve there is no MEASURE program) and
    must do so AFTER the section set has been judged: the two section refusals
    are the more specific answer.

    ``plan_linearization`` is the pure planner itself, injected for the reason
    :mod:`.fc_sweep` injects ``select_fc``: three sites in
    ``test_crossover_v2_planner_wiring`` substitute the flow module's own
    ``plan_linearization`` name by string, and importing it here instead would
    leave those patches addressing a name production no longer routes through
    (#2354).  It is deliberately NOT also imported into this module, so there
    is no second binding of that name for a later edit to call by mistake.

    ``journal`` is handed straight to the planner's own disclosure port.  The
    ``journal_dropped`` notice the returned plan may carry is the HOST's to
    say and is deliberately not said here: it reports on that very port, so
    routing it through the port would lose it exactly when it matters (a host
    formatter that throws on every record throws on the notice too), and
    ``test_crossover_v2_planner_wiring`` pins that it reaches the flow's own
    logger.
    """
    sections = (
        {role: tuple(regions) for role, regions in candidate_sections.items()}
        if candidate_sections is not None
        else sections_by_role(
            getattr(preset, "crossover_regions", ()) or ()
        )
    )
    context = CandidateAcousticContext.from_sections(sections)
    measure_program = program_for_phase(PHASE_MEASURE)
    seg_w = measure_program.segment("sweep_w")
    seg_t = measure_program.segment("sweep_t")
    # ProgramSegment.f1_hz/f2_hz are typed float | None (the general
    # ProgramSegment shape also covers non-stimulus/silence segments);
    # __post_init__ guarantees a KIND_SWEEP stimulus segment (which
    # "sweep_w"/"sweep_t" always are) never has either as None. Narrow
    # explicitly for mypy and as a defensive invariant check.
    assert seg_w.f1_hz is not None and seg_w.f2_hz is not None
    assert seg_t.f1_hz is not None and seg_t.f2_hz is not None
    request = request_from_analysis(
        analysis, cand,
        context=context,
        woofer_role=woofer_role,
        tweeter_role=tweeter_role,
        excited_band_hz={
            woofer_role: (seg_w.f1_hz, seg_w.f2_hz),
            tweeter_role: (seg_t.f1_hz, seg_t.f2_hz),
        },
        driver_class_by_role=driver_class_by_role,
        # Boost permission's ONE necessary condition, and the clause that
        # tells "no cloud by design" (R15's driver-only path) apart from
        # "a cloud was planned and lost". Both are the host's to answer —
        # the planner cannot see a session's phase list.
        post_apply_verifies=post_apply_verifies,
        cloud_phase_planned=cloud_phase_planned,
        cloud=cloud,
    )
    return plan_linearization(request, journal=journal)


def build_candidate(
    analysis: ProgramAnalysis,
    cand: Any,
    cloud: CloudFitEvidence | None = None,
    *,
    candidate_sections: Mapping[str, Sequence[CrossoverSection]] | None = None,
    source_preset: Any,
    woofer_role: str,
    tweeter_role: str,
    plan: Callable[..., LinearizationPlan],
    exclusion_evidence: Callable[[CloudFitEvidence], Mapping[str, Any]],
    journal: Callable[[Any], None],
) -> tuple[Any, LinearizationState]:
    """Build one candidate, and return what its linearization produced.

    The state is RETURNED rather than stashed (#2291 Phase 2b). It used to
    reach the accountability gate, the VERIFY prior and the proposal as
    seven ``self._last_*`` fields, which is why the Fc sweep had to
    snapshot and restore them around every candidate — see
    :class:`~jasper.active_speaker.crossover_v2.candidates.LinearizationState`.
    Callers thread one value instead, so a state can only ever describe the
    candidate returned beside it.

    ``cand`` is ``analysis.candidate``, passed rather than re-read: the host
    has already refused an analysis carrying none, under its own error
    vocabulary, and re-deriving it here would give this function a second
    opinion about a fact the caller already settled.

    ``plan`` and ``exclusion_evidence`` are ports; ``journal`` is the
    disclosure port the SF2 arm below says its one line through.  See the
    module docstring for why each is injected rather than reached for.

    ``journal`` is REQUIRED (#2361): the sole production caller always
    supplies it, and a default that is only correct because today's caller
    always supplies the argument is the permissive-default trap
    :class:`~.candidates.CloudFitEvidence`'s own docstring names — a defect
    waiting for a caller that does not. A ``journal`` that RAISES stays
    safe either way: see the guard below.
    """
    from jasper.active_speaker.measured_crossover_candidate import (
        MeasuredCrossoverAlignment,
        MeasuredCrossoverCandidate,
    )

    delay_us, delay_role, polarity = alignment_to_candidate_fields(
        analysis, woofer_role=woofer_role, tweeter_role=tweeter_role,
    )
    alignment = (
        MeasuredCrossoverAlignment(
            delay_us=delay_us, delay_role=delay_role, polarity=polarity,
        )
        if delay_role is not None
        else MeasuredCrossoverAlignment()
    )

    # Layer-1a driver linearization (#1668 PR-C). HARD GATE: reference-tier
    # mic AND both drivers paired N>=3 — anything else is byte-identical
    # to the pre-PR-C trims-only path (analysis.candidate.trim_db, empty
    # linearization dict). See ineligible_reason / plan_for_candidate.
    role_attenuations_db: Mapping[str, float] = dict(cand.trim_db)
    linearization: Mapping[str, Any] = {}
    ineligible = ineligible_reason(
        analysis, woofer_role=woofer_role, tweeter_role=tweeter_role,
    )
    state = LinearizationState(outcome=ineligible or "")
    if ineligible is None:
        try:
            fit = plan(
                analysis, cand, cloud, candidate_sections=candidate_sections,
            )
        except (
            ArithmeticError, AttributeError, RuntimeError, TypeError, ValueError,
            KeyError, IndexError,
        ) as exc:
            # SF2 (adversarial review, 2026-07-24): the fit path is
            # strictly additive — an eligible speaker with a bug in the
            # (still-young) fit engine must degrade EXACTLY to the
            # ineligible path, never fail the whole MEASURE accept.
            # Mirrors _safe_log_diag's "never let enrichment logic break
            # the primary path" posture, one layer earlier (this guards
            # the candidate build itself, not just its diagnostic log
            # line). The caught set matches _safe_log_diag's own
            # (attribute/key/index/type/value access on structured
            # data), extended with ArithmeticError since this call site
            # does floating-point curve fitting (division, log,
            # exponentiation), not plain field extraction, and with
            # RuntimeError because linearization_fit.fit_driver_linearization
            # (N1, this same review) raises exactly that on its own
            # cut-only invariant violation — without it here, N1's safety
            # net would escape SF2's and crash this accept instead of
            # degrading to it. Since #2291 Phase 2b it also catches the
            # planner's own typed refusals — ``NoCrossoverSectionsError``
            # for a candidate preset naming no crossover at all, and
            # ``CandidateFcDisagreementError`` for a section set naming two
            # corners — both ``CrossoverV2ContractError``, itself a
            # ``ValueError``. A prescription cannot be planned for a
            # crossover the candidate does not describe, and degrading to
            # the trims-only lane is the fail-closed answer: the household
            # still gets its measured trims, and nothing is fitted toward a
            # corner nobody committed to.
            #
            # Said HERE, inside the handler, and carrying the exception
            # itself: see :class:`FailureRecord` for why a record built
            # after the block cannot render this traceback.
            #
            # #2361: the port call itself is guarded IN TURN, mirroring
            # intervention.py:936-942 (the planner's own port guard) and the
            # identical exception coordinator.py states for its own seams.
            # Probed directly, an unguarded OSError/RuntimeError/ValueError
            # from ``journal`` PROPAGATED here and skipped the ``return``
            # below — the candidate build failing because its OWN disclosure
            # of an unrelated fit failure broke. Unlike the planner's own
            # ``emit()``, there is no return-value slot to carry the drop on
            # here: this function's product IS the candidate, and
            # LinearizationState/MeasuredCrossoverCandidate have no
            # journal_dropped-shaped field to widen for a loss that is
            # unreachable in production today (both installed handlers guard
            # their own ``emit``). So the drop goes out this module's own
            # logger instead — the one channel a broken journal port cannot
            # also take down.
            try:
                journal(FailureRecord(
                    EVENT_FIT_FAILED,
                    {"reason": type(exc).__name__},
                    logging.WARNING,
                    exc,
                ))
            except _JOURNAL_ERRORS as port_exc:
                log_event(
                    logger, EVENT_FIT_FAILED_JOURNAL_DROPPED,
                    level=logging.WARNING,
                    dropped_event=EVENT_FIT_FAILED,
                    reason=type(port_exc).__name__,
                    exc_info=True,
                )
            role_attenuations_db = dict(cand.trim_db)
            linearization = {}
            # PR-L4 item 1: a fit that raised part-way may already have
            # produced a partial verdict, and none of it survives — a
            # verdict about branches this candidate no longer carries is
            # worse than no verdict, because the accountability gate would
            # grade the wrong thing. A fresh state IS that clearing, and it
            # covers the linearized VERIFY prior too, which the field-based
            # degrade left behind.
            state = LinearizationState(outcome="fit_failed")
        else:
            role_attenuations_db = dict(fit.role_attenuations_db)
            linearization = dict(fit.linearization)
            state = LinearizationState.from_plan(fit)

    return MeasuredCrossoverCandidate(
        program_id=analysis.program_id,
        analysis=analysis_json(analysis),
        source_preset=source_preset,
        role_attenuations_db=role_attenuations_db,
        alignment=alignment,
        linearization=linearization,
        # The exclusion reason of record (plan PR-6b). Empty — the
        # pre-move shape — whenever no cloud evidence reached the fit,
        # INCLUDING when the fit itself failed above: a record of what the
        # envelope consumed must not ride a candidate whose corrections
        # came from the trims-only fallback instead.
        exclusion_evidence=(
            exclusion_evidence(cloud)
            if cloud is not None and linearization
            else {}
        ),
        # Gauge fix (2026-07-24): the single writer's own verdict,
        # stamped verbatim onto the candidate at the exact moment it
        # reaches its final value for this attempt — see
        # MeasuredCrossoverCandidate.linearization_outcome's own
        # docstring for why this module never re-derives it. Since #2291
        # Phase 2b the verdict is this build's own returned state rather
        # than a conductor field, so the candidate and the state beside it
        # cannot describe different builds.
        linearization_outcome=state.outcome,
    ), state
