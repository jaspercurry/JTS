# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import math

import numpy as np
import pytest

from jasper.audio_measurement.analysis import (
    THIRD_OCTAVE_BASS_BANDS_HZ,
    band_levels_from_magnitude,
    compression_curve,
    notch_excluded_tracking_error_db,
    thd_curve,
    tracking_error_db,
)
from jasper.audio_measurement.deconv import (
    extract_harmonic_ir,
    harmonic_magnitude_response,
    harmonic_time_advance_s,
    regularized_deconvolution_full,
)
from jasper.audio_measurement.sweep import synchronized_swept_sine


SR = 48000


def _distorted_ir(*, f1, f2, duration_s, order, coefficient):
    excitation, meta = synchronized_swept_sine(
        f1=f1,
        f2=f2,
        duration_approx_s=duration_s,
        sample_rate=SR,
    )
    x = excitation.astype(np.float64)
    distorted = x + coefficient * x ** order
    captured = np.concatenate((np.zeros(3 * SR), distorted))
    full_ir = regularized_deconvolution_full(captured, x, SR)
    direct_peak = int(np.argmax(np.abs(full_ir)))
    return full_ir, direct_peak, meta


def _local_peak(full_ir, predicted):
    radius = round(0.002 * SR)
    return predicted - radius + int(np.argmax(
        np.abs(full_ir[predicted - radius:predicted + radius + 1])
    ))


def _assert_recovered_ratio(full_ir, direct_peak, meta, order, coefficient):
    fundamental_ir = extract_harmonic_ir(full_ir, SR, direct_peak, meta, 1)
    fund_freqs, fund_db = harmonic_magnitude_response(fundamental_ir, SR, 1)
    harmonic_ir = extract_harmonic_ir(full_ir, SR, direct_peak, meta, order)
    harmonic_freqs, harmonic_db = harmonic_magnitude_response(
        harmonic_ir, SR, order
    )
    freqs, ratio = thd_curve(
        fund_freqs,
        fund_db,
        {order: (harmonic_freqs, harmonic_db)},
        band=(30.0, 150.0),
    )
    amplitude = 10.0 ** (meta.amplitude_dbfs / 20.0)
    expected_ratio = (
        coefficient * amplitude / 2.0
        if order == 2
        else (
            coefficient * amplitude ** 2 / 4.0
            / (1.0 + 3.0 * coefficient * amplitude ** 2 / 4.0)
        )
    )
    error_db = 20.0 * np.log10(np.maximum(ratio, 1e-12) / expected_ratio)
    assert freqs[0] >= 30.0 and freqs[-1] <= 150.0
    assert np.max(np.abs(error_db)) < 1.0


@pytest.mark.parametrize("order,coefficient", ((2, 0.03), (3, 0.01)))
def test_full_band_harmonic_offsets_and_recovered_ratios(order, coefficient):
    full_ir, direct_peak, meta = _distorted_ir(
        f1=20.0,
        f2=20000.0,
        duration_s=10.0,
        order=order,
        coefficient=coefficient,
    )
    predicted = direct_peak - round(SR * meta.L * math.log(order))
    assert _local_peak(full_ir, predicted) == pytest.approx(predicted, abs=2)
    _assert_recovered_ratio(full_ir, direct_peak, meta, order, coefficient)


@pytest.mark.parametrize("order,coefficient", ((2, 0.03), (3, 0.01)))
def test_narrow_bass_sweep_refines_images_and_recovers_ratios(order, coefficient):
    full_ir, direct_peak, meta = _distorted_ir(
        f1=10.0,
        f2=500.0,
        duration_s=8.0,
        order=order,
        coefficient=coefficient,
    )
    predicted = direct_peak - round(SR * meta.L * math.log(order))
    refined = _local_peak(full_ir, predicted)
    if order == 2:
        assert abs(refined - predicted) >= 30
    _assert_recovered_ratio(full_ir, direct_peak, meta, order, coefficient)


def test_thd_curve_masks_low_fundamental_snr_only():
    freqs = np.asarray((20.0, 40.0, 80.0, 160.0))
    fundamental_db = np.asarray((-60.0, -20.0, -20.0, -20.0))
    _, ratio = thd_curve(
        freqs,
        fundamental_db,
        {2: (freqs, fundamental_db - 30.0)},
        noise_floor=(freqs, np.asarray((-65.0, -80.0, -80.0, -80.0))),
    )
    assert np.isnan(ratio[0])
    assert np.all(np.isfinite(ratio[1:]))


def test_third_octave_bands_and_power_mean_levels():
    assert len(THIRD_OCTAVE_BASS_BANDS_HZ) == 11
    levels = band_levels_from_magnitude(
        np.asarray((20.0, 25.0, 31.5)),
        np.asarray((0.0, -10.0, -20.0)),
        ((20.0, 30.0), (30.0, 40.0)),
    )
    assert levels == pytest.approx((-2.596, -20.0), abs=0.001)


def test_extract_harmonic_ir_rejects_window_collision():
    _, meta = synchronized_swept_sine(
        f1=10.0, f2=500.0, duration_approx_s=8.0, sample_rate=SR
    )
    full_ir = np.zeros(1000)
    with pytest.raises(ValueError, match="crosses"):
        extract_harmonic_ir(full_ir, SR, 500, meta, 2)
    assert harmonic_time_advance_s(meta, 2) == pytest.approx(meta.L * math.log(2))


def test_compression_curve_on_soft_clipped_rungs():
    assert compression_curve((
        (-30.0, (-40.0, -42.0)),
        (-27.0, (-37.5, -40.0)),
        (-24.0, (-35.5, -38.0)),
    )) == (
        (0.0, 0.0),
        (-0.5, -1.0),
        (-1.5, -2.0),
    )


def test_tracking_error_is_level_offset_invariant():
    freqs = np.geomspace(10.0, 500.0, 200)
    predicted = -6.0 * np.log2(100.0 / freqs)
    ripple = 0.5 * np.sin(np.log(freqs))
    base = tracking_error_db(freqs, predicted + ripple, predicted, (20.0, 200.0))
    shifted = tracking_error_db(freqs, predicted + ripple + 17.0, predicted, (20.0, 200.0))
    assert shifted == pytest.approx(base)


# --------------------------------------------------------------------------- #
# notch-excluded VERIFY tracking (W6.7 ruling 1)
# --------------------------------------------------------------------------- #
#
# The W6 run-7 hardware VERIFY failed on a raw max-tracking error of 27.83 dB
# against a predicted sum whose own ripple was ~30 dB (a deep interference
# notch) — the comparator was comparing cancellation DEPTHS inside that
# notch, which is hypersensitive to sub-dB/sub-degree branch differences and
# not a meaningful tracking signal. These fixtures pin the fix: excluding
# bins where the PREDICTED sum sits deep below its own band median makes the
# gate pass when only the notch disagrees, while a genuine broadband
# divergence still fails either way.


def _synthetic_notch_db(freqs: np.ndarray, *, center_hz: float, depth_db: float,
                         half_width_octave: float) -> np.ndarray:
    """A smooth Gaussian-in-log-frequency notch, deepest at ``center_hz``."""
    log_ratio = np.log2(freqs / center_hz)
    return -depth_db * np.exp(-0.5 * (log_ratio / half_width_octave) ** 2)


def test_notch_excluded_tracking_passes_when_only_the_notch_disagrees():
    """Real destructive cancellation that deep is hypersensitive to phase, so
    the ACTUAL measured null lands at a different depth/position than the
    idealized prediction — but everywhere OUTSIDE the predicted notch's own
    -12 dB-below-median interior, measured tracks predicted closely. The OLD
    (raw) max fails hard on the notch-interior mismatch; the NEW
    notch-excluded max passes because that mismatch is entirely inside the
    excluded interior."""
    freqs = np.geomspace(200.0, 20000.0, 4000)
    band = (500.0, 8000.0)
    mask = (freqs >= band[0]) & (freqs <= band[1])
    ripple = 0.2 * np.sin(np.log(freqs) * 7.0)

    predicted_db = _synthetic_notch_db(
        freqs, center_hz=2000.0, depth_db=30.0, half_width_octave=0.2
    ) + ripple
    median_predicted = np.median(predicted_db[mask])
    notch_interior = predicted_db < (median_predicted - 12.0)

    # The real null: shifted center, much shallower (8 dB vs the predicted
    # 30 dB) -- entirely a different notch, confined to the excluded interior.
    alt_notch_db = _synthetic_notch_db(
        freqs, center_hz=2000.0 * (2 ** 0.08), depth_db=8.0, half_width_octave=0.2
    ) + ripple
    measurement_noise = np.random.default_rng(7).normal(0.0, 0.1, size=freqs.size)
    measured_db = np.where(notch_interior, alt_notch_db, predicted_db) + measurement_noise

    old_rms, old_max = tracking_error_db(freqs, measured_db, predicted_db, band)
    new_rms, new_max = notch_excluded_tracking_error_db(
        freqs, measured_db, predicted_db, band, notch_exclusion_db=12.0,
    )
    assert old_max > 1.5  # the OLD gate (VERIFY_TOLERANCE_DB) fails
    assert new_max <= 1.5  # the NEW gate passes: out-of-notch agreement is tight
    assert new_max < old_max


def test_notch_excluded_tracking_still_fails_on_genuine_broadband_divergence():
    """A real, non-notch divergence (a broadband shape mismatch, not just a
    uniform level offset) must still fail — notch exclusion narrows WHAT is
    compared, it does not loosen HOW MUCH divergence is tolerated."""
    freqs = np.geomspace(200.0, 20000.0, 4000)
    band = (500.0, 8000.0)
    ripple = 0.2 * np.sin(np.log(freqs) * 7.0)

    predicted_db = _synthetic_notch_db(
        freqs, center_hz=2000.0, depth_db=30.0, half_width_octave=0.2
    ) + ripple
    # A broadband shape mismatch (not a DC offset -- the metric is already
    # level-offset invariant) well outside the notch region too.
    measured_db = predicted_db + 3.5 * np.sin(np.log(freqs) * 2.3 + 0.7)

    old_rms, old_max = tracking_error_db(freqs, measured_db, predicted_db, band)
    new_rms, new_max = notch_excluded_tracking_error_db(
        freqs, measured_db, predicted_db, band, notch_exclusion_db=12.0,
    )
    assert old_max > 1.5
    assert new_max > 1.5


def test_measured_notch_at_predicted_flat_bins_still_fails():
    """W6.7 gate case A — pins the measured-side asymmetry. The exclusion key
    MUST be the PREDICTED level, never the measured one: a deep MEASURED
    notch where the prediction is FLAT is the wrong-polarity/wrong-alignment
    discriminant — real evidence the applied graph does not sum the way the
    candidate predicted — and must count in full. If the exclusion key were
    ever flipped to the measured level, this fixture's measured notch would
    be excluded and the gate would silently pass a broken apply; the failing
    assertions below are the guard against that flip."""
    freqs = np.geomspace(200.0, 20000.0, 4000)
    band = (500.0, 8000.0)
    ripple = 0.2 * np.sin(np.log(freqs) * 7.0)

    predicted_db = ripple.copy()  # flat prediction: no bin is ever excluded
    measured_db = _synthetic_notch_db(
        freqs, center_hz=2000.0, depth_db=25.0, half_width_octave=0.2
    ) + ripple

    rms, max_abs = notch_excluded_tracking_error_db(
        freqs, measured_db, predicted_db, band, notch_exclusion_db=12.0,
    )
    assert rms > 1.5  # both comparators fail: nothing was excluded
    assert max_abs > 1.5
    # And byte-identical to the raw comparator -- a flat prediction has no
    # notch interior to exclude.
    raw_rms, raw_max = tracking_error_db(freqs, measured_db, predicted_db, band)
    assert max_abs == pytest.approx(raw_max)
    assert rms == pytest.approx(raw_rms)


def test_full_interference_inversion_still_fails():
    """W6.7 gate case B — a polarity inversion of the applied sum. Two-tap
    comb model: the predicted (in-phase) sum nulls at k·2000 Hz; the
    inverted-polarity measured sum fills those nulls and nulls instead at
    the half-period offsets (1000/3000/5000 Hz…) where the prediction is
    FLAT. Exclusion (keyed on the prediction) removes only the PREDICTED
    null interiors; the measured nulls at predicted-flat bins remain fully
    counted and fail the gate by a wide margin."""
    freqs = np.geomspace(200.0, 20000.0, 4000)
    band = (500.0, 8000.0)
    tau = 1.0 / 2000.0
    predicted_db = 20.0 * np.log10(
        np.maximum(2.0 * np.abs(np.sin(np.pi * freqs * tau)), 1e-6)
    )
    measured_db = 20.0 * np.log10(
        np.maximum(2.0 * np.abs(np.cos(np.pi * freqs * tau)), 1e-6)
    )
    rms, max_abs = notch_excluded_tracking_error_db(
        freqs, measured_db, predicted_db, band, notch_exclusion_db=12.0,
    )
    assert rms > 1.5
    assert max_abs > 1.5
