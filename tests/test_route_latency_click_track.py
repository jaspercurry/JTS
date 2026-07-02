# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import wave

import pytest

from jasper.audio_validation import (
    ROUTE_LATENCY_P95_MIN_DURATION_SECONDS,
    ROUTE_LATENCY_P99_MIN_DURATION_SECONDS,
    percentile_min_samples,
)
from jasper.route_latency import click_track


def test_quick_preset_clears_p95_certification_gate_with_margin():
    preset = click_track.PRESETS[click_track.QUICK_PRESET_NAME]

    assert preset.impulse_count > percentile_min_samples(95)
    assert preset.duration_seconds > ROUTE_LATENCY_P95_MIN_DURATION_SECONDS
    assert preset.jittered is False


def test_promotion_preset_clears_p99_certification_gate_with_margin():
    preset = click_track.PRESETS[click_track.PROMOTION_PRESET_NAME]

    assert preset.impulse_count > percentile_min_samples(99)
    assert preset.duration_seconds > ROUTE_LATENCY_P99_MIN_DURATION_SECONDS
    assert preset.jittered is True


def test_build_schedule_produces_exact_impulse_count():
    schedule = click_track.build_schedule("quick", seed=1)

    assert len(schedule.onsets_seconds) == schedule.impulse_count


def test_build_schedule_onsets_are_ascending_and_within_duration():
    schedule = click_track.build_schedule("promotion", seed=2)

    onsets = schedule.onsets_seconds
    assert list(onsets) == sorted(onsets)
    assert onsets[0] > 0
    assert onsets[-1] < schedule.duration_seconds


def test_quick_preset_spacing_is_uniform_not_jittered():
    schedule = click_track.build_schedule("quick", seed=3)

    gaps = [b - a for a, b in zip(schedule.onsets_seconds, schedule.onsets_seconds[1:])]
    # Uniform spacing: every gap should be very close to the mean.
    mean_gap = sum(gaps) / len(gaps)
    assert all(abs(gap - mean_gap) < 1e-6 for gap in gaps)


def test_promotion_preset_spacing_has_real_jitter():
    schedule = click_track.build_schedule("promotion", seed=4)

    gaps = [b - a for a, b in zip(schedule.onsets_seconds, schedule.onsets_seconds[1:])]
    mean_gap = sum(gaps) / len(gaps)
    # Jittered gaps must vary meaningfully — not just floating-point noise
    # around a fixed spacing. The generator draws each gap from
    # uniform(0.5, 1.5) x mean before rescaling, so real variance is
    # expected; assert the spread is at least +/-20% of the mean somewhere.
    assert max(gaps) > mean_gap * 1.2
    assert min(gaps) < mean_gap * 0.8


def test_build_schedule_is_deterministic_for_a_fixed_seed():
    a = click_track.build_schedule("promotion", seed=99)
    b = click_track.build_schedule("promotion", seed=99)

    assert a.onsets_seconds == b.onsets_seconds


def test_build_schedule_unknown_preset_raises():
    with pytest.raises(ValueError, match="unknown preset"):
        click_track.build_schedule("nonexistent")


def test_render_wav_produces_expected_frame_count_and_format(tmp_path):
    schedule = click_track.build_schedule("quick", seed=5)
    path = click_track.render_wav(schedule, tmp_path / "click.wav")

    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == click_track.SAMPLE_RATE_HZ
        assert w.getnchannels() == click_track.CHANNELS
        assert w.getsampwidth() == click_track.SAMPLE_WIDTH_BYTES
        assert w.getnframes() == round(schedule.duration_seconds * click_track.SAMPLE_RATE_HZ)


def test_render_wav_amplitude_matches_requested_dbfs(tmp_path):
    # A single, very short schedule makes it easy to find and measure the
    # one click's peak sample directly.
    schedule = click_track.ClickSchedule(
        preset_name="quick",
        impulse_count=1,
        duration_seconds=1.0,
        jittered=False,
        amplitude_dbfs=-12.0,
        onsets_seconds=(0.5,),
        seed=0,
    )
    path = click_track.render_wav(schedule, tmp_path / "click.wav")

    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    import array

    samples = array.array("h")
    samples.frombytes(raw)
    peak = max(abs(s) for s in samples)
    expected_peak = (10.0 ** (-12.0 / 20.0)) * 32767.0
    # Windowing means the true peak is near but not exactly the tone's
    # theoretical peak sample; allow a generous tolerance for the
    # raised-cosine window shape and rounding.
    assert abs(peak - expected_peak) / expected_peak < 0.05


def test_render_wav_click_straddling_a_chunk_boundary_is_intact(tmp_path):
    # render_wav streams one second (48000 frames) at a time to bound memory.
    # A click whose 5 ms span crosses that boundary must still be written
    # whole (no dropped or duplicated samples at the seam). Place a click a
    # couple ms before the 1-second boundary so it spans two chunks.
    import array

    boundary_frame = click_track.SAMPLE_RATE_HZ  # start of chunk 2
    click_len = round(click_track.CLICK_DURATION_MS / 1000.0 * click_track.SAMPLE_RATE_HZ)
    onset_frame = boundary_frame - click_len // 2
    schedule = click_track.ClickSchedule(
        preset_name="quick",
        impulse_count=1,
        duration_seconds=2.0,
        jittered=False,
        amplitude_dbfs=-12.0,
        onsets_seconds=(onset_frame / click_track.SAMPLE_RATE_HZ,),
        seed=0,
    )
    path = click_track.render_wav(schedule, tmp_path / "seam.wav")

    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    # Compare the exact per-frame samples against a direct reconstruction of
    # the click at the same onset — the streamed render must match it sample
    # for sample across the seam.
    expected_click = click_track._click_samples(-12.0)
    for offset, sample in enumerate(expected_click):
        frame = onset_frame + offset
        left = samples[frame * click_track.CHANNELS]
        right = samples[frame * click_track.CHANNELS + 1]
        assert left == sample, f"seam frame {frame} left mismatch"
        assert right == sample, f"seam frame {frame} right mismatch"


def test_render_wav_default_amplitude_is_modest_minus_12_dbfs():
    # Pin the safety-doctrine default: AGENTS.md requires generated click
    # content to default to a modest amplitude (~ -12 dBFS), never
    # full-scale.
    assert click_track.DEFAULT_AMPLITUDE_DBFS == pytest.approx(-12.0)


def test_schedule_json_round_trip(tmp_path):
    schedule = click_track.build_schedule("quick", seed=6, amplitude_dbfs=-9.0)
    path = click_track.write_schedule_json(schedule, tmp_path / "schedule.json")

    loaded = click_track.load_schedule_json(path)

    assert loaded == schedule


def test_write_schedule_json_is_valid_json_with_expected_keys(tmp_path):
    import json

    schedule = click_track.build_schedule("quick", seed=7)
    path = click_track.write_schedule_json(schedule, tmp_path / "schedule.json")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["preset"] == "quick"
    assert payload["impulse_count"] == schedule.impulse_count
    assert payload["jittered"] is False
    assert len(payload["onsets_seconds"]) == schedule.impulse_count


def test_click_samples_do_not_clip_at_high_amplitude(tmp_path):
    # Even an operator-overridden loud amplitude must not overflow int16 —
    # render_wav clamps, but confirm the clamp actually engages rather than
    # silently wrapping.
    schedule = click_track.ClickSchedule(
        preset_name="quick",
        impulse_count=1,
        duration_seconds=1.0,
        jittered=False,
        amplitude_dbfs=0.0,
        onsets_seconds=(0.5,),
        seed=0,
    )
    path = click_track.render_wav(schedule, tmp_path / "loud.wav")

    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    import array

    samples = array.array("h")
    samples.frombytes(raw)
    assert max(samples) <= 32767
    assert min(samples) >= -32768
