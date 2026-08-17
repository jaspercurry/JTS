# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Delta-probe verification: did the speaker do what the correction asked?

The linearization-integrity ladder's PR-L5 primitive
(``docs/linearization-integrity-plan.md``). Every applied correction change is
verified as a **realized-vs-commanded per-frequency map** and classified into
one of four verdicts; the three non-matched ones roll the correction back
automatically. Pure computation — numpy in, a frozen verdict record out. No
I/O, no session state, no rollback: the session owns those.

**Why this exists.** On 2026-07-27 a linearization shipped whose emitted
shelves were realized at Q 0.476 while every gate in the fit engine evaluated
them at Q 0.707 (``slope: 6`` is not CamillaDSP's Butterworth — PR-L2). The
fit's realization gate, its residual, and its VERIFY prediction all used the
same wrong evaluator, so a shelf that missed its design by up to 1.70 dB
scored as exact. **A model cannot audit itself.** The only instrument that can
is a measurement of what the hardware actually did, compared against what the
filters were told to do. That is this module. PR-L2 fixed the specific Q bug;
this catches the whole class, permanently, including the next one.

**This is the IN-ROOM instrument. It has an offline twin.**
:mod:`jasper.active_speaker.bench` asks the same question of the same defect
class before anything is applied and without a microphone: it renders the
emitted config through the real pinned CamillaDSP binary and grades the result
against the fit's claim. It reuses this module's verdict vocabulary and
:func:`classify_delta_probe` itself rather than defining a parallel one. The two
are complementary, not redundant — the offline one sees only what the DSP does
to a signal, and this one is the only thing that can see what a driver in a room
did with it. If you are about to build a third, one of these two is the one to
extend.

**What "realized" and "commanded" are, exactly** (read this before trusting a
verdict — the algebra matters):

* ``commanded_delta_db`` is what the apply asks the summed response to CHANGE:
  the applied graph's predicted sum minus the predicted sum of **the graph it
  replaces**, both built from the SAME measured branches with the SAME
  summation model (:mod:`jasper.active_speaker.crossover_v2.commanded`). The
  branch measurements and the summation model therefore cancel out of it; what
  survives is every element the apply commands — the emitted filters, the role
  gains, the polarity and the delay. **Every element is load-bearing:** until
  #2611 both sides were evaluated at the applied candidate's own polarity,
  delay and gains, so those three cancelled by construction and a commanded
  +3.3209 dB role-gain step plus a commanded polarity flip arrived here as
  uncommanded surprises, and rolled a measured-better tune back.
* ``realized_delta_db`` is the measured post-apply response minus the SAME
  previous-graph prediction, at the same microphone position.

Their difference — the ``error_db`` map this module classifies — is
algebraically ``measured_post − predicted_post``: the previous-graph prediction
cancels. That is deliberate and is stated here so nobody later reads the delta
framing as implying an independent pre-apply *measurement* at the mark (there
is none at MEASURE; ``entry_delta_db`` below is a real one, and it is optional).
The delta framing earns its keep in two places the plain residual cannot reach:
the commanded curve is the axis the shortfall-vs-model-error discriminator
regresses against, and the spatial arm below IS two real measurements.

**This comparison is NOT level-offset-invariant, and that is why
``expected_offset_db`` exists** (issue #1811). Its sibling, the VERIFY tracking
check, mean-centers its error (``audio_measurement.analysis.
_offset_invariant_rms_and_max``: ``error -= float(np.mean(error))``), so a
uniform level difference between measured and predicted cannot read as a
tracking failure there. This probe deliberately does not, because a level
shortfall IS one of the things it classifies. The consequence is that a level
change the correction did not command lands directly in ``error_db``.

There is exactly such a change, every session: the applied graph absorbs its
correction's boost as a pre-split common attenuation
(``camilla_yaml.linearization_headroom_db`` folded into
``active_baseline_headroom``), and the emitted prediction this probe compares
against carries no such term. On the session that surfaced this the apply moved
that attenuation ``0 → −22.458 dB`` and the post-apply capture sat −7.9 dB
broadband below the pre-apply cloud — a whole-band common mode that this module
would have graded as ``model_error`` and rolled a healthy correction back for.

So the caller passes the offset the EMITTER knows it applied
(``baseline_profile.applied_program_level_delta_db``) and this module removes it
before classifying. Whatever is left is measured **in the quiet bins** — in
band, but below the commanded floor, where the correction asked for nothing and
therefore any level is uncommanded by construction — and disclosed as
``residual_offset_db``. A residual that is both material and sufficient to
explain the failure is its own verdict, :data:`VERDICT_LEVEL_MISMATCH`, rather
than being silently absorbed or misfiled as a shape defect.

**That residual is a CHANGE, and its claim is bounded by where it was
measured** (issue #2533). Both halves were wrong on the 2026-08-15 JTS3 session
and both are fixed here; they are independent defects that happened to compound.

*A change.* ``measured_post − predicted_post`` is an in-room gated measurement
against an on-axis two-branch model, and those two curves do not share a level
anchor. The mismatch between their anchors is a standing property of the
comparison — there before the apply, unchanged by it — so a residual that
reports it is not reporting a level move at all. The caller therefore also
passes the PRE-apply capture in the same frame (``entry_delta_db``, from the
``verify_priors.entry_baseline`` #2530 already retains), and the standing term
cancels: what is left is measurement-minus-measurement with the correction's own
command and the graph's own declared level move taken out. Measured on that
session, a reported −3.342 dB decomposed as **−1.660** standing anchoring,
**−1.457** real measured change confined to 12-20 kHz, and **−0.221** declared
graph move — only the middle term is a level move, and only the middle term
survives. The term removed is disclosed as ``entry_anchor_offset_db``; when no
pre-apply curve is available it is ``None`` and nothing is removed, which is the
same "nothing known, so leave it visible" rule ``expected_offset_db`` follows.

*Bounded by where it was measured.* A residual is measured in the quiet bins and
claimed over the GRADED ones, and nothing checked that those two sets meet. On
that session the correction commanded 463 Hz-12 kHz, so the quiet set was
12-20 kHz — a level measured entirely ABOVE the band it was claimed over, and
reported as a whole-band ``uncommanded_level_shift``. The set's own span did not
show it either: two stray bins at 493 Hz and 1.9 kHz made 158-of-160 bins above
12 kHz look band-wide, which is why ``quiet_core_band_hz`` is the INTERQUARTILE
span rather than the min/max ``frame.band_hz`` already reports. When that span
covers less than :data:`DELTA_PROBE_MIN_QUIET_COVERAGE` of the interquartile span
the graded band's OWN bins cover on the same grid, the finding stands and the
verdict is unchanged — narrowing it would make this instrument stricter on
evidence it had just declared unrepresentative — but the reason narrows to
:data:`REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND` and names the band it covers.

The absorption itself is NOT compensated at the speaker. It is the
excitation-safety property that keeps a boosted band at or under unity
(``camilla_yaml``'s ``MAX_LINEARIZATION_BOOST_DB`` note): removing it at the
main volume would put the boosted band over the driver's excitation cap by the
branch's own boost, on a sustained swept sine, below the per-driver limiters'
reach. It is corrected here, in the analysis, and nowhere else.

**This is not the VERIFY tracking check, and does not replace it.**
``crossover_v2_flow._verify_verdict`` compares the same two curves over the
crossover handoff band alone (``[Fc/2, 2·Fc]``, ~2–4 kHz on JTS3) at 1.5 dB.
The 2026-07-27 shelf error lived at 5–12 kHz — an octave and a half above
that band's top — and tracking could not have seen it at any tolerance. This
probe runs over **the band the correction actually commands something in**,
which is the only band where "did it do what we asked" is a question.

**Verdict priority.** ``matched`` at the mark and ``spatially_costly`` are
independent questions, so the order between them is a policy choice and it is
this: a map that does NOT match at the mark is diagnosed as a chain defect
(``model_error`` / ``level_dependent_shortfall``) even when the spatial arm
also flags, because the chain defect is the more proximate cause and the more
actionable remedy. ``spatially_costly`` is reserved for the case that is
otherwise invisible — the correction did exactly what it was asked at the
mark, and the room got less even for it. The two remedies are genuinely
different (fix the model / move the speaker), which is why one verdict must
win rather than both being reported as equals. The losing arm's evidence
still travels in the record.

**The frame between the two curves is measured and removed before a rollback
is decided** (issue #2521). The offset half of that frame was already handled
above; the TILT half was not, and a tilt is the ordinary state of a comparison
between an in-room gated measurement and an on-axis two-branch model — the
2026-07-29 corpus put **84 %** of a "predictions are 2.02× optimistic" headline
into a single −0.79 dB/octave frame difference (:mod:`jasper.audio_measurement.
frame_fit`). Left in, it made every speaker/room/microphone with a broadband
slope fail this probe no matter how well its filters realized: on the first
remote JTS3 session (2026-08-14) this probe read **7.24 dB rms** with the frame
in and rolled the correction back, while the tracking instrument's own
frame-removed grade of the SAME capture was **0.065 dB** (0.28 dB raw).

So the frame is fitted — one offset, one tilt — **over the QUIET bins, the ones
where the correction commanded nothing**, which is the same bin set and the same
argument :attr:`DeltaProbeMap.residual_offset_db` already uses for the offset
term: any level, and now any slope, measured where nothing was asked for is
uncommanded by construction and therefore cannot be the correction's doing.
Fitting it over the GRADED bins instead would let the defect set its own frame
and quietly absorb itself — measured on this module's own keystone fixture, a
graded-bin fit dropped the 2026-07-27 shelf-Q error's exceedance from 0.575
octaves to nothing, i.e. it deleted the one defect this module exists to catch.

What the frame may and may not do is asymmetric on purpose:

* it can only **narrow** a finding. The raw grade still decides whether there is
  a finding at all (``matched`` is unchanged); what is re-asked with the frame
  removed is the ROLLBACK question, and the gate sits ahead of **both** rollback
  doors — ``model_error`` and ``level_dependent_shortfall`` alike. A wild frame
  fitted from a noisy quiet region therefore cannot fabricate a rollback; the
  worst it can do is fail to demote, or move a demoted finding between those two
  names. Guarding only the shape door leaves the whole #2521 class walking
  through the scale one — see the gate comment in :func:`classify_delta_probe`
  for the reproduction.
* :data:`VERDICT_SPATIALLY_COSTLY` is outside the gate, deliberately. It is
  reached only when the mark map MATCHED, so no exceedance was ever in play, and
  it rests on two real measurements of the room's own spread rather than on a
  measurement against a model — there is no frame between them for this to
  remove.
* a frame that could not be fitted (too few quiet bins) removes nothing, so the
  verdict stands exactly as it did before this existed. Strict is the honest
  direction for an absent measurement.
* an exceedance that does NOT survive the removal is :data:`VERDICT_FRAME_MISMATCH`
  — disclosed, never silent, and never a rollback (owner ruling 2026-08-15:
  least-bad is adoptable with disclosure; hard stops are reserved for the safety
  class). It is the tilt-carrying sibling of :data:`VERDICT_LEVEL_MISMATCH`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.frame_fit import FRAME_UNFITTED, FrameFit, fit_frame

# --------------------------------------------------------------------------- #
# verdict vocabulary
# --------------------------------------------------------------------------- #

#: The correction realized what it commanded. Keep it.
VERDICT_MATCHED = "matched"
#: Realized and commanded disagree in SHAPE — the emitted filters are not
#: doing what the fit's model of them says they do. Roll back and flag. This
#: is the verdict that catches the PR-L2 shelf-Q class forever.
VERDICT_MODEL_ERROR = "model_error"
#: Realized tracks commanded in shape but falls materially short in scale,
#: where what was commanded is a lift — the driver did not deliver the level
#: it was asked for. A compression diagnostic. Roll back and flag.
VERDICT_LEVEL_DEPENDENT_SHORTFALL = "level_dependent_shortfall"
#: The map matched at the mark, but the cross-position spread WIDENED — the
#: correction bought flatness at one spot by trading it elsewhere, which is
#: the signature of correcting a position-specific interference feature. Roll
#: back and route the household to a placement-vs-speaker service verdict.
VERDICT_SPATIALLY_COSTLY = "spatially_costly"
#: The map fails ONLY because of a level shift that survives removing the
#: offset the emitter knows it applied — measured where the correction
#: commanded nothing, and sufficient on its own to explain the failure.
#: Something moved the level that nobody commanded: an incompletely
#: accounted emit (room-PEQ / output-trim attenuation the applied config drops
#: is the known systematic case), a mic or input-chain change between captures,
#: or a household volume touch. **Not a claim about the correction**, which is
#: why it is not a rollback verdict — see
#: :data:`DELTA_PROBE_ROLLBACK_VERDICTS`. It is named rather than absorbed so
#: the journal says which of those it is worth looking for.
VERDICT_LEVEL_MISMATCH = "level_mismatch"
#: The map fails ONLY because of the FRAME between the two curves — one level
#: offset and one broadband tilt, fitted where the correction commanded nothing
#: and therefore uncommanded by construction. Removing that frame makes the map
#: pass (#2521).
#:
#: It replaces whichever rollback the map would otherwise have reached — a SHAPE
#: claim (``model_error``) or a SCALE one (``level_dependent_shortfall``) — because
#: neither survives evidence that turns out to be the frame. A real but
#: in-tolerance depth shortfall riding a room tilt reaches this verdict, and
#: should: on its own it was ``matched``.
#:
#: The tilt-carrying sibling of :data:`VERDICT_LEVEL_MISMATCH`, and not a
#: rollback verdict for the same reason: a frame difference is a property of the
#: COMPARISON — an in-room gated measurement against an on-axis two-branch
#: model — not a claim about the correction's shape. Its ordinary causes are the
#: instrument (directivity between the two sittings, the microphone, the level
#: chain), and it is ALSO consistent with a mis-modelled broad filter; this
#: module measures the frame and does not attribute it (``frame_fit``'s own
#: rule). What it will not do is revert a household's correction on evidence
#: that cannot tell those apart (owner ruling 2026-08-15: least-bad is adoptable
#: with disclosure, hard stops are reserved for the safety class). It grants no
#: permission either: like ``level_mismatch``, it leaves the shape question
#: unanswered, and it says so.
VERDICT_FRAME_MISMATCH = "frame_mismatch"
#: No verdict is available — the correction commands nothing inside the
#: probe band, or the curves could not be compared. **Not a pass.** The
#: session must treat this the way every other honesty instrument in this
#: flow treats an unknown: no evidence to refuse on, and no permission
#: granted either.
VERDICT_UNAVAILABLE = "unavailable"
#: Only the HEARING-SAFETY half of this probe ran (#2614). The caller had the
#: applied graph's own declared transfer but no CHANGE axis — the reachable
#: case is a committed alternative-Fc candidate, whose branches are composed
#: through a crossover the previous graph never ran — so "did the speaker put
#: more energy into a driver than the graph declared" is answerable and "did
#: the correction realize the shape it commanded" is not.
#:
#: **Not a pass, and deliberately not a rollback.** The two directional
#: findings on this map are real and reach
#: :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_applied_safety`
#: exactly as they do on a full map, so an overshoot still comes off the
#: speaker. What is missing is the shape grade, and a missing grade is not
#: evidence of a defect — the same rule :data:`VERDICT_UNAVAILABLE` carries.
#: It is its own word rather than ``matched`` because a household and a receipt
#: must be able to tell "the shape check passed" from "the shape check did not
#: run", and rather than ``unavailable`` because something WAS measured.
VERDICT_SAFETY_ONLY = "safety_only"

#: Why the shape half did not run on a :data:`VERDICT_SAFETY_ONLY` map. One
#: string today; it is a constant so the journal, the receipt and the household
#: caveat all quote the same one.
REASON_COMMANDED_AXIS_UNAVAILABLE = "commanded_axis_unavailable"

#: Every verdict this module can return. Pinned by a test so a new
#: classification path cannot ship an un-enumerated string.
DELTA_PROBE_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MATCHED,
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY,
    VERDICT_LEVEL_MISMATCH,
    VERDICT_FRAME_MISMATCH,
    VERDICT_UNAVAILABLE,
    VERDICT_SAFETY_ONLY,
})

#: The verdicts on which rollback is AUTOMATIC (plan PR-L5: "Rollback is
#: automatic on the non-matched classes"). ``unavailable`` is deliberately
#: NOT here — an absent measurement is not evidence of a bad correction, and
#: rolling back on it would revert every session whose household closed the
#: phone before the post-apply sweep.
#:
#: :data:`VERDICT_LEVEL_MISMATCH` is not here either, for the same reason
#: stated a different way: it is a finding about the LEVEL AXIS of this
#: comparison, not about the correction's shape. Its most likely production
#: cause is a known incompleteness in our own accounting — the applied
#: crossover config is emitted without room-PEQ / preference EQ, so a
#: household that has either can see a real level move the offset reader does
#: not know about — and reverting a household's correction because our
#: bookkeeping was short would be a false accusation against a correction that
#: may be perfect. It grants no permission either: like ``unavailable``, it
#: leaves the shape question unanswered, and it says so.
#:
#: :data:`VERDICT_FRAME_MISMATCH` is not here either, and it is the same
#: sentence with one more term in it: an offset AND a tilt, measured in the same
#: uncommanded bins (#2521). See that verdict's own note.
DELTA_PROBE_ROLLBACK_VERDICTS: frozenset[str] = frozenset({
    VERDICT_MODEL_ERROR,
    VERDICT_LEVEL_DEPENDENT_SHORTFALL,
    VERDICT_SPATIALLY_COSTLY,
})

#: The one reason the seam hands a rollback verdict to the adoption table
#: instead of restoring on it (#2559). A stable string, because it rides the
#: journal and the round receipt: an immediate restore that did not happen must
#: be as legible as one that did.
SEAM_DEFERRED_QUIETER_THAN_COMMANDED = "model_error_quieter_than_commanded"


def seam_rollback_deferral(probe: Any | None) -> str:
    """Why this map's seam-bound rollback DEFERS to the adoption table, or ``""``.

    Owner ruling, 2026-08-15: a ``model_error`` whose realized deviation points
    entirely QUIETER than commanded is a quality miss that keeps for iteration,
    not a hazard that comes off the speaker. It is the same directional
    discipline #2537 put on the level axis — *"quieter-than-declared costs a
    household some output and tells the next round something, while
    louder-than-declared is energy nobody asked for"* — applied to the shape
    axis, and it is the rule that would have changed the 2026-08-15 14:47 jts3
    round: a −3.32 dB dip at 1330 Hz, nothing realized louder than commanded
    anywhere, tracking passed, 2.399 dB of measured improvement, reverted by a
    seam that never asked which way the miss pointed.

    **The seam preempts the table, so the narrowing has to happen here.** A
    seam rollback ends the session before ``decide_adoption`` runs at all
    (2026-08-15: ``attempt_decision decision=ungraded``), so a quality row can
    only fire for a round the seam let through. Deferring is what makes the
    table reachable; it decides nothing about the outcome, and ``row5``'s
    restore on a measured regression is as available afterwards as before.

    **It NARROWS one class and touches nothing else.** Every other rollback
    verdict is unchanged, and so is every positive-direction finding:

    * :data:`VERDICT_LEVEL_DEPENDENT_SHORTFALL` and
      :data:`VERDICT_SPATIALLY_COSTLY` never defer. The first is a claim about a
      driver's headroom and the second about the room's own spread; neither is
      the shape claim this ruling is about.
    * A map with ANY graded bin realized louder than commanded past tolerance
      never defers, whatever else it measured.
    * ``boost_over_declared_bound`` never defers. That is implied by the bin
      rule above — an overshooting boost IS a bin realized louder — and it is
      stated anyway, because a fence that holds only through a numeric
      implication between two independently-tunable bounds is not a fence. Both
      halves are pinned.

    ``""`` for an absent probe and for a non-rollback verdict: those never
    reached a seam rollback, so there is nothing for them to defer and saying
    otherwise would put a deferral on the record of a round that had none.

    Duck-typed on the probe for :func:`~...verification.evaluate_applied_safety`'s
    reason — it holds whatever the host handed it, including ``None`` — and so
    that one function answers for both readers: the seam that acts on it and the
    receipt that discloses it.
    """
    if probe is None:
        return ""
    if str(getattr(probe, "verdict", "") or "") != VERDICT_MODEL_ERROR:
        return ""
    if bool(getattr(probe, "realized_louder_than_commanded", False)):
        return ""
    if bool(getattr(probe, "boost_over_declared_bound", False)):
        return ""
    return SEAM_DEFERRED_QUIETER_THAN_COMMANDED

# --------------------------------------------------------------------------- #
# classification thresholds
# --------------------------------------------------------------------------- #

# Max |realized − commanded| tolerated below :data:`DELTA_PROBE_HF_SPLIT_HZ`.
#
# 1.5 dB, for two reasons that agree. (a) It is the flow's own established bar
# for "a measurement matched its prediction" — ``crossover_v2_flow.
# VERIFY_TOLERANCE_DB`` — so this probe and the tracking check do not hold the
# same chain to two different standards in two different bands. (b) It must sit
# BELOW the defect it exists to catch: the 2026-07-27 shelf-Q realization error
# peaked at 1.70 dB (FORENSICS-SYNTHESIS.md, chunk 2), deepest around 6.9 kHz —
# inside this tier. The margin is only 0.2 dB at the peak, which is why the
# exceedance-WIDTH rule below matters: that error is a wide systematic tilt
# across the whole shelf transition, so it clears a width test comfortably
# even where it barely clears the amplitude one.
DELTA_PROBE_TOLERANCE_LOW_DB: float = 1.5

# Max |realized − commanded| tolerated at/above :data:`DELTA_PROBE_HF_SPLIT_HZ`.
#
# Measurement uncertainty grows with frequency and a rollback fabricated by
# HF noise is worse than no probe at all. The fit engine's own repeat-agreement
# gate (``linearization_fit.HF_AGREEMENT_LIMIT_HIGH_DB``) ACCEPTS up to 2.0 dB
# of spread between repeat sweeps of the same driver at these frequencies, and
# the owner's per-serial UMIK-2 uncertainty research puts the stock-cal
# protocol at ~±2.3 dB @16 kHz. A tolerance at or under 2.0 would therefore be
# rejecting corrections for noise the fit engine already declared acceptable.
# 2.5 clears both, and the width rule still has to be satisfied on top.
DELTA_PROBE_TOLERANCE_HIGH_DB: float = 2.5

# Where the low tier ends and the high tier begins. Mirrors
# ``linearization_fit._HF_AGREEMENT_TIER_SPLIT_HZ`` so "high frequencies" means
# one thing across the fit and its verification.
DELTA_PROBE_HF_SPLIT_HZ: float = 10_000.0

# A tolerance exceedance must span at least this many contiguous octaves to
# count as a finding.
#
# The measured curves are ladder-smoothed at 1/6 octave below 4 kHz and 1/3
# octave from there up, so an excursion narrower than one smoothing window is
# measurement texture, not a claim about the model — the same argument
# ``linearization_fit.HF_REALIZATION_TOLERANCE_DB`` records ("an isolated
# 1.5-2.0 dB excursion at the smoothing scale is measurement texture, not a
# shape failure"). One third of an octave is the coarsest of those windows, so
# a run this wide has survived a full smoothing window everywhere in the band.
# Every realization defect this probe is built for — a mis-Q'd shelf, a
# mis-modelled slope, a compressed driver — is broad by construction; none of
# them produces a single-bin spike.
DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES: float = 1.0 / 3.0

# Below this, the correction commands nothing worth verifying at that bin.
# Mirrors ``linearization_fit._MIN_FILTER_GAIN_DB`` — the fit engine's own
# "this filter is cosmetic" floor. You can only ask "did it do what we asked"
# where something was asked.
#
# This is also THE QUIET FLOOR: a bin under it is where the correction asked for
# nothing, which is what makes ``residual_offset_db`` and the fitted frame
# measurable there at all. Below the HF split it is the graded floor too; above
# it, the stricter :data:`DELTA_PROBE_MIN_COMMANDED_HIGH_DB` applies. The two
# roles are deliberately not merged (see that constant).
DELTA_PROBE_MIN_COMMANDED_DB: float = 0.5

# The commanded floor a bin must clear to be GRADED at or above
# :data:`DELTA_PROBE_HF_SPLIT_HZ` (#2521).
#
# Numerically equal to :data:`DELTA_PROBE_TOLERANCE_HIGH_DB` and defined
# separately on purpose — the same measurement-uncertainty bar asked of a
# different quantity (what the correction ASKED for at a bin, versus how far its
# realization may miss). The agreement is the point: above the split this module
# already concedes 2.5 dB of per-bin uncertainty, because the fit engine's own
# repeat-agreement gate accepts 2.0 dB between two sweeps of the same driver up
# there and the owner's per-serial UMIK-2 research puts the stock-cal protocol
# at ~±2.3 dB @16 kHz. A bin whose commanded value is SMALLER than that
# concession cannot answer "did the speaker do what we asked": whatever is
# measured there is dominated by the uncertainty the tolerance already granted.
# The first remote JTS3 session's headline was exactly such a bin — |commanded|
# grazing 0.5 dB at 21.3 kHz, graded against 2.5 dB, reporting 23.4 dB of
# "error" (#2521).
#
# Why the floor is NOT lifted below the split, which is the tempting symmetric
# move: measured on this module's own keystone fixture, a flat 1.0 dB floor
# drops the 2026-07-27 shelf-Q defect's exceedance run from 0.575 to 0.307
# octaves — under :data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES` — because that
# defect's commanded shelf passes through 0.5-1.5 dB across the very octaves its
# error lives in. Lifting the floor there would delete the one defect this
# module exists to catch. The tiered floor leaves that fixture bit-for-bit
# untouched (137 graded bins, 0.575 octaves) while removing the HF class above.
DELTA_PROBE_MIN_COMMANDED_HIGH_DB: float = 2.5

# The probe band must retain at least this many bins after masking, or there
# is not enough of a curve to regress or to measure a run width against.
DELTA_PROBE_MIN_BINS: int = 8

# Best-fit realized/commanded scale factor below which a shape-tracking map is
# called a level-dependent SHORTFALL rather than a model error.
#
# 0.85 is chosen to agree with :data:`DELTA_PROBE_TOLERANCE_LOW_DB` about what
# "material" means at the depths this fit produces: a 15% shortfall on the
# ~10 dB lift a CD-horn continuation commands is 1.5 dB, exactly the low-band
# tolerance. So a shortfall large enough to be named here is always large
# enough to have failed the amplitude test that got us into this branch, and
# the two constants cannot disagree about a correction of that size.
DELTA_PROBE_SHORTFALL_GAIN_CEILING: float = 0.85

#: The band ids a realization ratio is reported per (#2649).
#:
#: Reported instead of relying on ONE least-squares slope over the whole graded
#: band, because that scalar can be manufactured by a defect confined to a band
#: the system is not even allowed to command in. On the 2026-08-16 shortfall
#: round the pooled slope read 0.664 while the trusted HF realized 96-101% of
#: commanded: ~90% of the squared error came from a -3.04 dB hole ABOVE the
#: 16.4 kHz mic-trust ceiling.
#:
#: ``crossover`` is the graded tier BELOW :data:`DELTA_PROBE_HF_SPLIT_HZ` —
#: named for what it contains (the crossover region and the bulk of commanded
#: correction) rather than derived from any Fc, which this function is not told
#: and must not guess. ``trusted_hf`` is the graded tier at or above that split.
#: ``above_ceiling`` is reported and NEVER graded: it is the span inside the
#: caller's requested band that the mic-trust ceiling excluded.
DELTA_PROBE_BAND_CROSSOVER = "crossover"
DELTA_PROBE_BAND_TRUSTED_HF = "trusted_hf"
DELTA_PROBE_BAND_ABOVE_CEILING = "above_ceiling"

#: Every band id a realization block can carry, in report order.
DELTA_PROBE_REALIZATION_BANDS: tuple[str, ...] = (
    DELTA_PROBE_BAND_CROSSOVER,
    DELTA_PROBE_BAND_TRUSTED_HF,
    DELTA_PROBE_BAND_ABOVE_CEILING,
)

# Widening of the across-position level spread (``BandSpread.sigma_db``, dB)
# beyond which the post-apply cloud is called spatially costly.
#
# The envelope's own ``linearization_envelope.position_stability_limit`` spends
# this spread as ``sigma_db / sqrt(n_positions)`` when deciding how much
# correction depth a band may have at all — so 1.0 dB of RAW sigma growth is
# already several times the depth the cloud terms would have licensed in that
# band. A correction that widens the room's spread by that much is not
# flattening the speaker; it is fitting one microphone position.
DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB: float = 1.0

# |residual common mode| beyond which an otherwise-unexplained whole-band level
# shift is named :data:`VERDICT_LEVEL_MISMATCH` rather than left inside the
# shape verdict (#1811).
#
# Numerically equal to :data:`DELTA_PROBE_TOLERANCE_LOW_DB` and defined
# separately on purpose — the same "material disagreement" bar, asked of a
# different quantity (one constant over the band, versus a per-bin excursion),
# so a future retune of either must be a deliberate decision about that
# quantity rather than a silent inheritance. The agreement is the point: a
# common mode smaller than the per-bin tolerance cannot by itself have pushed a
# bin past that tolerance, so a smaller bar here would name a shift that
# explains nothing.
#
# This is a magnitude bar, NOT the discriminator — see
# :func:`classify_delta_probe` for the two conditions that actually separate
# "the level moved" from "a shape defect that happens to have a mean".
DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB: float = 1.5

# How spread the quiet evidence must be, relative to a FULL sampling of the band
# its level is claimed over, before that claim may be made band-wide (#2533).
#
# The quantity is :attr:`DeltaProbeMap.quiet_probe_coverage`: the interquartile
# octave span of the quiet bins divided by **the same statistic taken over every
# graded-band bin on the same grid**. Not divided by the band's whole span —
# that ratio is not a property of the evidence, it is a property of the GRID.
# Production grids are structurally LINEAR (``rfftfreq``; ``spatial_combine``
# refuses anything else, because ``smooth_fractional_octave`` assumes linear
# bins), so bin density rises with frequency and any interquartile span is pulled
# toward the top octaves. Measured on the retained 2026-08-15 grid (511 bins,
# 46.9208 Hz apart), a quiet set sampling a 357 Hz-10 kHz graded band perfectly
# and uniformly scored **0.303** against that band's whole span — i.e. the
# whole-span form of this ratio could not be cleared by ANY quiet set on real
# data, at any band width.
#
# Dividing by the band's own interquartile span fixes that by construction:
# numerator and denominator are the same statistic, over the same grid, so
# whatever the grid does to one it does to the other. **1.0 therefore means
# "spread exactly like a full sampling of the graded band", and it is grid
# invariant** — an interleaved co-spanning quiet set measures 1.000 on the
# production linear grid and 1.000 on a log one (pinned).
#
# **0.5 is a judgment, not a derivation, and it is stated as one.** What is
# derived is the 1.0 reference above. The bar says a quiet set must be at least
# half as spread as the band's own bins are, and it is placed in a wide measured
# gap rather than fitted to anything: on the production grid the tightest PASSING
# shape (co-spanning in alternating half-octave blocks) scores 0.870 and the
# shape this exists to catch scores 0.248, so any bar between roughly 0.3 and 0.8
# makes the same calls. That is why the two numbers are disclosed on every map —
# :attr:`DeltaProbeMap.quiet_probe_coverage` and
# :attr:`DeltaProbeMap.quiet_core_band_hz` — rather than reduced away to a
# pass/fail.
#
# What it rules out is the case that produced it (2026-08-15 JTS3 cycle 4): the
# correction commanded 463 Hz-12 kHz, so the quiet set was a 12-20 kHz sliver —
# 158 of its 160 bins above 12 kHz, coverage 0.239 — and the level measured in it
# was reported as a whole-band ``uncommanded_level_shift``.
DELTA_PROBE_MIN_QUIET_COVERAGE: float = 0.5

#: ``reason`` for a level shift the quiet bins measured across the WHOLE graded
#: band — the ordinary case, and the only one that supports a whole-band claim.
REASON_UNCOMMANDED_LEVEL_SHIFT = "uncommanded_level_shift"
#: ``reason`` for the same finding when the quiet bins that measured it are less
#: spread than :data:`DELTA_PROBE_MIN_QUIET_COVERAGE` of the graded band
#: (#2533). The finding stands and the verdict is unchanged — what narrows is
#: the BAND the claim names, which travels as
#: :attr:`DeltaProbeMap.quiet_core_band_hz`.
REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND = (
    "uncommanded_level_shift_outside_probe_band"
)

#: Appended to a non-matched verdict's ``reason`` when there were too few
#: quiet bins to measure ``residual_offset_db`` at all (#1811). The verdict is
#: still the honest one for the evidence available — but it was reached
#: WITHOUT the level discriminator, so an undeclared level shift could be
#: wearing a shape defect's clothes, and a rollback decided that way should
#: say so in the same string a reader is already looking at.
_LEVEL_CHECK_UNAVAILABLE_SUFFIX = "|level_check_unavailable"


# --------------------------------------------------------------------------- #
# spatial arm
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpatialCost:
    """Did the correction make the room LESS even? (the spatial arm)

    Built from the two position groups the flow already walks — CLOUD_MEASURE
    (pre-apply) and CLOUD_VERIFY (post-apply) — so unlike the at-the-mark arm
    this one really is measurement-minus-measurement. ``available`` is False
    when either group carries no usable spread (fewer than two positions: the
    express tier's post-apply group is the mark alone by design, and
    ``spatial_combine`` returns no ``band_spread`` below N=2).
    """

    available: bool
    widened: bool
    worst_center_hz: float
    worst_widening_db: float
    tolerance_db: float
    n_bands: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "widened": self.widened,
            "worst_center_hz": self.worst_center_hz,
            "worst_widening_db": self.worst_widening_db,
            "tolerance_db": self.tolerance_db,
            "n_bands": self.n_bands,
        }


SPATIAL_COST_UNAVAILABLE = SpatialCost(
    available=False, widened=False, worst_center_hz=0.0,
    worst_widening_db=0.0, tolerance_db=DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
    n_bands=0,
)


def evaluate_spatial_cost(
    before: Sequence[Any],
    after: Sequence[Any],
    *,
    tolerance_db: float = DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB,
) -> SpatialCost:
    """Compare two clouds' per-octave level spread, before vs after the apply.

    ``before``/``after`` are ``spatial_combine.BandSpread`` sequences (duck-
    typed on ``center_hz``/``sigma_db``, so a test fixture need not import the
    real class). Bands are paired by ``center_hz``; a band present in only one
    group is skipped rather than compared against nothing.

    ``sigma_db`` — the spread of each position's BAND LEVEL — is the right
    reading here, not ``max_sigma_db``. ``max_sigma_db`` rides comb nulls on
    purpose, and comb structure moves with the microphone whether or not a
    correction was applied; a level-spread comparison asks the question this
    verdict is actually about, which is whether the corrected speaker is more
    or less even across the room than the uncorrected one was.
    """
    before_by_center = {
        round(float(b.center_hz), 3): float(b.sigma_db)
        for b in before
        if math.isfinite(float(b.sigma_db))
    }
    worst_center_hz = 0.0
    worst_widening_db = -math.inf
    n_bands = 0
    for band in after:
        sigma_after = float(band.sigma_db)
        if not math.isfinite(sigma_after):
            continue
        key = round(float(band.center_hz), 3)
        if key not in before_by_center:
            continue
        n_bands += 1
        widening = sigma_after - before_by_center[key]
        if widening > worst_widening_db:
            worst_widening_db = widening
            worst_center_hz = float(band.center_hz)
    if n_bands == 0:
        return SPATIAL_COST_UNAVAILABLE
    return SpatialCost(
        available=True,
        widened=worst_widening_db > tolerance_db,
        worst_center_hz=worst_center_hz,
        worst_widening_db=float(worst_widening_db),
        tolerance_db=float(tolerance_db),
        n_bands=n_bands,
    )


# --------------------------------------------------------------------------- #
# the map
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeltaProbeMap:
    """One applied correction's realized-vs-commanded verdict and evidence.

    ``verdict`` is one of :data:`DELTA_PROBE_VERDICTS`; ``rollback`` is True
    exactly when it is in :data:`DELTA_PROBE_ROLLBACK_VERDICTS`, computed here
    so no caller can decide it differently.

    ``gain_factor`` is the least-squares realized/commanded scale over the
    probe band — 1.0 means the correction landed at full depth, 0.6 means it
    delivered 60% of what it asked for. It is reported on every classified map,
    not just the shortfall one, because it is the single most legible number in
    this record for a human reading the journal, and it is ``None`` on an
    unavailable map: 0.0 there would read as the measured claim "delivered
    nothing", which is the opposite of "not measured".

    **``gain_factor`` carries an INTERCEPT** (:attr:`gain_intercept_db`) and is
    measured on the frame-removed curve (#2521). Through the origin, on a
    commanded curve that is mostly one sign, a pure level offset arrives as
    apparent scale: the first remote JTS3 session's constant −7.8 dB against a
    76.5 %-negative commanded curve read as scale **2.02** and drove the
    shortfall-vs-model-error branch on a number that was measuring the level
    axis. A regression that may state where it crosses zero cannot make that
    mistake, and the offset it would have absorbed is reported beside it.
    """

    verdict: str
    reason: str
    probe_band_hz: tuple[float, float]
    n_bins: int
    max_error_db: float
    rms_error_db: float
    worst_hz: float
    exceedance_octaves: float
    gain_factor: float | None
    tolerance_low_db: float
    tolerance_high_db: float
    spatial: SpatialCost
    #: The level move the EMITTER told us it made, dB, removed from the
    #: realized curve before anything below was computed (#1811). Disclosed so
    #: a reader can tell a probe that was level-corrected from one that was
    #: not, and by how much — every scalar above is measured AFTER its removal.
    expected_offset_db: float = 0.0
    #: The level CHANGE across the apply that nobody commanded, measured where
    #: the correction commanded NOTHING — the mean of
    #: ``(realized − expected_offset) − commanded − entry_delta`` across the
    #: in-band bins BELOW the commanded floor. The ``− commanded`` term is
    #: small but real there rather than exactly zero (the floor admits up to
    #: :data:`DELTA_PROBE_MIN_COMMANDED_DB`, so it can bias this by up to
    #: 0.5 dB against a 1.5 dB bar) and is subtracted rather than assumed away.
    #: Any level in those bins is uncommanded by construction, which is what
    #: makes it separable from a shape defect inside the probe band.
    #:
    #: **A CHANGE, not an absolute** (#2533). The ``− entry_delta`` term is the
    #: pre-apply capture in the same frame, so the standing disagreement between
    #: an in-room measurement and an on-axis model — which is not a level move at
    #: all, and was the largest term in the 2026-08-15 JTS3 verdict — cancels
    #: instead of being reported as one. See :attr:`entry_anchor_offset_db` for
    #: the term that was removed and what it means when it is ``None``.
    #:
    #: **On a CHAINED round this number is not trustworthy without checking the
    #: previous round's commanded band.** It carries ``−mean(previous round's
    #: commanded curve over these bins)``, which is zero when that round
    #: commanded nothing here and unbounded when it did — see
    #: :func:`classify_delta_probe`'s docstring for the fabricate and mask cases.
    #:
    #: ``None`` when the correction commands something almost everywhere in band
    #: and there are too few quiet bins to measure it — "not measured", which 0.0
    #: would misreport as "measured, and nothing moved" (the same distinction
    #: ``gain_factor`` draws).
    residual_offset_db: float | None = None
    residual_offset_tolerance_db: float = DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
    #: The band the caller HANDED IN — this capture's trusted band. Distinct
    #: from ``probe_band_hz``, which is the span of the bins that then cleared
    #: the commanded floor inside it. Both travel because a reader diagnosing a
    #: verdict needs to know whether a bin was excluded for lack of trust or for
    #: lack of a command (#2521).
    requested_band_hz: tuple[float, float] = (0.0, 0.0)
    #: The frame fitted between the two curves over the QUIET bins — one offset,
    #: one tilt, both uncommanded by construction (#2521). ``FRAME_UNFITTED``
    #: when there were too few quiet bins, which means "no frame measured" and
    #: leaves every ``frame_removed_*`` number below ``None``.
    frame: FrameFit = FRAME_UNFITTED
    #: The three graded scalars again, taken after :attr:`frame` was removed
    #: from the realized curve. ``None`` together and only together, and only
    #: when no frame was fitted — never defaulted to their raw twins, which
    #: would read as "removing the frame changed nothing" (a measurement)
    #: instead of "nothing was measured" (an absence). The RAW twins above are
    #: still what decides whether there is a finding at all; these decide
    #: whether the finding is a rollback.
    frame_removed_max_db: float | None = None
    frame_removed_rms_db: float | None = None
    frame_removed_exceedance_octaves: float | None = None
    #: Where the ``gain_factor`` regression crosses zero commanded, dB — the
    #: level term it no longer has to absorb as scale. ``None`` on an
    #: unavailable map, exactly like ``gain_factor``.
    gain_intercept_db: float | None = None
    #: The STANDING disagreement between the pre-apply measurement and the
    #: two-branch model, dB, measured over the same quiet bins (#2533) — the
    #: mean of the caller's ``entry_delta_db``. Removed from
    #: :attr:`residual_offset_db` so that field measures a change rather than an
    #: absolute.
    #:
    #: ``None`` means **not measured**, and is treated exactly the way an
    #: unsupplied :attr:`expected_offset_db` is: nothing is removed, so the
    #: standing offset stays visible inside ``residual_offset_db`` rather than
    #: being pretended away. Reached when the caller supplies no pre-apply curve
    #: (a session with no entry baseline, a state file written before that key
    #: shipped, a length disagreement) or when too few quiet bins carry a finite
    #: one.
    #:
    #: It discloses what was REMOVED, and cannot disclose what the removal left
    #: behind: on a chained round the entry capture rides a graph whose own
    #: correction this module never sees, so a non-``None`` anchor is not a
    #: warrant that the residual beside it is clean — see
    #: :func:`classify_delta_probe`'s docstring.
    entry_anchor_offset_db: float | None = None
    #: How many quiet bins :attr:`residual_offset_db` was measured over. Distinct
    #: from ``frame.n_bins``: the frame is always fitted over the whole quiet
    #: set, while an anchored residual is measured only where the pre-apply curve
    #: is finite too.
    quiet_n_bins: int = 0
    #: The INTERQUARTILE span of those bins' frequencies, Hz — the middle half,
    #: which is where they actually sit (#2533). ``frame.band_hz`` reports their
    #: min/max, and min/max is what two stray bins defeat: on 2026-08-15 a set
    #: with 158 of 160 bins above 12 kHz reported a 463 Hz-20 kHz span because
    #: one bin sat at 493 Hz and one at 1.9 kHz. ``None`` when no residual was
    #: measured.
    quiet_core_band_hz: tuple[float, float] | None = None
    #: :attr:`quiet_core_band_hz`'s span in octaves divided by the SAME
    #: statistic over every graded-band bin on this grid — how spread the
    #: evidence for a level claim is, relative to a full sampling of the band
    #: that claim is made over. A quiet set spread like the graded band's own
    #: bins scores exactly 1.0 (see
    #: :data:`DELTA_PROBE_MIN_QUIET_COVERAGE`); below that bar the level verdict
    #: keeps its finding but narrows its reason to
    #: :data:`REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND`. ``None`` when no
    #: residual was measured, or when the graded band spans no octaves at all
    #: for a coverage to be a fraction OF.
    quiet_probe_coverage: float | None = None
    #: Did a BOOST realize more lift than the applied graph declared,
    #: structurally? (#2537) The adoption table's one delta-probe-sourced hard
    #: stop — see :func:`boost_overshoot` for the rule, why it is the one
    #: directional exceedance in this module, and why since #2614 it is asked
    #: over the SAFETY bins (this apply's changes UNION the applied graph's own
    #: declared transfer) rather than the graded ones alone. ``False`` both when
    #: nothing overshot and when no safety bin carried a boost; the two are told
    #: apart by :attr:`boost_overshoot_db`, ``None`` only in the second case.
    boost_over_declared_bound: bool = False
    #: The worst signed ``realized − commanded``, dB, over the safety bins where
    #: a boost was on the table. Positive is over-realized. ``None`` when none
    #: was — "not measured", never 0.0.
    boost_overshoot_db: float | None = None
    #: The widest contiguous run, in octaves, over which that excess cleared
    #: this probe's per-bin tolerance. ``0.0`` means nothing cleared it.
    boost_overshoot_octaves: float = 0.0
    #: Did ANY safety bin realize LOUDER than commanded, past that bin's own
    #: tolerance? (#2559) The direction of the shape miss, measured over every
    #: safety bin rather than only the boosted ones — see
    #: :func:`louder_than_commanded` for why it is unstructured, why it is
    #: taken on the raw realized curve, and which mask reaches it. ``False`` on
    #: an unavailable map, where :attr:`max_signed_error_db` is ``None`` to say
    #: nothing was measured.
    realized_louder_than_commanded: bool = False
    #: The most POSITIVE ``realized − commanded`` over the safety bins, dB.
    #: Negative on a map whose every bin realized quieter than commanded — which
    #: is a measurement, and the one :attr:`realized_louder_than_commanded`
    #: reduces to a finding against the per-bin tolerance. ``None`` when no bin
    #: was in the safety mask, never 0.0 (``gain_factor``'s distinction).
    max_signed_error_db: float | None = None
    #: How much of what was commanded arrived, PER BAND (#2649), keyed by
    #: :data:`DELTA_PROBE_REALIZATION_BANDS`. Each entry is
    #: ``{band_hz, n_bins, ratio, graded}``; ``ratio`` is ``None`` for a band
    #: too thin to fit a slope through, and ``graded`` is False for the
    #: above-ceiling band, which is reported so a reader can see what the
    #: mic-trust ceiling excluded and never enters a verdict.
    #:
    #: Empty on every map that never reached the fit (``unavailable``,
    #: ``safety_only``) — the same "not measured" those maps already report by
    #: leaving ``gain_factor`` ``None``.
    band_realization: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    #: The mic-trust ceiling the caller applied, in Hz — ``None`` when the
    #: caller supplied none and the graded band is the requested one.
    trust_ceiling_hz: float | None = None
    #: The band actually GRADED: the requested band intersected with the
    #: ceiling above. Reported beside ``requested_band_hz`` rather than
    #: replacing it, so "what the caller asked for" and "what was judged" stay
    #: two readable facts instead of one ambiguous one.
    graded_band_hz: tuple[float, float] | None = None

    @property
    def trusted_floor_hz(self) -> float | None:
        """The graded band's lower edge — the gate's trusted floor.

        A named accessor because the round receipt banks this number beside its
        objectives (#2609 SF5): a later round can then refuse a cross-floor
        comparison instead of consuming a gate-length change as movement. It is
        the requested band's floor by construction — the ceiling only ever
        narrows the top — and reading it off the graded band keeps floor and
        ceiling coming from one place.
        """
        band = self.graded_band_hz or self.requested_band_hz
        return None if band is None else float(band[0])

    @property
    def matched(self) -> bool:
        return self.verdict == VERDICT_MATCHED

    @property
    def rollback(self) -> bool:
        return self.verdict in DELTA_PROBE_ROLLBACK_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "rollback": self.rollback,
            "probe_band_hz": list(self.probe_band_hz),
            "n_bins": self.n_bins,
            "max_error_db": self.max_error_db,
            "rms_error_db": self.rms_error_db,
            "worst_hz": self.worst_hz,
            "exceedance_octaves": self.exceedance_octaves,
            "gain_factor": self.gain_factor,
            "tolerance_low_db": self.tolerance_low_db,
            "tolerance_high_db": self.tolerance_high_db,
            "expected_offset_db": self.expected_offset_db,
            "residual_offset_db": self.residual_offset_db,
            "residual_offset_tolerance_db": self.residual_offset_tolerance_db,
            "requested_band_hz": list(self.requested_band_hz),
            "gain_intercept_db": self.gain_intercept_db,
            # The quiet-bin evidence ``residual_offset_db`` rests on, nested for
            # the reason the frame block below is: a reader judging a level
            # claim needs how many bins measured it, where they sit, how much of
            # the graded band they cover, and what standing offset was removed —
            # as one set, not four keys to pair up by name.
            "quiet": {
                "n_bins": self.quiet_n_bins,
                "core_band_hz": (
                    None if self.quiet_core_band_hz is None
                    else list(self.quiet_core_band_hz)
                ),
                "probe_coverage": self.quiet_probe_coverage,
                "entry_anchor_offset_db": self.entry_anchor_offset_db,
            },
            # The directional boost finding, nested for the same reason: a
            # reader judging a hard stop needs the answer, the amount, and how
            # wide it ran as one set (#2537).
            "boost": {
                "over_declared_bound": self.boost_over_declared_bound,
                "overshoot_db": self.boost_overshoot_db,
                "overshoot_octaves": self.boost_overshoot_octaves,
            },
            # Which WAY the graded bins missed, nested for the boost block's
            # reason (#2559): the finding and the amount behind it are one set,
            # and a reader asking whether a rollback was deferred needs both.
            "direction": {
                "realized_louder_than_commanded": (
                    self.realized_louder_than_commanded
                ),
                "max_signed_error_db": self.max_signed_error_db,
                "seam_rollback_deferral": seam_rollback_deferral(self),
            },
            # The frame's own terms and the grades taken with it removed, nested
            # together so a reader picks a frame of reference once and reads a
            # matching set rather than pairing keys by name.
            # How much of the commanded correction arrived, PER BAND, with the
            # trusted-band provenance that says which bins were allowed to
            # answer (#2649). Nested for the reason every other block here is:
            # a reader judging a realization claim needs the per-band ratios,
            # the band that was graded, and the ceiling that narrowed it as one
            # set, not five keys to pair up by name.
            #
            # ``pooled`` is ``gain_factor`` under its band-resolved name, kept
            # here as well as at the top level so a consumer reading this block
            # never has to reach outside it to compare the whole-band answer
            # against the per-band ones.
            "realization": {
                "pooled": self.gain_factor,
                "bands": {
                    band_id: dict(entry)
                    for band_id, entry in self.band_realization.items()
                },
                "graded_band_hz": (
                    None if self.graded_band_hz is None
                    else list(self.graded_band_hz)
                ),
                "trusted_floor_hz": self.trusted_floor_hz,
                "trust_ceiling_hz": self.trust_ceiling_hz,
            },
            "frame": {
                **self.frame.to_dict(),
                "removed": {
                    "max_db": self.frame_removed_max_db,
                    "rms_db": self.frame_removed_rms_db,
                    "exceedance_octaves": self.frame_removed_exceedance_octaves,
                },
            },
            "spatial": self.spatial.to_dict(),
        }


def _unavailable(
    reason: str,
    spatial: SpatialCost,
    *,
    expected_offset_db: float = 0.0,
    requested_band_hz: tuple[float, float] = (0.0, 0.0),
) -> DeltaProbeMap:
    return DeltaProbeMap(
        verdict=VERDICT_UNAVAILABLE, reason=reason, probe_band_hz=(0.0, 0.0),
        n_bins=0, max_error_db=0.0, rms_error_db=0.0, worst_hz=0.0,
        exceedance_octaves=0.0, gain_factor=None,
        tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
        tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
        spatial=spatial,
        expected_offset_db=expected_offset_db,
        requested_band_hz=requested_band_hz,
    )


def _safety_only(
    spatial: SpatialCost,
    *,
    expected_offset_db: float,
    requested_band_hz: tuple[float, float],
    probe_band_hz: tuple[float, float],
    n_bins: int,
    boost_over_declared_bound: bool,
    boost_overshoot_db: float | None,
    boost_overshoot_octaves: float,
    realized_louder_than_commanded: bool,
    max_signed_error_db: float | None,
) -> DeltaProbeMap:
    """A map carrying the two directional findings and NO shape grade (#2614).

    Deliberately shaped like :func:`_unavailable` plus five fields. Every shape
    and level scalar keeps its dataclass default — 0.0 error, ``None`` gain,
    ``None`` residual, an unfitted frame — because on this path the classifier
    was handed the STATE axis in the commanded slot, and every one of those
    numbers computed against a state axis would be a claim in the wrong frame:
    the residual is the chained-round contaminant #2611 removed, and the frame
    fit sits on quiet bins that mean something else. Returning them "for
    information" is how a wrong number gets read as a right one, so they are
    not returned at all.

    The two that ARE returned survive the frame change because they are
    measured on ``realized − commanded``, which is ``measured_post −
    predicted_post`` whichever graph the two sides are stated against.
    """
    return DeltaProbeMap(
        verdict=VERDICT_SAFETY_ONLY,
        reason=REASON_COMMANDED_AXIS_UNAVAILABLE,
        probe_band_hz=probe_band_hz,
        n_bins=n_bins,
        max_error_db=0.0, rms_error_db=0.0, worst_hz=0.0,
        exceedance_octaves=0.0, gain_factor=None,
        tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
        tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
        spatial=spatial,
        expected_offset_db=expected_offset_db,
        requested_band_hz=requested_band_hz,
        boost_over_declared_bound=boost_over_declared_bound,
        boost_overshoot_db=boost_overshoot_db,
        boost_overshoot_octaves=boost_overshoot_octaves,
        realized_louder_than_commanded=realized_louder_than_commanded,
        max_signed_error_db=max_signed_error_db,
    )


def _tolerance_curve(freqs_hz: np.ndarray) -> np.ndarray:
    """The two-tier per-bin tolerance (see the two tolerance constants)."""
    return np.where(
        freqs_hz < DELTA_PROBE_HF_SPLIT_HZ,
        DELTA_PROBE_TOLERANCE_LOW_DB,
        DELTA_PROBE_TOLERANCE_HIGH_DB,
    )


def graded_command_floor_db(freqs_hz: np.ndarray) -> np.ndarray:
    """The per-bin commanded floor a bin must clear to be GRADED (#2521).

    The two-tier sibling of :func:`_tolerance_curve`, split at the same
    frequency and for the same reason — see
    :data:`DELTA_PROBE_MIN_COMMANDED_HIGH_DB`. Public because the band a probe
    graded is only reconstructible offline with the rule that produced it.

    NOT the quiet floor. A bin under :data:`DELTA_PROBE_MIN_COMMANDED_DB` is
    where the correction asked for NOTHING, which is a statement about the
    command and not about measurement uncertainty, so that floor stays flat
    across the band and keeps ``residual_offset_db`` and the fitted frame
    measuring exactly what they measured before this tier existed. Between the
    two, at HF, sits a band of bins that are neither: something was asked for,
    but less than the uncertainty this probe already concedes there. Those bins
    are graded by nothing and corroborate nothing, which is the honest answer
    for them.
    """
    return np.where(
        freqs_hz < DELTA_PROBE_HF_SPLIT_HZ,
        DELTA_PROBE_MIN_COMMANDED_DB,
        DELTA_PROBE_MIN_COMMANDED_HIGH_DB,
    )


def widest_exceedance_octaves(
    freqs_hz: np.ndarray, exceeds: np.ndarray,
) -> tuple[float, float]:
    """``(widest contiguous run in octaves, that run's low edge in Hz)``.

    A "run" is contiguous in GRID INDEX, not merely in the exceeding set — two
    exceeding bins either side of a compliant one are two runs, which is the
    whole point of a width rule. Width is measured in log2 frequency between
    the run's first and last bin, so it is the same quantity at 500 Hz and at
    15 kHz. Returns ``(0.0, 0.0)`` when nothing exceeds.
    """
    widest = 0.0
    widest_lo_hz = 0.0
    idx = np.flatnonzero(exceeds)
    if idx.size == 0:
        return 0.0, 0.0
    breaks = np.flatnonzero(np.diff(idx) != 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    for s, e in zip(starts, ends):
        lo_hz = float(freqs_hz[idx[s]])
        hi_hz = float(freqs_hz[idx[e]])
        if lo_hz <= 0.0 or hi_hz <= 0.0:
            continue
        span = math.log2(hi_hz / lo_hz) if hi_hz > lo_hz else 0.0
        if span > widest:
            widest = span
            widest_lo_hz = lo_hz
    return widest, widest_lo_hz


def _structured_exceedance(
    freqs_hz: np.ndarray,
    error_db: np.ndarray,
    tolerance_db: np.ndarray,
    probe_mask: np.ndarray,
) -> tuple[bool, float]:
    """``(is a real finding, widest run in octaves)`` for one error curve.

    **Every array is on the FULL grid**, and ``probe_mask`` marks the bins
    inside the probe band. That is load-bearing, not a convenience: the width
    rule counts a run as contiguous in GRID INDEX, so it can only tell
    structure from texture if the grid it walks is the real one. Evaluating it
    on the compacted ``freqs[mask]`` subarray instead silently welds bins that
    are octaves apart in Hz into one "wide" run whenever the mask has a hole —
    and the commanded floor puts a hole in it on every ordinary correction that
    cuts low and boosts high. Two isolated single-bin errors either side of
    such a gap then scored 2.9 octaves of structure and rolled the correction
    back (adversarial review B1, reproduced). Masking the EXCEEDANCE instead
    keeps every removed bin as a run-breaker, which is what it physically is:
    a frequency where the correction asked for nothing, so nothing there can
    corroborate a defect on the far side of it.
    """
    exceeds = probe_mask & (np.abs(error_db) > tolerance_db)
    widest, _ = widest_exceedance_octaves(freqs_hz, exceeds)
    return widest >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES, widest


def boost_overshoot(
    freqs_hz: np.ndarray,
    realized_db: np.ndarray,
    commanded_db: np.ndarray,
    tolerance_db: np.ndarray,
    probe_mask: np.ndarray,
    declared_db: np.ndarray | None = None,
) -> tuple[bool, float | None, float]:
    """Did a BOOST realize MORE lift than the graph declared? (#2537)

    ``(over the declared bound, worst signed excess in dB, widest run in
    octaves)``, measured only in graded bins where a **boost** is on the table.
    Every array is on the full grid, exactly as :func:`_structured_exceedance`
    requires and for the same run-contiguity reason.

    **Two axes select those bins, and the second one is what makes this a STATE
    question** (#2614). ``commanded_db`` is a CHANGE — what this apply asks the
    summed response to do relative to the graph it replaces — so on a repeat
    round it is ~0 across every band the apply leaves alone, including a band
    the applied graph still boosts by 5 dB. ``declared_db`` is that graph's own
    predicted transfer against the uncorrected crossover, so it is where the
    standing boosts are, and a bin qualifies when EITHER curve boosts. The
    union is deliberate: the hearing-safety question is "is the speaker putting
    more energy into a driver than the applied graph declares, ANYWHERE", and
    a band this apply did not touch is exactly as able to be over-realized as
    one it did. ``None`` falls back to ``commanded_db`` alone, which is both
    the pre-#2614 behaviour and exactly right for a first-ever apply — there
    the graph being replaced is the raw crossover, so the two curves are one.

    **Directional, where every other exceedance rule here is not.** The rest of
    this module asks "did realized and commanded disagree", takes ``abs``, and
    is right to: a shape defect is a defect in either direction. This one asks
    the hearing-safety question instead — *is the speaker putting more energy
    into a driver than the graph declared it would* — and under-realizing a
    boost is not that. A boost that delivered 3 dB of a commanded 6 is a
    quality miss to learn from; one that delivered 9 is energy nobody asked
    for.

    The bound is this probe's own per-bin tolerance, which is why it is an
    argument rather than a second constant: a bin is over the bound when
    ``realized − commanded > tolerance(f)``, the same slack the graded verdict
    already concedes. And the finding is STRUCTURED on the same rule as every
    other one here (:data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES`), so a single
    noisy bin cannot pull a household's correction off the speaker.

    The middle value is ``None`` when no graded bin carried a boost on either
    axis — "not measured", never 0.0, which would read as "measured, and it did
    not overshoot" (the distinction ``gain_factor`` and ``residual_offset_db``
    both draw). A cut-only graph reaches that, and so does a boost whose bins
    all fell under the graded floor.
    """
    declared = commanded_db if declared_db is None else declared_db
    boosted = probe_mask & ((commanded_db > 0.0) | (declared > 0.0))
    if not bool(boosted.any()):
        return False, None, 0.0
    excess = realized_db - commanded_db
    worst = float(np.max(excess[boosted]))
    widest, _ = widest_exceedance_octaves(
        freqs_hz, boosted & (excess > tolerance_db)
    )
    return widest >= DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES, worst, float(widest)


def louder_than_commanded(
    realized_db: np.ndarray,
    commanded_db: np.ndarray,
    tolerance_db: np.ndarray,
    probe_mask: np.ndarray,
) -> tuple[bool, float | None]:
    """Did ANY graded bin realize LOUDER than commanded, past tolerance? (#2559)

    ``(over the bound anywhere, the most POSITIVE realized − commanded in dB)``.
    The direction of a shape miss, which this module measured nowhere before:
    every other exceedance rule here takes ``abs`` — right for "is the shape
    wrong", useless for "which way".

    **Deliberately unstructured, where every sibling rule is structured.**
    :func:`boost_overshoot` and :func:`_structured_exceedance` require a
    :data:`DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES` run before they will call
    something a finding, because they are deciding whether a defect EXISTS and
    a single noisy bin must not pull a household's correction off the speaker.
    This one is not deciding that — the verdict is already a finding by the time
    anyone asks. It decides whether a finding that exists may be handled the
    LENIENT way, so one bin measured louder is enough to withhold the lenience.
    A width rule here would be a rule for admitting more positive-direction
    evidence into the quiet class, which is the wrong direction to be generous
    in. There is deliberately no octaves term returned, so nothing downstream
    can gate on structure by accident.

    **Bins only, no frame removal.** :func:`boost_overshoot`'s reason, verbatim
    and for the same question: a frame is removed to ask whether the SHAPE is
    right, and this asks how much energy actually reached the driver. Subtracting
    a fitted offset first would hide exactly the whole-band overshoot the
    lenience must not be granted for.

    ``probe_mask`` is :func:`classify_delta_probe`'s SAFETY mask, not its graded
    one (#2614): every bin this apply changed, PLUS every bin the applied graph
    itself declares a transfer in. Same reason :func:`boost_overshoot` takes a
    second axis — the lenience is withheld on evidence about how much energy
    reached the driver, and a band a repeat round left alone still has a driver
    in it.

    ``None`` for the scalar only when the mask selects nothing — "not measured",
    never 0.0. ``classify_delta_probe`` never reaches here with an empty mask
    (:data:`DELTA_PROBE_MIN_BINS` is checked on the graded mask, which the
    safety mask contains), so this arm serves a direct caller rather than a
    production path.
    """
    if not bool(probe_mask.any()):
        return False, None
    excess = realized_db - commanded_db
    return (
        bool((probe_mask & (excess > tolerance_db)).any()),
        float(np.max(excess[probe_mask])),
    )


def _octave_span(span_hz: tuple[float, float]) -> float:
    """A ``(low, high)`` frequency span's width in octaves; ``0.0`` if degenerate.

    Measured in log2 so it is the same quantity at 500 Hz and at 15 kHz, exactly
    like :func:`widest_exceedance_octaves`' run width.
    """
    lo, hi = float(span_hz[0]), float(span_hz[1])
    if not (lo > 0.0 and hi > lo):
        return 0.0
    return math.log2(hi / lo)


def interquartile_band_hz(freqs_hz: np.ndarray) -> tuple[float, float] | None:
    """The middle half of a bin set, as ``(low, high)`` in hertz (#2533).

    The robust reading of "where does this evidence sit": min/max is what two
    stray bins defeat, and the delta probe already reports that as
    ``frame.band_hz``. Public because the coverage ratio a verdict turns on is
    only reconstructible offline with the statistic that produced it.

    ``None`` for an empty set, or one whose quartiles do not straddle a positive
    span — no middle half, so no span to state.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    if freqs.size == 0:
        return None
    lo, hi = (float(v) for v in np.percentile(freqs, (25.0, 75.0)))
    return (lo, hi) if hi > lo > 0.0 else None


def _band_realization(
    freqs: np.ndarray,
    deframed: np.ndarray,
    commanded: np.ndarray,
    *,
    graded: np.ndarray,
    in_band: np.ndarray,
    ceiling_hz: float | None,
) -> dict[str, dict[str, Any]]:
    """Per-band realization ratio: how much of what was commanded arrived.

    One least-squares slope per band instead of one over all of them, so a
    defect confined to a band cannot be smeared across the whole graded span —
    and so a band the fitter was never allowed to command in cannot contribute
    to the verdict at all. See :data:`DELTA_PROBE_REALIZATION_BANDS` for the
    three ids and why they are cut where they are.

    Each entry carries ``band_hz`` (the bins actually present, not the nominal
    edges), ``n_bins``, ``ratio``, and ``graded``. ``ratio`` is ``None`` for a
    band with fewer than :data:`DELTA_PROBE_MIN_BINS` bins — too few to fit a
    slope through, and a slope that says nothing must not be reported as a
    number that does. ``graded`` is False for ``above_ceiling`` always: those
    bins are reported so a reader can SEE what the ceiling excluded, and they
    are excluded from the verdict for the same reason the fitter may not
    command there.
    """

    hf = freqs >= DELTA_PROBE_HF_SPLIT_HZ
    above = (
        in_band & (freqs > float(ceiling_hz))
        if ceiling_hz is not None
        else np.zeros_like(in_band)
    )
    selectors = (
        (DELTA_PROBE_BAND_CROSSOVER, graded & ~hf, True),
        (DELTA_PROBE_BAND_TRUSTED_HF, graded & hf, True),
        (DELTA_PROBE_BAND_ABOVE_CEILING, above & np.isfinite(commanded), False),
    )
    out: dict[str, dict[str, Any]] = {}
    for band_id, sel, is_graded in selectors:
        n = int(np.count_nonzero(sel))
        entry: dict[str, Any] = {
            "band_hz": (
                [float(freqs[sel][0]), float(freqs[sel][-1])] if n else None
            ),
            "n_bins": n,
            "ratio": None,
            "graded": bool(is_graded),
        }
        if n >= DELTA_PROBE_MIN_BINS:
            c_b = commanded[sel]
            design = np.column_stack((np.ones_like(c_b), c_b))
            try:
                _, slope = np.linalg.lstsq(
                    design, deframed[sel], rcond=None,
                )[0]
            except (np.linalg.LinAlgError, ValueError):
                slope = np.nan
            if np.isfinite(slope):
                entry["ratio"] = float(slope)
        out[band_id] = entry
    return out


def classify_delta_probe(
    freqs_hz: np.ndarray,
    realized_delta_db: np.ndarray,
    commanded_delta_db: np.ndarray,
    *,
    band_hz: tuple[float, float],
    spatial: SpatialCost = SPATIAL_COST_UNAVAILABLE,
    expected_offset_db: float = 0.0,
    entry_delta_db: Any | None = None,
    declared_transfer_db: Any | None = None,
    trust_ceiling_hz: float | None = None,
    state_axis_only: bool = False,
) -> DeltaProbeMap:
    """Classify one applied correction's realized-vs-commanded map.

    All three arrays share one frequency grid (the caller interpolates). The
    probe band is ``band_hz`` intersected with the bins where the correction
    commands at least :func:`graded_command_floor_db` — outside that, either
    nothing was asked for or less was asked for than this probe's own tolerance
    concedes at that frequency, and there is nothing to verify either way.

    ``band_hz`` is the band the CALLER trusts, and this function does not
    second-guess it: it owns no gate, no validity floor, and no ceiling. The
    production caller hands in the capture's own gate-derived trusted band
    (:func:`jasper.audio_measurement.gate_disclosure.evaluation_band_hz`), which
    is the single owner of that decision. Handing in the raw grid edges instead
    is what let a bin 1.3 kHz above the trusted ceiling produce a 23.4 dB
    headline and a rollback (#2521).

    ``expected_offset_db`` is the whole-band level move the EMITTER knows it
    made across the apply and did NOT command as part of the correction's shape
    — in production, the pre-split headroom the applied graph charges for its
    own boost (``baseline_profile.applied_program_level_delta_db``; negative
    when the apply made the speaker quieter). It is subtracted from the
    realized curve before anything is measured, because this comparison is not
    mean-centered and would otherwise read that shift as a defect (see the
    module docstring). Default ``0.0`` — an unsupplied or non-finite offset
    means "nothing known", which is honest and leaves the whole shift visible
    in ``residual_offset_db`` rather than pretending it was accounted for.

    ``entry_delta_db`` is the PRE-apply capture in the same frame as
    ``realized_delta_db`` — ``measured_pre − predicted_previous``, on the same
    grid — so ``residual_offset_db`` can be a CHANGE (#2533). Optional,
    and governed by exactly ``expected_offset_db``'s rule: an unsupplied curve,
    one whose length disagrees, or one that is non-finite across the quiet bins
    all mean "nothing known", so nothing is removed and the standing offset
    stays visible rather than being pretended away. A length disagreement is an
    absence here rather than the ``grid_mismatch`` the three graded arrays get,
    for ``verify_measured_curve_from_state``'s reason: a truncated optional
    record should read as "no anchor", not reach the classifier as a bad grid.

    ``declared_transfer_db`` is the STATE axis the two directional safety rules
    read, and it exists because ``commanded_delta_db`` stopped being one (#2614).
    It is the applied graph's OWN predicted transfer against the uncorrected
    crossover — what the graph declares it does, not what this apply changes —
    on the same grid. :func:`boost_overshoot` and
    :func:`louder_than_commanded` are then measured over the UNION of the two
    axes' graded bins, so a repeat round that leaves an existing boost band
    untouched still has that band watched. Optional, on the same rule as
    ``entry_delta_db``: unsupplied or length-disagreeing means "nothing known"
    and the graded mask alone is used, which is the pre-#2614 behaviour and an
    identity on a first-ever apply. Everything else this function measures —
    the error curve, the statistics, the exceedance width, the gain fit, the
    frame, the quiet residual — is untouched by it, because ``realized −
    commanded`` is ``measured_post − predicted_post`` whichever graph the two
    sides are stated against.

    ``state_axis_only`` says the curve in the ``commanded_delta_db`` slot is
    that STATE axis rather than a change axis, because the caller has no change
    axis to give (#2614). The reachable case is a committed alternative-Fc
    candidate: its branches are composed through a crossover the previous graph
    never ran, so the previous side is refused and the change axis with it —
    while the applied graph's own declared transfer is well-defined at every
    swept corner. The two directional findings then still hold, because they
    are measured on ``realized − commanded``; nothing else does, so nothing
    else is returned. The verdict is :data:`VERDICT_SAFETY_ONLY` and the map
    carries no shape or level grade at all. Do NOT also pass
    ``entry_delta_db`` on that path — it is a change measurement and shares no
    reference with a state axis.

    **CHAINED ROUNDS: the reference graph is shared, and that is what makes the
    residual meaningful** (#2611 closed the #2545 hazard). ``commanded_delta_db``
    and ``entry_delta_db`` are both stated against the graph that was live at
    entry — the former by construction (the caller's previous side IS the
    applied profile), the latter because it is a measurement of that graph — so
    they subtract cleanly and the residual is ``mean(measured_post −
    measured_pre − commanded)`` over the quiet bins. Until #2611 the commanded
    curve was measured against the RAW crossover instead, and a REPEAT round
    therefore carried ``−mean(previous round's commanded curve over this round's
    quiet bins)``: a term this function is never given, cannot bound, and which
    could both fabricate a shift (a previous round correcting out to 20 kHz
    against a new one stopping at 8 kHz measured a +6.000 dB phantom) and mask
    one (a genuine −2.2 dB shift re-grading to 0.000). What the caller still
    owes is that its previous side describe the graph the entry capture actually
    went through. The crossover corner is the part of that which can move, and
    since #2614 both of the ways it can are checked and refused rather than
    owed — see ``crossover_v2.commanded``'s "The corner has to match, and it is
    CHECKED rather than assumed".

    Topology-agnostic by construction: this function knows about a measured
    curve, a commanded curve, and a band. It has no notion of drivers, ways,
    or crossovers, so a 1-way passive speaker's summed chain classifies
    through exactly this code path with no special case.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    realized = np.asarray(realized_delta_db, dtype=np.float64)
    commanded = np.asarray(commanded_delta_db, dtype=np.float64)
    offset = float(expected_offset_db)
    if not math.isfinite(offset):
        offset = 0.0
    requested_band_hz = (float(band_hz[0]), float(band_hz[1]))
    if not (freqs.shape == realized.shape == commanded.shape):
        return _unavailable(
            "grid_mismatch", spatial, expected_offset_db=offset,
            requested_band_hz=requested_band_hz,
        )

    # Remove the KNOWN move before any measurement below. Everything the record
    # reports — max/rms error, worst bin, gain factor, exceedance width — is
    # therefore a statement about what is left after the emitter's own
    # accounting, which is the only part that could be a defect.
    realized = realized - offset

    # The mic-trust ceiling, intersected in (#2649). **The fitter may not
    # command there; the probe may not grade there.** The caller derives the
    # ceiling (this function owns no gate, no validity floor and no ceiling of
    # its own — it is TOLD both bounds), but the intersection happens here so
    # one place decides which bins are graded and the map can report the
    # requested band, the applied ceiling and the result together instead of a
    # reader pairing three numbers from two layers.
    #
    # Why it matters: the grading band comes from the capture's own gate
    # disclosure, which is blind to the mic tier. On a `reference` mic the fit
    # envelope is exactly zero from about 16.4 kHz, so every bin above that was
    # commanded at zero and measured through a microphone nobody trusts —
    # grading them manufactured 90% of the 2026-08-16 round's squared error.
    lo_hz, hi_hz = requested_band_hz
    graded_hi_hz = hi_hz
    if trust_ceiling_hz is not None and float(trust_ceiling_hz) < graded_hi_hz:
        graded_hi_hz = float(trust_ceiling_hz)
    measurable = np.isfinite(realized) & np.isfinite(commanded)
    # What the CALLER asked to grade, before the ceiling narrowed it. Kept so
    # the map can report the excluded span rather than silently dropping it —
    # a ceiling that removes bins without saying so is the same blindness in
    # the other direction.
    requested_in_band = (freqs >= lo_hz) & (freqs <= hi_hz) & measurable
    in_band = requested_in_band & (freqs <= graded_hi_hz)
    floor = graded_command_floor_db(freqs)
    mask = in_band & (np.abs(commanded) >= floor)
    if int(mask.sum()) < DELTA_PROBE_MIN_BINS:
        return _unavailable(
            "nothing_commanded", spatial, expected_offset_db=offset,
            requested_band_hz=requested_band_hz,
        )

    # The STATE axis, and the two directional safety rules are the only things
    # that read it (#2614). ``commanded`` is a CHANGE — since #2611 it is the
    # applied graph minus the graph it replaces — so on a repeat round it is ~0
    # everywhere the apply left alone, and the graded ``mask`` above therefore
    # stops covering a band the applied graph still boosts by 5 dB. The
    # hearing-safety question is not "did the change realize", it is *is the
    # speaker putting more energy into a driver than the applied graph
    # declares, anywhere*, so those two rules watch the UNION: every bin this
    # apply changed, plus every bin the applied graph declares a transfer in.
    #
    # A union rather than a swap, deliberately. The graded mask is what the
    # model_error axis is measured over and it stays exactly as it is; and no
    # bin watched before this change stops being watched, so the fix can only
    # add coverage. ``None`` — a caller that cannot state the applied graph's
    # own transfer — degrades to the graded mask alone, which is both the
    # pre-#2614 behaviour and an identity on a first-ever apply (the graph
    # being replaced IS the raw crossover there, so the two axes coincide).
    declared: np.ndarray | None = None
    if declared_transfer_db is not None:
        candidate_declared = np.asarray(declared_transfer_db, dtype=np.float64)
        if candidate_declared.shape == freqs.shape:
            declared = candidate_declared
    safety_mask = mask
    if declared is not None:
        safety_mask = mask | (
            in_band & np.isfinite(declared) & (np.abs(declared) >= floor)
        )

    f = freqs[mask]
    r = realized[mask]
    c = commanded[mask]
    probe_band_hz = (float(f[0]), float(f[-1]))

    # Scalar statistics read the probe bins only — a bin outside the band
    # contributes to no claim. The exceedance WIDTH, by contrast, is measured
    # on the full grid with the mask applied to the exceedance itself, so grid
    # adjacency survives (see _structured_exceedance).
    error = r - c
    max_error_db = float(np.max(np.abs(error)))
    rms_error_db = float(np.sqrt(np.mean(error ** 2)))
    worst_hz = float(f[int(np.argmax(np.abs(error)))])

    # The uncommanded remainder, measured in the QUIET bins (#1811) — inside
    # the analysis band, but BELOW the commanded floor, so outside the probe
    # band the statistics above were taken over.
    #
    # It has to be measured there. Inside the probe band, "flat across every
    # bin we look at" is exactly what a correction that overshot its whole
    # commanded region also looks like — the probe band IS the commanded
    # region, so the two are indistinguishable from those bins alone. Where the
    # correction asked for nothing, any level is uncommanded by construction.
    # That is a different question with a clean answer, and it is the one this
    # verdict needs.
    quiet = in_band & (np.abs(commanded) < DELTA_PROBE_MIN_COMMANDED_DB)
    quiet_measurable = int(quiet.sum()) >= DELTA_PROBE_MIN_BINS

    # ...and measured as a CHANGE across the apply, not as an absolute
    # disagreement with the model (#2533).
    #
    # ``realized − commanded`` is ``measured_post − predicted_post``: an in-room
    # gated measurement against an on-axis two-branch model. Those two curves do
    # not share a level anchor, and the mismatch between their anchors is a
    # standing property of the comparison — present before the apply, unchanged
    # by it, and not a level MOVE by any reading. Subtracting the same quantity
    # measured on the PRE-apply capture cancels it exactly, because the caller's
    # ``entry_delta_db`` is built against the same model curve:
    #
    #     (measured_post − predicted_raw − offset) − (measured_pre − predicted_raw)
    #         − commanded  ==  (measured_post − measured_pre) − commanded − offset
    #
    # so what is left is measurement-minus-measurement with the two terms the
    # apply DID declare — the correction's own command and the graph's own level
    # move — taken out. ``expected_offset_db`` needs no reinterpretation to sit
    # in that frame; it is already exactly a change across the apply
    # (``profile_program_headroom_db`` of the previous graph minus the applied
    # one), which is what it was always measuring and could not previously say.
    #
    # On the 2026-08-15 JTS3 session the standing term was the LARGEST of the
    # three: a reported −3.342 dB decomposed as −1.660 standing anchoring
    # (already there before the apply), −1.457 real measured change confined to
    # 12-20 kHz, and −0.221 declared graph move. Only the middle term is a level
    # move at all.
    #
    # **The two curves share one reference graph, which is what makes the
    # subtraction an identity** (#2611). ``commanded`` is the applied graph's
    # predicted sum minus the ENTRY graph's, and ``entry_delta`` is a
    # measurement of that same entry graph, so the model term cancels and what
    # is left is ``measured_post − measured_pre − commanded``.
    #
    # It did not always. While ``commanded`` was stated against the RAW
    # crossover instead, a REPEAT round's residual carried a contaminant of
    # exactly
    #
    #     −mean(previous round's commanded curve over this round's quiet bins)
    #
    # — zero when the previous round commanded nothing there, unbounded when it
    # did, and invisible to this function. Two shapes were constructed
    # (adversarial gate, PR #2545): a previous round correcting out to 20 kHz
    # against a new one stopping at 8 kHz put a **+6.000 dB phantom** in the
    # residual, and the same overlap could MASK — a genuine −2.2 dB uncommanded
    # shift re-grading to residual 0.000 and ``frame_mismatch``. Both shapes are
    # unreachable now: the previous round's curve is no longer a hidden term,
    # because the previous GRAPH is an explicit input to the commanded axis.
    # ``entry_anchor_offset_db`` still discloses what was removed.
    entry: np.ndarray | None = None
    if entry_delta_db is not None:
        candidate = np.asarray(entry_delta_db, dtype=np.float64)
        if candidate.shape == freqs.shape:
            entry = candidate
    anchored = (
        quiet_measurable
        and entry is not None
        and int((quiet & np.isfinite(entry)).sum()) >= DELTA_PROBE_MIN_BINS
    )
    # One bin set for the residual and for the anchor removed from it, so the
    # two cannot be means over different bins and the decomposition below is an
    # identity rather than an approximation.
    residual_bins = (quiet & np.isfinite(entry)) if anchored and entry is not None else quiet
    entry_anchor_offset_db: float | None = (
        float(np.mean(entry[residual_bins])) if anchored and entry is not None else None
    )
    residual_offset_db: float | None = (
        float(
            np.mean(realized[residual_bins] - commanded[residual_bins])
            - (entry_anchor_offset_db or 0.0)
        )
        if quiet_measurable
        else None
    )

    # WHERE those bins sit, and how spread they are relative to a FULL sampling
    # of the band their level is claimed over (#2533). A residual is measured in
    # the quiet bins and asserted across the GRADED ones, so how far the evidence
    # itself reaches is part of the claim. The INTERQUARTILE span is the robust
    # reading, and the robustness is load-bearing rather than stylistic: min/max
    # — which is what ``frame.band_hz`` already reports — is exactly what two
    # stray bins defeat, and on 2026-08-15 two of them (493 Hz and 1.9 kHz) made
    # a set with 158 of its 160 bins above 12 kHz span 463 Hz-20 kHz on paper.
    #
    # The denominator is that SAME statistic over every graded-band bin on this
    # grid, never the band's whole span — see
    # :data:`DELTA_PROBE_MIN_QUIET_COVERAGE`. Production grids are linear, so a
    # whole-span denominator measures the grid rather than the evidence, and no
    # real quiet set can clear any bar against it.
    quiet_n_bins = int(residual_bins.sum()) if quiet_measurable else 0
    quiet_core_band_hz: tuple[float, float] | None = None
    quiet_probe_coverage: float | None = None
    if quiet_measurable:
        quiet_core_band_hz = interquartile_band_hz(freqs[residual_bins])
        band_core_hz = interquartile_band_hz(
            freqs[in_band & (freqs >= probe_band_hz[0]) & (freqs <= probe_band_hz[1])]
        )
        band_core_octaves = 0.0 if band_core_hz is None else _octave_span(band_core_hz)
        if quiet_core_band_hz is not None and band_core_octaves > 0.0:
            quiet_probe_coverage = (
                _octave_span(quiet_core_band_hz) / band_core_octaves
            )

    # The FRAME between the two curves, fitted over the QUIET bins (#2521) — the
    # same argument one term further: a slope measured where the correction asked
    # for nothing is uncommanded by construction, just as the level above is.
    #
    # **Its offset term is the ABSOLUTE quiet-bin disagreement, which is the
    # residual only on the unanchored path** (#2533). ``fit_frame`` pivots at the
    # fitted bins' geometric mean, so the offset is the plain mean of
    # ``realized − commanded`` over ``quiet``; ``residual_offset_db`` is that
    # same mean over ``residual_bins`` with ``entry_anchor_offset_db`` removed.
    # The two bin sets coincide unless a supplied entry curve is non-finite
    # somewhere in ``quiet``, in which case ``residual_bins`` is a strict subset.
    # So the identity is ``residual == frame.offset_db − entry_anchor_offset_db``
    # when every quiet bin carried a usable anchor, and it degrades to the
    # pre-#2533 ``residual == frame.offset_db`` when none did.
    #
    # NOT fitted over the graded bins. A two-parameter fit over the region the
    # correction commands lets the defect set its own frame and then subtract
    # itself: on this module's keystone fixture, a graded-bin fit took the
    # 2026-07-27 shelf-Q error's exceedance from 0.575 octaves to zero.
    frame = (
        fit_frame(freqs[quiet], realized[quiet], commanded[quiet])
        if quiet_measurable
        else FRAME_UNFITTED
    )
    deframed = realized - frame.frame_db(freqs)

    # Least-squares realized/commanded scale WITH an intercept, on the
    # frame-removed curve. Both halves matter and they are different fixes:
    # the intercept stops a level offset from arriving as apparent scale
    # (#2521's ≈2.02 on a −7.8 dB constant), and the frame-removed input is what
    # keeps a room's broadband tilt from arriving as one either — through the
    # origin OR with an intercept, a tilt against a ramp-shaped command is a
    # scale factor, and reading it as one is how a tilted room got a
    # ``level_dependent_shortfall`` rollback it had not earned.
    #
    # ``c`` carries only bins at or above the graded floor, so it cannot be
    # all-zero; the design matrix is rank-deficient only if every graded bin
    # commands the SAME value, which ``lstsq`` resolves to the minimum-norm
    # solution rather than raising. On that degenerate shape the intercept and
    # the scale are not separately identifiable — only their sum at the one
    # commanded value is — so the reported pair is a minimum-norm split of it,
    # and the split is NOT a near-miss of the plain ratio: for a flat ``k`` dB
    # command realized EXACTLY, it returns ``k²/(1+k²)``, which is under
    # :data:`DELTA_PROBE_SHORTFALL_GAIN_CEILING` for every ``k`` below 2.38 dB
    # (0.80 at 2 dB, 0.50 at 1 dB). A perfectly realized shallow flat lift would
    # read as a deep shortfall.
    #
    # What keeps that off every production path is not the size of the error but
    # how knife-edge the degeneracy is: the second column only has to vary at
    # all. A commanded range of 1e-9 dB across the graded bins already returns
    # gain exactly 1.0 (measured). A correction's commanded curve is the response
    # of a filter cascade sampled on a log grid, so its graded bins are never one
    # repeated constant — and the branch is unreachable anyway unless something
    # ELSE already put the map over tolerance, since an exactly realized
    # correction never gets past ``matched``.
    design = np.column_stack((np.ones_like(c), c))
    intercept, gain_factor = (
        float(v) for v in np.linalg.lstsq(design, deframed[mask], rcond=None)[0]
    )
    # The same question asked per band (#2649). ``gain_factor`` above stays
    # exactly what it was and keeps its place on the wire; this is the
    # band-resolved answer beside it, and it is what the shortfall verdict now
    # reads.
    realization = _band_realization(
        freqs, deframed, commanded,
        graded=mask,
        in_band=requested_in_band,
        ceiling_hz=graded_hi_hz if trust_ceiling_hz is not None else None,
    )

    tolerance_full = _tolerance_curve(freqs)
    error_full = np.where(mask, realized - commanded, 0.0)
    exceeded, exceedance_octaves = _structured_exceedance(
        freqs, error_full, tolerance_full, mask,
    )
    # The same three graded scalars, taken with the frame removed. Reported
    # beside the raw ones rather than instead of them: the raw grade is still
    # what decides whether there is a finding at all, and only the ROLLBACK
    # question is re-asked down here (#2521).
    frame_error_full = np.where(mask, deframed - commanded, 0.0)
    frame_exceeded, frame_exceedance_octaves = _structured_exceedance(
        freqs, frame_error_full, tolerance_full, mask,
    )
    frame_error = frame_error_full[mask]

    # Did a commanded boost realize MORE than it asked for? (#2537) Measured on
    # the RAW realized curve, not the frame-removed one, and that is the point:
    # a frame is removed to answer whether the correction's SHAPE is right, and
    # this asks how much energy actually reached the driver. Subtracting a
    # fitted offset first would hide exactly the whole-band overshoot the
    # adoption table's hard stop exists for.
    #
    # On the SAFETY mask, not the graded one (#2614) — see its construction
    # above: a repeat round's graded mask does not contain the bands the apply
    # left alone, and an untouched boost is still a boost.
    boost_over_bound, boost_overshoot_db, boost_overshoot_octaves = boost_overshoot(
        freqs, realized, commanded, tolerance_full, safety_mask,
        declared_db=declared,
    )
    # WHICH WAY the safety bins missed (#2559), on the raw curve for the same
    # reason the boost finding above is: this asks how much energy reached the
    # driver, not whether the shape is right.
    realized_louder, max_signed_error_db = louder_than_commanded(
        realized, commanded, tolerance_full, safety_mask,
    )

    # The caller had no CHANGE axis and said so (#2614): what it handed in as
    # ``commanded`` is the applied graph's own declared transfer, so the two
    # findings above are honest and every shape and level scalar computed above
    # is a claim in the wrong frame. Return the first and none of the second.
    # The shape work above is not skipped, only discarded — the alternative is a
    # second exit path through half this function, and one lstsq and one frame
    # fit per session is a smaller price than two orders of statements about
    # what was measured.
    if state_axis_only:
        return _safety_only(
            spatial,
            expected_offset_db=offset,
            requested_band_hz=requested_band_hz,
            probe_band_hz=probe_band_hz,
            n_bins=int(f.size),
            boost_over_declared_bound=boost_over_bound,
            boost_overshoot_db=boost_overshoot_db,
            boost_overshoot_octaves=boost_overshoot_octaves,
            realized_louder_than_commanded=realized_louder,
            max_signed_error_db=max_signed_error_db,
        )

    def _map(verdict: str, reason: str) -> DeltaProbeMap:
        return DeltaProbeMap(
            verdict=verdict, reason=reason, probe_band_hz=probe_band_hz,
            n_bins=int(f.size), max_error_db=max_error_db,
            rms_error_db=rms_error_db, worst_hz=worst_hz,
            exceedance_octaves=float(exceedance_octaves),
            gain_factor=gain_factor,
            tolerance_low_db=DELTA_PROBE_TOLERANCE_LOW_DB,
            tolerance_high_db=DELTA_PROBE_TOLERANCE_HIGH_DB,
            spatial=spatial,
            expected_offset_db=offset,
            residual_offset_db=residual_offset_db,
            requested_band_hz=requested_band_hz,
            frame=frame,
            frame_removed_max_db=(
                float(np.max(np.abs(frame_error))) if frame.fitted else None
            ),
            frame_removed_rms_db=(
                float(np.sqrt(np.mean(frame_error ** 2))) if frame.fitted else None
            ),
            frame_removed_exceedance_octaves=(
                float(frame_exceedance_octaves) if frame.fitted else None
            ),
            gain_intercept_db=intercept,
            entry_anchor_offset_db=entry_anchor_offset_db,
            quiet_n_bins=quiet_n_bins,
            quiet_core_band_hz=quiet_core_band_hz,
            quiet_probe_coverage=quiet_probe_coverage,
            boost_over_declared_bound=boost_over_bound,
            boost_overshoot_db=boost_overshoot_db,
            boost_overshoot_octaves=boost_overshoot_octaves,
            realized_louder_than_commanded=realized_louder,
            max_signed_error_db=max_signed_error_db,
            band_realization=realization,
            trust_ceiling_hz=(
                None if trust_ceiling_hz is None else float(trust_ceiling_hz)
            ),
            graded_band_hz=(float(lo_hz), float(graded_hi_hz)),
        )

    if not exceeded:
        # The chain did what it was told at the mark. Now — and only now — is
        # the spatial question the interesting one (see the module docstring's
        # verdict-priority note).
        if spatial.available and spatial.widened:
            return _map(VERDICT_SPATIALLY_COSTLY, "cross_position_spread_widened")
        return _map(VERDICT_MATCHED, "")

    # The map does not match. Before asking shape-or-scale, ask whether it
    # fails only because the level moved by something nobody commanded (#1811).
    #
    # Two conditions, and BOTH are required.
    #
    # (a) The quiet-bin residual — level where nothing was asked for — is
    #     material on its own terms. This is the EVIDENCE that a level shift
    #     exists at all, and it comes from bins no shape defect can reach.
    # (b) Removing the quiet bins' offset from the probe band makes the map
    #     pass. This is what makes the shift SUFFICIENT: if the map still fails
    #     after accounting for a shift we independently measured, then whatever
    #     else is wrong is a shape claim and belongs in the verdicts below.
    #
    # Together they keep every diagnostic underneath intact. A proportional
    # shortfall moves the quiet bins by nothing (it scales a command that is
    # zero there), so it fails (a). A mis-realized shelf leaves structure that
    # survives subtracting a constant, so it fails (b).
    #
    # **They subtract DIFFERENT numbers, and they have to** (#2533). (a) asks
    # whether the level MOVED, which is a change question, so it reads the
    # anchored ``residual_offset_db``. (b) asks whether the quiet bins explain
    # the graded failure — and that failure is measured against the model, so
    # what has to come out of it is the quiet bins' whole disagreement with the
    # model, standing anchor included. ``quiet_offset_db`` below is that
    # absolute term (it is ``frame.offset_db`` whenever every quiet bin carried a
    # usable anchor, and this branch is byte-identical to its pre-#2533 self
    # whenever no anchor was measured at all). Handing (b) the anchored number
    # instead would leave the standing offset inside the levelled error, so a
    # genuine uncommanded shift on any speaker whose model is not perfectly
    # anchored would stop reaching this verdict and arrive one gate later as
    # ``frame_mismatch`` — a true statement, but the less specific one.
    if residual_offset_db is not None:
        quiet_offset_db = residual_offset_db + (entry_anchor_offset_db or 0.0)
        levelled_error_full = np.where(
            mask, realized - commanded - quiet_offset_db, 0.0
        )
        levelled_exceeded, _ = _structured_exceedance(
            freqs, levelled_error_full, tolerance_full, mask,
        )
        if (
            abs(residual_offset_db) > DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
            and not levelled_exceeded
        ):
            # (c) WHERE the evidence sits (#2533). Conditions (a) and (b) both
            # hold, so there IS a finding — that is not re-litigated here, and
            # deliberately so: falling through would hand the same evidence to
            # the frame gate and the shape branch below, either of which can
            # return a ROLLBACK. An instrument that has just declared its
            # evidence unrepresentative must not become STRICTER on it; that is
            # the exact inversion of the "a gate may only narrow a finding"
            # asymmetry this module already holds for the frame.
            #
            # So the verdict, the rollback answer, and the household surface are
            # all unchanged. What narrows is the CLAIM: a level measured only
            # above the graded band is disclosed as the band-scoped thing it is,
            # with ``quiet_core_band_hz`` naming the band it covers, instead of
            # asserting a whole-band shift the quiet bins never saw.
            #
            # A shift whose evidence is spread like the graded band's own bins
            # keeps the whole-band reason: the ratio is 1.0 for a co-spanning
            # quiet set on either grid shape, well clear of the bar. What the bar
            # narrows is evidence concentrated somewhere the band is not — the
            # 12-20 kHz sliver at 0.248, a single mid-band notch at 0.036.
            band_scoped = (
                quiet_probe_coverage is not None
                and quiet_probe_coverage < DELTA_PROBE_MIN_QUIET_COVERAGE
            )
            return _map(
                VERDICT_LEVEL_MISMATCH,
                REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND if band_scoped
                else REASON_UNCOMMANDED_LEVEL_SHIFT,
            )
        unavailable_suffix = ""
    else:
        # No quiet bins ⇒ the level discriminator could not run at all, so the
        # verdicts below are being reached WITHOUT the check that would have
        # separated "the level moved" from "the shape is wrong". An undeclared
        # offset lands as ``model_error`` here — a rollback — and nothing else
        # in the record would say why that call was made blind. Say it.
        unavailable_suffix = _LEVEL_CHECK_UNAVAILABLE_SUFFIX

    # THE FRAME GATE, and it sits AHEAD OF BOTH ROLLBACK DOORS on purpose
    # (#2521, owner ruling 2026-08-15). Does the exceedance survive removing the
    # frame? An exceedance that does not survive is a statement about the two
    # curves' frames, which this comparison cannot attribute to the correction —
    # so it is disclosed, loudly, and the household keeps the tuning. One that
    # does survive is graded below with the instrument's own offset and slope
    # already out of the way, and reaches the same rollback it always did.
    #
    # **It guards the SHORTFALL door too, and it has to.** The first cut of this
    # gate sat below the shape-or-scale discriminator, guarding ``model_error``
    # alone — which is what the issue's wording asked for and is a hole:
    # ``level_dependent_shortfall`` is equally a rollback, and a real but
    # in-tolerance depth shortfall combined with a room tilt walks straight
    # through it. Reproduced (adversarial gate, 2026-08-15): a 4 dB Gaussian
    # lift at 5 kHz realized at 80 % depth is ``matched`` on its own — the 0.8 dB
    # miss never clears tolerance — and becomes a ``level_dependent_shortfall``
    # ROLLBACK once a −0.9 dB/octave frame is added, with its frame-removed
    # exceedance still exactly 0.0. Over a randomized sweep, 203 of 4,000 draws
    # rolled back on evidence that was entirely frame. The finding there is the
    # tilt, and the tilt is precisely what this probe may not refuse on.
    #
    # An unfitted frame removes nothing, so ``frame_exceeded`` equals
    # ``exceeded`` there and this branch cannot fire: no frame measured, no
    # demotion.
    #
    # :data:`VERDICT_SPATIALLY_COSTLY` is deliberately NOT behind this gate, and
    # cannot be: it is returned above, from the branch where the mark map
    # MATCHED. It is also a different evidence class — two real measurements of
    # the room's own spread, with no model and therefore no frame between them —
    # so there is nothing here that could explain it away.
    if not frame_exceeded:
        return _map(VERDICT_FRAME_MISMATCH, "uncommanded_frame_shift")

    # Shape or scale?
    #
    # Re-measure the error against the best-fit SCALED command. If the residual
    # then passes, the correction's shape is right and only its depth is short
    # — the driver delivered a fraction of what it was asked for, uniformly.
    # If the residual still fails, the shape itself is wrong, which is a claim
    # about our model of the filters, not about the driver's headroom.
    #
    # Both sides of this comparison are the frame-removed curve and the fitted
    # line through it, so the intercept is USED and not merely estimated: a
    # residual measured against ``gain·commanded`` alone would re-admit the very
    # level term the intercept exists to hold out.
    scaled_error_full = np.where(
        mask, deframed - (intercept + gain_factor * commanded), 0.0
    )
    scaled_exceeded, _ = _structured_exceedance(
        freqs, scaled_error_full, tolerance_full, mask,
    )
    # A *level-dependent* shortfall is a claim about a driver failing to
    # deliver LEVEL, so it requires that level was what the correction asked
    # for. A proportional undershoot of a set of CUTS is not compression —
    # attenuation does not compress — and belongs in the model-error bucket
    # where someone will look at the filter math.
    commanded_is_lift = float(np.max(c)) >= DELTA_PROBE_MIN_COMMANDED_DB
    # **The GRADED BANDS decide, not the pooled slope** (#2649).
    #
    # Two narrowings of when a rollback verdict fires, and both are needed.
    # Bins above the mic-trust ceiling are excluded from ``mask`` before this
    # point, so they cannot manufacture a slope at all. And the ratio this
    # tests is the per-band one, because the pooled fit is not a realization
    # measurement when the graded band carries two disjoint COMMANDED ranges.
    #
    # Why the pooled fit lies there, concretely: ``lstsq`` puts one line
    # through the (commanded, realized) cloud, and with the crossover band
    # commanded near one value and the trusted HF near another, that line is
    # the CHORD BETWEEN THE TWO BAND CENTROIDS. Its slope reports the level
    # difference between the bands, not the fraction of command either band
    # realized. A round where both bands realized **1.00x** of what they were
    # told — nothing missing anywhere — fits a chord of 0.459 and, with the
    # residual against that chord inside tolerance, was classified
    # ``level_dependent_shortfall`` and ROLLED BACK. That is a false restore of
    # a correction that worked, which is the fifth-principle violation this
    # issue exists to kill. (An earlier cut of this migration called the
    # per-band gate inert; that claim was made against a fixture whose two
    # bands shared one commanded range, where the chord degenerates to the true
    # slope, so it structurally could not exhibit the case. It is pinned now.)
    #
    # EVERY graded band must fall short, not just the worst: a driver failing
    # to deliver LEVEL fails everywhere it was asked, and a band-localised miss
    # is a shape error — ``model_error``, which is where it goes. Taking the
    # worst band instead would fire more often on less evidence, the wrong
    # direction for a verdict that restores the household's previous sound.
    #
    # Falls back to the pooled slope only when no graded band cleared
    # ``DELTA_PROBE_MIN_BINS`` — a band too thin to fit is not evidence either
    # way, and that is the pre-#2649 reading exactly.
    graded_ratios = [
        entry["ratio"] for entry in realization.values()
        if entry["graded"] and entry["ratio"] is not None
    ]
    shortfall_ratio = max(graded_ratios) if graded_ratios else gain_factor
    if (
        not scaled_exceeded
        and commanded_is_lift
        and 0.0 <= shortfall_ratio < DELTA_PROBE_SHORTFALL_GAIN_CEILING
    ):
        return _map(
            VERDICT_LEVEL_DEPENDENT_SHORTFALL,
            "realized_short_of_commanded" + unavailable_suffix,
        )
    return _map(
        VERDICT_MODEL_ERROR,
        "realized_shape_differs_from_commanded" + unavailable_suffix,
    )


def spatial_cost_from_group_spreads(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
) -> SpatialCost:
    """Adapter: two cloud-group result mappings → a :class:`SpatialCost`.

    Reads the ``"band_spread"`` list each group publishes (a list of plain
    dicts after the JSON round-trip, or ``BandSpread`` objects in-process).
    Absent/short spreads degrade to :data:`SPATIAL_COST_UNAVAILABLE` rather
    than raising — an express session has no post-apply group at all, and
    "no spatial evidence" is an honest answer, not an error.
    """

    def _bands(group: Mapping[str, Any] | None) -> list[Any]:
        if not isinstance(group, Mapping):
            return []
        raw = group.get("band_spread")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return []
        out: list[Any] = []
        for entry in raw:
            if isinstance(entry, Mapping):
                center = entry.get("center_hz")
                sigma = entry.get("sigma_db")
                if isinstance(center, (int, float)) and isinstance(sigma, (int, float)):
                    out.append(_PlainBand(float(center), float(sigma)))
            elif hasattr(entry, "center_hz") and hasattr(entry, "sigma_db"):
                out.append(entry)
        return out

    before_bands, after_bands = _bands(before), _bands(after)
    if not before_bands or not after_bands:
        return SPATIAL_COST_UNAVAILABLE
    return evaluate_spatial_cost(before_bands, after_bands)


@dataclass(frozen=True)
class _PlainBand:
    """The two fields :func:`evaluate_spatial_cost` reads, off a JSON dict."""

    center_hz: float
    sigma_db: float


__all__ = [
    "DELTA_PROBE_BAND_ABOVE_CEILING",
    "DELTA_PROBE_BAND_CROSSOVER",
    "DELTA_PROBE_BAND_TRUSTED_HF",
    "DELTA_PROBE_HF_SPLIT_HZ",
    "DELTA_PROBE_MIN_BINS",
    "DELTA_PROBE_MIN_COMMANDED_DB",
    "DELTA_PROBE_MIN_COMMANDED_HIGH_DB",
    "DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES",
    "DELTA_PROBE_MIN_QUIET_COVERAGE",
    "DELTA_PROBE_REALIZATION_BANDS",
    "DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB",
    "DELTA_PROBE_ROLLBACK_VERDICTS",
    "DELTA_PROBE_SHORTFALL_GAIN_CEILING",
    "DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB",
    "DELTA_PROBE_TOLERANCE_HIGH_DB",
    "DELTA_PROBE_TOLERANCE_LOW_DB",
    "DELTA_PROBE_VERDICTS",
    "SEAM_DEFERRED_QUIETER_THAN_COMMANDED",
    "SPATIAL_COST_UNAVAILABLE",
    "REASON_COMMANDED_AXIS_UNAVAILABLE",
    "REASON_UNCOMMANDED_LEVEL_SHIFT",
    "REASON_UNCOMMANDED_LEVEL_SHIFT_OUTSIDE_BAND",
    "DeltaProbeMap",
    "SpatialCost",
    "VERDICT_FRAME_MISMATCH",
    "VERDICT_LEVEL_DEPENDENT_SHORTFALL",
    "VERDICT_LEVEL_MISMATCH",
    "VERDICT_MATCHED",
    "VERDICT_MODEL_ERROR",
    "VERDICT_SAFETY_ONLY",
    "VERDICT_SPATIALLY_COSTLY",
    "VERDICT_UNAVAILABLE",
    "boost_overshoot",
    "classify_delta_probe",
    "evaluate_spatial_cost",
    "graded_command_floor_db",
    "interquartile_band_hz",
    "louder_than_commanded",
    "seam_rollback_deferral",
    "spatial_cost_from_group_spreads",
    "widest_exceedance_octaves",
]
