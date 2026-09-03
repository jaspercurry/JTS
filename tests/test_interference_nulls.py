# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the interference-null identification gate.

Five layers, per the plan's second honesty instrument
(docs/historical/linearization-campaign-2026-07.md "S0 executed" section e.1, and
docs/historical/linearization-campaign-2026-07.md PR-1):

A. **Synthetic ground truth** — a two-path impulse response with a known
   ``tau`` and ``r``, and the magnitude curve derived from that *same* IR, so
   the time-domain and frequency-domain evidence the gate insists must agree
   really are two views of one physical object rather than two fixtures that
   happen to match. Recovery is asserted against the truth, with the
   estimator biases the module documents stated as tolerances rather than
   hidden inside a loose ``approx``.
B. **The depth statistic's own calibration** — the sweep quoted by
   ``NULL_DEPTH_STATISTIC``, re-derived: how much a 1/6-octave window fills a
   null versus how much it shaves a peak. The whole "read the flank smoothed,
   read the null raw" design rests on that asymmetry, so it runs rather than
   being quoted.
C. **Refusal paths** — every ``REASON_*`` and every ``CANDIDATE_*`` the
   module can emit, each from a cloud built to produce exactly that shape.
   A gate whose refusals are untested is a gate that silently identifies.
D. **Invariants** — the runaway-exclusion guard as a property over a sweep of
   synthetic clouds, and the arithmetic identities the physics helpers claim.
E. **Real-data acceptance** against the 2026-07-25 S0 session: the 8-16 kHz
   family the plan is pre-registered against, the 1.8 kHz dip's acquittal by
   depth ceiling, the ground plane's refusal to fabricate, and the four-way
   calibration table every constant in the module quotes. Gated on
   ``JTS_FLAT_LIN_S0``; absent in CI, where these skip. **A corpus-acceptance
   PR is not done until they have been seen to PASS** — a wrong-but-existing
   path skips silently.
"""
from __future__ import annotations

from types import MappingProxyType

import numpy as np
import pytest

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.interference_nulls import (
    CANDIDATE_BELOW_MIN_DEPTH,
    CANDIDATE_DEPTH_EXCEEDS_CEILING,
    CANDIDATE_NO_MATCHING_RUNG,
    CANDIDATE_NOT_MEASURABLE,
    CANDIDATE_OUTSIDE_CONTIGUOUS_RUN,
    CLASSIFICATION_INSUFFICIENT_EVIDENCE,
    CLASSIFICATION_POSITION_DEPENDENT,
    CLASSIFICATION_POSITION_INVARIANT,
    DEFAULT_MIN_NULL_DEPTH_DB,
    DEPTH_CEILING_MARGIN_DB,
    EXCLUSION_CAP_FRACTION,
    LADDER_ARRIVAL_TOLERANCE,
    MIN_CORROBORATING_POSITIONS,
    MIN_LADDER_RUNGS,
    POSITION_PRESENCE_FRACTION,
    R_AGREEMENT_TOLERANCE,
    REASON_EXCLUSION_CAP,
    REASON_LADDER_ARRIVAL_MISMATCH,
    REASON_NO_CANDIDATE_NULLS,
    REASON_NO_CORROBORATING_ARRIVALS,
    REASON_NO_LADDER,
    REASON_NO_PER_POSITION_CURVES,
    REASON_R_DISAGREEMENT,
    REASON_TOO_FEW_POSITIONS,
    RUNG_MATCH_TOLERANCE_SPACINGS,
    RefusedCandidate,
    classify_dip_position_variance,
    identify_interference_nulls,
    null_depth_ceiling_db,
    reflection_ratio_from_depth,
)
from jasper.audio_measurement.spatial_combine import (
    PositionCapture,
    combine_positions,
)
from tests._flat_lin_corpus import (
    S0_DESK_EDGE,
    S0_GROUND_PLANE,
    S0_MAIN,
    S0_MAIN_HAND_WIDTH_LOW,
    S0_MAIN_TWEETER_HEIGHT,
    requires_s0_curves,
    s0_position_captures,
)

SAMPLE_RATE = 48_000
# 4096 -> a 2049-bin curve at 11.7 Hz spacing. Chosen for cost: the combiner
# is O(bins x window) per smoothing pass and these layers build hundreds of
# clouds, so the grid is sized to the narrowest feature under test rather
# than to a capture's native resolution. The narrowest is a comb null a few
# hundred Hz wide, tens of bins here. Checked rather than assumed: at 2049,
# 4097 and 8193 bins the same clouds return the same rung sets, tau within
# 0.1 % and r_freq within 0.002 of each other.
N_FFT = 4096
# The band the synthetic layers search. Wide enough to hold four or five
# rungs of a ~300 us ladder, and clear of the grid edges so a flank search
# never runs off the end.
SYNTHETIC_BAND_HZ = (4000.0, 19_000.0)


# --------------------------------------------------------------------------- #
# Synthetic cloud construction
#
# One IR per position; the magnitude curve is that IR's own spectrum. Doing
# it this way rather than writing the comb analytically is the point: the
# gate's whole claim is that an arrival measured in the time domain and a
# ladder measured in the frequency domain describe the same reflection, and a
# fixture that built the two independently could not falsify it.
# --------------------------------------------------------------------------- #


def _two_path_ir(delay_samples: int, r: float, *, seed: int, noise: float = 1e-7) -> np.ndarray:
    """Direct arrival plus one delayed copy, on a low noise floor.

    ``delay_samples`` rather than microseconds because the delay must land on
    a sample for the ground truth to be exact: at 48 kHz a "300 us" echo is
    14.4 samples and rounds to 291.667 us, so a test asserting 300 us would
    be grading the module against a fixture that never contained one.
    """
    rng = np.random.default_rng(seed)
    ir = rng.normal(0.0, noise, 4096)
    ir[100] += 1.0
    if r != 0.0:
        ir[100 + delay_samples] += r
    return ir


def _curve_from_ir(ir: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    magnitude = 20.0 * np.log10(np.maximum(np.abs(np.fft.rfft(ir, N_FFT)), 1e-12))
    return freqs, magnitude


def _cloud(
    delays_samples: list[int],
    r: float,
    *,
    seed0: int = 1000,
    noise: float = 1e-7,
) -> list[PositionCapture]:
    """One :class:`PositionCapture` per entry of ``delays_samples``."""
    captures: list[PositionCapture] = []
    for k, delay in enumerate(delays_samples):
        ir = _two_path_ir(delay, r, seed=seed0 + k, noise=noise)
        freqs, magnitude = _curve_from_ir(ir)
        captures.append(
            PositionCapture(
                position_id=f"p{k:02d}",
                freqs_hz=freqs,
                magnitude_db=magnitude,
                sample_rate=SAMPLE_RATE,
                ir=ir,
            )
        )
    return captures


def _with_magnitude(capture: PositionCapture, magnitude_db: np.ndarray) -> PositionCapture:
    return PositionCapture(
        position_id=capture.position_id,
        freqs_hz=capture.freqs_hz,
        magnitude_db=magnitude_db,
        sample_rate=capture.sample_rate,
        ir=capture.ir,
    )


def _tau_us(delay_samples: int) -> float:
    return delay_samples / SAMPLE_RATE * 1e6


def _identify(captures, band_hz=SYNTHETIC_BAND_HZ, **kwargs):
    return identify_interference_nulls(
        combine_positions(captures), band_hz=band_hz, **kwargs
    )


# --------------------------------------------------------------------------- #
# A. Synthetic ground truth
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("delay_samples", [12, 15, 18])
@pytest.mark.parametrize("r_true", [0.20, 0.37, 0.55])
def test_a_constructed_two_path_cloud_is_identified_with_its_own_numbers(
    delay_samples, r_true
):
    """A — the headline: known truth in, the same truth out.

    Six positions sharing one sample-aligned delay, so the fixture's ``tau``
    and ``r`` are exact and every recovery error belongs to the module.

    Tolerances are the module's own documented biases, not slack:

    * ``tau`` to 1 %, because the ladder is fitted to minima located on a
      1/6-octave curve and the rung-match tolerance itself is 0.15 rung
      spacings.
    * ``r_freq`` **below** the truth by at most 0.05, and never above it —
      the direction is the assertion. ``NULL_DEPTH_STATISTIC`` reads the
      flank off the smoothed curve, which shaves a peak, so the depth is a
      lower bound and so is the ``r`` inverted from it. An ``r_freq`` above
      the truth would mean the statistic had manufactured depth.
    * every rung index exact, because an off-by-one in ``n`` would put the
      ladder at a different ``tau`` entirely.
    """
    tau_us = _tau_us(delay_samples)
    report = _identify(_cloud([delay_samples] * 6, r_true))

    assert report.reason == ""
    assert report.classification == CLASSIFICATION_POSITION_INVARIANT
    assert len(report.nulls) >= MIN_LADDER_RUNGS

    assert report.tau_ladder_us == pytest.approx(tau_us, rel=0.01)
    assert report.arrival_tau_us == pytest.approx(tau_us, rel=0.02)
    assert abs(report.ladder_arrival_gap) <= LADDER_ARRIVAL_TOLERANCE

    assert report.arrival_r_time == pytest.approx(r_true, abs=0.02)
    assert -0.05 <= report.r_freq - r_true <= 0.0, "r_freq must be a lower bound"
    assert report.agreement <= R_AGREEMENT_TOLERANCE

    # The rungs are consecutive and each lands where its own index predicts.
    indices = [null.n for null in report.nulls]
    assert indices == list(range(indices[0], indices[0] + len(indices)))
    for null in report.nulls:
        predicted = (null.n + 0.5) / (tau_us * 1e-6)
        assert null.f_center_hz == pytest.approx(predicted, rel=0.02), null
        assert null.evidence["rung_error_spacings"] <= RUNG_MATCH_TOLERANCE_SPACINGS
        assert null.f_lo_hz < null.f_center_hz < null.f_hi_hz
        assert null.classification == CLASSIFICATION_POSITION_INVARIANT


def test_every_identified_null_is_recomputable_from_its_own_evidence():
    """A — the auditability rule, asserted rather than asserted-in-prose.

    ``spatial_combine`` holds every refusal to "recomputable from the record";
    this module holds every *identification* to it too. Each arithmetic
    relation the docstrings claim is re-derived here from the record's own
    numbers.
    """
    report = _identify(_cloud([15] * 6, 0.37))
    assert report.reason == ""

    for null in report.nulls:
        evidence = null.evidence
        # depth = flank baseline - null level
        assert null.depth_db == pytest.approx(
            evidence["flank_baseline_db"] - evidence["null_level_db"], abs=1e-9
        )
        # the rung error is the distance to the predicted rung, in spacings
        assert evidence["rung_error_spacings"] == pytest.approx(
            abs(null.f_center_hz - evidence["predicted_hz"]) * null.tau_us * 1e-6,
            abs=1e-9,
        )
        # the predicted rung is (n + 1/2) / tau
        assert evidence["predicted_hz"] == pytest.approx(
            (null.n + 0.5) / (null.tau_us * 1e-6), rel=1e-9
        )
        # the ceiling is the one the arrival's loudest r permits
        assert evidence["depth_ceiling_db"] == pytest.approx(
            null_depth_ceiling_db(report.arrival_r_max), abs=1e-9
        )
        # ...and this null did not exceed it, which is why it was not acquitted
        assert null.depth_db <= evidence["depth_ceiling_db"] + DEPTH_CEILING_MARGIN_DB
        # agreement is the two instruments' disagreement
        assert null.agreement == pytest.approx(abs(null.r_time - null.r_freq), abs=1e-12)
        # the classification is the presence fraction against its threshold
        fraction = evidence["positions_present"] / evidence["positions_total"]
        expected = (
            CLASSIFICATION_POSITION_INVARIANT
            if fraction >= POSITION_PRESENCE_FRACTION
            else CLASSIFICATION_POSITION_DEPENDENT
        )
        assert null.classification == expected


def test_the_mask_and_the_intervals_are_the_same_fact():
    """A — the exclusion mask, its merged intervals and the reported fraction
    agree, and the intervals come from the combiner's own interval owner.

    ``merged_true_intervals`` is shared with ``spatial_combine`` and
    ``flat_spec`` precisely so a consumer unioning two exclusion lists is
    unioning one rule's output; this pins that this module did not grow its
    own.
    """
    from jasper.audio_measurement.spatial_combine import merged_true_intervals

    combined = combine_positions(_cloud([15] * 6, 0.37))
    report = identify_interference_nulls(combined, band_hz=SYNTHETIC_BAND_HZ)
    assert report.reason == ""

    assert report.excluded.shape == combined.freqs_hz.shape
    assert report.excluded_bands_hz == merged_true_intervals(
        combined.freqs_hz, report.excluded
    )
    assert len(report.excluded_bands_hz) == len(report.nulls)
    for null, (lo, hi) in zip(report.nulls, report.excluded_bands_hz, strict=True):
        assert (lo, hi) == (null.f_lo_hz, null.f_hi_hz)

    band = (combined.freqs_hz >= SYNTHETIC_BAND_HZ[0]) & (
        combined.freqs_hz <= SYNTHETIC_BAND_HZ[1]
    )
    assert report.excluded_fraction == pytest.approx(
        np.count_nonzero(report.excluded[band]) / np.count_nonzero(band)
    )
    # Nothing outside the analysis band is ever excluded by this gate.
    assert not report.excluded[~band].any()
    # The mask is not writeable, like every other array this stack returns.
    assert not report.excluded.flags.writeable


def test_the_gate_is_orthogonal_to_the_power_vs_median_screen():
    """A — the blind spot this module exists for, demonstrated end to end.

    A cloud whose positions share one delay is exactly the case the combiner
    screen is documented to miss: every position sees the same null, so the
    power mean and the dB median agree and nothing is flagged. The null gate
    identifies the same nulls the screen cannot see. This is the plan's
    "necessary but not sufficient" finding (section e.1) as an executable
    assertion rather than a doc claim.
    """
    combined = combine_positions(_cloud([15] * 6, 0.55))
    report = identify_interference_nulls(combined, band_hz=SYNTHETIC_BAND_HZ)

    band = (combined.freqs_hz >= SYNTHETIC_BAND_HZ[0]) & (
        combined.freqs_hz <= SYNTHETIC_BAND_HZ[1]
    )
    assert not combined.excluded[band].any(), "the screen should see nothing here"
    assert combined.geometry.locked, "...because the geometry is locked"
    assert report.reason == ""
    assert np.count_nonzero(report.excluded[band]) > 0, "the null gate should"


# --------------------------------------------------------------------------- #
# B. The depth statistic's calibration
# --------------------------------------------------------------------------- #


def test_smoothing_fills_a_null_far_more_than_it_shaves_a_peak():
    """B — the measurement ``NULL_DEPTH_STATISTIC`` is built on.

    A 1/6-octave window costs a comb *null* most of its depth and a comb
    *peak* almost nothing, because the null is a narrow notch and the peak a
    broad arch. That asymmetry is the entire reason the shipped statistic
    reads the flank off the smoothed curve and the null off the unsmoothed
    one, so it is re-derived here on exact arithmetic rather than quoted.

    The grid is committed, not described: tau 300 and 450 us x r 0.20 / 0.37 /
    0.55 x rungs n = 1..5, on the same 1.465 Hz analysis grid the combiner
    decimates to. Measured 2026-07-25.
    """
    freqs = np.fft.rfftfreq(1 << 15, 1.0 / SAMPLE_RATE)
    freqs = freqs[(freqs >= 200.0) & (freqs <= 22_000.0)]

    fills: list[float] = []
    shaves: list[float] = []
    for tau_us in (300.0, 450.0):
        tau_s = tau_us * 1e-6
        for r in (0.20, 0.37, 0.55):
            raw = 20.0 * np.log10(np.abs(1.0 + r * np.exp(-2j * np.pi * freqs * tau_s)))
            diag = smooth_fractional_octave(freqs, raw, fraction=6)
            for n in range(1, 6):
                f_null = (n + 0.5) / tau_s
                f_peak = n / tau_s
                if f_null > freqs[-1] or (n + 1) / tau_s > freqs[-1]:
                    continue
                i = int(np.argmin(np.abs(freqs - f_null)))
                j = int(np.argmin(np.abs(freqs - f_peak)))
                fills.append(float(diag[i] - raw[i]))
                shaves.append(float(raw[j] - diag[j]))

    assert len(fills) == len(shaves) == 30
    assert min(fills) == pytest.approx(0.13, abs=0.02)
    assert max(fills) == pytest.approx(5.98, abs=0.02)
    assert min(shaves) == pytest.approx(0.027, abs=0.005)
    assert max(shaves) == pytest.approx(1.044, abs=0.02)
    # The asymmetry itself, which is the design claim: on every single case
    # the null loses more than the peak, and in the worst case by 5.7x.
    assert all(fill > shave for fill, shave in zip(fills, shaves, strict=True))
    assert max(fills) / max(shaves) == pytest.approx(5.7, abs=0.3)


def test_the_depth_statistic_is_a_lower_bound_on_the_true_depth():
    """B — the direction of the statistic's bias, on known truth.

    The physics gives the exact depth for a two-path sum. The shipped
    statistic must come in at or below it — never above, which would mean
    depth had been manufactured — and the shortfall must stay inside the
    peak-shave the sweep above bounds.
    """
    for r_true in (0.20, 0.37, 0.55):
        report = _identify(_cloud([15] * 6, r_true))
        assert report.reason == ""
        truth = null_depth_ceiling_db(r_true)
        for null in report.nulls:
            assert null.depth_db <= truth, (r_true, null)
            assert truth - null.depth_db <= 1.5, (r_true, null)


# --------------------------------------------------------------------------- #
# C. Refusal paths
# --------------------------------------------------------------------------- #


def test_a_cloud_with_no_echo_identifies_nothing():
    """C — the negative control. No echo in the IRs, no comb in the curves,
    so nothing to corroborate and nothing to fit; the registry is empty and
    the reason says which of the two was missing first."""
    report = _identify(_cloud([0] * 6, 0.0))
    assert report.reason == REASON_NO_CORROBORATING_ARRIVALS
    assert report.classification == CLASSIFICATION_INSUFFICIENT_EVIDENCE
    assert report.nulls == ()
    assert report.excluded_bands_hz == ()
    assert not report.excluded.any()
    assert report.n_corroborating == 0


def test_a_ladder_without_an_arrival_is_insufficient_evidence():
    """C — frequency-domain evidence alone is not an identification.

    The curves carry a real, clean comb; the IRs are withheld, so no position
    can corroborate it in the time domain. The gate refuses. This is the
    estimator-independence rule doing its job: a ladder is not allowed to
    vouch for itself.
    """
    combed = [
        PositionCapture(
            position_id=capture.position_id,
            freqs_hz=capture.freqs_hz,
            magnitude_db=capture.magnitude_db,
            sample_rate=capture.sample_rate,
            ir=None,
        )
        for capture in _cloud([15] * 6, 0.37)
    ]
    # The comb really is there — the same curves identify once IRs are given.
    assert _identify(_cloud([15] * 6, 0.37)).reason == ""

    report = _identify(combed)
    assert report.reason == REASON_NO_CORROBORATING_ARRIVALS
    assert report.nulls == ()
    assert report.n_corroborating == 0


def test_an_arrival_without_a_ladder_is_insufficient_evidence():
    """C — and time-domain evidence alone is not one either.

    Real two-path IRs, so ``detect_echo`` finds and clusters the arrival; the
    magnitude curves are replaced with a flat response, so there is no comb
    to attribute to it. The gate reports the arrival it found and identifies
    nothing.
    """
    flat = [
        _with_magnitude(capture, np.zeros_like(capture.magnitude_db))
        for capture in _cloud([15] * 6, 0.37)
    ]
    report = _identify(flat)
    assert report.reason == REASON_NO_CANDIDATE_NULLS
    assert report.nulls == ()
    # The arrival is still reported: the refusal is about the curve, and a
    # reader must be able to see which half of the evidence was present.
    assert report.n_corroborating == 6
    assert report.arrival_tau_us == pytest.approx(_tau_us(15), rel=0.02)
    assert report.arrival_r_time == pytest.approx(0.37, abs=0.02)


def test_a_single_estimate_is_never_enough_to_identify():
    """C — the "at least two corroborating positions" rule (method step 1).

    One position carries the echo; the rest have none. A lone estimate sits
    within any tolerance of its own median, so calling that a cluster would
    be vacuous — the same reasoning ``GEOMETRY_MIN_CONFIDENT`` encodes.
    """
    captures = _cloud([0] * 5, 0.0)
    echoing = _cloud([15], 0.37, seed0=99)[0]
    report = _identify([echoing, *captures])
    assert report.reason == REASON_NO_CORROBORATING_ARRIVALS
    assert report.n_corroborating == 1
    assert report.n_corroborating < MIN_CORROBORATING_POSITIONS
    assert report.nulls == ()
    # The lone estimate is disclosed rather than zeroed: the refusal is about
    # how many positions agreed, and a reader has to be able to see what the
    # one that did actually measured.
    assert report.arrival_tau_us == pytest.approx(_tau_us(15), rel=0.02)
    assert report.arrival_r_time == pytest.approx(0.37, abs=0.02)
    assert report.arrival_r_max == report.arrival_r_time


def test_a_dip_deeper_than_the_ceiling_is_acquitted_and_left_alone():
    """C — the depth-ceiling acquittal (method step 5), on known truth.

    A genuine r = 0.25 two-path cloud (ceiling 4.46 dB) with an added narrow
    notch, constructed 3 dB deeper than that ceiling, at a frequency off the
    ladder. The notch cannot be that arrival's doing, so it is refused
    attribution by name — and, per the plan, it is **left alone**: not
    identified, not excluded, not silently dropped.
    """
    ceiling = null_depth_ceiling_db(0.25)
    captures = [
        _with_magnitude(
            capture,
            capture.magnitude_db
            - (ceiling + 3.0)
            * np.exp(-0.5 * ((capture.freqs_hz - 3000.0) / 120.0) ** 2),
        )
        for capture in _cloud([15] * 6, 0.25)
    ]
    report = _identify(captures, band_hz=(2000.0, 19_000.0))

    assert report.reason == "", "the real ladder is still identified"
    acquitted = [
        refusal
        for refusal in report.refusals
        if refusal.reason == CANDIDATE_DEPTH_EXCEEDS_CEILING
    ]
    assert len(acquitted) == 1, report.refusals
    (notch,) = acquitted
    assert notch.f_center_hz == pytest.approx(3000.0, abs=100.0)
    # The refusal is recomputable from the record, and the ceiling on it is
    # the one the cloud's own loudest arrival permits.
    assert notch.evidence["depth_ceiling_db"] == pytest.approx(
        null_depth_ceiling_db(report.arrival_r_max), abs=1e-9
    )
    assert notch.evidence["depth_ceiling_db"] == pytest.approx(ceiling, abs=0.1)
    assert notch.depth_db > notch.evidence["depth_ceiling_db"] + DEPTH_CEILING_MARGIN_DB
    # ...and the identified rungs of the real ladder are all under it, so the
    # test is not passing because the rule fires on everything.
    assert all(
        null.depth_db <= null.evidence["depth_ceiling_db"] + DEPTH_CEILING_MARGIN_DB
        for null in report.nulls
    )
    # Acquitted is not excluded.
    assert all(not (lo <= 3000.0 <= hi) for lo, hi in report.excluded_bands_hz)
    assert all(abs(null.f_center_hz - 3000.0) > 200.0 for null in report.nulls)


def test_a_single_dip_is_not_a_ladder():
    """C — ``MIN_LADDER_RUNGS``: one dip is not a ladder under any reading.

    A real comb, searched in a band narrow enough to hold exactly one of its
    rungs. The dip is genuine, deep and corroborated by a real arrival — and
    still refused, because a single frequency carries no spacing information
    and any tau at all can be made to pass through it.
    """
    report = _identify(_cloud([15] * 6, 0.50), band_hz=(10_000.0, 12_500.0))

    assert report.reason == REASON_NO_LADDER
    assert report.nulls == ()
    assert report.n_corroborating == 6
    material = [
        refusal
        for refusal in report.refusals
        if refusal.depth_db >= DEFAULT_MIN_NULL_DEPTH_DB
    ]
    assert len(material) == 1, report.refusals
    assert material[0].reason == CANDIDATE_NO_MATCHING_RUNG
    # ...and it is the n=3 rung of the fixture's own ladder, so the band
    # really does hold exactly one real null rather than none.
    assert material[0].f_center_hz == pytest.approx(3.5 * SAMPLE_RATE / 15.0, rel=0.02)


def test_dips_that_fit_no_single_tau_ladder_are_refused():
    """C — ``REASON_NO_LADDER`` on a curve with real, material dips.

    Two isolated notches on an otherwise flat response, with a genuine
    arrival present to corroborate against. No single tau places both within
    ``RUNG_MATCH_TOLERANCE_SPACINGS`` of a rung, so the gate refuses and
    names each dip.

    **The frequencies are chosen, and that is worth knowing.** An arbitrary
    pair usually *can* be read as two adjacent rungs of some shorter ladder —
    5 kHz and 14 kHz, for instance, sit within tolerance of n=0 and n=1 of a
    111 us ladder, and are then refused one step later by arrival
    corroboration instead. 6 kHz and 15 kHz fit no ladder at all, which
    isolates this refusal from that one.
    """
    captures = []
    for capture in _cloud([15] * 6, 0.37):
        flat = np.zeros_like(capture.magnitude_db)
        for centre in (6000.0, 15_000.0):
            flat -= 6.0 * np.exp(
                -0.5 * ((capture.freqs_hz - centre) / (0.03 * centre)) ** 2
            )
        captures.append(_with_magnitude(capture, flat))
    report = _identify(captures)

    assert report.reason == REASON_NO_LADDER
    assert report.nulls == ()
    assert report.n_corroborating == 6, "the arrival was there; the ladder was not"
    material = {
        round(refusal.f_center_hz, -2): refusal.reason
        for refusal in report.refusals
        if refusal.depth_db >= DEFAULT_MIN_NULL_DEPTH_DB
    }
    assert material == {
        6000.0: CANDIDATE_NO_MATCHING_RUNG,
        15_000.0: CANDIDATE_NO_MATCHING_RUNG,
    }, report.refusals


def test_only_a_consecutive_run_counts_as_a_ladder():
    """C — the contiguity rule itself, at the level it is decided.

    A non-adjacent pair pins no tau: any delay dividing the gap explains it.
    The rule that stops a fit claiming a gap-skipping shape like "n=3 and
    n=8" is that only the longest *consecutive* run of matched rungs is
    kept, and it must reach ``MIN_LADDER_RUNGS``. (The measured S0
    counterfactual is the 1.8 kHz dip admitted as a skipped-rung n=0 — see
    test_contiguity_is_what_keeps_the_1_8_khz_dip_out_of_the_registry; the
    ground-plane leg never reaches the fit at all, per MIN_LADDER_RUNGS's
    own retraction note.) Pinned directly because a fixture that reaches it
    end-to-end would be testing the locator's arithmetic as much as this
    rule's.
    """
    from jasper.audio_measurement.interference_nulls import _longest_consecutive

    assert _longest_consecutive([3, 8]) == [3]
    assert _longest_consecutive([2, 3, 4]) == [2, 3, 4]
    assert _longest_consecutive([0, 2, 3, 4, 7]) == [2, 3, 4]
    assert _longest_consecutive([1, 2, 5, 6]) == [1, 2], "earliest wins a tie"
    assert len(_longest_consecutive([3, 8])) < MIN_LADDER_RUNGS


def test_a_ladder_at_the_wrong_delay_is_refused_by_arrival_corroboration():
    """C — ``REASON_LADDER_ARRIVAL_MISMATCH``: a real comb, a real arrival,
    and they are not the same reflection.

    The curves carry a comb at one delay and the IRs an arrival at a very
    different one. Each instrument on its own is perfectly happy; the gate
    refuses because they disagree, which is the corroboration step's whole
    purpose.
    """
    curves = _cloud([15] * 6, 0.45)
    arrivals = _cloud([30] * 6, 0.45, seed0=2000)
    mixed = [
        PositionCapture(
            position_id=curve.position_id,
            freqs_hz=curve.freqs_hz,
            magnitude_db=curve.magnitude_db,
            sample_rate=curve.sample_rate,
            ir=arrival.ir,
        )
        for curve, arrival in zip(curves, arrivals, strict=True)
    ]
    report = _identify(mixed)

    assert report.reason == REASON_LADDER_ARRIVAL_MISMATCH
    assert report.nulls == ()
    # Both halves are reported, so the disagreement is legible.
    assert report.tau_ladder_us == pytest.approx(_tau_us(15), rel=0.02)
    assert report.arrival_tau_us == pytest.approx(_tau_us(30), rel=0.02)
    assert abs(report.ladder_arrival_gap) > LADDER_ARRIVAL_TOLERANCE


def test_a_ladder_at_the_right_delay_but_the_wrong_strength_is_refused():
    """C — ``REASON_R_DISAGREEMENT``: the second corroboration, alone.

    Same delay in both domains, so the tau check passes; the curve's comb is
    far shallower than the IR's arrival is loud, so the two implied
    reflection ratios cannot describe one reflection. Refused, with both
    numbers on the record.

    The disagreement has to run *this* way round — a comb too **deep** for
    the arrival never reaches this gate, because the depth-ceiling acquittal
    catches it first and refuses each rung individually (a stronger,
    per-candidate answer). So the reachable shape of an r disagreement is a
    ladder shallower than its arrival, which is what this fixture builds.
    """
    curves = _cloud([15] * 6, 0.15)
    arrivals = _cloud([15] * 6, 0.55, seed0=3000)
    mixed = [
        PositionCapture(
            position_id=curve.position_id,
            freqs_hz=curve.freqs_hz,
            magnitude_db=curve.magnitude_db,
            sample_rate=curve.sample_rate,
            ir=arrival.ir,
        )
        for curve, arrival in zip(curves, arrivals, strict=True)
    ]
    report = _identify(mixed, min_depth_db=1.0)

    assert report.reason == REASON_R_DISAGREEMENT
    assert report.nulls == ()
    assert abs(report.ladder_arrival_gap) <= LADDER_ARRIVAL_TOLERANCE
    assert report.agreement > R_AGREEMENT_TOLERANCE
    assert report.r_freq < report.arrival_r_time


def test_a_shallow_dip_is_refused_as_immaterial_and_the_floor_is_the_knob():
    """C — ``CANDIDATE_BELOW_MIN_DEPTH``, and that ``min_depth_db`` is what
    moves it.

    A comb too shallow to matter is refused at the default floor and
    identified when the caller lowers it — which is the point of the floor
    being a parameter: it is a product judgement, and this module holds no
    product policy.
    """
    captures = _cloud([15] * 6, 0.10)
    ceiling = null_depth_ceiling_db(0.10)
    assert ceiling < DEFAULT_MIN_NULL_DEPTH_DB, "the fixture must be immaterial"

    default = _identify(captures)
    assert default.reason == REASON_NO_CANDIDATE_NULLS
    assert default.nulls == ()
    assert {refusal.reason for refusal in default.refusals} == {
        CANDIDATE_BELOW_MIN_DEPTH
    }
    assert all(
        refusal.evidence["min_depth_db"] == DEFAULT_MIN_NULL_DEPTH_DB
        for refusal in default.refusals
    )

    lowered = _identify(captures, min_depth_db=0.5)
    assert lowered.reason == ""
    assert lowered.min_depth_db == 0.5
    assert len(lowered.nulls) >= MIN_LADDER_RUNGS


def test_a_matched_rung_outside_the_longest_run_is_refused_by_name():
    """C — ``CANDIDATE_OUTSIDE_CONTIGUOUS_RUN``, the refusal that keeps an
    orphan rung out of the registry without pretending it did not match.

    The S0 main leg produces exactly this shape over a wide band: the
    1.8 kHz lobing dip lands within tolerance of the ladder's n=0 rung while
    n=1 is missing, so it matches an index but not the run. Here it is on
    known truth — a comb with its n=1 rung erased by a local boost, leaving
    n=0 stranded.
    """
    captures = []
    for capture in _cloud([15] * 6, 0.50):
        f = capture.freqs_hz
        n1 = 1.5 / (_tau_us(15) * 1e-6)
        boosted = capture.magnitude_db + 12.0 * np.exp(-0.5 * ((f - n1) / 400.0) ** 2)
        captures.append(_with_magnitude(capture, boosted))
    report = _identify(captures, band_hz=(1000.0, 19_000.0))

    assert report.reason == ""
    orphans = [
        refusal
        for refusal in report.refusals
        if refusal.reason == CANDIDATE_OUTSIDE_CONTIGUOUS_RUN
    ]
    assert orphans, report.refusals
    n0 = 0.5 / (_tau_us(15) * 1e-6)
    assert any(abs(o.f_center_hz - n0) / n0 < 0.05 for o in orphans), orphans
    # It is recorded with the rung it matched, so the refusal is auditable.
    for orphan in orphans:
        assert orphan.evidence["rung_error_spacings"] <= RUNG_MATCH_TOLERANCE_SPACINGS
    assert [null.n for null in report.nulls] == list(
        range(report.nulls[0].n, report.nulls[0].n + len(report.nulls))
    )
    assert all(null.n > 0 for null in report.nulls)


def test_a_top_octave_rolloff_is_not_mistaken_for_a_null():
    """C — the cliff at the top of the band, which is the shape that would
    otherwise read as one very deep "null".

    Nothing special handles it: a monotonic run has no local minimum, so it
    never becomes a candidate, and the flank search's bound by the
    *neighbouring* minima means the ladder's own top rung cannot borrow the
    cliff's floor as an envelope either. Asserted because "we rely on there
    being no local minimum" is the kind of claim that stops being true when
    someone changes the locator.
    """
    captures = []
    for capture in _cloud([15] * 6, 0.37):
        f = capture.freqs_hz
        rolloff = np.where(f > 17_000.0, -0.004 * (f - 17_000.0), 0.0)
        captures.append(_with_magnitude(capture, capture.magnitude_db + rolloff))
    report = _identify(captures, band_hz=(4000.0, 19_500.0))

    assert report.reason == ""
    assert all(
        refusal.f_center_hz < 17_000.0 or refusal.depth_db < DEFAULT_MIN_NULL_DEPTH_DB
        for refusal in report.refusals
    ), report.refusals
    assert all(hi < 19_000.0 for _lo, hi in report.excluded_bands_hz)


def test_a_grid_too_coarse_for_a_local_envelope_refuses_rather_than_invents_one():
    """C — ``CANDIDATE_NOT_MEASURABLE``, and the regime that reaches it.

    The flank search is bounded to half an octave either side so the
    "envelope" stays local. On a grid coarse enough that half an octave falls
    inside a single bin, there is no bin in that window to read a flank from
    — and the honest answer is to refuse the candidate rather than measure it
    against whatever bin happened to be nearest.

    The fixture is that grid: 1 kHz linear spacing, so at 2 kHz the half
    octave below reaches 1414 Hz and lands inside the 1000-2000 Hz bin. This
    is a degenerate capture rather than anything the plan's flow produces
    (its grids are ~1.5 Hz), which is exactly why the branch needs a test —
    nothing else in the suite goes near it.
    """
    freqs = np.arange(0.0, 24_001.0, 1000.0)
    captures = []
    for k in range(4):
        magnitude = np.zeros_like(freqs)
        magnitude[2] = -8.0
        captures.append(
            PositionCapture(
                position_id=f"p{k:02d}",
                freqs_hz=freqs,
                magnitude_db=magnitude,
                sample_rate=SAMPLE_RATE,
                ir=_two_path_ir(15, 0.37, seed=500 + k),
            )
        )
    report = identify_interference_nulls(
        combine_positions(captures), band_hz=(1000.0, 5000.0), min_depth_db=0.5
    )
    assert [refusal.reason for refusal in report.refusals] == [CANDIDATE_NOT_MEASURABLE]
    assert report.refusals[0].f_center_hz == pytest.approx(2000.0)
    assert report.refusals[0].depth_db == 0.0, "no depth was ever measured"
    assert report.nulls == ()
    assert not report.excluded.any()


def test_a_response_without_per_position_curves_is_refused_rather_than_guessed():
    """C — ``REASON_NO_PER_POSITION_CURVES``.

    ``combine_positions`` always populates them; a hand-built or deserialised
    :class:`CombinedResponse` need not. Classification is *defined* per
    position, so the honest answer is to refuse rather than to classify
    everything ``position_invariant`` by default.
    """
    from dataclasses import replace

    combined = combine_positions(_cloud([15] * 6, 0.37))
    assert identify_interference_nulls(combined, band_hz=SYNTHETIC_BAND_HZ).reason == ""

    stripped = replace(
        combined,
        per_position_db=np.empty((0, 0)),
        per_position_diag_db=np.empty((0, 0)),
    )
    report = identify_interference_nulls(stripped, band_hz=SYNTHETIC_BAND_HZ)
    assert report.reason == REASON_NO_PER_POSITION_CURVES
    assert report.classification == CLASSIFICATION_INSUFFICIENT_EVIDENCE
    assert report.nulls == ()


@pytest.mark.parametrize(
    "band_hz, match",
    [
        (None, "pair of finite numbers"),
        ((5000.0,), "pair of finite numbers"),
        ((5000.0, 6000.0, 7000.0), "pair of finite numbers"),
        (("a", "b"), "pair of finite numbers"),
        ((0.0, 19_000.0), "0 < lo < hi"),
        ((19_000.0, 5000.0), "0 < lo < hi"),
        ((float("nan"), 19_000.0), "0 < lo < hi"),
        ((1e-9, 2e-9), "need at least 3"),
    ],
)
def test_a_malformed_band_raises_rather_than_refusing(band_hz, match):
    """C — malformed *config* raises; malformed *data* refuses.

    The same line ``combine_positions`` draws: an analysis band is the
    caller's own configuration, wrong for the whole cloud at once and
    unfixable by looking at the captures, so it fails loudly instead of
    turning into a report that says "found nothing".
    """
    combined = combine_positions(_cloud([15] * 6, 0.37))
    with pytest.raises(ValueError, match=match):
        identify_interference_nulls(combined, band_hz=band_hz)


def test_a_non_positive_depth_floor_raises():
    combined = combine_positions(_cloud([15] * 6, 0.37))
    with pytest.raises(ValueError, match="min_depth_db must be positive"):
        identify_interference_nulls(
            combined, band_hz=SYNTHETIC_BAND_HZ, min_depth_db=0.0
        )


# --------------------------------------------------------------------------- #
# D. Invariants
# --------------------------------------------------------------------------- #


def test_the_physics_helpers_invert_each_other():
    """D — ``null_depth_ceiling_db`` and ``reflection_ratio_from_depth`` are
    one relation and its inverse, plus the edge conventions they document."""
    for r in (0.01, 0.1, 0.25, 0.37, 0.55, 0.8, 0.99):
        assert reflection_ratio_from_depth(null_depth_ceiling_db(r)) == pytest.approx(
            r, rel=1e-9
        )
    assert null_depth_ceiling_db(0.0) == 0.0
    assert null_depth_ceiling_db(-0.5) == 0.0
    assert null_depth_ceiling_db(1.0) == float("inf")
    assert reflection_ratio_from_depth(0.0) == 0.0
    assert reflection_ratio_from_depth(-3.0) == 0.0
    # The S0 headline arithmetic, from the plan's "S0 executed" section b:
    # an r = 0.373 arrival cannot cut a null deeper than 6.81 dB.
    assert null_depth_ceiling_db(0.373) == pytest.approx(6.81, abs=0.01)
    # ...and 6.19 dB of null depth implies r = 0.342.
    assert reflection_ratio_from_depth(6.19) == pytest.approx(0.342, abs=0.001)


# The committed grid behind ``EXCLUSION_CAP_FRACTION``'s synthetic ceiling.
# Delays in samples at 48 kHz (208-625 us), reflection ratios spanning
# shallow to strong, and three analysis bands — the widest coverage comes
# from a narrow band holding many rungs, which is why one band is deliberately
# narrower than the others. A described grid is not a grid, so it is a
# constant the test reads rather than prose.
_CAP_SWEEP_DELAYS = (10, 12, 14, 15, 18, 22, 26, 30)
_CAP_SWEEP_RATIOS = (0.15, 0.25, 0.37, 0.50, 0.65, 0.80)
_CAP_SWEEP_BANDS = ((4000.0, 19_000.0), (2000.0, 20_000.0), (6000.0, 16_000.0))


def test_the_runaway_exclusion_cap_holds_over_the_committed_grid():
    """D — the runaway guard's property, and the ceiling it is set above.

    Two claims from one sweep of 144 clouds, because they are the same
    measurement read two ways.

    **Per case** — either the excluded fraction is at or under the cap, or
    the report is capped; and a capped report identifies nothing at all,
    rather than an arbitrary subset that happens to fit.

    **Over the population** — the worst legitimate reading is 48.24 %, from a
    dense ladder (six rungs of a ~542 us comb inside a 10 kHz band). A
    complete ladder's half-depth intervals approach the whole band from below
    as ``r`` falls, so this ceiling is physics rather than runaway, and a cap
    set at 0.50 would refuse correct behaviour — which means refusing *every*
    identification in the report. That is why the constant sits above it.
    Measured 2026-07-25.
    """
    worst = 0.0
    worst_case = None
    for delay in _CAP_SWEEP_DELAYS:
        for r_true in _CAP_SWEEP_RATIOS:
            captures = _cloud([delay] * 4, r_true)
            for band in _CAP_SWEEP_BANDS:
                report = _identify(captures, band_hz=band)
                assert report.capped == (report.reason == REASON_EXCLUSION_CAP)
                if report.capped:
                    assert report.nulls == ()
                    assert report.excluded_bands_hz == ()
                    assert not report.excluded.any()
                    assert report.excluded_fraction > EXCLUSION_CAP_FRACTION
                else:
                    assert report.excluded_fraction <= EXCLUSION_CAP_FRACTION
                if report.excluded_fraction > worst:
                    worst = report.excluded_fraction
                    worst_case = (delay, r_true, band, [n.n for n in report.nulls])

    assert worst == pytest.approx(0.4824, abs=0.006), worst_case
    assert EXCLUSION_CAP_FRACTION > worst
    assert EXCLUSION_CAP_FRACTION - worst == pytest.approx(0.168, abs=0.007)


def test_the_runaway_cap_refuses_wholesale_when_it_binds(monkeypatch):
    """D — the guard's enforcement path, forced.

    Comb physics bounds a real ladder's total half-depth width well under the
    shipped cap (see ``EXCLUSION_CAP_FRACTION`` for the measured figures), so
    the binding case has to be provoked: the constant is lowered to below what
    a known-good cloud excludes, and the same cloud must go from a full
    registry to an empty one with the attempted fraction disclosed.
    """
    import jasper.audio_measurement.interference_nulls as module

    captures = _cloud([15] * 6, 0.37)
    baseline = _identify(captures)
    assert baseline.reason == ""
    assert baseline.nulls

    monkeypatch.setattr(
        module, "EXCLUSION_CAP_FRACTION", baseline.excluded_fraction / 2.0
    )
    capped = _identify(captures)
    assert capped.reason == REASON_EXCLUSION_CAP
    assert capped.capped is True
    assert capped.classification == CLASSIFICATION_INSUFFICIENT_EVIDENCE
    assert capped.nulls == ()
    assert not capped.excluded.any()
    # The disclosure: what it would have excluded, and the fit it refused to
    # report, are both still on the record.
    assert capped.excluded_fraction == pytest.approx(baseline.excluded_fraction)
    assert capped.tau_ladder_us == pytest.approx(baseline.tau_ladder_us)
    assert capped.agreement == pytest.approx(baseline.agreement)


def test_the_report_never_claims_more_than_it_measured():
    """D — the empty-report contract: a refusal's diagnostic fields are
    populated only as far as the method got, and zeroed after."""
    report = _identify(_cloud([0] * 6, 0.0))
    assert report.reason == REASON_NO_CORROBORATING_ARRIVALS
    assert (report.tau_ladder_us, report.r_freq, report.agreement) == (0.0, 0.0, 0.0)
    assert (report.arrival_r_time, report.arrival_r_max) == (0.0, 0.0)
    assert report.ladder_arrival_gap == 0.0
    assert report.excluded_fraction == 0.0
    assert report.capped is False
    # The band and the floor are always echoed back — they are the frame every
    # other number is only interpretable in.
    assert report.band_hz == SYNTHETIC_BAND_HZ
    assert report.min_depth_db == DEFAULT_MIN_NULL_DEPTH_DB


def test_every_refusal_slug_the_module_defines_is_exercised_here():
    """D — layer C's own claim, made executable.

    This file says it covers "every ``REASON_*`` and every ``CANDIDATE_*``".
    A new refusal added to the module without a test would quietly falsify
    that, and the failure mode of an untested refusal is the worst one this
    gate has: a path that silently identifies.

    The check is textual on purpose. Collecting the slugs a test *run*
    produces would need every case to share one harness, which would make the
    refusal fixtures harder to read than the guard is worth; a slug that
    appears nowhere in this file is unambiguously untested either way.
    """
    from pathlib import Path

    import jasper.audio_measurement.interference_nulls as module

    source = Path(__file__).read_text()
    slugs = {
        name: value
        for name, value in vars(module).items()
        if name.startswith(("REASON_", "CANDIDATE_")) and isinstance(value, str)
    }
    assert len(slugs) == 13, sorted(slugs)
    untested = sorted(name for name in slugs if name not in source)
    assert untested == [], untested


def test_the_audit_trail_cannot_be_edited_after_the_fact():
    """D — ``evidence`` is read-only, on identifications and refusals alike.

    ``frozen=True`` freezes the field, not the mapping behind it, and these
    records are the whole point of the module: a verdict a later reader
    re-derives. The arrays this stack returns are set non-writeable for the
    same reason; this is that rule applied to the mappings.
    """
    report = _identify(_cloud([15] * 6, 0.37), band_hz=(2000.0, 19_000.0))
    assert report.nulls and report.refusals

    for record in (*report.nulls, *report.refusals):
        assert isinstance(record.evidence, MappingProxyType), record
        with pytest.raises(TypeError):
            record.evidence["predicted_hz"] = 0.0  # type: ignore[index]

    # The proxy is over a *copy*, so a caller who kept a reference to the
    # mapping they passed in cannot reach through it either.
    mutable = {"depth_ceiling_db": 1.0}
    pinned = RefusedCandidate(
        f_center_hz=100.0, depth_db=1.0, reason=CANDIDATE_BELOW_MIN_DEPTH,
        evidence=mutable,
    )
    mutable["depth_ceiling_db"] = 99.0
    assert pinned.evidence["depth_ceiling_db"] == 1.0

    # Values stay plain floats, so a consumer that has to serialise the
    # registry needs nothing but dict().
    import json

    assert json.loads(json.dumps(dict(report.nulls[0].evidence)))["positions_total"]


def test_refusals_are_reported_in_ascending_frequency():
    """D — a stable order, so a reader scanning a chart and a reader scanning
    the registry see the same sequence regardless of which stage refused."""
    report = _identify(_cloud([15] * 6, 0.37), band_hz=(2000.0, 19_000.0))
    frequencies = [refusal.f_center_hz for refusal in report.refusals]
    assert frequencies == sorted(frequencies)
    assert [null.f_center_hz for null in report.nulls] == sorted(
        null.f_center_hz for null in report.nulls
    )


# --------------------------------------------------------------------------- #
# E. Real-data acceptance — the 2026-07-25 S0 session
#
# The plan pre-registered this gate against these captures before it existed
# (docs/historical/linearization-campaign-2026-07.md PR-1, "Acceptance"), so
# these are the tests that decide whether it works. Every number asserted
# below is quoted in the module's own constants.
# --------------------------------------------------------------------------- #

# The S0 report's own framing of the family: 8.4 / 11.5 / 15 kHz, inside the
# tweeter's band, searched over the same 5-19 kHz the shipped echo detector
# defaults to (REPORT.md § Q1/Q2).
S0_BAND_HZ = (5000.0, 19_000.0)
# A wider band, used only where a test needs the ~1.8 kHz woofer-band dip in
# scope. It is not the band the family is graded in.
S0_WIDE_BAND_HZ = (1200.0, 19_000.0)


@pytest.fixture(scope="module")
def s0_main_leg() -> list[PositionCapture]:
    """The ten-position desk cloud, leg A — the plan's acceptance corpus."""
    return s0_position_captures(S0_MAIN)


@requires_s0_curves
def test_s0_main_leg_identifies_the_8_to_16_khz_family(s0_main_leg):
    """E — the acceptance case the whole PR is pre-registered against.

    On the S0 main-leg cloud over 5-19 kHz, the gate identifies the
    8.4 / 11.5 / 15 kHz family as one ladder, corroborated both ways.
    Measured 2026-07-25 — the numbers ``RUNG_MATCH_TOLERANCE_SPACINGS``,
    ``LADDER_ARRIVAL_TOLERANCE`` and ``R_AGREEMENT_TOLERANCE`` all quote:

      tau_ladder    298.747 us      arrival tau   321.478 us
      gap           -7.071 %        r_time        0.3765
      r_freq        0.3438          agreement     0.0327

    The ladder sits **below** the arrival, by more than the 1/6-octave
    smoothing bandwidth would allow — which is exactly why the fit takes tau
    as a free parameter rather than anchoring it on the arrival.

    RE-PINNED 2026-07-27 (flow-simplification PR-U1) for the corpus-reader
    alignment fix — ``_flat_lin_corpus.sweep_anchor``, which anchors an
    archived capture on the sweep it deconvolves instead of on the whole
    composed program. The IDENTIFIED FAMILY is untouched (same three rungs,
    same centres to 0.1 Hz, same depths to 0.01 dB, same ladder tau); what
    moved is cloud_04, whose re-aligned read clears the corroboration
    confidence floor it used to sit below — so the ladder is now corroborated
    by all ten positions rather than nine, and the arrival-derived numbers
    shift in the third decimal accordingly.

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote, which
    re-gates ``cloud_04`` -- see ``tests._flat_lin_corpus`` "The 2026-08-02
    re-pin era" for the ablation. ``cloud_04`` is a member of this cloud, so
    its wider window moves the combined curve: the two lower rung centres
    shift by ~13 Hz (8659.4 -> 8646.2, 11614.0 -> 11627.2) while the 15 kHz
    rung holds to 0.1 Hz, and the depth-implied ``r_freq`` moves 0.3490 ->
    0.3438 with ``agreement`` 0.0275 -> 0.0327. The IDENTIFICATION is
    untouched -- same three rungs, same n, same classification, same
    arrival tau and ``r_time``, still corroborated by all ten positions.
    """
    report = identify_interference_nulls(
        combine_positions(s0_main_leg), band_hz=S0_BAND_HZ
    )
    assert report.reason == ""
    assert report.classification == CLASSIFICATION_POSITION_INVARIANT

    assert [null.n for null in report.nulls] == [2, 3, 4]
    centres = [null.f_center_hz for null in report.nulls]
    assert centres == pytest.approx([8646.2, 11_627.2, 14_977.3], abs=2.0)
    depths = [null.depth_db for null in report.nulls]
    assert depths == pytest.approx([5.93, 6.23, 3.12], abs=0.05)

    # All ten positions corroborate since the 2026-07-27 alignment fix —
    # cloud_04 used to sit below the confidence floor purely because the
    # whole-program correlation had mis-anchored its deconvolution window.
    assert report.n_corroborating == 10
    assert report.arrival_tau_us == pytest.approx(321.478, abs=0.05)
    assert report.arrival_r_time == pytest.approx(0.3765, abs=0.001)
    assert report.tau_ladder_us == pytest.approx(298.747, abs=0.05)

    # The measured ladder-vs-arrival gap, and that the band admits it while a
    # smoothing-bandwidth window (about +-6 %) would not.
    assert report.ladder_arrival_gap == pytest.approx(-0.07071, abs=0.0005)
    assert abs(report.ladder_arrival_gap) > 0.06
    assert abs(report.ladder_arrival_gap) < LADDER_ARRIVAL_TOLERANCE

    # The plan's acceptance bar for the two-instrument agreement.
    assert report.r_freq == pytest.approx(0.3438, abs=0.001)
    assert report.agreement == pytest.approx(0.0327, abs=0.001)
    assert report.agreement <= 0.05


@requires_s0_curves
def test_s0_main_leg_family_is_position_invariant(s0_main_leg):
    """E — the classification, with the per-position counts it rests on.

    Measured 2026-07-25 on the ten-position cloud: the 8.6 and 11.6 kHz rungs
    are present at all ten positions, the 15 kHz rung at eight. The two
    misses are not absences — the rung is located at those positions too, its
    depth there (2.40 and 2.49 dB) just falls under the 2.5 dB materiality
    floor. 0.80 against a 0.70 threshold is the tightest reading
    ``POSITION_PRESENCE_FRACTION`` has.
    """
    combined = combine_positions(s0_main_leg)
    report = identify_interference_nulls(combined, band_hz=S0_BAND_HZ)
    assert report.reason == ""
    presence = {
        null.n: (null.evidence["positions_present"], null.evidence["positions_total"])
        for null in report.nulls
    }
    assert presence == {2: (10.0, 10.0), 3: (10.0, 10.0), 4: (8.0, 10.0)}
    assert all(
        null.classification == CLASSIFICATION_POSITION_INVARIANT
        for null in report.nulls
    )
    assert 8.0 / 10.0 >= POSITION_PRESENCE_FRACTION
    # The report-level roll-up requires *every* rung to have earned it, so
    # this is the case where the conservative rule and the headline agree.
    assert report.classification == CLASSIFICATION_POSITION_INVARIANT

    # The two "missing" positions on the 15 kHz rung, pinned — because
    # ``POSITION_PRESENCE_FRACTION`` quotes them to argue that 8/10 is a
    # materiality miss rather than an absence. The rung IS located at both;
    # its depth there just falls a shade under the 2.5 dB floor.
    import jasper.audio_measurement.interference_nulls as module

    freqs = np.asarray(combined.freqs_hz, dtype=float)
    band_idx = np.flatnonzero(
        (freqs >= S0_BAND_HZ[0]) & (freqs <= S0_BAND_HZ[1])
    )
    per_position = module._per_position_candidates(
        combined, band_idx, 1.0 / combined.diag_fraction
    )
    top_rung = next(null for null in report.nulls if null.n == 4)
    tau_s = report.tau_ladder_us * 1e-6
    sub_floor = {}
    for position_id, candidates in zip(
        combined.position_ids, per_position, strict=True
    ):
        near = [
            candidate
            for candidate in candidates
            if abs(candidate.f_hz - top_rung.f_center_hz) * tau_s
            <= RUNG_MATCH_TOLERANCE_SPACINGS
        ]
        assert near, f"{position_id} located no minimum near the 15 kHz rung"
        deepest = max(candidate.depth_db for candidate in near)
        if deepest < DEFAULT_MIN_NULL_DEPTH_DB:
            sub_floor[position_id] = deepest
    assert sorted(sub_floor) == ["cloud_06", "cloud_09"], sub_floor
    assert sub_floor["cloud_06"] == pytest.approx(2.49, abs=0.02)
    assert sub_floor["cloud_09"] == pytest.approx(2.40, abs=0.02)


@requires_s0_curves
def test_the_report_roll_up_is_the_conservative_one():
    """E — one rung that moved makes the headline ``position_dependent``.

    The S0 desk-front-edge leg is the corpus's own instance: three positions,
    and the 15 kHz rung is material at only two of them. Its two lower rungs
    are present at all three, so an "any rung" roll-up would headline
    ``position_invariant`` — and license copy saying the nulls sat at the
    same frequency at every position, which is not what this cloud measured.
    """
    report = identify_interference_nulls(
        combine_positions(s0_position_captures(S0_DESK_EDGE)), band_hz=S0_WIDE_BAND_HZ
    )
    assert report.reason == ""
    per_rung = {null.n: null.classification for null in report.nulls}
    assert per_rung == {
        2: CLASSIFICATION_POSITION_INVARIANT,
        3: CLASSIFICATION_POSITION_INVARIANT,
        4: CLASSIFICATION_POSITION_DEPENDENT,
    }
    assert report.classification == CLASSIFICATION_POSITION_DEPENDENT
    # ...and the rung that moved says so in its own numbers: 2 of 3, which on
    # a three-position cloud is the coarsest the fraction can be.
    moved = next(null for null in report.nulls if null.n == 4)
    assert moved.evidence["positions_present"] == 2.0
    assert moved.evidence["positions_total"] == 3.0
    assert 2.0 / 3.0 < POSITION_PRESENCE_FRACTION


@requires_s0_curves
def test_s0_acquits_the_1_8_khz_dip_by_depth_ceiling():
    """E — the acquittal the plan's two-mechanism verdict rests on.

    The S0 report measured the 1.8 kHz dip deepest on the six tweeter-height
    positions, and showed it is **physically impossible** for the ~320 us
    arrival to have cut it: that arrival's r caps any null at 6.81 dB and the
    dip is 10.71 dB ("S0 executed" section b). Through the shipped statistic
    on the six-position cloud the same verdict comes out at 10.08 dB against a
    7.01 dB ceiling — +3.08 dB over, and acquitted.

    RE-PINNED 2026-08-02 (#2045): PR #1991's prominence vote re-gates
    ``cloud_04``, one of these six positions — see ``tests._flat_lin_corpus``
    "The 2026-08-02 re-pin era". The dip reads deeper and slightly lower
    (9.24 -> 10.08 dB, 1814.2 -> 1808.3 Hz) against an unchanged 7.008 dB
    ceiling, so the acquittal margin grows +2.23 -> +3.08 dB. The VERDICT is
    unchanged and is what this test exists for: the dip is over its ceiling,
    so the ~320 us arrival cannot have cut it.

    **The subgroup is load-bearing and is the honest scope of this claim** —
    the ten-position divergence is asserted by
    test_the_all_ten_cloud_refuses_the_same_dip_by_a_different_rule, not left
    to this prose. This is the one place shipped behaviour diverges from the
    work order's acceptance phrasing, so it fails loudly if the mechanism
    moves.
    """
    tweeter_height = s0_position_captures(S0_MAIN, only=S0_MAIN_TWEETER_HEIGHT)
    assert len(tweeter_height) == 6
    report = identify_interference_nulls(
        combine_positions(tweeter_height), band_hz=S0_WIDE_BAND_HZ
    )

    acquitted = [
        refusal
        for refusal in report.refusals
        if refusal.reason == CANDIDATE_DEPTH_EXCEEDS_CEILING
    ]
    assert len(acquitted) == 1, report.refusals
    (dip,) = acquitted
    assert dip.f_center_hz == pytest.approx(1808.3, abs=2.0)
    assert dip.depth_db == pytest.approx(10.08, abs=0.05)
    assert dip.evidence["depth_ceiling_db"] == pytest.approx(7.008, abs=0.01)
    assert dip.depth_db - dip.evidence["depth_ceiling_db"] == pytest.approx(
        3.08, abs=0.02
    )

    # Left alone: not identified, not excluded.
    assert all(not (lo <= 1808.3 <= hi) for lo, hi in report.excluded_bands_hz)
    # ...and the real family is still identified on the same call.
    assert report.reason == ""
    assert [null.n for null in report.nulls] == [2, 3, 4]


@requires_s0_curves
def test_the_analysis_grid_cap_costs_hundredths_of_a_db_end_to_end(s0_main_leg):
    """E — what ``MAX_ANALYSIS_BINS`` actually costs, on the real corpus.

    ``NULL_DEPTH_STATISTIC``'s decimation paragraph and
    ``test_decimated_and_undecimated_curves_agree``'s docstring both quote an
    end-to-end cost, and until now **neither asserted it**: that test runs on a
    synthetic cloud and bounds worst-bin curve agreement, then defers here for
    the end-to-end figures. So the quoted numbers were free to rot, and they
    did — the shipped comment carried the 2026-07-26 originals (depths
    -0.029/+0.026/-0.004 dB, tau 0.074 us, r_freq 0.0013, margin 0.052 dB)
    straight through the 2026-07-27 corpus-reader alignment fix and the
    2026-08-02 (#2045) prominence-vote re-gate that moved the readings
    behind them.
    This test is the missing pin, so the next era fails here instead of
    quietly leaving a number in prose that no longer describes the code.

    RE-DERIVED 2026-08-22, both roots reachable, cap lifted in-process:
    the 524289-bin raw grid against the shipped 16385 cap (realized 16384).

      quantity                          decimated -> undecimated      move
      rung set (main, 5-19 kHz)         [2, 3, 4] -> [2, 3, 4]        none
      rung 2 depth                      5.9308 -> 5.8979 dB          -0.0329
      rung 3 depth                      6.2250 -> 6.2306 dB          +0.0056
      rung 4 depth                      3.1164 -> 3.1179 dB          +0.0015
      tau_ladder                        298.747 -> 298.706 us        -0.0412
      r_freq                            0.34375 -> 0.34404           +0.00028
      acquittal margin (tweeter ht, 6)  3.0767 -> 3.0504 dB          -0.0263

    Five of those six moves are SMALLER than the figure they replace and one
    is LARGER: rung 2's depth move grew, 0.029 -> 0.0329 dB. So the
    conclusion — the statistic is safe at this bound — does NOT follow a
    fortiori, and the reason it still holds is worth stating instead of
    waving at: every move remains hundredths of a dB, and the tightest
    consumer, the acquittal margin, halved (0.052 -> 0.026 dB).
    The acquittal moves because its DEPTH moves;
    the ceiling is an arrival-derived quantity and does not move at all, which
    is why the margin's move and the depth's move are the same number.

    The 0.98 dB of one-sided headroom this is measured against is
    ``DEPTH_CEILING_MARGIN_DB``'s, pinned by
    ``test_s0_ladder_calibration_populations_bracket_the_constants``.
    """
    from jasper.audio_measurement import spatial_combine as combine_module

    tweeter_height = s0_position_captures(S0_MAIN, only=S0_MAIN_TWEETER_HEIGHT)

    def read(captures, band):
        return identify_interference_nulls(combine_positions(captures), band_hz=band)

    def acquittal_margin(report):
        (dip,) = [
            refusal
            for refusal in report.refusals
            if refusal.reason == CANDIDATE_DEPTH_EXCEEDS_CEILING
        ]
        return dip.depth_db - dip.evidence["depth_ceiling_db"], dip

    shipped_cap = read(s0_main_leg, S0_BAND_HZ)
    shipped_margin, shipped_dip = acquittal_margin(
        read(tweeter_height, S0_WIDE_BAND_HZ)
    )

    # The cap is read fresh inside ``combine_positions`` on every call, which
    # is what makes this measurable in-process at all.
    original = combine_module.MAX_ANALYSIS_BINS
    try:
        combine_module.MAX_ANALYSIS_BINS = 10**9
        lifted_cap = read(s0_main_leg, S0_BAND_HZ)
        lifted_margin, lifted_dip = acquittal_margin(
            read(tweeter_height, S0_WIDE_BAND_HZ)
        )
    finally:
        combine_module.MAX_ANALYSIS_BINS = original

    # The rung SET is identical — the decimation costs precision, not rungs.
    assert [null.n for null in shipped_cap.nulls] == [2, 3, 4]
    assert [null.n for null in lifted_cap.nulls] == [2, 3, 4]

    moves = {
        shipped.n: lifted.depth_db - shipped.depth_db
        for shipped, lifted in zip(shipped_cap.nulls, lifted_cap.nulls, strict=True)
    }
    assert moves[2] == pytest.approx(-0.0329, abs=0.002)
    assert moves[3] == pytest.approx(+0.0056, abs=0.002)
    assert moves[4] == pytest.approx(+0.0015, abs=0.002)

    assert lifted_cap.tau_ladder_us - shipped_cap.tau_ladder_us == pytest.approx(
        -0.0412, abs=0.003
    )
    assert lifted_cap.r_freq - shipped_cap.r_freq == pytest.approx(
        0.00028, abs=0.00005
    )

    # The acquittal, which is the tightest thing the statistic feeds.
    assert lifted_margin - shipped_margin == pytest.approx(-0.0263, abs=0.002)
    # ...and it moves because the DEPTH moves; the ceiling is arrival-derived.
    assert lifted_dip.evidence["depth_ceiling_db"] == pytest.approx(
        shipped_dip.evidence["depth_ceiling_db"], abs=1e-9
    )
    # Whatever the digits, the verdict may never flip: still acquitted, and
    # still inside the margin rule's headroom.
    assert lifted_margin > 0.0
    assert abs(lifted_margin - shipped_margin) < DEPTH_CEILING_MARGIN_DB


@requires_s0_curves
def test_the_all_ten_cloud_refuses_the_same_dip_by_a_different_rule(s0_main_leg):
    """E — where shipped behaviour diverges from the work order's phrasing.

    The acceptance text asks the main-leg cloud to refuse the 1.8 kHz dip *by
    depth ceiling*. On the six tweeter-height positions it does. On the full
    ten-position cloud it does **not**, and the reason is the instrument
    working correctly rather than failing: the dip's frequency *moves* with
    mic height (1814 Hz at tweeter height, 1974 Hz a hand-width low — the S0
    report's own numbers), so the power mean fills it from 10.08 dB to
    **5.14 dB**, which is under the ceiling. It is still refused — as
    ``outside_contiguous_run``, being the n=0 rung of a ladder whose n=1 rung
    is absent — and still not excluded.

    Asserted rather than described because it is the single place this module
    departs from the pre-registered wording, and a silent change to which rule
    catches that dip would be exactly the kind of drift the registry exists to
    prevent.

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote re-gating
    ``cloud_04`` — see ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin era".

    **This one never went red, which is why it is worth a note.** The depth
    moved 5.19 -> 5.1441 dB, and ``abs=0.05`` absorbed it with **0.0041 dB to
    spare** — so the suite stayed green on a reading that had already moved,
    and the shipped ``MIN_LADDER_RUNGS`` rationale (which quotes this same
    refusal at 5.14 dB) silently disagreed with this test. The dip's centre
    moved the same way, 1864.0 -> 1846.4 Hz, leaving the ``< 20.0`` selector
    below only 2.4 Hz of margin. Both are re-pinned to the measurement rather
    than left riding on tolerance, because #1774's next honest re-read would
    otherwise tip this red and read as a phantom regression.
    """
    report = identify_interference_nulls(
        combine_positions(s0_main_leg), band_hz=S0_WIDE_BAND_HZ
    )
    assert report.reason == ""

    dip = next(
        refusal
        for refusal in report.refusals
        if abs(refusal.f_center_hz - 1846.4) < 20.0
    )
    # Smeared by the cloud, so the ceiling rule cannot reach it...
    assert dip.depth_db == pytest.approx(5.14, abs=0.05)
    assert dip.depth_db < null_depth_ceiling_db(report.arrival_r_max)
    assert dip.reason == CANDIDATE_OUTSIDE_CONTIGUOUS_RUN
    # ...and it is the n=0 rung it matched, with n=1 missing from the run.
    assert dip.evidence["predicted_hz"] == pytest.approx(
        0.5 / (report.tau_ladder_us * 1e-6), rel=0.05
    )
    assert dip.evidence["rung_error_spacings"] <= RUNG_MATCH_TOLERANCE_SPACINGS
    # No acquittal fires anywhere on this cloud.
    assert not any(
        refusal.reason == CANDIDATE_DEPTH_EXCEEDS_CEILING
        for refusal in report.refusals
    )
    # The registry is unchanged by it: same three rungs as the narrow band,
    # and nothing near 1.8 kHz excluded.
    assert [null.n for null in report.nulls] == [2, 3, 4]
    assert all(not (lo <= dip.f_center_hz <= hi) for lo, hi in report.excluded_bands_hz)
    assert all(lo > 5000.0 for lo, _hi in report.excluded_bands_hz)


@requires_s0_curves
def test_contiguity_is_what_keeps_the_1_8_khz_dip_out_of_the_registry(s0_main_leg):
    """E — ``MIN_LADDER_RUNGS``'s counterfactual, with one rule mutated.

    The shipped configuration in every respect — same pool, same
    depth-weighted score, same tolerance, same band — except that
    ``_longest_consecutive`` is replaced by ``sorted``, which is precisely
    "allow gaps". Measured 2026-07-26 on the ten-position main leg over
    1.2-19 kHz:

      shipped        rungs [2, 3, 4], 24.18 % of the band excluded
      gaps allowed   rungs [0, 2, 3, 4], 26.99 % excluded — n=1 skipped

    The extra rung is the 1.8 kHz lobing dip, which the plan's two-mechanism
    verdict says is not the comb at all. Note it passes **both**
    corroborations while wrong, because the deepest rung sets ``r_freq`` and
    a spurious shallow rung does not move it — so contiguity is the only rule
    standing between the registry and that claim.

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote re-gating
    ``cloud_04`` — see ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin era".
    Only the digits moved (24.27 -> 24.18 %, 27.21 -> 26.99 %, and the spurious
    rung's centre 1864.0 -> 1846.4 Hz); the counterfactual is unchanged, which
    is the whole point of the test — allowing gaps still admits the 1.8 kHz
    lobing dip as a fourth rung, and both corroborations still miss it.
    """
    import jasper.audio_measurement.interference_nulls as module

    combined = combine_positions(s0_main_leg)
    shipped = identify_interference_nulls(combined, band_hz=S0_WIDE_BAND_HZ)
    assert [null.n for null in shipped.nulls] == [2, 3, 4]
    assert shipped.excluded_fraction == pytest.approx(0.2418, abs=0.001)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "_longest_consecutive", sorted)
        gapped = identify_interference_nulls(combined, band_hz=S0_WIDE_BAND_HZ)

    assert [null.n for null in gapped.nulls] == [0, 2, 3, 4]
    assert gapped.nulls[0].f_center_hz == pytest.approx(1846.4, abs=2.0)
    assert gapped.excluded_fraction == pytest.approx(0.2699, abs=0.001)
    # It would have been excluded from correction as a comb rung...
    assert any(
        lo <= 1846.4 <= hi for lo, hi in gapped.excluded_bands_hz
    ), gapped.excluded_bands_hz
    # ...and neither corroboration would have caught it.
    assert gapped.reason == ""
    assert abs(gapped.ladder_arrival_gap) <= LADDER_ARRIVAL_TOLERANCE
    assert gapped.agreement == pytest.approx(shipped.agreement, abs=1e-9)

    # The two regimes where this counterfactual does NOT appear, so the one
    # above is not over-read: the narrow band has nothing to skip a rung to
    # reach, and the ground plane never reaches the fit at all.
    for band in (S0_BAND_HZ,):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, "_longest_consecutive", sorted)
            narrow = identify_interference_nulls(combined, band_hz=band)
        assert [null.n for null in narrow.nulls] == [2, 3, 4], band

    ground_plane = combine_positions(s0_position_captures(S0_GROUND_PLANE))
    for band in (S0_BAND_HZ, S0_WIDE_BAND_HZ):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, "_longest_consecutive", sorted)
            gp = identify_interference_nulls(ground_plane, band_hz=band)
        assert gp.reason == REASON_NO_CORROBORATING_ARRIVALS, band
        assert gp.nulls == ()


@requires_s0_curves
def test_s0_ground_plane_does_not_fabricate_an_identification():
    """E — the negative acceptance case.

    The ground-plane leg's own protocol failure (a mic capsule centimetres
    proud of the floor) put a 125-146 us arrival on all three positions at
    -0.64 to -2.57 dB re direct, and ``detect_echo`` refuses all three. At
    *this* test's window — the default (120, 800) us, which contains those
    arrivals rather than excluding them — the refusal is
    ``tau_at_window_lower_edge``: the candidates hug the window's lower edge,
    which is the edge margin's job, not the dominance rule's. (An earlier
    revision of this docstring said ``earlier_dominant_arrival``. That was
    never true at the default window on any tree; it is the slug those
    captures produce through S0's raised protocol window, and since #1750
    only when that window's lower edge is sample-aligned — both halves are
    pinned in tests/test_spatial_combine.py.) The assertion below is
    deliberately slug-agnostic, because what this test needs is only that
    nothing was usable. So this cloud has curves with real
    structure in them and **no usable arrival at all** — and the gate must
    not turn the proud-capsule arrival, or anything else, into an
    identification.

    It refuses at the first step, by name, with an empty registry: no
    exclusion is proposed from a cloud whose ladder nothing corroborates.
    """
    ground_plane = s0_position_captures(S0_GROUND_PLANE)
    assert len(ground_plane) == 3
    combined = combine_positions(ground_plane)
    # The premise, asserted rather than assumed: every position is refused.
    assert all(
        echo is not None and echo.refusal != "" for echo in combined.per_position_echo
    )

    for band in (S0_BAND_HZ, S0_WIDE_BAND_HZ):
        report = identify_interference_nulls(combined, band_hz=band)
        assert report.reason == REASON_NO_CORROBORATING_ARRIVALS, band
        assert report.classification == CLASSIFICATION_INSUFFICIENT_EVIDENCE
        assert report.nulls == ()
        assert report.excluded_bands_hz == ()
        assert not report.excluded.any()
        assert report.n_corroborating == 0
        assert report.tau_ladder_us == 0.0


@requires_s0_curves
def test_s0_ladder_calibration_populations_bracket_the_constants(s0_main_leg):
    """E — the four-way calibration table every ladder constant quotes.

    The S0 corpus offers four independent cloud groupings of the same
    speaker: the ten-position main leg, its two mic-height subgroups (the
    report's own split), and the three-position desk-front-edge leg. Each is
    fitted independently, and this is where the figures in
    ``RUNG_MATCH_TOLERANCE_SPACINGS``, ``LADDER_ARRIVAL_TOLERANCE``,
    ``R_AGREEMENT_TOLERANCE``, ``DEPTH_CEILING_MARGIN_DB`` and
    ``DEFAULT_MIN_NULL_DEPTH_DB`` come from, so they cannot rot silently.

    Measured 2026-07-25, GAP AND AGREEMENT COLUMNS RE-DERIVED 2026-07-27:

      grouping        tau_ladder  gap %    worst rung  r_freq  agreement
      main, 10        298.747     -7.071   0.0830      0.3438  0.0327
      tweeter ht, 6   298.904     -6.671   0.0818      0.3479  0.0269
      low, 4          297.961     -7.540   0.0933      0.3374  0.0410
      desk edge, 3    298.343     -7.058   0.0926      0.3746  0.0187

    The re-derivation is the corpus-reader alignment fix
    (``_flat_lin_corpus.sweep_anchor``, flow-simplification PR-U1): archived
    captures are now anchored on the sweep they deconvolve rather than on the
    whole composed program. Only ``cloud_04`` and ``cloud_09`` moved, so the
    three groupings that contain them moved and ``desk_edge`` — which contains
    neither — is byte-identical, which is the control on the change. The two
    columns this table exists to calibrate, ``tau_ladder`` and ``worst rung``,
    did not move at all; the arrival-derived gap and the agreement did, in the
    third decimal.

    RE-DERIVED AGAIN 2026-08-02 (#2045) for PR #1991's prominence vote, which
    re-gates ``cloud_04`` — see ``tests._flat_lin_corpus`` "The 2026-08-02
    re-pin era". The same control shape holds and is stronger this time: only
    ``cloud_04`` moved, so only the two groupings CONTAINING it moved (``main``
    and ``tweeter ht``), while ``low, 4`` (cloud_07-10) and ``desk edge`` are
    byte-identical across the change. Every constant this table calibrates
    keeps its headroom, because the three ``worst_*`` aggregates are all set by
    groupings that did not move.
    """
    groupings = {
        "main": (s0_main_leg, 298.747, -0.07071, 0.0830, 0.3438, 0.0327),
        "tweeter_height": (
            s0_position_captures(S0_MAIN, only=S0_MAIN_TWEETER_HEIGHT),
            298.904, -0.06671, 0.0818, 0.3479, 0.0269,
        ),
        "hand_width_low": (
            s0_position_captures(S0_MAIN, only=S0_MAIN_HAND_WIDTH_LOW),
            297.961, -0.07540, 0.0933, 0.3374, 0.0410,
        ),
        "desk_edge": (
            s0_position_captures(S0_DESK_EDGE),
            298.343, -0.07058, 0.0926, 0.3746, 0.0187,
        ),
    }

    worst_rung_error = 0.0
    worst_gap = 0.0
    worst_agreement = 0.0
    ceiling_deltas: list[float] = []
    identified_depths: list[float] = []
    unexplained_depths: list[float] = []
    shallow_depths: list[float] = []
    for name, (captures, tau, gap, rung, r_freq, agreement) in groupings.items():
        band = S0_BAND_HZ if name == "main" else S0_WIDE_BAND_HZ
        report = identify_interference_nulls(combine_positions(captures), band_hz=band)
        assert report.reason == "", name
        assert [null.n for null in report.nulls] == [2, 3, 4], name

        identified_depths.extend(null.depth_db for null in report.nulls)
        for refusal in report.refusals:
            if refusal.reason == CANDIDATE_BELOW_MIN_DEPTH:
                shallow_depths.append(refusal.depth_db)
            elif refusal.reason != CANDIDATE_DEPTH_EXCEEDS_CEILING:
                unexplained_depths.append(refusal.depth_db)

        assert report.tau_ladder_us == pytest.approx(tau, abs=0.05), name
        assert report.ladder_arrival_gap == pytest.approx(gap, abs=0.0005), name
        assert report.r_freq == pytest.approx(r_freq, abs=0.001), name
        assert report.agreement == pytest.approx(agreement, abs=0.001), name

        errors = [null.evidence["rung_error_spacings"] for null in report.nulls]
        assert max(errors) == pytest.approx(rung, abs=0.0005), name
        # The systematic part: the worst rung is n=2 on every grouping.
        assert errors[0] == max(errors), name

        worst_rung_error = max(worst_rung_error, max(errors))
        worst_gap = max(worst_gap, abs(report.ladder_arrival_gap))
        worst_agreement = max(worst_agreement, report.agreement)
        ceiling_deltas.extend(
            null.depth_db - null.evidence["depth_ceiling_db"] for null in report.nulls
        )

    # The headroom each constant claims over this population.
    assert worst_rung_error == pytest.approx(0.0933, abs=0.0005)
    assert RUNG_MATCH_TOLERANCE_SPACINGS / worst_rung_error == pytest.approx(
        1.61, abs=0.05
    )
    assert worst_gap == pytest.approx(0.07540, abs=0.0005)
    assert LADDER_ARRIVAL_TOLERANCE / worst_gap == pytest.approx(1.99, abs=0.05)
    assert worst_agreement == pytest.approx(0.0410, abs=0.001)
    assert R_AGREEMENT_TOLERANCE / worst_agreement == pytest.approx(2.44, abs=0.08)

    # The depth-ceiling margin's "must not acquit" population: 12 genuine
    # rungs, spanning -4.05 to +0.27 dB relative to their own ceiling. The
    # ceiling is what the margin has to clear, and it is *positive* — a real
    # rung can read over the bound.
    assert len(ceiling_deltas) == 12
    assert min(ceiling_deltas) == pytest.approx(-4.05, abs=0.05)
    assert max(ceiling_deltas) == pytest.approx(+0.27, abs=0.02)
    assert DEPTH_CEILING_MARGIN_DB > max(ceiling_deltas)
    # ...and the gap to the acquittal case (+3.08 dB, the test above). The
    # constant separates the two populations from BOTH sides, but no longer
    # symmetrically: RE-PINNED 2026-08-02 (#2045), PR #1991's prominence vote
    # deepened the acquitted dip 9.24 -> 10.08 dB, so the acquittal side's
    # headroom grew 0.98 -> 1.83 dB while the "must not acquit" side held at
    # 0.98 dB. The constant is now the tighter guard against acquitting a real
    # rung, which is the direction that matters — an acquittal is what removes
    # a null from the registry.
    assert DEPTH_CEILING_MARGIN_DB - max(ceiling_deltas) == pytest.approx(0.98, abs=0.03)
    assert 3.08 - DEPTH_CEILING_MARGIN_DB == pytest.approx(1.83, abs=0.03)

    # ``DEFAULT_MIN_NULL_DEPTH_DB``'s three populations, and the overlap that
    # constant is explicit about: depth alone separates nothing here.
    assert len(identified_depths) == 12
    assert (min(identified_depths), max(identified_depths)) == pytest.approx(
        (2.72, 6.84), abs=0.02
    )
    assert len(unexplained_depths) == 3
    assert (min(unexplained_depths), max(unexplained_depths)) == pytest.approx(
        (2.88, 4.15), abs=0.02
    )
    assert min(unexplained_depths) < max(identified_depths)
    assert max(unexplained_depths) > min(identified_depths), "the two overlap"
    assert len(shallow_depths) == 36
    assert (min(shallow_depths), max(shallow_depths)) == pytest.approx(
        (-1.86, 2.44), abs=0.02
    )
    assert max(shallow_depths) < DEFAULT_MIN_NULL_DEPTH_DB <= min(identified_depths)


@requires_s0_curves
def test_s0_exclusion_stays_far_below_the_runaway_cap(s0_main_leg):
    """E — the measured population behind ``EXCLUSION_CAP_FRACTION``.

    Measured 2026-07-25: the identified family excludes 30.74 % of the
    5-19 kHz band on the main leg — the widest of the S0 readings, because
    that band is the narrowest the family is graded in — and 23.85-25.35 %
    over the wider 1.2-19 kHz band across the four groupings. The cap is a
    guard, not a knob, and it must not bind on real data.

    RE-PINNED 2026-08-02 (#2045) for PR #1991's prominence vote re-gating
    ``cloud_04`` — see ``tests._flat_lin_corpus`` "The 2026-08-02 re-pin era".
    The fractions moved in the third decimal (30.85 -> 30.74 %, and the wide
    band's floor 23.91 -> 23.85 %) and the cap's headroom GREW slightly. The
    claim being guarded — that ``EXCLUSION_CAP_FRACTION`` never binds on real
    data — is unchanged and further from binding than before.
    """
    narrow = identify_interference_nulls(
        combine_positions(s0_main_leg), band_hz=S0_BAND_HZ
    )
    assert narrow.excluded_fraction == pytest.approx(0.3074, abs=0.001)
    assert narrow.capped is False

    fractions = [narrow.excluded_fraction]
    for captures in (
        s0_main_leg,
        s0_position_captures(S0_MAIN, only=S0_MAIN_TWEETER_HEIGHT),
        s0_position_captures(S0_MAIN, only=S0_MAIN_HAND_WIDTH_LOW),
        s0_position_captures(S0_DESK_EDGE),
    ):
        report = identify_interference_nulls(
            combine_positions(captures), band_hz=S0_WIDE_BAND_HZ
        )
        assert report.capped is False
        fractions.append(report.excluded_fraction)

    assert max(fractions) == pytest.approx(0.3074, abs=0.001)
    assert min(fractions[1:]) == pytest.approx(0.2385, abs=0.001)
    # The TOP of the wide-band range. Pinned 2026-08-22 because
    # ``EXCLUSION_CAP_FRACTION``'s comment quotes the range as "23.85-25.35 %"
    # and calls this test its authority: without this line the upper end lived
    # only in prose, so the claim to assert "every figure" was one figure short
    # — the same reads-as-covered gap a half-pinned set always has.
    assert max(fractions[1:]) == pytest.approx(0.2535, abs=0.001)
    assert EXCLUSION_CAP_FRACTION - max(fractions) == pytest.approx(0.3426, abs=0.002)


# --------------------------------------------------------------------------- #
# F. Position variance without the ladder (#1967)
#
# `classify_dip_position_variance` is stage 6 of the gate above, run on its
# own. These pin the two things that make it usable as a boost bound: it
# answers where the full gate refuses, and its "invariant" verdict is not a
# licence.
# --------------------------------------------------------------------------- #


def _variance(captures, band_hz=SYNTHETIC_BAND_HZ, **kwargs):
    return classify_dip_position_variance(
        combine_positions(captures), band_hz=band_hz, **kwargs
    )


# The band the notch fixtures below are read over: low enough that a single
# comb rung would be the only feature there, which is the regime this check
# is actually USED in (below the registry's 4 kHz floor). The synthetic band
# above is deliberately NOT reused -- see
# `test_f_dense_combs_read_invariant_and_that_direction_is_the_safe_one`.
NOTCH_BAND_HZ = (1200.0, 3400.0)


def _notched_cloud(
    notch_hz: list[float],
    *,
    depth_db: float = 18.0,
    width_oct: float = 0.06,
    ripple_db: float = 1.5,
    ripple_oct: float = 1.7,
) -> list[PositionCapture]:
    """A magnitude-domain cloud where each position carries ONE narrow notch,
    at whatever frequency that position was given.

    Built in the magnitude domain rather than from an IR because the property
    under test is purely "does this dip sit at the same place at every
    position", and an IR fixture would additionally have to keep a whole comb
    from wandering into the answer. The gentle log-frequency ripple is not
    decoration: a perfectly flat baseline has no strict local maxima, so a
    notch on it has no flank to measure a depth against and is refused as
    ``CANDIDATE_NOT_MEASURABLE`` before it can be classified.

    ``ir=None`` throughout, so these clouds also carry no arrival evidence --
    which is the shape the real 2026-07-30 session had.
    """
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / SAMPLE_RATE)
    log_f = np.log2(np.maximum(freqs, 1.0))
    baseline = ripple_db * np.sin(2.0 * np.pi * log_f / ripple_oct)
    return [
        PositionCapture(
            position_id=f"p{k:02d}",
            freqs_hz=freqs,
            magnitude_db=baseline
            - depth_db * np.exp(-0.5 * ((log_f - np.log2(f0)) / width_oct) ** 2),
            sample_rate=SAMPLE_RATE,
            ir=None,
        )
        for k, f0 in enumerate(notch_hz)
    ]


def test_f_positions_sharing_one_delay_read_invariant_and_exclude_nothing():
    """Eight positions, one sample-aligned delay: the comb sits at the same
    frequencies everywhere, so every material dip is present at every
    position and NOTHING is offered for exclusion.

    The empty ``position_dependent_bands_hz`` is the assertion. A consumer
    using this as a boost bound must get "nothing contradicted" here, or the
    bound would withhold gain on a cloud that agrees with itself.
    """
    report = _variance(_cloud([15] * 8, 0.37))

    assert report.reason == ""
    assert report.n_positions == 8
    assert report.dips
    assert all(
        dip.classification == CLASSIFICATION_POSITION_INVARIANT for dip in report.dips
    )
    assert all(dip.positions_present == 8 for dip in report.dips)
    assert report.position_dependent_bands_hz == ()


def test_f_a_dip_that_moves_with_position_reads_dependent_and_is_offered():
    """Five positions notch at 1800 Hz and three at 2400 Hz: the combined
    curve carries both dips, and NEITHER is present at enough positions.

    This is the case the bound acts on. The bands returned are the dips' own
    half-depth intervals, so a consumer acting on them touches exactly the
    bins the registry would have excluded had it identified the same feature.
    """
    report = _variance(
        _notched_cloud([1800.0] * 5 + [2400.0] * 3), band_hz=NOTCH_BAND_HZ,
    )

    assert report.reason == ""
    dependent = [
        dip for dip in report.dips
        if dip.classification == CLASSIFICATION_POSITION_DEPENDENT
    ]
    assert [d.positions_present for d in dependent] == [5, 3]
    assert all(
        dip.positions_present / dip.positions_total < POSITION_PRESENCE_FRACTION
        for dip in dependent
    )
    assert len(report.position_dependent_bands_hz) == 2
    for lo, hi in report.position_dependent_bands_hz:
        assert any(d.f_lo_hz <= lo and hi <= d.f_hi_hz for d in dependent), (lo, hi)


def test_f_the_same_notch_at_every_position_is_not_offered():
    """The control for the test above, differing in one thing: where the
    notch sits. Eight positions notched at the SAME frequency read 8/8
    invariant and nothing is offered -- so the verdict tracks the dip's
    mobility rather than its presence or its depth."""
    report = _variance(_notched_cloud([1800.0] * 8), band_hz=NOTCH_BAND_HZ)

    assert report.reason == ""
    assert [d.positions_present for d in report.dips] == [8]
    assert report.dips[0].classification == CLASSIFICATION_POSITION_INVARIANT
    assert report.position_dependent_bands_hz == ()


def test_f_dense_combs_read_invariant_and_that_direction_is_the_safe_one():
    """A limit of this check, pinned rather than left to be discovered.

    Where combs are DENSE relative to the smoothing window -- eight positions
    on eight different delays, read over 4-19 kHz -- some minimum of every
    position lands inside the proximity window of every combined-curve dip,
    so the check reads ``position_invariant`` even though no single feature
    is shared. It over-reads agreement, never disagreement.

    That direction is why this is a usable boost bound anyway: over-reading
    agreement offers nothing for exclusion, which leaves the permission
    exactly where the gate already had it. Under-reading would have withheld
    gain on a cloud that agrees with itself. The regime the bound is USED in
    (below 4 kHz, where a 300 us comb has at most one rung) is the sparse one
    the notch fixtures above model.
    """
    report = _variance(_cloud([11, 14, 17, 20, 23, 26, 29, 32], 0.37))

    assert report.reason == ""
    assert report.dips
    assert all(
        dip.classification == CLASSIFICATION_POSITION_INVARIANT
        for dip in report.dips
    )
    assert report.position_dependent_bands_hz == ()


def test_f_it_answers_where_the_full_gate_refuses_for_want_of_arrivals():
    """The motivating shape (#1967). A cloud whose positions supplied no IR
    gives the full gate nothing to corroborate against, so it refuses with
    ``no_corroborating_arrivals`` and classifies nothing -- which is exactly
    what the real 2026-07-30 JTS3 session's registry returned.

    The variance check reads the same curves and still answers, because
    per-position presence never needed the arrival evidence. Without this
    property the bound would be a no-op on the very session that motivated
    it.
    """
    combined = combine_positions(_notched_cloud([1800.0] * 5 + [2400.0] * 3))

    gate = identify_interference_nulls(combined, band_hz=NOTCH_BAND_HZ)
    assert gate.reason == REASON_NO_CORROBORATING_ARRIVALS
    assert gate.nulls == ()
    assert gate.excluded_bands_hz == ()

    variance = classify_dip_position_variance(combined, band_hz=NOTCH_BAND_HZ)
    assert variance.reason == ""
    assert variance.dips
    assert variance.position_dependent_bands_hz


def test_f_a_single_position_cannot_support_a_cross_position_statistic():
    """N < 2 refuses by name rather than reading 0/1 or 1/1 as a verdict —
    the same line ``combine_positions`` draws when it returns an empty
    ``band_spread``."""
    report = _variance(_cloud([15], 0.37))

    assert report.reason == REASON_TOO_FEW_POSITIONS
    assert report.dips == ()
    assert report.position_dependent_bands_hz == ()


def test_f_a_record_without_per_position_curves_refuses_by_name():
    """A deserialised or hand-built record can legally carry empty
    per-position arrays. It must refuse, never silently classify every dip as
    invariant — which is the direction that would grant boost."""
    import dataclasses

    combined = combine_positions(_cloud([15] * 6, 0.37))
    stripped = dataclasses.replace(
        combined,
        per_position_db=np.empty((0, 0)),
        per_position_diag_db=np.empty((0, 0)),
    )
    report = classify_dip_position_variance(stripped, band_hz=SYNTHETIC_BAND_HZ)

    assert report.reason == REASON_NO_PER_POSITION_CURVES
    assert report.dips == ()
    assert report.position_dependent_bands_hz == ()


def test_f_a_flat_cloud_has_no_candidate_nulls():
    """No dip clears the materiality floor, so there is nothing to classify —
    reported by name rather than as an empty success."""
    report = _variance(_cloud([15] * 6, 0.0))

    assert report.reason == REASON_NO_CANDIDATE_NULLS
    assert report.dips == ()


def test_f_malformed_config_raises_through_the_shared_validator():
    """Both entry points validate the band with one function, so a band that
    is illegal for the gate is illegal here — identically, and by raising
    rather than refusing, because it is caller configuration."""
    combined = combine_positions(_cloud([15] * 6, 0.37))

    for bad in ((0.0, 19_000.0), (19_000.0, 4000.0), (4000.0, float("inf"))):
        with pytest.raises(ValueError):
            classify_dip_position_variance(combined, band_hz=bad)
        with pytest.raises(ValueError):
            identify_interference_nulls(combined, band_hz=bad)

    with pytest.raises(ValueError):
        classify_dip_position_variance(
            combined, band_hz=SYNTHETIC_BAND_HZ, min_depth_db=0.0,
        )
    # A band that lands on fewer than three bins of this grid.
    with pytest.raises(ValueError):
        classify_dip_position_variance(combined, band_hz=(4000.0, 4000.5))


def test_f_presence_proximity_is_derived_from_the_smoothing_window(monkeypatch):
    """The one quantity this function introduces is not a chosen threshold:
    it is the width of the 1/``diag_fraction``-octave window the minima were
    located through, so two minima closer than one window are one feature.

    Pinned because a future edit that swaps it for a literal would look
    harmless and would silently re-calibrate the verdict.
    """
    from jasper.audio_measurement import interference_nulls as module

    combined = combine_positions(_notched_cloud([1800.0] * 5 + [2400.0] * 3))
    calls: list[tuple[float, int]] = []
    real = module._smoothing_bandwidth_hz

    def _spy(f_hz, fraction):
        calls.append((f_hz, fraction))
        return real(f_hz, fraction)

    monkeypatch.setattr(module, "_smoothing_bandwidth_hz", _spy)
    report = module.classify_dip_position_variance(combined, band_hz=NOTCH_BAND_HZ)

    assert report.dips
    # Every window this stage asks for is the diagnostic curve's own fraction
    # at a dip's own frequency — never a literal.
    assert {fraction for _f, fraction in calls} == {combined.diag_fraction}
    assert {dip.f_center_hz for dip in report.dips} <= {f for f, _frac in calls}

    # And it tracks the fraction rather than a constant: a coarser diagnostic
    # curve gives a wider window at the same frequency. Asked of ``real``,
    # which the patch never replaced.
    assert real(4000.0, 3) > real(4000.0, 6)
