# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign verdict math is real — composed from the existing kernels."""

from __future__ import annotations

import numpy as np

from jasper.bass_extension.bench import analysis
from jasper.bass_extension.targets import MARGINS

MARGIN = MARGINS["conservative"]
SAMPLE_RATE = 48_000


def _clean_capture(n: int = SAMPLE_RATE, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    return amplitude * np.sin(2 * np.pi * 80.0 * t)


def test_sample_peak_dbfs_matches_the_full_scale_reference() -> None:
    assert analysis.sample_peak_dbfs(np.array([0.5, -0.5])) == np.float64(
        20.0 * np.log10(0.5)
    )
    assert analysis.sample_peak_dbfs(np.zeros(16)) == -120.0


def test_digital_clamp_uses_the_margin_headroom() -> None:
    # conservative digital_margin_db = 4.0
    assert analysis.digital_clamp_passed(-5.0, MARGIN) is True
    assert analysis.digital_clamp_passed(-4.0, MARGIN) is True
    assert analysis.digital_clamp_passed(-3.0, MARGIN) is False


def test_transfer_match_requires_sha_and_size() -> None:
    assert (
        analysis.transfer_match(
            deployed_sha256="a" * 64,
            deployed_byte_size=100,
            reference_sha256="a" * 64,
            reference_byte_size=100,
        )
        == "pass"
    )
    assert (
        analysis.transfer_match(
            deployed_sha256="a" * 64,
            deployed_byte_size=100,
            reference_sha256="b" * 64,
            reference_byte_size=100,
        )
        == "fail"
    )
    assert (
        analysis.transfer_match(
            deployed_sha256="a" * 64,
            deployed_byte_size=100,
            reference_sha256="a" * 64,
            reference_byte_size=101,
        )
        == "fail"
    )


def _sweep(
    *,
    harmonic_db: float,
    compression_rungs,
    tracking_offset_db: float = 0.0,
    repeat_levels=(-20.0, -20.0),
    snr_db: float = 40.0,
) -> analysis.SweepVerdicts:
    fund_freqs = np.array([50.0, 100.0, 150.0])
    fund_db = np.array([-20.0, -20.0, -20.0])
    harmonics = {
        2: (fund_freqs * 2, np.array([harmonic_db, harmonic_db, harmonic_db])),
    }
    tracking_freqs = np.array([40.0, 80.0, 160.0])
    measured = np.array([-20.0, -20.0, -20.0])
    predicted = measured - tracking_offset_db
    return analysis.assess_sweep(
        captured=_clean_capture(),
        sample_rate=SAMPLE_RATE,
        sweep_n_samples=SAMPLE_RATE,
        has_mic_calibration=True,
        fund_freqs=fund_freqs,
        fund_db=fund_db,
        harmonics=harmonics,
        compression_rungs=compression_rungs,
        tracking_freqs=tracking_freqs,
        tracking_measured_db=measured,
        tracking_predicted_db=predicted,
        repeat_band_levels_db=repeat_levels,
        snr_db=snr_db,
        band=(20.0, 200.0),
        margin=MARGIN,
        min_snr_db=25.0,
    )


def test_sweep_passes_quality_and_protection_when_clean() -> None:
    verdicts = _sweep(
        harmonic_db=-80.0,
        compression_rungs=[(-40.0, (-40.0,)), (-30.0, (-30.0,))],
    )
    assert verdicts.quality_verdict == "pass"
    assert verdicts.protection_verdict == "pass"
    assert verdicts.thd_max < MARGIN.thd_fail_ratio


def test_sweep_fails_protection_on_compression() -> None:
    # A rung that is 3 dB below its linear extrapolation exceeds the
    # conservative 1.5 dB compression fail threshold.
    verdicts = _sweep(
        harmonic_db=-80.0,
        compression_rungs=[(-40.0, (-40.0,)), (-30.0, (-33.0,))],
    )
    assert verdicts.protection_verdict == "fail"
    assert verdicts.compression_max_db >= MARGIN.compression_fail_db


def test_sweep_fails_quality_on_repeat_spread() -> None:
    verdicts = _sweep(
        harmonic_db=-80.0,
        compression_rungs=[(-40.0, (-40.0,)), (-30.0, (-30.0,))],
        repeat_levels=(-20.0, -25.0),
    )
    assert verdicts.quality_verdict == "fail"


def test_sustain_sag_and_corner_shift_gate_protection() -> None:
    ok = analysis.assess_sustain(
        start_level_db=-20.0,
        end_level_db=-20.5,
        start_corner_hz=40.0,
        end_corner_hz=41.0,
        snr_db=40.0,
        margin=MARGIN,
        min_snr_db=25.0,
    )
    assert ok.protection_verdict == "pass"

    sagging = analysis.assess_sustain(
        start_level_db=-20.0,
        end_level_db=-22.0,  # 2 dB sag > 1.5 dB threshold
        start_corner_hz=40.0,
        end_corner_hz=40.0,
        snr_db=40.0,
        margin=MARGIN,
        min_snr_db=25.0,
    )
    assert sagging.protection_verdict == "fail"

    drifting = analysis.assess_sustain(
        start_level_db=-20.0,
        end_level_db=-20.0,
        start_corner_hz=40.0,
        end_corner_hz=44.0,  # +10% > 5% threshold
        snr_db=40.0,
        margin=MARGIN,
        min_snr_db=25.0,
    )
    assert drifting.protection_verdict == "fail"


def test_transparency_tracks_the_reference_within_the_policy() -> None:
    freqs = np.array([40.0, 80.0, 160.0])
    reference = np.array([-10.0, -10.0, -10.0])
    close = reference + 0.2
    far = reference + np.array([0.0, 3.0, -3.0])

    verdict_pass, rms_pass, _ = analysis.assess_transparency(
        freqs=freqs,
        candidate_response_db=close,
        reference_response_db=reference,
        band=(20.0, 200.0),
        max_tracking_rms_db=1.0,
    )
    assert verdict_pass == "pass"
    assert rms_pass <= 1.0

    verdict_fail, _, _ = analysis.assess_transparency(
        freqs=freqs,
        candidate_response_db=far,
        reference_response_db=reference,
        band=(20.0, 200.0),
        max_tracking_rms_db=1.0,
    )
    assert verdict_fail == "fail"
