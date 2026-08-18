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
it. Damping is not optional: series-1 realized/commanded ratios ranged 0.136 to
**11.736×** across bands, and a per-band loop gain that large diverges at
``k = 1``.

**It fails to a no-op, never to a boost.** Every refusal arm returns zero
filters and a reason naming which one fired, so a round that corrected nothing
says *why* — "the region was already clean" and "the instrument refused" are
different facts. An unreadable incumbent is one of those arms: it is #2653's
"refuse when the reconciliation cannot be established", applied to this
module's own quantity. Unlike the level datum #2653 is about, that
reconciliation is cheap here — the incumbent is a filter list the system itself
wrote and persisted, not a per-role level a summed capture cannot separate.

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

#: Q of every emitted blend cut. A deliberate tightening against the fit
#: engine's own ``Q ≤ 8`` ceiling for cuts: a narrower cut chases a feature
#: this instrument cannot resolve and the room will not reproduce off-axis.
#: 2.0 is also the Q every peaking filter the series-1 fits actually emitted
#: used, so the shape is one the loop has already realized on hardware.
BLEND_FILTER_Q = 2.0

#: Per-filter cut ceiling, dB. Derived rather than chosen: the woofer's own
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

#: Damping on the incumbent-accounted iteration. See the module docstring:
#: the loop converges for a realized/commanded gain in ``(0, 2/k)``, and
#: series-1 measured band gains from 0.136 to 11.736. ``k = 0.7`` costs one
#: bite — three rounds with an uncorrected first reach ~91% of the fixed point
#: at unit gain — which is cheap insurance against divergence on a rig whose
#: realization ratio has ranged that far.
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
    #: included bins — with only its bin set narrowed to the region. A pinning
    #: test asserts the two agree when the region spans the graded band.
    residual_db: float
    #: How many bins that RMS was computed from. Part of the answer, not
    #: decoration: a residual that fell because the honesty mask grew is the
    #: same speaker graded on fewer bins.
    n_bins: int
    #: The worst SIGNED deviation in the region and where it sat. Signed
    #: because a dip and a peak through the handoff are opposite defects.
    worst_db: float
    worst_hz: float

    def to_dict(self) -> dict[str, Any]:
        return {
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


def _refusal(reason: str, *, band_hz: tuple[float, float] | None = None,
             incumbent: tuple[dict[str, Any], ...] = (),
             reading: BlendRegionReading | None = None) -> BlendCorrection:
    return BlendCorrection(
        filters=(), reason=reason, band_hz=band_hz, incumbent=incumbent,
        reading=reading,
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
        try:
            freq = float(entry["freq"])
            q = float(entry["q"])
            gain = float(entry["gain"])
        except (KeyError, TypeError, ValueError):
            return None
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
        ``program_analysis.crossover_region_band_hz``'s output, reached through
        its existing production consumer (the VERIFY absolute claim), so the
        band corrected over is byte-identically the band the household is shown.
        Anything unreadable — including every ``not_evaluated`` arm of that
        claim — is :data:`BLEND_NO_TRUSTED_BAND`.
      incumbent: the blend correction the measured capture rode. ``None`` means
        it could not be established and the round refuses; ``()`` means it rode
        none, which is the ordinary first round.

    Returns:
      A :class:`BlendCorrection`. Its ``filters`` are a TOTAL — the whole
      correction the next round applies, not a delta against ``incumbent``.

    **The reference is the speaker's, not the region's.** Deviation is measured
    against ``report.reference_db`` — the flat spec's own broadband level over
    ``REFERENCE_BAND_HZ`` — never against the region's own mean. Re-centering on
    the region would make a region that is uniformly *below* the speaker's level
    look like it has hot shoulders around its dip, and cutting those shoulders
    trades one narrow notch for a wide hole across the whole presence band. The
    contract's target is flat: one level for the spectrum, so one reference.
    """

    if incumbent is None:
        return _refusal(BLEND_NO_INCUMBENT)
    incumbent_filters = tuple(dict(entry) for entry in incumbent)

    band = _band_from(band_hz)
    if band is None:
        return _refusal(BLEND_NO_TRUSTED_BAND, incumbent=incumbent_filters)
    if graded is None:
        return _refusal(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )

    try:
        freqs = np.asarray(graded.freqs_hz, dtype=np.float64)
        curve = np.asarray(graded.curve_db, dtype=np.float64)
        mask = np.asarray(graded.excluded, dtype=bool)
        reference_db = float(graded.report.reference_db)
    except (TypeError, ValueError, AttributeError):
        return _refusal(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )
    if freqs.ndim != 1 or freqs.shape != curve.shape or freqs.shape != mask.shape:
        return _refusal(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )
    if not (np.all(np.isfinite(freqs)) and np.all(np.isfinite(curve))
            and math.isfinite(reference_db)):
        return _refusal(
            BLEND_NOT_COMPARABLE, band_hz=band, incumbent=incumbent_filters,
        )

    # Intersected with the span the flat spec actually grades
    # (``reference_band_hz`` — SPEC_BANDS[0] ∪ SPEC_BANDS[1], already raised to
    # this evaluation's trusted floor), so the region residual below is the
    # pooled spec residual with only its bin set narrowed rather than a second
    # estimator over bins the spec never graded.
    graded_lo, graded_hi = graded.report.reference_band_hz
    region = (
        (freqs >= max(band[0], float(graded_lo)))
        & (freqs <= min(band[1], float(graded_hi)))
        & ~mask
    )
    if int(np.count_nonzero(region)) < BLEND_MIN_REGION_BINS:
        return _refusal(BLEND_NO_TRUSTED_BAND, band_hz=band,
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

    # The incumbent-accounted, damped total. `desired` is clamped non-positive
    # so the fit below can only ever ask for attenuation — the first of the two
    # independent places cuts-only is enforced (the emitter is the second).
    incumbent_db = _cascade_db(incumbent_filters, freqs)
    desired = np.minimum(incumbent_db - BLEND_DAMPING * deviation, 0.0)
    filters = _fit_cuts(freqs, desired, region)
    return BlendCorrection(
        filters=filters,
        reason=BLEND_CORRECTED if filters else BLEND_NOTHING_TO_CUT,
        band_hz=band,
        incumbent=incumbent_filters,
        reading=reading,
    )
