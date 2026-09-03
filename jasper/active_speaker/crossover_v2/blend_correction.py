"""The blend region's summed-response-owned shape correction (decision 10).

Per-driver linearization is blind across the crossover blend — neither branch's
own sweep can say what the SUM does there — so this module prescribes bounded,
cuts-first shape correction from the summed at-the-mark measurement. Canonical
contract: ``docs/active-speaker-tuning-layers-design.md``, decision 10 and "The
region-based adjustment contract (2026-08-17)", clause (b). Level stays the
trim's fact (clause (c)); alignment, polarity and Fc keep their own tools.

``≤ 2`` RBJ Peaking CUTS, ``Q ≤ 2.0``, each ``≤ 3.0 dB``, composing to
``≤ 4.0 dB``, emitted PRE-SPLIT on the stereo bus
(``camilla_yaml._emit_baseline_pipeline``). Pre-split is what makes the
correction common-mode BY CONSTRUCTION — one ``B(f)`` on every role gives
``B · Σ_r sign_r·C_r·D_r``, so the sum is scaled and the inter-driver complex
ratio is untouched — which makes an asymmetric application unrepresentable
rather than merely tested against. A cuts-only correction cannot fill a dip:
with ``c(f) = −max(d(f), 0)`` every peak goes to the mean and every trough
stays exactly where it was.

The excluded bins handed in here are the cloud's MERGED honesty mask, and it is
the only structural protection against cutting an interference null instead of
a real excess: the null detector reports ``uncalibrated_below_hf_floor`` across
the entire blend window of any crossover below 4 kHz (#2600 item 1). A bin the
mask removed is not a bin this module may cut.

The summed VERIFY capture rides the APPLIED incumbent, so its deviation already
contains the incumbent's own blend correction and re-deriving ``B = −d``
absolutely would oscillate. The shipped form is a damped fixed-point
iteration::

    B_{N+1}(f) = clamp( B_N(f) − k · d_{N+1}(f) ),   k = BLEND_DAMPING = 0.7

still a TOTAL re-derived every round. Refusals HOLD the incumbent rather than
revert (panel ruling, 2026-08-18): an instrument that has just said it could
not measure has no standing to remove a correction adopted on measured
evidence. :data:`BLEND_NO_INCUMBENT` is the one arm that cannot hold, because
it is exactly the state of not knowing what to hold; its cost is that it can
REMOVE an applied correction, and it is reachable only through a corrupt or
absent applied profile.

Scope tripwire: this reads the summed response against an analytic,
offset-invariant reference and commands a common-mode filter. Reading a
per-role level, delay or trim out of a summed capture is #2653's territory.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# A LEAF of the crossover_v2 DAG — no intra-package import at all — so it can be
# read, tested and mutated without loading the round.
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
#: evidence here does not support fine sculpting: one mono sweep per position,
#: an honesty mask that removes bins inside the very window being corrected, and
#: a null detector that is uncalibrated in this band (#2600 item 1).
BLEND_MAX_FILTERS = 2

#: Q of every emitted blend cut, for THIS solver only — a PRESCRIBED cut's Q is
#: unbounded at the intake (ADR-0207). A deliberate tightening against the fit
#: engine's own ``Q ≤ 8`` peaking ceiling, and 2.0 is the Q every peaking filter
#: the series-1 fits emitted actually used, so the shape is one the loop has
#: already realized on hardware. A cut wider than its defect over-corrects the
#: shoulders, which is the skirt damage both prescribed rounds were rolled back
#: on, and it is why a FIXED-Q iterating solver still wants 2.0 — see the
#: narrow-defect stop in :func:`solve_blend_correction`.
BLEND_FILTER_Q = 2.0

#: Per-filter cut ceiling, dB — the deterministic solver's own emission bound,
#: and nothing else's: a PRESCRIBED cut has no depth ceiling (ADR-0207). Derived
#: rather than chosen: the woofer's acknowledged ``measured_excess_db`` inside
#: the blind zone was 2.09–2.26 dB across series-1 rounds r1/r2/r4
#: (1291.4–2077.2 Hz), and this model's measured tracking error on jts3 is
#: 0.5 dB, so 2.26 + 0.5 = 2.76, rounded to 3.0.
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
#: holds. A correction smaller than the gap between what the model predicts and
#: what the hardware realizes cannot be honestly claimed.
BLEND_MIN_CUT_DB = 0.5

#: Damping on the incumbent-accounted iteration: each round commands 70% of the
#: excess it measured.
#:
#: The loop converges for a realized/commanded gain ``g`` in ``(0, 2/k)``, so
#: ``k = 0.7`` widens the stable range from ``g < 2`` to ``g < 2.86``. It does
#: NOT make every measured gain stable: series-1 measured band gains from 0.136
#: to 11.736, and ``g = 11.7`` diverges at ``k = 0.7`` exactly as at ``k = 1``.
#: What bounds the excursion there is the CLAMP — the per-filter and composed
#: ceilings above — so only the clamps are load-bearing at the extreme. The cost
#: is one bite: three rounds with an uncorrected first reach ~91% of the fixed
#: point at unit gain.
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
#: smaller than :data:`BLEND_MIN_CUT_DB`. The honest outcome for a region whose
#: defect is a dip rather than a peak.
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

    Banked as the ``realized`` half of the receipt's commanded-vs-realized pair:
    what the INCUMBENT achieved, measured through it, and the number the next
    round's prescription is derived from (decision 11).
    """

    #: The graded band, ``(lo_hz, hi_hz)``.
    band_hz: tuple[float, float]
    #: RMS deviation over the region's surviving bins, dB. The SAME estimator
    #: :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` pools —
    #: deviation against the one flat reference, squared, averaged over included
    #: bins — with only its bin set narrowed to the region.
    residual_db: float
    #: How many bins that RMS was computed from: a residual that fell because
    #: the honesty mask grew is the same speaker graded on fewer bins.
    n_bins: int
    #: The worst SIGNED deviation in the region and where it sat. Signed
    #: because a dip and a peak through the handoff are opposite defects.
    worst_db: float
    worst_hz: float

    #: Which instrument produced :attr:`residual_db`, written into the record.
    #: The receipt carries TWO region residuals over the same band — this one and
    #: ``region_benefit.evidence.post_residual_db`` — and they legitimately
    #: differ because they are referenced differently: this one against the
    #: speaker's broadband flat level, the benefit axis's against the region's
    #: own surviving bins after its band mask.
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

    The refusal shape for every arm except :data:`BLEND_NO_INCUMBENT`.
    ``filters`` is the incumbent rather than ``()`` — see the module docstring
    on why a refusing instrument may not remove an adopted correction.
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

    The reader for the incumbent. "The incumbent is empty" and "the incumbent
    cannot be read" are different facts with different consequences — the first
    is a normal first round, the second is :data:`BLEND_NO_INCUMBENT` — so an
    absent/``None`` input is NOT the same as ``[]`` and callers must keep them
    apart.

    Cuts-only is re-checked here rather than assumed, because this is where data
    that left the process comes back into it. A positive gain means the record is
    not one this module wrote: unreadable, not clampable.
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
        # Real numbers, NOT anything ``float()`` will coerce: this system writes
        # floats, so a ``"1900"`` is by definition a record something else wrote.
        # ``bool`` is excluded because it is an ``int`` subclass and
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

    Through ``branch_chain.chain_response``, the ONE biquad evaluator in this
    codebase. Evaluating the incumbent any other way here would make this module
    a second opinion about what CamillaDSP realizes.
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

    ``desired_db`` is the TOTAL correction wanted, already clamped non-positive
    by the caller, so ``required = −desired`` is how much cut each bin still
    wants. Each pass takes the deepest still-wanted bin inside the region, emits
    one Peaking cut there, and subtracts what that cut actually delivers
    (evaluated, not assumed) before looking again.

    Cuts-only is STRUCTURAL here, not checked: ``required`` can only ask for
    attenuation and every emitted ``gain`` is ``−depth`` for a non-negative
    ``depth``. No branch in this function can produce a boost.
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
        # The composed-depth ceiling is enforced on the EVALUATED cascade, so two
        # cuts whose skirts overlap cannot sum past it. One shrink, then one
        # re-check: the composition is monotone in this filter's gain, so the
        # filter either lands inside the ceiling or is dropped.
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
        because this region's error varies with position; the merged mask
        because it is the only structural protection against cutting a null.
      band_hz: the crossover region, ``(lo, hi)`` —
        ``comparison_bands.crossover_region_band_hz``'s output reached through
        its existing production consumer (the VERIFY absolute claim), so the
        band corrected over is byte-identically the band the household is shown.
        Anything unreadable is :data:`BLEND_NO_TRUSTED_BAND`.
      incumbent: the blend correction the measured capture rode. ``None`` means
        it could not be established and the round refuses; ``()`` means it rode
        none, which is the ordinary first round.
      no_crossover_reason: set only for a 1-way main, whose preset declares no
        region. It outranks every arm below, ``incumbent`` included (#3480).
      previous_residual_db: the region residual the PREVIOUS round read, or
        ``None`` for the first round of a series.

    Returns:
      A :class:`BlendCorrection` whose ``filters`` are a TOTAL — the whole
      correction the next round applies, not a delta against ``incumbent``.

    **The reference is the speaker's, not the region's.** Deviation is measured
    against ``report.reference_db``, the flat spec's own level over
    ``REFERENCE_BAND_HZ``, never against the region's own mean: re-centering on
    the region would make a region uniformly BELOW the speaker's level look like
    it has hot shoulders around its dip, and cutting those trades one narrow
    notch for a wide hole across the presence band.

    **The narrow-defect stop.** Convergence is verified only where the defect is
    at most as narrow as the correction can represent (``Q <= BLEND_FILTER_Q``);
    a narrower defect is over-corrected at the shoulders each round, and the
    loop limit-cycles instead of converging. Measured on a 4 dB defect at
    1500 Hz over six rounds, the applied region rms converges at defect Q=2.0,
    two-cycles at Q=3.0 and Q=4.0, and wanders at Q=6.0 — with every cap held at
    every Q, so this is a quality defect and not a safety one. The stop: once
    the region stops improving, hold the incumbent and stop re-prescribing
    (:data:`BLEND_REGION_NOT_IMPROVING`).

    What the stop compares is :attr:`BlendRegionReading.residual_db` — the
    MEASURED curve's deviation against the speaker's broadband flat reference —
    and NOT the applied region rms, which is the outcome number. The two move
    differently: at defect Q=4 the applied rms rises a round before the reading
    does. The bar is "improved at all", not "improved provably", because a
    provable bar needs a round-to-round noise estimate for this residual that
    this program does not have; the nearest available number
    (:data:`BLEND_MIN_CUT_DB`) is a per-band model tracking error, the wrong
    quantity for an RMS over a hundred bins, and large enough to stop the
    CONVERGING case at its second round.

    It does not guarantee the region ends no worse than round 1: the overshoot
    that triggers the stop has already been applied, so at defect Q=6 the stop
    settles above the low half of a cycle the loop cannot stay in anyway. What
    it buys there is a stable value rather than a better one — an unbounded
    wander converted into a bounded one.
    """

    # The band is resolved FIRST so every refusal after it still names the region
    # it was refusing about: a receipt saying "no readable incumbent" without the
    # band cannot be told apart from a round that had no crossover region at all,
    # and the receipt writer uses this field to decide whether the round had a
    # blend question worth banking.
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

    # Intersected with the span the flat spec actually grades (``graded_band_hz``,
    # already clamped to this evaluation's trusted floor and ceiling), so the
    # region residual below is the pooled spec residual with only its bin set
    # narrowed. NOT ``reference_band_hz``: that is the low-mid frame the
    # deviations are stated FROM, and intersecting with it would stop this
    # correction at 2 kHz.
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
        # means nothing in the region EARNED a cut, which is not evidence that an
        # adopted correction should be removed. Reachable with a non-empty
        # incumbent only when that incumbent is shallower than the minimum
        # emittable cut.
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
