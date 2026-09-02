# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign verdict math is real — composed from the existing kernels."""

from __future__ import annotations

import numpy as np

from jasper.bass_extension.bench import analysis
from jasper.bass_extension.targets import MARGINS

MARGIN = MARGINS["conservative"]


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
