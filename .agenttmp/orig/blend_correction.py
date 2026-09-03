"""The blend region's summed-response-owned shape correction (decision 10).

Per-driver linearization is deliberately blind across the crossover blend —
neither branch's own sweep can say what the SUM does there, and
``linearization_fit._blind_zone_placements`` already refuses to guess. What
this module adds is the other owner the contract names: the summed at-the-mark
measurement, which sees the region at every position, prescribing **bounded,
cuts-first shape correction** over it.

Canonical contract: ``docs/active-speaker-tuning-layers-design.md``, decision
10 and "The region-based adjustment contract (2026-08-17)", clause (b). This
module fills the four things that contract left to its implementing session —
filter form, band edges, count, and how much depth the summed evidence earns —
and nothing else. Level stays the trim's fact (clause (c)); alignment, polarity
and Fc keep their own tools.

**What it is.** ``≤ 2`` RBJ Peaking **cuts**, ``Q ≤ 2.0``, each ``≤ 3.0 dB``,
composing to ``≤ 4.0 dB`` over the region. They are emitted PRE-SPLIT on the
stereo bus (``camilla_yaml._emit_baseline_pipeline``), which is what makes the
correction common-mode *by construction*: applying one ``B(f)`` to every role
gives ``Σ_r sign_r·B·C_r·D_r = B · Σ_r sign_r·C_r·D_r``, so the sum is scaled
and the inter-driver complex ratio is untouched. An asymmetric application
would change the interference pattern, which is alignment work wearing a
shape-correction hat — pre-split makes that unrepresentable rather than merely
tested against.

**What it cannot do, said plainly.** A cuts-only correction cannot fill a dip.
With ``c(f) = −max(d(f), 0)`` the new deviation is ``min(d, 0)``: every peak
goes to the mean and every trough stays exactly where it was. The region's rms
falls and the trough's depth *below the new mean* shrinks by the average cut —
on the series-1 evidence that is a win of roughly 0.5–1.5 dB of region rms, not
the closing of a 4.2 dB notch. That is the honest prediction, and it is also
why the grading in ``round_evidence`` is region-scoped: a localized win of that
size is invisible inside a full-spectrum pooled average.

**Why the evidence must carry an honesty mask.** The excluded bins handed in
here are the cloud's MERGED honesty mask (the combiner's power-vs-median screen
∪ the identified-null registry, unioned once in
``crossover_v2_flow.assemble_cloud_group_result``). That mask is the only
structural protection against cutting an interference null instead of a real
excess: per #2600 item 1 the null detector reports
``uncalibrated_below_hf_floor`` across the entire blend window of any crossover
below 4 kHz, so the one instrument that could say "this is a null" cannot
answer here. This module therefore never invents coverage — a bin the mask
removed is not a bin it may cut.

**Iteration is incumbent-accounted and damped.** Per-branch MEASURE sweeps ride
the protected-neutral graph, so a trim re-derived from scratch each round is
correct. The summed VERIFY capture rides the APPLIED incumbent, so its
deviation already contains the incumbent's own blend correction. Re-deriving
``B = −d`` absolutely would oscillate: a perfect ``B_N = −u`` measures
``d_{N+1} = 0``, the correction is removed, and the next round puts it back.
The shipped form is a damped fixed-point iteration::

    B_{N+1}(f) = clamp( B_N(f) − k · d_{N+1}(f) ),   k = BLEND_DAMPING = 0.7

still a TOTAL re-derived every round — same discipline as the trim — but with
the incumbent entering the derivation because the measurement was taken through
it. Damping widens the stable range of realized/commanded loop gain from
``g < 2`` to ``g < 2/k``; the CLAMPS are what bound the excursion when ``g``
lands outside even that, which series-1 shows it can (band gains ranged 0.136
to 11.736). See :data:`BLEND_DAMPING` for why the distinction is stated rather
than blurred.

**Every arm that HAS an incumbent holds it; a profile with no readable
incumbent emits none — the one revert path, and it is reachable only on
corruption the emitter would refuse anyway.** Nothing here can fail to a boost.
Every arm names itself on the receipt, so a round that changed nothing says
*why* — "the region was already clean" and "the instrument refused" are
different facts. What each arm PRESCRIBES is the part worth stating precisely,
because the prescription becomes the next round's graph:

===========================  ================================================
arm                          what the next round applies
===========================  ================================================
:data:`BLEND_CORRECTED`      the newly solved total
:data:`BLEND_NOTHING_TO_CUT` the incumbent, unchanged
:data:`BLEND_NOT_COMPARABLE` the incumbent, unchanged
:data:`BLEND_NO_TRUSTED_BAND` the incumbent, unchanged
:data:`BLEND_REGION_NOT_IMPROVING` the incumbent, unchanged
:data:`BLEND_NO_INCUMBENT`   nothing — there is no incumbent to hold
``no_crossover_reason``      the incumbent, unchanged — a 1-way main has no
                             region, so nothing here is ever prescribed
===========================  ================================================

**Refusals hold rather than revert** (panel ruling, 2026-08-18). A round whose
evidence failed for reasons unrelated to the correction — an unusable capture,
a region the gate could not trust — has no standing to remove a correction that
was adopted on measured evidence. Writing an empty prescription there would
silently drop up to the full composed ceiling of adopted cut on the next apply,
which is a change to what a household hears made by an instrument that just
said it could not measure. The least-bad-measured principle runs the other way:
what plays stays until measured evidence says otherwise.

:data:`BLEND_NO_INCUMBENT` is the one arm that cannot hold, because it is
exactly the state of not knowing what to hold. It is #2653's "refuse when the
reconciliation cannot be established", applied to this module's own quantity.
Unlike the level datum #2653 is about, that reconciliation is cheap here — the
incumbent is a filter list the system itself wrote and persisted, not a
per-role level a summed capture cannot separate.

**Its cost, stated rather than glossed:** it is the one path that can REMOVE an
applied correction, so the region can get up to the composed ceiling louder on
the next apply. That is accepted, not overlooked. The alternative — carrying
forward a list the strict reader just refused to vouch for — is worse in the
direction that matters, and the emitter would refuse it at the graph boundary
anyway. The state is reachable only through a corrupt or absent applied
profile, which is also the state in which nothing else about the live graph can
be established either.

**Scope tripwire.** This module reads the summed response against an analytic,
offset-invariant reference and commands a common-mode filter. It never asks how
much of the region's excess belongs to the woofer. Reading a per-role level,
delay, or trim out of a summed capture is #2653's territory, not this one's.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# No intra-package import at all: this module is a LEAF of the crossover_v2
# DAG, which is what lets it be read, tested and mutated without loading the
# round. Its two dependencies are the module that owns flat-spec grading and
# the ONE biquad evaluator in this codebase.
from jasper.active_speaker.branch_chain import chain_response
from jasper.active_speaker.flat_spec import GradedSpec

__all__ = [
    "BLEND_CORRECTED",
    "BLEND_DAMPING",
    "BLEND_FILTER_Q",
    "BLEND_MAX_FILTERS",
    "BLEND_MAX_FILTER_CUT_DB",
    "BLEND_MAX_TOTAL_CUT_DB",
    "BLEND_MIN_CUT_DB",
    "BLEND_MIN_REGION_BINS",
    "BLEND_NOTHING_TO_CUT",
    "BLEND_NOT_COMPARABLE",
    "BLEND_NO_INCUMBENT",
    "BLEND_NO_TRUSTED_BAND",
    "BLEND_REGION_NOT_IMPROVING",
    "BlendCorrection",
    "BlendRegionReading",
    "blend_filters_from_mapping",
    "solve_blend_correction",
]


# --------------------------------------------------------------------------- #
# bounds — every one derived, none chosen by taste
# --------------------------------------------------------------------------- #

#: How many Peaking cuts one blend correction may carry. Two, because the
#: evidence in this region does not support fine sculpting: the summed capture
#: is one mono sweep per position, the honesty mask removes bins inside the
#: very window being corrected, and the null detector is uncalibrated here
#: (#2600 item 1). Two cuts take out the two largest hot lobes, which is what
#: a cuts-first posture can honestly claim.
BLEND_MAX_FILTERS = 2

#: Q of every emitted blend cut — THIS solver's, and no longer the whole
#: region's. A deliberate tightening against the fit engine's own ``Q ≤ 8``
#: peaking ceiling: a narrower cut chases a feature this instrument cannot
#: resolve and the room will not reproduce off-axis. 2.0 is also the Q every
#: peaking filter the series-1 fits actually emitted used, so the shape is one
#: the loop has already realized on hardware.
#:
#: **Scope.** The first sentence was measured (2026-08-19) and found too
#: strong for a PRESCRIBED cut, whose Q is now unbounded at the intake
#: (ADR-0207). This constant did not move, and does not need that
#: argument to hold: its second leg is sufficient on its own — the narrow-defect
#: convergence stop in :func:`solve_blend_correction`'s docstring, which is
#: untouched, and which is why a FIXED-Q iterating solver still wants 2.0 even
#: where a one-shot prescription may go narrower. Worth reading rather than
#: re-derived here: it predicted this campaign's outcome before the campaign
#: measured it — a cut wider than its defect "over-corrects the shoulders" —
#: which is the skirt damage both prescribed rounds were rolled back on.
BLEND_FILTER_Q = 2.0

#: Per-filter cut ceiling, dB — the deterministic solver's own emission bound,
#: and nothing else's: a PRESCRIBED cut has no depth ceiling (ADR-0207).
#: Derived rather than chosen: the woofer's own
#: acknowledged ``measured_excess_db`` inside the blind zone was 2.09–2.26 dB
#: across series-1 rounds r1/r2/r4 (1291.4–2077.2 Hz), and this model's own
#: measured tracking error on jts3 is 0.5 dB
#: (``attempt_grading.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB``). 2.26 + 0.5 is
#: 2.76, rounded to 3.0 — "what the blind zone was shown to hide, plus one
#: model error", traceable to series-1 rather than to preference.
BLEND_MAX_FILTER_CUT_DB = 3.0

#: Ceiling on the COMPOSED correction's depth over the region, dB. Sized just
#: under the whole observed defect (the series-1 cloud flat-spec gauge read
#: −4.24 dB worst): correcting more than the defect is over-correction by
#: definition, and there is no evidence for a deeper cut than the deepest
#: thing measured. Enforced on the evaluated cascade, not on a sum of gains,
#: because two cuts that overlap deliver more than either alone.
BLEND_MAX_TOTAL_CUT_DB = 4.0

#: The smallest cut worth emitting, dB — this model's own measured tracking
#: error, the same floor ``attempt_grading.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB``
#: holds for the same reason. A correction smaller than the gap between what
#: the model predicts and what the hardware realizes is not a correction that
#: can be honestly claimed, so it is not emitted at all.
BLEND_MIN_CUT_DB = 0.5

#: Damping on the incumbent-accounted iteration: each round commands 70% of the
#: excess it measured, not all of it.
#:
#: The loop converges for a realized/commanded gain ``g`` in ``(0, 2/k)``, so
#: ``k = 0.7`` widens the stable range from ``g < 2`` to ``g < 2.86``. **It does
#: not make every measured gain stable, and the docstring must not claim it
#: does**: series-1 measured band gains from 0.136 to 11.736, and ``g = 11.7``
#: diverges at ``k = 0.7`` exactly as it does at ``k = 1``. What bounds the
#: excursion there is the CLAMP — the per-filter and composed ceilings above,
#: which hold whatever the gain assumption turns out to be — and after that,
#: adoption's keep/restore on a round that measured worse. Damping and clamping
#: are belt and braces, and only the braces are load-bearing at the extreme.
#:
#: The cost is one bite: three rounds with an uncorrected first reach ~91% of
#: the fixed point at unit gain. Cheap, on a rig whose realization ratio has
#: ranged that far.
BLEND_DAMPING = 0.7

#: The fewest surviving region bins a correction may be solved from. Below
#: this the region is mostly mask, and a "worst bin" is an artefact of what
#: little the honesty instruments left rather than a shape.
BLEND_MIN_REGION_BINS = 8


# --------------------------------------------------------------------------- #
# outcome vocabulary — a round that corrected nothing must say which arm fired
# --------------------------------------------------------------------------- #

#: The region had no trusted band: the VERIFY absolute claim was not evaluated
#: (no Fc, no crossover target, or no trusted crossover region), or the band it
#: named survives too few unmasked bins to read.
BLEND_NO_TRUSTED_BAND = "no_trusted_band"

#: There was a band, but the curve could not be graded over it — a degenerate
#: axis, a mask that removed the reference band, or a residual the estimator
#: reported unevaluable. Evidence refusal outranks everything.
BLEND_NOT_COMPARABLE = "not_comparable"

#: The incumbent correction could not be established, so the measurement's
#: frame is unknown. #2653's condition, applied here: refuse rather than assume
#: ``B_N = 0``, which would double-count exactly the way the reverted
#: level-datum formula did.
BLEND_NO_INCUMBENT = "no_incumbent"

#: The region was read successfully and nothing in it earned a cut — every
#: surviving bin sat at or below the reference, or the deepest excess was
#: smaller than :data:`BLEND_MIN_CUT_DB`. This is the honest outcome for a
#: region whose defect is a dip rather than a peak, which is the case series-1
#: is most likely to hit.
BLEND_NOTHING_TO_CUT = "nothing_to_cut"

#: The region was read and did not improve on the previous round's reading, so
#: the incumbent is held rather than deepened. The narrow-defect stop: see
#: :func:`solve_blend_correction` on why the bar is "improved at all" rather
#: than "improved provably".
BLEND_REGION_NOT_IMPROVING = "region_not_improving"

#: Filters were prescribed.
BLEND_CORRECTED = "corrected"


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlendRegionReading:
    """What the summed evidence said about the region, before prescribing.

    Banked as the ``realized`` half of the receipt's commanded-vs-realized
    pair: this is what the INCUMBENT achieved, measured through it, and it is
    the number the next round's prescription is derived from. Decision 11 makes
    that pairing deterministic forever regardless of who eventually prescribes.
    """

    #: The graded band, ``(lo_hz, hi_hz)``.
    band_hz: tuple[float, float]
    #: RMS deviation over the region's surviving bins, dB. The SAME estimator
    #: :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` pools
    #: — deviation against the one flat reference, squared, averaged over
    #: included bins — with only its bin set narrowed to the region.
    #:
    #: A pinning test asserts the two agree when the region spans the graded
    #: band, and it has to mask outside the region as well to make that
    #: comparison meaningful: ``spec_convergence_residual`` pools every spec
    #: band, including bins this reading never sees. Unmasked the two grade
    #: different bin SETS — a difference in coverage, not in estimator.
    residual_db: float
    #: How many bins that RMS was computed from. Part of the answer, not
    #: decoration: a residual that fell because the honesty mask grew is the
    #: same speaker graded on fewer bins.
    n_bins: int
    #: The worst SIGNED deviation in the region and where it sat. Signed
    #: because a dip and a peak through the handoff are opposite defects.
    worst_db: float
    worst_hz: float

    #: Which instrument produced :attr:`residual_db`, written into the record.
    #: The receipt carries TWO region residuals over the same band — this one
    #: and ``region_benefit.evidence.post_residual_db`` — and they legitimately
    #: differ (1.0006 vs 0.8902 on the panel's fixture) because they are
    #: referenced differently: this one against the speaker's broadband flat
    #: level, the benefit axis's against the region's own surviving bins after
    #: its band mask. Both are right for their own question; a reader holding
    #: two unlabelled numbers cannot know that, and would reasonably read the
    #: difference as a bug in one of them.
    INSTRUMENT = "cloud_flat_reference"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.INSTRUMENT,
            "band_hz": [self.band_hz[0], self.band_hz[1]],
            "residual_db": self.residual_db,
            "n_bins": self.n_bins,
            "worst_db": self.worst_db,
            "worst_hz": self.worst_hz,
        }


@dataclass(frozen=True)
class BlendCorrection:
    """One round's blend-region prescription, and why it is what it is.

    ``filters`` is a TOTAL, not a delta — the whole correction the next round
    should apply, incumbent included. An empty tuple is a legitimate answer on
    every path; ``reason`` says which one.
    """

    #: The prescribed cuts, in emission order. Always cuts: a positive gain is
    #: not representable by the solver that built them.
    filters: tuple[dict[str, Any], ...]
    #: One of the ``BLEND_*`` reason codes above.
    reason: str
    #: The band the prescription was solved over, or ``None`` when no band was
    #: established.
    band_hz: tuple[float, float] | None = None
    #: The correction the measured capture rode, echoed so the receipt records
    #: what the prescription was derived FROM rather than only its result.
    incumbent: tuple[dict[str, Any], ...] = ()
    #: The region reading, or ``None`` when the region was never graded.
    reading: BlendRegionReading | None = None

    @property
    def emitted(self) -> bool:
        return bool(self.filters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "band_hz": (
                None if self.band_hz is None
                else [self.band_hz[0], self.band_hz[1]]
            ),
            "commanded": [dict(f) for f in self.filters],
            "incumbent": [dict(f) for f in self.incumbent],
            "damping": BLEND_DAMPING,
            "realized": None if self.reading is None else self.reading.to_dict(),
        }


def _hold(reason: str, *, band_hz: tuple[float, float] | None = None,
          incumbent: tuple[dict[str, Any], ...] = (),
          reading: BlendRegionReading | None = None) -> BlendCorrection:
    """Prescribe the incumbent unchanged, and say which arm decided that.

    The refusal shape for every arm except :data:`BLEND_NO_INCUMBENT`: the
    round could not improve on what is applied, so what is applied continues.
    ``filters`` is the incumbent rather than ``()`` — see the module docstring
    on why an instrument that just said it could not measure has no standing to
    remove an adopted correction.
    """

    return BlendCorrection(
        filters=incumbent, reason=reason, band_hz=band_hz, incumbent=incumbent,
        reading=reading,
    )


def _no_incumbent(band_hz: tuple[float, float] | None) -> BlendCorrection:
    """The one arm that cannot hold: there is nothing established to hold."""

    return BlendCorrection(
        filters=(), reason=BLEND_NO_INCUMBENT, band_hz=band_hz, incumbent=(),
    )


# --------------------------------------------------------------------------- #
# reading a persisted correction back
# --------------------------------------------------------------------------- #


def blend_filters_from_mapping(raw: Any) -> tuple[dict[str, Any], ...] | None:
    """Normalize a persisted blend-correction list, or ``None`` if unreadable.

    The reader for the incumbent: a candidate's ``blend_correction`` field has
    crossed a JSON round trip, and "the incumbent is empty" and "the incumbent
    cannot be read" are different facts with different consequences — the first
    is a normal first round, the second is a refusal (:data:`BLEND_NO_INCUMBENT`).
    An absent/``None`` input is therefore NOT the same as ``[]`` and callers
    must keep them apart.

    Cuts-only is re-checked here rather than assumed, because this is the point
    where data that left the process comes back into it. A positive gain means
    the record is not one this module wrote; it is unreadable, not clampable.
    """

    if raw is None:
        return None
    if isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        return None
    if not isinstance(raw, Sequence):
        return None
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            return None
        if entry.get("biquad_type") != "Peaking":
            return None
        raw = (entry.get("freq"), entry.get("q"), entry.get("gain"))
        # Real numbers, NOT anything ``float()`` will coerce. A ``"1900"``
        # parses cleanly and would sail through — but this system writes
        # floats, so a string is by definition a record something else wrote,
        # which is exactly the question this reader exists to ask. ``bool`` is
        # excluded for the usual reason: it is an ``int`` subclass, and
        # ``gain=True`` would read as a +1 dB boost.
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in raw
        ):
            return None
        freq, q, gain = (float(value) for value in raw)
        if not (math.isfinite(freq) and math.isfinite(q) and math.isfinite(gain)):
            return None
        if freq <= 0.0 or q <= 0.0 or gain > 0.0:
            return None
        out.append({"biquad_type": "Peaking", "freq": freq, "q": q, "gain": gain})
    if len(out) > BLEND_MAX_FILTERS:
        return None
    return tuple(out)


# --------------------------------------------------------------------------- #
# the solve
# --------------------------------------------------------------------------- #


def _cascade_db(
    filters: Sequence[Mapping[str, Any]], freqs_hz: np.ndarray,
) -> np.ndarray:
    """The dB magnitude a cascade of emitted biquads applies at ``freqs_hz``.

    Through ``branch_chain.chain_response``, which is the ONE biquad evaluator
    in this codebase — the same one the emitter's headroom charge, the fit
    engine's realization gate, and the runtime contract's proof bottom out in.
    Evaluating the incumbent (or a candidate cut) any other way here would make
    this module a second opinion about what CamillaDSP realizes.
    """

    if not filters:
        return np.zeros_like(freqs_hz, dtype=np.float64)
    response = np.asarray(chain_response(filters, freqs_hz))
    return 20.0 * np.log10(np.maximum(np.abs(response), 1e-12))


def _band_from(band_hz: Any) -> tuple[float, float] | None:
    if not isinstance(band_hz, (list, tuple)) or len(band_hz) != 2:
        return None
    try:
        lo, hi = float(band_hz[0]), float(band_hz[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    if lo <= 0.0 or hi <= lo:
        return None
    return (lo, hi)


def _fit_cuts(
    freqs_hz: np.ndarray, desired_db: np.ndarray, region: np.ndarray,
) -> tuple[dict[str, Any], ...]:
    """Greedy peak-picking of at most :data:`BLEND_MAX_FILTERS` cuts.

    ``desired_db`` is the TOTAL correction wanted, already clamped
    non-positive by the caller, so ``required = −desired`` is how much cut each
    bin still wants. Each pass takes the deepest still-wanted bin inside the
    region, emits one Peaking cut there, and subtracts what that cut actually
    delivers (evaluated, not assumed) before looking again.

    **Cuts-only is structural here, not checked.** ``required`` can only ask
    for attenuation, every emitted ``gain`` is ``−depth`` for a non-negative
    ``depth``, and the emitter re-proves it independently at the graph
    boundary. There is no branch in this function that can produce a boost.
    """

    required = -np.asarray(desired_db, dtype=np.float64)
    out: list[dict[str, Any]] = []
    for _ in range(BLEND_MAX_FILTERS):
        masked = np.where(region, required, -np.inf)
        index = int(np.argmax(masked))
        if not np.isfinite(masked[index]):
            break
        depth = min(float(required[index]), BLEND_MAX_FILTER_CUT_DB)
        if depth < BLEND_MIN_CUT_DB:
            break
        freq = float(freqs_hz[index])
        # The composed-depth ceiling is enforced on the EVALUATED cascade, so
        # two cuts whose skirts overlap cannot sum past it. One shrink, then
        # one re-check: the composition is monotone in this filter's gain, so
        # a single correction either lands inside the ceiling or the filter has
        # no room left and is dropped rather than emitted at a token depth.
        accepted: dict[str, Any] | None = None
        for _attempt in range(2):
            trial = {
                "biquad_type": "Peaking", "freq": freq,
                "q": BLEND_FILTER_Q, "gain": -depth,
            }
            composed = _cascade_db([*out, trial], freqs_hz)
            worst = float(np.min(composed[region]))
            if worst >= -BLEND_MAX_TOTAL_CUT_DB:
                accepted = trial
                break
            depth -= (-BLEND_MAX_TOTAL_CUT_DB) - worst
            if depth < BLEND_MIN_CUT_DB:
                break
        if accepted is None:
            break
        out.append(accepted)
        delivered = _cascade_db(out, freqs_hz)
        required = -(np.asarray(desired_db, dtype=np.float64) - delivered)
    return tuple(out)


def solve_blend_correction(
    *,
    graded: GradedSpec | None,
    band_hz: Any,
    incumbent: Sequence[Mapping[str, Any]] | None,
    previous_residual_db: float | None = None,
    no_crossover_reason: str | None = None,
) -> BlendCorrection:
    """Prescribe the next round's blend-region correction from summed evidence.

    Args:
      graded: the post-apply spatial cloud's flat-spec evaluation — the curve,
        the MERGED honesty mask it was graded under, and its report, as ONE
        record so the three cannot come from different evaluations. The cloud
        because the plan's own fundamental says so ("the cloud IS the
        measurement") and because this region's error varies with position; the
        merged mask because it is the only structural protection against
        cutting an interference null (module docstring).
      band_hz: the crossover region, ``(lo, hi)``. This is
        ``comparison_bands.crossover_region_band_hz``'s output, reached through
        its existing production consumer (the VERIFY absolute claim), so the
        band corrected over is byte-identically the band the household is shown.
        Anything unreadable — including every ``not_evaluated`` arm of that
        claim — is :data:`BLEND_NO_TRUSTED_BAND`.
      incumbent: the blend correction the measured capture rode. ``None`` means
        it could not be established and the round refuses; ``()`` means it rode
        none, which is the ordinary first round.
      no_crossover_reason: set only for a 1-way main, whose preset declares no
        region. It outranks every arm below, ``incumbent`` included: those all
        describe a region a round could not establish (#3480).
      previous_residual_db: the region residual the PREVIOUS round read, or
        ``None`` for the first round of a series. The narrow-defect stop below
        turns on it.

    Returns:
      A :class:`BlendCorrection`. Its ``filters`` are a TOTAL — the whole
      correction the next round applies, not a delta against ``incumbent``.

    **The reference is the speaker's, not the region's.** Deviation is measured
    against ``report.reference_db`` — the flat spec's own level over
    ``REFERENCE_BAND_HZ`` — never against the region's own mean. Re-centering on
    the region would make a region that is uniformly *below* the speaker's level
    look like it has hot shoulders around its dip, and cutting those shoulders
    trades one narrow notch for a wide hole across the whole presence band. The
    contract's target is flat: one level for the spectrum, so one reference.

    **The narrow-defect stop, and exactly what it does and does not buy.** The
    convergence this module claims is verified where the defect is at most as
    narrow as the correction can represent (``Q <= BLEND_FILTER_Q``). A
    NARROWER defect cannot be matched by a ``Q = 2`` cut, so each round's fit
    over-corrects the shoulders, the over-correction reads as next round's
    defect, and the loop limit-cycles instead of converging.

    **TWO QUANTITIES, and mixing them is how a reader mis-predicts this stop.**
    Both are "the region's rms" in English and neither is the other:

    * **applied region rms** — the rms of what the SPEAKER ends up radiating
      across the region, i.e. the defect plus the emitted cascade. It is the
      outcome number, and it is what the table below reports.
    * :attr:`BlendRegionReading.residual_db` — the MEASURED curve's deviation
      against the speaker's broadband flat reference, over the region
      intersected with the graded band and minus the honesty mask. It is the
      instrument number, it is what a round banks, and **it is the only one
      this stop compares.**

    Measured on a 4 dB defect at 1500 Hz, six rounds, no stop — applied region
    rms::

        defect Q=2.0  1.095 0.557 0.528 0.266 0.136 0.072   converges
        defect Q=3.0  0.667 0.344 0.392 0.344 0.392 0.344   two-cycles
        defect Q=4.0  0.532 0.493 0.732 0.493 0.732 0.493   two-cycles
        defect Q=6.0  0.617 1.009 0.767 1.134 1.171 0.767   wanders

    Every cap held at every Q — never past the per-filter or composed ceiling,
    never a boost — so this is a quality defect, not a safety one. The stop:
    **once the region stops improving, hold the incumbent and stop
    re-prescribing** (:data:`BLEND_REGION_NOT_IMPROVING`).

    Applying that rule to the table above predicts the wrong round, which is
    exactly why both quantities are named. Q=4's applied rms rises at round 3
    (0.493 → 0.732), but the reading the stop actually compares is still
    falling there and does not rise until round 4::

        Q=4 residual_db  1.142 0.532 0.422 0.458 → stops at round 4

    The bar is "improved at all", not "improved provably", and that choice is
    deliberate. A provable-improvement bar needs a round-to-round noise
    estimate for this residual, which this program does not have — and the
    nearest available number (:data:`BLEND_MIN_CUT_DB`, a per-band model
    tracking error) is the wrong quantity for an RMS over a hundred bins, large
    enough to stop the CONVERGING case at its second round.

    **What it does not buy, said plainly:** it does not guarantee the region
    ends no worse than round 1, because the overshoot that triggers the stop
    has already been applied. With the stop on, the applied region rms settles
    at::

        defect Q=3.0  0.344     (unstopped: alternates 0.344 / 0.392)
        defect Q=4.0  0.732     (unstopped: alternates 0.493 / 0.732)
        defect Q=6.0  1.009     (unstopped: wanders 0.767 … 1.171)

    Q=6 is the honest worst case and is stated rather than buried: the stop
    settles ABOVE the low half of a cycle the loop cannot stay in anyway, so
    what it buys there is a stable value rather than a better one. It converts
    an unbounded wander into a bounded one, and the region benefit claim
    reports where it settled. Undoing the last step would need the round to
    carry its PREVIOUS prescription as well as its current one, which is a
    second history this program has not earned yet.
    """

    # The band is resolved FIRST so that every refusal after it still names the
    # region it was refusing about. A receipt saying "no readable incumbent"
    # without the band cannot be told apart by a reader from a round that had
    # no crossover region at all, and those are different facts with different
    # next actions — and the receipt writer uses exactly this field to decide
    # whether the round had a blend question worth banking.
    band = _band_from(band_hz)
    # …and "no crossover region at all" is the ONE refusal that legitimately
    # carries no band, so it answers first, with its caller's own reason.
    if no_crossover_reason is not None:
        return _hold(
            no_crossover_reason,
            incumbent=tuple(dict(entry) for entry in (incumbent or ())),
        )
    if incumbent is None:
        # Checked before the band, because "no incumbent" is the one arm whose
        # disposition does not depend on having a region: with nothing
        # established to hold, there is nothing to prescribe either way.
        return _no_incumbent(band)
    incumbent_filters = tuple(dict(entry) for entry in incumbent)
    if band is None:
        return _hold(BLEND_NO_TRUSTED_BAND, incumbent=incumbent_filters)
    if graded is None:
        return _hold(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )

    try:
        freqs = np.asarray(graded.freqs_hz, dtype=np.float64)
        curve = np.asarray(graded.curve_db, dtype=np.float64)
        mask = np.asarray(graded.excluded, dtype=bool)
        reference_db = float(graded.report.reference_db)
    except (TypeError, ValueError, AttributeError):
        return _hold(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )
    if freqs.ndim != 1 or freqs.shape != curve.shape or freqs.shape != mask.shape:
        return _hold(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )
    if not (np.all(np.isfinite(freqs)) and np.all(np.isfinite(curve))
            and math.isfinite(reference_db)):
        return _hold(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )

    # Intersected with the span the flat spec actually grades
    # (``graded_band_hz`` — the whole table, already clamped to this
    # evaluation's trusted floor and ceiling), so the region residual below is
    # the pooled spec residual with only its bin set narrowed rather than a
    # second estimator over bins the spec never graded. NOT
    # ``reference_band_hz``: that is the low-mid frame the deviations are
    # stated FROM, and intersecting with it would stop this correction at
    # 2 kHz.
    graded_lo, graded_hi = graded.report.graded_band_hz
    region = (
        (freqs >= max(band[0], float(graded_lo)))
        & (freqs <= min(band[1], float(graded_hi)))
        & ~mask
    )
    if int(np.count_nonzero(region)) < BLEND_MIN_REGION_BINS:
        return _hold(BLEND_NO_TRUSTED_BAND, band_hz=band,
                     incumbent=incumbent_filters)

    deviation = curve - reference_db
    in_region = np.where(region, np.abs(deviation), -np.inf)
    worst = int(np.argmax(in_region))
    reading = BlendRegionReading(
        band_hz=band,
        residual_db=float(np.sqrt(np.mean(deviation[region] ** 2))),
        n_bins=int(np.count_nonzero(region)),
        worst_db=float(deviation[worst]),
        worst_hz=float(freqs[worst]),
    )

    if (
        previous_residual_db is not None
        and math.isfinite(previous_residual_db)
        and reading.residual_db >= previous_residual_db
    ):
        return _hold(
            BLEND_REGION_NOT_IMPROVING, band_hz=band,
            incumbent=incumbent_filters, reading=reading,
        )

    # The incumbent-accounted, damped total. `desired` is clamped non-positive
    # so the fit below can only ever ask for attenuation — the first of the two
    # independent places cuts-only is enforced (the emitter is the second).
    incumbent_db = _cascade_db(incumbent_filters, freqs)
    desired = np.minimum(incumbent_db - BLEND_DAMPING * deviation, 0.0)
    filters = _fit_cuts(freqs, desired, region)
    if not filters:
        # HOLD, not revert, on the same rule as the refusal arms: an empty fit
        # means nothing in the region EARNED a cut, which is not evidence that
        # an adopted correction should be removed. Reachable with a non-empty
        # incumbent only when that incumbent is shallower than the minimum
        # emittable cut — a deeper one is re-derived by the fit above, since
        # the prescription is a total.
        return _hold(
            BLEND_NOTHING_TO_CUT, band_hz=band, incumbent=incumbent_filters,
            reading=reading,
        )
    return BlendCorrection(
        filters=filters,
        reason=BLEND_CORRECTED,
        band_hz=band,
        incumbent=incumbent_filters,
        reading=reading,
    )
