# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The promotion paths — verdicts the flow already computed become findings.

``docs/historical/attribution-stage-plan.md`` §3.1: "The excluded-band tau records are
the embryo: [attribution] promotes them from 'reason to refuse EQ' to findings
with mechanism and fix class attached."

That is exactly and only what this module does, and it now does it for **two**
records rather than one. Both are promotions in the same strict sense: the
numbers already exist, computed by shipped instruments for their own purposes,
and promotion attaches the three things a finding adds — a ``mechanism``, a
``fix_class``, and a ``confidence`` tier with the probe that would raise it.

* :func:`promote_carve_outs` — the excluded-band records, each already
  carrying its band, its tau, its r, its depth, its position-variance
  classification, and its household sentence.
* :func:`promote_level_frame_disagreement` — the level-frame gate's own
  comparisons, banked when EITHER has something to report: the two estimators
  disagreeing (the owner's 2026-07-30 ruling on #1866), or the committed pair's
  realized levels landing further apart than the tolerance (doctrine deviation
  (i), which turned that second one from a refusal into a finding). The record's
  ``reason`` says which, and the household sentence and fix class follow it.

**Neither is a detector** (that is WO-4). No signal is analysed here, no
threshold is applied, no classification is computed. Every number comes from a
record the shipped pipeline produced; promotion is a translation, and it can
only ever name a mechanism some shipped instrument already decided.

**Three rules the plan binds the carve-out path to, each pinned by test:**

1. **Every promoted finding is ``unsure``.** §5: "Any finding whose only
   support is P2 stays ``unsure`` with P4 as its recommended probe." The cloud
   is a P2 instrument — the shipped ``interference_nulls`` docstring says so
   itself, that position-invariance within one session "is consistent with an
   origin that travels with the speaker **or** with a path through the room
   that did not change while the session ran, and a single session cannot
   separate the two". P4 (rotation) is the adjudicator. So a promoted finding
   is never ``likely`` and never ``confident``, no matter how clean the tau.
2. **``eq`` is never routed.** §3.3's hard rule: ``eq`` is never the routed
   class for a position-variant null or a source-fixed interference ripple.
   The physics warrant is the load-bearing one (§11.3 X22) — energy added into
   a cancellation is itself cancelled, so the null is an interference zero,
   not a deficit the drive can fill. M5's registry entry permits ``eq`` for
   boundary *loading*; this path never sees loading, only identified
   interference nulls, so it never routes it.
3. **The household sentence is copied, never rewritten.** The carve-out
   record's own ``reason`` is the shipped SSOT for what a household is told
   about an excluded band (``crossover_v2.spatial._carve_out_records`` composed
   with ``_null_classification_copy``). Minting a second sentence here would
   be the second computation of one verdict that §3.1 forbids — and would put
   the hardware-noun prohibition in two places instead of one.

A record this path cannot promote honestly is **left alone**, not guessed at:
a position-screen carve has no tau and no classification, and a null the
shipped gate classified ``insufficient_evidence`` has, by that gate's own
verdict, nothing to attribute. §10's "No speculative mechanisms" applies to
the promoter as much as to the registry.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Mapping, Sequence

from jasper.log_event import log_event

from .findings import EvidenceRef, Finding, FindingError
from .closed_sets import (
    CONFIDENCE_UNSURE,
    PROBE_DESIGN_AXIS,
    PROBE_POSITION_VARIANCE,
    PROBE_REPEAT_VARIANCE,
    PROBE_ROTATION,
)
from .mechanisms import (
    MECHANISM_BOUNDARY_SBIR,
    MECHANISM_HF_REFLECTION,
    MECHANISM_LEVEL_FRAME,
)
from .session_identity import SessionIdentity

logger = logging.getLogger(__name__)

#: Producer id written into the finding set's provenance marker.
PRODUCED_BY = "jasper.attribution.promotion.promote_carve_outs"

#: The level-frame path's own provenance marker. Separate from
#: :data:`PRODUCED_BY` because the two paths publish to different phases from
#: different points in the session, and a reader asking "who wrote this set"
#: should get the function that actually did.
PRODUCED_BY_LEVEL_FRAME = (
    "jasper.attribution.promotion.promote_level_frame_disagreement"
)

#: The one carve-out source that carries attributable evidence. Mirrors
#: ``crossover_v2.spatial.CARVE_OUT_SOURCE_IDENTIFIED_NULL``; the sibling
#: ``position_screen`` source has no tau and no classification by
#: construction, so it is skipped rather than promoted with invented fields.
SOURCE_IDENTIFIED_NULL = "identified_null"

#: Position-variance classification -> (mechanism, routed fix class). Mirrors
#: ``interference_nulls.CLASSIFICATION_*``. ``insufficient_evidence`` is
#: deliberately absent: the shipped gate already said it could not tell.
_CLASSIFICATION_ROUTES: Mapping[str, tuple[str, str]] = {
    # Source-fixed: sat at the same frequencies at every position. §4 M2's
    # fix classes are document_as_physics + carve; `carve` is what the
    # pipeline actually did to these bins, so it is what the finding asserts.
    "position_invariant": (MECHANISM_HF_REFLECTION, "carve"),
    # Position-variant interference null -> `physical`, never `eq` (§4 M5,
    # library panel 2026-07-29: the split is a detector requirement, and the
    # discriminating move is physical).
    "position_dependent": (MECHANISM_BOUNDARY_SBIR, "physical"),
}

_EVIDENCE_KEYS = ("f_center_hz", "n", "tau_us", "r_time", "r_freq", "depth_db")


def _intervals(carve_outs: Any) -> list[Mapping[str, Any]]:
    """Flatten the persisted per-band carve-out structure, de-duplicated.

    ``carve_outs_by_band`` lists a null under **every** spec band it overlaps
    — correct for disclosure, since the null removes bins from both — but a
    straddling null is one physical feature and must become one finding.
    De-duplicate on the interval's own identity (its edges plus the
    instrument that carved it), which is stable because a registry row's
    interval is the null's own unclipped half-depth width.
    """

    if not isinstance(carve_outs, Sequence) or isinstance(carve_outs, (str, bytes)):
        return []
    seen: set[tuple[Any, Any, Any]] = set()
    flat: list[Mapping[str, Any]] = []
    for band in carve_outs:
        if not isinstance(band, Mapping):
            continue
        rows = band.get("intervals")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key = (row.get("f_lo_hz"), row.get("f_hi_hz"), row.get("source"))
            if key in seen:
                continue
            seen.add(key)
            flat.append(row)
    return flat


def _band_bounds(row: Mapping[str, Any]) -> tuple[float, float] | None:
    """This record's ``(f_lo_hz, f_hi_hz)`` as real floats, or ``None``.

    ``Finding`` validates the band itself, so this is not a second validator
    — it is the narrowing that lets the call site pass a genuine
    ``tuple[float, float]`` instead of two ``Any | None`` values that only
    happen to be numbers. Persisted carve-out records come from JSON, where
    any field can be absent or the wrong type, and relying on the
    constructor to catch that made the call site statically dishonest about
    what it was passing.

    ``bool`` is excluded explicitly: ``isinstance(True, int)`` is true in
    Python, so a band edge of ``True`` would otherwise become 1.0 Hz.
    Ordering and non-negativity stay :class:`Finding`'s to enforce — one
    owner per rule.
    """

    bounds: list[float] = []
    for key in ("f_lo_hz", "f_hi_hz"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        bounds.append(number)
    return bounds[0], bounds[1]


def promote_carve_outs(
    carve_outs: Any,
    *,
    session: SessionIdentity,
    cites: Iterable[EvidenceRef],
) -> tuple[Finding, ...]:
    """Promote every attributable carve-out record to a finding.

    Args:
      carve_outs: the persisted ``carve_outs`` block from the cloud
        pipeline's result — a list of ``{band_hz, intervals, ...}``. Taking
        the *persisted dict* rather than the live ``InterferenceNullReport``
        is deliberate: it is what a bundle actually holds, so the same call
        promotes a live close and a replayed archive, and this package stays
        free of the analysis stack.
      session: the session these records belong to.
      cites: the evidence pointers every produced finding carries. At least
        one must be a commissioning-bundle citation (``Finding`` enforces it)
        — normally the cloud artifact these records were read from.

    Returns:
      Findings in ascending band order. Empty is a legitimate and common
      answer — a clean speaker carves nothing.
    """

    pointers = tuple(cites)
    out: list[Finding] = []
    for row in _intervals(carve_outs):
        if row.get("source") != SOURCE_IDENTIFIED_NULL:
            continue
        route = _CLASSIFICATION_ROUTES.get(str(row.get("classification") or ""))
        if route is None:
            continue
        mechanism, fix_class = route
        evidence: dict[str, Any] = {
            key: row[key] for key in _EVIDENCE_KEYS if row.get(key) is not None
        }
        evidence["classification"] = str(row.get("classification"))
        band_hz = _band_bounds(row)
        if band_hz is None:
            # A record that IS attributable but whose band is unusable. This
            # is a REFUSAL, not one of the two skips above, and the
            # distinction is deliberate: those skip records that are
            # correctly not attributable (§10's "no speculative mechanisms"),
            # whereas a null the shipped gate classified but whose own
            # frequency edges are missing or non-finite is a malformed
            # record, and a silent drop would hide it. Same event and same
            # continue as the ``FindingError`` arm below, which this simply
            # reaches earlier and with a better message.
            log_event(
                logger,
                "attribution.carve_out_promotion_refused",
                level=logging.WARNING,
                mechanism=mechanism,
                classification=str(row.get("classification")),
                error=(
                    "carve-out record has no usable band: "
                    f"f_lo_hz={row.get('f_lo_hz')!r} f_hi_hz={row.get('f_hi_hz')!r}"
                ),
            )
            continue
        try:
            out.append(
                Finding(
                    mechanism=mechanism,
                    band_hz=band_hz,
                    evidence=evidence,
                    # Rule 1 — P2-only support never rises above `unsure`.
                    confidence=CONFIDENCE_UNSURE,
                    fix_class=fix_class,
                    # Rule 3 — the shipped sentence, copied.
                    household_copy=str(row.get("reason") or ""),
                    probes_run=(PROBE_POSITION_VARIANCE,),
                    probes_recommended=(PROBE_ROTATION,),
                    cites=pointers,
                )
            )
        except FindingError as exc:
            # A malformed record must not take the session's whole findings
            # set with it. It is not dropped silently either: the record
            # stays in the persisted null registry and the carve-out
            # disclosure (where a reader asking "why was this band excluded"
            # already goes), and the reason it could not be promoted is
            # named here. In practice this fires on a household sentence that
            # broke its own contract — the one failure the schema is designed
            # to catch before a household sees it.
            log_event(
                logger,
                "attribution.carve_out_promotion_refused",
                level=logging.WARNING,
                mechanism=mechanism,
                classification=str(row.get("classification")),
                error=str(exc),
            )
            continue
    out.sort(key=lambda finding: finding.band_hz)
    return tuple(out)


#: The one sentence a household may be shown when the ESTIMATORS disagree,
#: and the ONLY place it is written. Its realized-level sibling is
#: :data:`REALIZED_LEVEL_HOUSEHOLD_COPY` below; the record's ``reason`` picks
#: between them, and nothing here re-derives which condition fired.
#:
#: **Minted here rather than copied**, which is the one way this path differs
#: from :func:`promote_carve_outs`'s rule 3. That rule exists because the
#: carve-out record already carries the shipped sentence a household is told
#: about an excluded band, so writing a second one would be two owners for one
#: verdict. The frame gate has no such sentence for this outcome, so this
#: string is the first copy for it, and it lives here — beside the schema that
#: validates it — rather than in the flow, so there is still exactly one owner.
#:
#: Three claims, each true at the moment of minting and no more:
#:
#: * two cross-checks disagreed with the measurement the tuning was set from;
#: * the tuning nonetheless used that measurement, so nothing was guessed at;
#: * re-running the room pass is worth the household's time.
#:
#: **The second claim is the one the single-datum-owner migration made true.**
#: The copy this replaced said a further check "found the tuning itself lands
#: the two ranges level, so it was offered rather than refused" — which was an
#: honest description of a mechanism that no longer exists, and would now be
#: false twice over: no check adjudicates the placement any more (the summed
#: capture measures it), and a disagreement cannot refuse anything.
#:
#: **It reports an outcome and asks for nothing** — ruling S8, and the third
#: thing this sentence has had to stop claiming. The copy it replaced asked the
#: household to re-run the room pass, on the reading that two cross-checks of
#: one quantity had conflicted and better evidence would settle it. S8 says
#: they were never reads of one quantity: one measures the level where the two
#: ranges hand over, the other averages each range across its own span, and on
#: a horn with a sloped response those legitimately part company by many dB.
#: Re-measuring cannot close a gap that is a property of two definitions, so
#: asking for it would send someone to do work that changes nothing.
#:
#: It names no part of the speaker: §3.1's hardware-noun
#: prohibition is enforced by :class:`~jasper.attribution.findings.Finding`
#: itself, so a future edit that reaches for "woofer" fails at construction
#: rather than in front of a household.
LEVEL_FRAME_HOUSEHOLD_COPY = (
    "Two different ways of reading how this speaker's high and low ranges "
    "balance came out apart from each other. They measure different things, "
    "so that is expected here and neither one is wrong. The tuning was set "
    "from the measurement either way — nothing to do."
)

#: The sentence for the OTHER condition this record can carry: the committed
#: pair's two REALIZED levels sit further apart than the tolerance
#: (``intervention.REALIZED_LEVEL_SUSPECT_REASON``). It exists because the
#: realized-level demotion (`docs/measurement-loop-doctrine.md` deviation (i))
#: turned that condition from a refusal into a banked finding, and the
#: estimator sentence above is false about it in all three of its claims: the
#: two cross-checks AGREED (that is why this reason won), nothing "cross-checks
#: the balance" here — one estimator read the pair that would ship — and
#: re-running the room pass is not the fix.
#:
#: **It restores the recommendation the demotion would otherwise have lost.**
#: The refusal this replaced carried the actionable sentence (``refusal_copy``'s
#: registry row, deleted in the same change): "The two drivers would not have
#: ended up at matching levels, so JTS left your speaker alone. Re-check the
#: driver details — sensitivity and any resistor pad — in speaker setup, then
#: measure again." Doctrine §3 requires a defect outside §4's closed list to
#: "disclose and recommend a next action", so the recommendation is carried
#: over and only the two things that stopped being true are dropped: the
#: speaker is NOT left alone (the round proceeds to the review screen), and the
#: hardware nouns cannot survive
#: :func:`~jasper.attribution.findings._validated_household_copy` — "driver" is
#: on its banned list, so the ranges are named the way the sibling above names
#: them.
#:
#: **"would not end up" and not "did not come out"**, which is the tense the
#: deleted refusal used and the one the instrument earns. The levels are read
#: off the pair as the fit MODELS it emitting — the measured per-branch
#: responses through the modelled correction — not off a capture of the applied
#: tuning, which is the delta probe's job after an apply that has not happened
#: yet. A household sentence saying the pair WAS measured that way would claim a
#: capture the session never took.
REALIZED_LEVEL_HOUSEHOLD_COPY = (
    "This speaker's high and low ranges would not end up level with each other "
    "on the tuning this pass produced. Re-check what you entered in speaker "
    "setup — each range's sensitivity, and any resistor pad — then measure "
    "again."
)

#: The band keys, which become ``band_hz`` rather than evidence. Named so the
#: producer and this reader cannot drift; every OTHER key in the record is
#: evidence, deliberately (see :func:`promote_level_frame_disagreement`).
_LEVEL_FRAME_BAND_KEYS = ("f_lo_hz", "f_hi_hz")


def promote_level_frame_disagreement(
    record: Any,
    *,
    session: SessionIdentity,
    cites: Iterable[EvidenceRef],
) -> Finding | None:
    """Promote one banked level-frame disagreement to an M7 finding.

    **TWO conditions reach here, and the record's own ``reason`` says which.**
    The producer (:func:`~jasper.active_speaker.crossover_v2.accountability.
    level_frame_record`) banks when the two level DEFINITIONS differ, when
    the committed pair's REALIZED levels disagree, or both — and it writes one
    ``reason``, with the estimator condition winning when both fire. This
    function reads that field and nothing else to choose the household sentence
    and the fix class. It does **not** look at ``realized_difference_db`` and
    compare it against ``realized_tolerance_db``, or at the estimator pair, for
    the same reason the paragraph below gives: that comparison is the gate's,
    and computing it twice is §3.1's forbidden second verdict. The consequence
    is deliberate and is the producer's stated ordering — when both fire the
    household is told about the estimators, because two instruments that
    disagree about the frame make the realized read downstream of a suspect
    frame, and better evidence comes before acting on a setup value.

    **What the DEFINITION finding means (#2609, corrected by ruling S8).** The
    two per-driver numbers — the trim solve's mirrored ±1-octave power average
    and the fit's core-band median — no longer vote on anything, and S8 then
    established that they were never reads of ONE quantity to compare. One is
    the HANDOVER level (the level fact); the other is the PASSBAND estimate
    (the starting estimate that sizes fixed attenuation). A record reaches here
    when their relative placements sit further APART than
    :data:`~jasper.active_speaker.crossover_v2.intervention.LEVEL_ESTIMATOR_TOLERANCE_DB`
    — a DISCLOSURE TRIGGER, not an agreement bar
    (:func:`jasper.active_speaker.crossover_v2.intervention.compare_level_definitions`).
    There is no third measurement to hunt for and nothing to adjudicate: on a
    horn with a sloped response the gap is expected, the pair is anchored on the
    raw measured trim whatever the two say, and the same trims ship either way.
    So the household is told what was seen and asked for nothing.

    **What it used to mean, and why the change is a narrowing.** Under the
    owner's 2026-07-30 ruling on #1866 this recorded that two estimators had
    disagreed past a 3.0 dB bar, that a closed-loop realized-level check had
    nonetheless passed, and that the session had proceeded on a placement one of
    those two disputed estimators produced. The finding therefore had to carry
    the whole argument for shipping past a gate. It no longer does, because
    nothing ships past a gate here: the disagreement changes no committed
    number. #2609's conviction comment records what the old arrangement cost —
    a 0.326 dB miss at that bar moved a tweeter +3.79 dB hotter than its own
    measurement asked, and the round was rolled back.

    **What the REALIZED finding means** (doctrine deviation (i)). Not two
    estimates of the frame disagreeing, but the pair that would ship measured
    apart: ``realized_branch_level_match`` re-reads each branch's level on its
    own mirrored half-band about Fc, after the committed trim, and the two land
    further apart than ``REALIZED_LEVEL_MATCH_TOLERANCE_DB``. A 2-way sums flat
    only when both branches hand off at the same level, so this is a
    tonal-balance defect no amount of per-branch flattening can hide. It used to
    REFUSE the round; it now discloses, so this finding is the durable half of
    that disclosure and carries the recommendation the refusal used to.

    **It re-decides nothing.** There is no threshold here, no comparison of the
    disagreement against the tolerance, no re-reading of the realized check —
    all three are the planner's and the gate's, and duplicating any of them
    would be the second computation of one verdict §3.1 forbids. A record that
    arrives is promoted or is refused as malformed; there is no third answer.

    **Every non-band key is evidence, by rule rather than by list.** The
    carve-out path names its evidence keys explicitly because it reads a
    persisted structure with many fields it does NOT want. This record is
    built for exactly this purpose one call away, so a key list here would be
    a second schema to keep in sync — and the failure mode of drift is a
    finding silently missing the number a reader needed. ``Finding`` validates
    every value is a finite scalar, a string, a bool, or ``None``, so a
    malformed record is refused rather than persisted.

    Args:
      record: the flow's banked record — the two band keys plus the evidence.
      session: the session this disagreement belongs to.
      cites: the evidence pointers the finding carries; at least one must be a
        commissioning-bundle citation (``Finding`` enforces it).

    Returns:
      The finding, or ``None`` when the record is malformed — logged, never
      silent, and never raised: a findings failure must not cost a session
      that the gate has already decided may proceed.
    """

    if not isinstance(record, Mapping):
        return None
    band_hz = _band_bounds(record)
    evidence = {
        str(key): value
        for key, value in record.items()
        if key not in _LEVEL_FRAME_BAND_KEYS
    }
    if band_hz is None:
        log_event(
            logger,
            "attribution.level_frame_promotion_refused",
            level=logging.WARNING,
            mechanism=MECHANISM_LEVEL_FRAME,
            error=(
                "banked level-frame record has no usable band: "
                f"f_lo_hz={record.get('f_lo_hz')!r} "
                f"f_hi_hz={record.get('f_hi_hz')!r}"
            ),
        )
        return None
    # The producer's own answer to "why does this record exist", read and never
    # recomputed (see the docstring's first paragraph). Imported from its one
    # owner rather than re-declared here, which is what the one-vocabulary rule
    # asks; `storage.py`'s local import of the evidence store is the same move.
    #
    # Inside the function for two reasons, and the second is the load-bearing
    # one. Cost: `intervention` is ~1.8 s and ~1000 modules, against ~0.1 s for
    # the whole attribution package, which is otherwise a leaf a light surface
    # can import. Safety: on the ONLY path that reaches here the module is
    # already in `sys.modules` — `crossover_v2_flow` imports `accountability`,
    # which imports this same constant from `intervention` — so this is a dict
    # lookup that cannot raise. That matters because the seam above catches
    # `(OSError, RuntimeError, TypeError, ValueError)` and an `ImportError`
    # would escape it, costing a session whose candidate is already published
    # the fail-soft guarantee this function's own docstring makes.
    from jasper.active_speaker.crossover_v2.intervention import (
        REALIZED_LEVEL_SUSPECT_REASON,
    )

    # `.get`, so an absent or unrecognised `reason` falls to the estimator arm
    # — the conservative default rather than the arbitrary one. That arm asks
    # for a re-measure, which costs a household nothing if it is wrong; the
    # realized arm asks them to go change a setup value, which is an action to
    # take only on a record that actually says so. The producer always writes
    # the field, so this is a floor and not an expected path.
    realized_only = record.get("reason") == REALIZED_LEVEL_SUSPECT_REASON
    try:
        return Finding(
            mechanism=MECHANISM_LEVEL_FRAME,
            band_hz=band_hz,
            evidence=evidence,
            # `unsure` on both arms, for two DIFFERENT honest reasons rather
            # than one reason stretched over both.
            #
            # DEFINITION arm: THAT the two definitions differed is measured;
            # that the disagreement is a real inter-driver level error is not,
            # because the shipped gate's own residual produces this exact
            # signature on a healthy speaker — a pair identical by construction
            # reads 0.910 dB apart, and ordinary woofer passband tilt adds
            # roughly 1.33 dB per dB/octave (both measured, see
            # `LEVEL_ESTIMATOR_TOLERANCE_DB`'s own comment). A single session
            # cannot separate "the drivers really sit that far apart" from
            # "these two estimators read different spans of a curve that is not
            # flat in the same way over both", so it does not claim to. That
            # tier survived the single-datum-owner migration on purpose and is
            # now MORE right, not less: the claim narrowed to "this capture is
            # worth re-taking", and a suspicion about a capture is exactly an
            # unsure one.
            #
            # REALIZED arm: none of the above applies — there is no rival
            # estimate and no span mismatch. `realized_branch_level_match` is
            # explicit that it is "One estimator, not a second opinion",
            # re-reading the SAME power-band average over the SAME halves that
            # set the trim, so THAT the pair lands apart is measured about the
            # pair that would ship. What is unsure is the CAUSE, which is what
            # a mechanism finding claims: the levels are read off the emission
            # the fit MODELS (`resp.complex_tf * correction`), not off a
            # post-apply capture — the delta probe is what measures that — and
            # nothing here separates a wrong sensitivity or pad value in setup
            # from an error in the fit's own frame. `polish_delta_db_*` rides
            # the evidence precisely so a reader can subtract the one
            # instrument-side contribution that is known and bounded.
            confidence=CONFIDENCE_UNSURE,
            # M7 declares BOTH classes and the split is plan §4's: `eq` when a
            # driver's level is genuinely low, `refit` "when the level error is
            # upstream in the fit's own frame". The two arms land on opposite
            # sides of exactly that line, which is why the declaration has two
            # entries rather than one.
            #
            # ESTIMATOR -> `refit`: a disagreement BETWEEN two estimates of the
            # frame is upstream of every trim derived from them by
            # construction, so adding level cannot be the fix.
            #
            # REALIZED -> `eq`: the frame is not in dispute (that is why this
            # reason won); what is measured is the committed pair sitting at
            # levels that do not match, which is the first half of the split
            # verbatim. Routing `refit` here would send a re-solve at a
            # measurement the re-solve already agrees with.
            fix_class="eq" if realized_only else "refit",
            household_copy=(
                REALIZED_LEVEL_HOUSEHOLD_COPY
                if realized_only
                else LEVEL_FRAME_HOUSEHOLD_COPY
            ),
            # NO probe was run. The evidence is the flow's own two estimators
            # plus the realized-level check, none of which is a §5 primitive,
            # and claiming one ran would be the cheapest possible way to
            # launder a model-derived number into probe-adjudicated standing.
            probes_run=(),
            probes_recommended=(PROBE_DESIGN_AXIS, PROBE_REPEAT_VARIANCE),
            cites=tuple(cites),
        )
    except FindingError as exc:
        log_event(
            logger,
            "attribution.level_frame_promotion_refused",
            level=logging.WARNING,
            mechanism=MECHANISM_LEVEL_FRAME,
            error=str(exc),
        )
        return None


__all__ = [
    "LEVEL_FRAME_HOUSEHOLD_COPY",
    "PRODUCED_BY",
    "PRODUCED_BY_LEVEL_FRAME",
    "REALIZED_LEVEL_HOUSEHOLD_COPY",
    "SOURCE_IDENTIFIED_NULL",
    "promote_carve_outs",
    "promote_level_frame_disagreement",
]
