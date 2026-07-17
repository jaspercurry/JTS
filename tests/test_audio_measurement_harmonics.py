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
