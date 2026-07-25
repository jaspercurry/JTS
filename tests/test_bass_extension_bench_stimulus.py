# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R6 — runner-owned lead-in/lead-out silence padding.

Pins the lead-in (10x slowest IIR time constant, or the longest Conv impulse
if longer) and lead-out (owner-path group delay, or the longest Conv impulse
if longer) minima, and the padded-WAV assembly itself.
"""

from __future__ import annotations

import io
import math
import wave
from pathlib import Path

import pytest

from jasper.bass_extension.bench import stimulus


def _write_wav(path: Path, *, frames: int, channels: int = 1, sample_rate: int = 48000) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        # A simple non-zero pattern so the body is distinguishable from silence.
        out.writeframes(bytes([1, 0] * channels) * frames)


# --------------------------------------------------------------------------- #
# Padding minima — IIR time constant, Conv impulse length, Delay seconds
# --------------------------------------------------------------------------- #


def test_lead_in_uses_ten_time_constants_of_the_slowest_biquad() -> None:
    filters = [
        {
            "type": "Biquad",
            "parameters": {"type": "LinkwitzTransform", "freq_act": 40.0, "freq_target": 40.0},
        }
    ]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=48000)
    expected_tc_s = 1.0 / (2.0 * math.pi * 40.0)
    expected_samples = math.ceil(10.0 * expected_tc_s * 48000)
    assert minima.lead_in_time_constant_component_samples == expected_samples
    assert minima.lead_in_samples == expected_samples
    assert minima.lead_in_conv_component_samples == 0


def test_lead_in_takes_the_lowest_frequency_across_multiple_biquads() -> None:
    filters = [
        {"type": "Biquad", "parameters": {"type": "Highpass", "freq": 200.0}},
        {"type": "BiquadCombo", "parameters": {"type": "ButterworthHighpass", "freq": 30.0}},
    ]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=48000)
    expected_tc_s = 1.0 / (2.0 * math.pi * 30.0)  # the LOWER of 200/30 dominates
    assert minima.lead_in_time_constant_component_samples == math.ceil(
        10.0 * expected_tc_s * 48000
    )


def test_biquad_with_no_recognized_frequency_key_refuses() -> None:
    filters = [{"type": "Biquad", "parameters": {"a1": 0.5, "a2": 0.1, "b0": 1.0}}]
    with pytest.raises(stimulus.StimulusAssemblyError):
        stimulus.compute_padding_minima(filters, sample_rate_hz=48000)


def test_conv_values_length_can_exceed_the_time_constant_component() -> None:
    filters = [
        {"type": "Biquad", "parameters": {"type": "Highpass", "freq": 5000.0}},  # tiny TC
        {"type": "Conv", "parameters": {"type": "Values", "values": [0.0] * 5000}},
    ]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=48000)
    assert minima.lead_in_conv_component_samples == 5000
    assert minima.lead_in_samples == 5000  # Conv dominates over the tiny time constant


def test_conv_dummy_length() -> None:
    filters = [{"type": "Conv", "parameters": {"type": "Dummy", "length": 256}}]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=48000)
    assert minima.lead_in_conv_component_samples == 256
    assert minima.lead_out_conv_component_samples == 256


def test_conv_values_missing_refuses() -> None:
    filters = [{"type": "Conv", "parameters": {"type": "Values"}}]
    with pytest.raises(stimulus.StimulusAssemblyError):
        stimulus.compute_padding_minima(filters, sample_rate_hz=48000)


def test_conv_unrecognized_type_refuses() -> None:
    filters = [{"type": "Conv", "parameters": {"type": "Mystery"}}]
    with pytest.raises(stimulus.StimulusAssemblyError):
        stimulus.compute_padding_minima(filters, sample_rate_hz=48000)


@pytest.mark.parametrize(
    "unit,delay,sample_rate,expected_s",
    [
        ("ms", 5.0, 48000, 0.005),
        ("us", 500.0, 48000, 0.0005),
        ("samples", 480.0, 48000, 0.01),
        ("mm", 343.0, 48000, 1.0 / 1000.0),  # 343 mm / speed_of_sound(343 m/s)/1000
    ],
)
def test_delay_seconds_every_time_unit(
    unit: str, delay: float, sample_rate: int, expected_s: float
) -> None:
    filters = [{"type": "Delay", "parameters": {"delay": delay, "unit": unit}}]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=sample_rate)
    expected_samples = math.ceil(expected_s * sample_rate)
    assert minima.lead_out_delay_component_samples == expected_samples


def test_delay_seconds_defaults_to_milliseconds_when_unit_absent() -> None:
    filters = [{"type": "Delay", "parameters": {"delay": 10.0}}]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=48000)
    assert minima.lead_out_delay_component_samples == math.ceil(0.01 * 48000)


def test_delay_seconds_sum_across_multiple_stages() -> None:
    filters = [
        {"type": "Delay", "parameters": {"delay": 5.0, "unit": "ms"}},
        {"type": "Delay", "parameters": {"delay": 3.0, "unit": "ms"}},
    ]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=48000)
    assert minima.lead_out_delay_component_samples == math.ceil(0.008 * 48000)


def test_delay_unrecognized_unit_refuses() -> None:
    filters = [{"type": "Delay", "parameters": {"delay": 1.0, "unit": "furlongs"}}]
    with pytest.raises(stimulus.StimulusAssemblyError):
        stimulus.compute_padding_minima(filters, sample_rate_hz=48000)


def test_gain_and_limiter_contribute_nothing() -> None:
    filters = [
        {"type": "Gain", "parameters": {"gain": -6.0}},
        {"type": "Limiter", "parameters": {"clip_limit": -1.0, "soft_clip": True}},
    ]
    minima = stimulus.compute_padding_minima(filters, sample_rate_hz=48000)
    assert minima.lead_in_samples == 0
    assert minima.lead_out_samples == 0


def test_sample_rate_must_be_positive() -> None:
    with pytest.raises(stimulus.StimulusAssemblyError):
        stimulus.compute_padding_minima([], sample_rate_hz=0)


# --------------------------------------------------------------------------- #
# WAV padding assembly
# --------------------------------------------------------------------------- #


def test_pad_stimulus_wav_prepends_and_appends_silence(tmp_path: Path) -> None:
    src = tmp_path / "gen.wav"
    _write_wav(src, frames=100, channels=1, sample_rate=48000)
    minima = stimulus.PaddingMinima(
        lead_in_samples=10,
        lead_out_samples=20,
        lead_in_time_constant_component_samples=10,
        lead_in_conv_component_samples=0,
        lead_out_delay_component_samples=20,
        lead_out_conv_component_samples=0,
    )
    padded = stimulus.pad_stimulus_wav(src, minima=minima)
    assert padded.lead_in_frames == 10
    assert padded.lead_out_frames == 20
    assert padded.body_frames == 100
    assert padded.channels == 1
    assert padded.sample_rate_hz == 48000
    assert padded.sample_width_bytes == 2

    with wave.open(io.BytesIO(padded.wav_bytes), "rb") as check:
        assert check.getnframes() == 10 + 100 + 20
        data = check.readframes(check.getnframes())
        lead_in_bytes = data[: 10 * 2]
        lead_out_bytes = data[(10 + 100) * 2 :]
        assert lead_in_bytes == b"\x00" * (10 * 2)
        assert lead_out_bytes == b"\x00" * (20 * 2)


def test_pad_stimulus_wav_preserves_stereo_frame_geometry(tmp_path: Path) -> None:
    src = tmp_path / "gen.wav"
    _write_wav(src, frames=50, channels=2, sample_rate=44100)
    minima = stimulus.PaddingMinima(5, 5, 5, 0, 5, 0)
    padded = stimulus.pad_stimulus_wav(src, minima=minima)
    assert padded.channels == 2
    assert padded.sample_rate_hz == 44100
    assert len(padded.wav_bytes) > 0


def test_padding_minima_to_receipt_shape() -> None:
    minima = stimulus.PaddingMinima(1, 2, 1, 0, 2, 0)
    receipt = minima.to_receipt()
    assert receipt == {
        "lead_in_samples": 1,
        "lead_out_samples": 2,
        "lead_in_time_constant_component_samples": 1,
        "lead_in_conv_component_samples": 0,
        "lead_out_delay_component_samples": 2,
        "lead_out_conv_component_samples": 0,
    }
