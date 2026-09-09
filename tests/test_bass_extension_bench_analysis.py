# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign verdict math is real — composed from the existing kernels."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from jasper.audio_measurement.playback import ensure_bandlimited_noise_wav
from jasper.audio_measurement.program import (
    KIND_SWEEP,
    PROGRAM_PHASE_MEASURE,
    ProgramSegment,
    finalize_program,
)
from jasper.audio_measurement.sweep import synchronized_swept_sine
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


# --------------------------------------------------------------------------- #
# One acoustic capture read into the frozen verdicts
# --------------------------------------------------------------------------- #

RATE = 48_000
SWEEP_BAND = (100.0, 400.0)
HOLD_BAND = (30.0, 200.0)
POLICY = analysis.MeasurementPolicy(min_snr_db=25.0, max_tracking_rms_db=1.0)

# A short decaying room response: direct arrival plus two reflections.
_ROOM_IR = np.zeros(256)
_ROOM_IR[0], _ROOM_IR[40], _ROOM_IR[120] = 1.0, 0.4, -0.15


def _sweep_program():
    """A one-segment MEASURE program describing the sweep body below."""
    sweep, _meta = synchronized_swept_sine(
        f1=SWEEP_BAND[0],
        f2=SWEEP_BAND[1],
        duration_approx_s=1.0,
        sample_rate=RATE,
        amplitude_dbfs=-6.0,
    )
    body = np.asarray(sweep, dtype=np.float64)
    segment = ProgramSegment(
        segment_id="bench_woofer_0",
        kind=KIND_SWEEP,
        role="woofer",
        channel=0,
        start_sample=0,
        n_samples=body.size,
        f1_hz=SWEEP_BAND[0],
        f2_hz=SWEEP_BAND[1],
        gain_db=-6.0,
        effective_peak_dbfs=-6.0,
    )
    return finalize_program(PROGRAM_PHASE_MEASURE, 1, [segment], body.size), body


def _sweep_capture(body, *, pre_roll_s, second_order):
    """silence + (body through a short IR, plus an H2 term) + tail + a noise floor."""
    played = np.convolve(body, _ROOM_IR)
    played = played + second_order * played**2
    capture = np.concatenate(
        [
            np.zeros(int(round(pre_roll_s * RATE))),
            played,
            np.zeros(int(0.7 * RATE)),
        ]
    )
    return capture + np.random.default_rng(7).normal(0.0, 1e-5, capture.size)


def _clean_pre_roll_s(program) -> float:
    return analysis.sweep_pre_roll_s(program.segment("bench_woofer_0"))


def _analyze_sweep(program, body, capture, *, policy=POLICY):
    return analysis.analyze_sweep_capture(
        capture=capture,
        program=program,
        segment_id="bench_woofer_0",
        stimulus_body=body,
        band=SWEEP_BAND,
        margin=MARGIN,
        policy=policy,
    )


@pytest.mark.parametrize("lag", [0, 1_337, 40_000])
def test_stimulus_lag_recovers_a_known_offset(lag: int) -> None:
    rng = np.random.default_rng(19)
    burst = rng.normal(0.0, 0.2, 4_096)
    capture = np.concatenate([np.zeros(lag), burst, np.zeros(9_000)])
    capture = capture + rng.normal(0.0, 1e-4, capture.size)

    assert (
        analysis.stimulus_lag_samples(capture, burst, sample_rate_hz=RATE) == lag
    )


def test_stimulus_lag_refuses_a_capture_shorter_than_the_stimulus() -> None:
    with pytest.raises(analysis.CaptureUnanalyzable):
        analysis.stimulus_lag_samples(
            np.zeros(100), np.zeros(200), sample_rate_hz=RATE
        )


@pytest.mark.parametrize(
    ("second_order", "expected"), [(0.02, "pass"), (0.1, "fail"), (0.3, "fail")]
)
def test_sweep_protection_gates_on_the_margin_thd_ratio(
    second_order: float, expected: str
) -> None:
    program, body = _sweep_program()
    capture = _sweep_capture(
        body,
        pre_roll_s=_clean_pre_roll_s(program),
        second_order=second_order,
    )
    result = _analyze_sweep(program, body, capture)

    assert result.images_clean is True
    assert result.protection_verdict == expected
    assert (result.thd_max_ratio > MARGIN.thd_fail_ratio) is (expected == "fail")


def test_sweep_protection_fails_a_reading_the_floor_owns_outright() -> None:
    """A linear capture's harmonics ARE the noise floor, so its THD says
    nothing about the driver: unproven fails closed, like an unclean image."""

    program, body = _sweep_program()
    capture = _sweep_capture(
        body, pre_roll_s=_clean_pre_roll_s(program), second_order=0.0
    )
    result = _analyze_sweep(program, body, capture)

    assert result.images_clean is True
    assert result.thd_max_ratio == 0.0
    assert result.protection_verdict == "fail"


class _Reading:
    """The two surfaces :func:`proven_thd_max_ratio` reads off a reading."""

    orders = (2, 3)

    def __init__(self, thd_percent, floor_limited) -> None:
        self.thd_percent = np.asarray(thd_percent, dtype=np.float64)
        self._floor_limited = {
            order: np.asarray(mask, dtype=bool)
            for order, mask in floor_limited.items()
        }

    def floor_limited(self, order: int) -> np.ndarray:
        return self._floor_limited[order]


def test_proven_thd_max_ratio_skips_the_points_the_floor_owns() -> None:
    thd = [1.0, 9.0, 2.0]
    clear = [False, False, False]

    # The one high point is the measurement's, not the driver's.
    assert analysis.proven_thd_max_ratio(
        _Reading(thd, {2: [False, True, False], 3: clear})
    ) == pytest.approx(0.02)
    # Every point floor-limited by ANY order: nothing was proved.
    assert analysis.proven_thd_max_ratio(
        _Reading(thd, {2: [True, True, False], 3: [False, False, True]})
    ) is None
    assert analysis.proven_thd_max_ratio(
        _Reading(thd, {2: clear, 3: clear})
    ) == pytest.approx(0.09)


@pytest.mark.parametrize(("min_snr_db", "expected"), [(25.0, "pass"), (400.0, "fail")])
def test_sweep_quality_flips_with_the_policy_snr_floor(
    min_snr_db: float, expected: str
) -> None:
    program, body = _sweep_program()
    capture = _sweep_capture(
        body, pre_roll_s=_clean_pre_roll_s(program), second_order=0.02
    )
    policy = analysis.MeasurementPolicy(
        min_snr_db=min_snr_db, max_tracking_rms_db=1.0
    )
    result = _analyze_sweep(program, body, capture, policy=policy)

    assert result.quality_verdict == expected
    assert result.signal_dict()["min_snr_db"] == min_snr_db


def test_sweep_refuses_a_capture_placed_inside_the_harmonic_pre_guard() -> None:
    program, body = _sweep_program()
    capture = _sweep_capture(body, pre_roll_s=0.2, second_order=0.02)

    with pytest.raises(analysis.CaptureUnanalyzable):
        _analyze_sweep(program, body, capture)


def _hold(tmp_path: Path, seconds: float = 8.0) -> np.ndarray:
    """The REAL stimulus body: ``ensure_bandlimited_noise_wav``'s bytes.

    The generator the executor plays, not a stand-in — a noise realization's
    own bin-to-bin structure is exactly what the sustain reading has to divide
    out, so a synthetic tone stack would not exercise it.
    """
    path = ensure_bandlimited_noise_wav(
        f_lo_hz=HOLD_BAND[0],
        f_hi_hz=HOLD_BAND[1],
        duration_s=seconds,
        dbfs=-12.0,
        sample_rate=RATE,
        cache_dir=tmp_path,
    )
    with wave.open(str(path), "rb") as source:
        frames = source.readframes(source.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32767.0


def _highpass(samples: np.ndarray, corner_hz: float) -> np.ndarray:
    """A time-invariant 2nd-order high-pass: a plant that never changes."""
    return sosfilt(
        butter(2, corner_hz / (RATE / 2.0), btype="highpass", output="sos"), samples
    )


def _sustain(played: np.ndarray, body: np.ndarray):
    capture = np.concatenate(
        [np.zeros(int(0.8 * RATE)), played, np.zeros(int(0.5 * RATE))]
    )
    capture = capture + np.random.default_rng(5).normal(0.0, 1e-5, capture.size)
    return analysis.analyze_sustain_capture(
        capture=capture,
        sample_rate_hz=RATE,
        stimulus_body=body,
        band=HOLD_BAND,
        margin=MARGIN,
        policy=POLICY,
    )


def test_sustain_reads_the_plant_not_the_noise_realization(tmp_path: Path) -> None:
    """A hold played through an UNCHANGING plant sags nowhere and moves no
    corner, however differently the two edges of the noise happen to fall."""

    body = _hold(tmp_path)
    result = _sustain(_highpass(body, 40.0), body)

    assert abs(result.sag_db) < 0.2
    assert result.fc_shift_pct < 1.0
    assert result.protection_verdict == "pass"
    assert result.quality_verdict == "pass"


def test_sustain_fails_a_real_sag_and_corner_shift(tmp_path: Path) -> None:
    body = _hold(tmp_path)
    half = body.size // 2
    lost = _highpass(body, 48.0) * 10 ** (-3.0 / 20.0)
    played = np.concatenate([_highpass(body, 40.0)[:half], lost[half:]])

    result = _sustain(played, body)

    assert result.sag_db == pytest.approx(3.0, abs=0.5)
    assert result.fc_shift_pct == pytest.approx(20.0, rel=0.25)
    assert result.protection_verdict == "fail"


@pytest.mark.parametrize("corner", [40.0, 60.0, 90.0])
def test_corner_hz_finds_a_known_six_db_corner(corner: float) -> None:
    n = RATE * 2
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    # |H| = 1 / sqrt(1 + (10**0.6 - 1) * (corner/f)**8): exactly -6 dB at f=corner.
    ratio = corner / np.maximum(freqs, 1e-9)
    magnitude = 1.0 / np.sqrt(1.0 + (10.0 ** (6.0 / 10.0) - 1.0) * ratio**8)
    phase = np.random.default_rng(11).uniform(0.0, 2 * np.pi, freqs.size)
    spectrum = magnitude * np.exp(1j * phase)
    spectrum[0] = 0.0
    # The reference carries the SAME realization at unit magnitude, so the
    # ratio spectrum is the transfer alone.
    stimulus = np.fft.irfft(np.exp(1j * phase) * (freqs > 0.0), n)

    measured = analysis.corner_hz(
        np.fft.irfft(spectrum, n),
        reference=stimulus,
        sample_rate_hz=RATE,
        band=(20.0, 400.0),
    )
    assert measured / corner == pytest.approx(1.0, abs=0.05)
