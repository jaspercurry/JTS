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

* ``commanded_delta_db`` is the correction's own predicted transfer on the
  summed response: the linearized-branch prediction minus the raw-branch
  prediction, both built from the SAME measured branches with the SAME
  summation model. The branch measurements and the summation model therefore
  cancel out of it; what survives is the shape the emitted filters and trims
  command.
* ``realized_delta_db`` is the measured post-apply response minus the SAME
  raw-branch prediction, at the same microphone position.

Their difference — the ``error_db`` map this module classifies — is
algebraically ``measured_post − predicted_post``: the raw-branch prediction
cancels. That is deliberate and is stated here so nobody later reads the delta
framing as implying an independent pre-apply *measurement* at the mark (there
is none; MEASURE captures per-driver sweeps, not a summed curve). The delta
framing earns its keep in two places the plain residual cannot reach: the
commanded curve is the axis the shortfall-vs-model-error discriminator
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
from dataclasses import dataclass
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
    #: What was left over, measured where the correction commanded NOTHING —
    #: the mean of ``(realized − expected_offset) − commanded`` across the
    #: in-band bins BELOW the commanded floor. The ``− commanded`` term is
    #: small but real there rather than exactly zero (the floor admits up to
    #: :data:`DELTA_PROBE_MIN_COMMANDED_DB`, so it can bias this by up to
    #: 0.5 dB against a 1.5 dB bar) and is subtracted rather than assumed away.
    #: Any level in those bins is uncommanded by construction, which is what
    #: makes it separable from a shape defect inside the probe band. ``None``
    #: when the correction commands something almost everywhere in band and
    #: there are too few quiet bins to measure it — "not measured", which 0.0
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
            # The frame's own terms and the grades taken with it removed, nested
            # together so a reader picks a frame of reference once and reads a
            # matching set rather than pairing keys by name.
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


def classify_delta_probe(
    freqs_hz: np.ndarray,
    realized_delta_db: np.ndarray,
    commanded_delta_db: np.ndarray,
    *,
    band_hz: tuple[float, float],
    spatial: SpatialCost = SPATIAL_COST_UNAVAILABLE,
    expected_offset_db: float = 0.0,
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

    lo_hz, hi_hz = requested_band_hz
    in_band = (
        (freqs >= lo_hz)
        & (freqs <= hi_hz)
        & np.isfinite(realized)
        & np.isfinite(commanded)
    )
    mask = in_band & (np.abs(commanded) >= graded_command_floor_db(freqs))
    if int(mask.sum()) < DELTA_PROBE_MIN_BINS:
        return _unavailable(
            "nothing_commanded", spatial, expected_offset_db=offset,
            requested_band_hz=requested_band_hz,
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
    residual_offset_db: float | None = (
        float(np.mean(realized[quiet] - commanded[quiet]))
        if quiet_measurable
        else None
    )

    # The FRAME between the two curves, fitted in exactly those quiet bins
    # (#2521) — the same set, and the same argument, one term further: a slope
    # measured where the correction asked for nothing is uncommanded by
    # construction, just as the level above is. Its offset term IS
    # ``residual_offset_db`` (``fit_frame`` pivots at the fitted bins' geometric
    # mean, which makes the offset the plain mean of the difference); the tilt
    # is the term nothing in this module could see before.
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
    # (b) Removing that same residual from the probe band makes the map pass.
    #     This is what makes the shift SUFFICIENT: if the map still fails after
    #     accounting for a shift we independently measured, then whatever else
    #     is wrong is a shape claim and belongs in the verdicts below.
    #
    # Together they keep every diagnostic underneath intact. A proportional
    # shortfall moves the quiet bins by nothing (it scales a command that is
    # zero there), so it fails (a). A mis-realized shelf leaves structure that
    # survives subtracting a constant, so it fails (b).
    if residual_offset_db is not None:
        levelled_error_full = np.where(
            mask, realized - commanded - residual_offset_db, 0.0
        )
        levelled_exceeded, _ = _structured_exceedance(
            freqs, levelled_error_full, tolerance_full, mask,
        )
        if (
            abs(residual_offset_db) > DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB
            and not levelled_exceeded
        ):
            return _map(VERDICT_LEVEL_MISMATCH, "uncommanded_level_shift")
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
    if (
        not scaled_exceeded
        and commanded_is_lift
        and 0.0 <= gain_factor < DELTA_PROBE_SHORTFALL_GAIN_CEILING
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
    "DELTA_PROBE_HF_SPLIT_HZ",
    "DELTA_PROBE_MIN_BINS",
    "DELTA_PROBE_MIN_COMMANDED_DB",
    "DELTA_PROBE_MIN_COMMANDED_HIGH_DB",
    "DELTA_PROBE_MIN_EXCEEDANCE_OCTAVES",
    "DELTA_PROBE_RESIDUAL_OFFSET_TOLERANCE_DB",
    "DELTA_PROBE_ROLLBACK_VERDICTS",
    "DELTA_PROBE_SHORTFALL_GAIN_CEILING",
    "DELTA_PROBE_SPREAD_WIDENING_TOLERANCE_DB",
    "DELTA_PROBE_TOLERANCE_HIGH_DB",
    "DELTA_PROBE_TOLERANCE_LOW_DB",
    "DELTA_PROBE_VERDICTS",
    "SPATIAL_COST_UNAVAILABLE",
    "DeltaProbeMap",
    "SpatialCost",
    "VERDICT_FRAME_MISMATCH",
    "VERDICT_LEVEL_DEPENDENT_SHORTFALL",
    "VERDICT_LEVEL_MISMATCH",
    "VERDICT_MATCHED",
    "VERDICT_MODEL_ERROR",
    "VERDICT_SPATIALLY_COSTLY",
    "VERDICT_UNAVAILABLE",
    "classify_delta_probe",
    "evaluate_spatial_cost",
    "graded_command_floor_db",
    "spatial_cost_from_group_spreads",
    "widest_exceedance_octaves",
]
