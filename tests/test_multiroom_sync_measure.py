from __future__ import annotations

import numpy as np
import pytest

from jasper.multiroom import sync_measure


def _mono_capture(
    delta_ms: float = 0.0,
    sample_rate: int = 48_000,
    *,
    playback_start_delay_s: float = 0.0,
    extra_tail_s: float = 0.0,
) -> np.ndarray:
    """Synthetic mic capture for tests.

    ``delta_ms`` is right-arrival minus left-arrival. Positive means the
    right marker is late relative to the left marker.
    """
    marker = sync_measure.marker_wave(sample_rate)
    total = int(round(
        (
            sync_measure.TOTAL_DURATION_S
            + playback_start_delay_s
            + extra_tail_s
        ) * sample_rate
    ))
    out = np.zeros(total, dtype=np.float64)
    left = int(round(
        (
            sync_measure.LEFT_MARKER_OFFSET_S
            + playback_start_delay_s
        ) * sample_rate
    ))
    right = int(round(
        (
            sync_measure.RIGHT_MARKER_OFFSET_S
            + playback_start_delay_s
            + delta_ms / 1000.0
        ) * sample_rate
    ))
    out[left:left + marker.size] += 0.7 * marker
    out[right:right + marker.size] += 0.7 * marker
    return out


def test_analyze_capture_reports_right_minus_left_delta():
    result = sync_measure.analyze_capture(_mono_capture(delta_ms=2.25), 48_000)

    assert result.ok is True
    assert result.delta_ms == pytest.approx(2.25, abs=0.001)
    assert result.confidence > 0.9


def test_analyze_capture_tolerates_browser_recording_before_playback():
    result = sync_measure.analyze_capture(
        _mono_capture(delta_ms=2.25, playback_start_delay_s=0.72),
        48_000,
    )

    assert result.ok is True
    assert result.left_arrival_s == pytest.approx(
        sync_measure.LEFT_MARKER_OFFSET_S + 0.72,
        abs=0.001,
    )
    assert result.delta_ms == pytest.approx(2.25, abs=0.001)


def test_analyze_capture_bounds_correlation_work(monkeypatch):
    samples = _mono_capture(
        delta_ms=1.0,
        playback_start_delay_s=0.6,
        extra_tail_s=20.0,
    )
    lengths = []
    orig_correlate = sync_measure.np.correlate

    def wrapped(a, b, mode="valid"):
        lengths.append(len(a))
        return orig_correlate(a, b, mode)

    monkeypatch.setattr(sync_measure.np, "correlate", wrapped)

    result = sync_measure.analyze_capture(samples, 48_000)

    assert result.ok is True
    assert result.delta_ms == pytest.approx(1.0, abs=0.001)
    assert lengths
    assert max(lengths) < int(2.0 * 48_000)


def test_recommend_channel_delays_positive_only():
    assert sync_measure.recommend_channel_delays(3.5).to_dict() == {
        "left_delay_ms": 3.5,
        "right_delay_ms": 0.0,
    }
    assert sync_measure.recommend_channel_delays(-1.25).to_dict() == {
        "left_delay_ms": 0.0,
        "right_delay_ms": 1.25,
    }


def test_marker_wav_round_trip_analyzes():
    samples = _mono_capture(delta_ms=-1.0)
    # Write a tiny WAV through the public bytes path.
    import io
    import wave

    pcm = (samples * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(48_000)
        f.writeframes(pcm.tobytes())

    result = sync_measure.analyze_wav_bytes(buf.getvalue())

    assert result.ok is True
    assert result.delta_ms == pytest.approx(-1.0, abs=0.001)


def test_aggregate_measurements_rejects_inconsistent_repeats():
    a = sync_measure.analyze_capture(_mono_capture(delta_ms=1.0), 48_000)
    b = sync_measure.analyze_capture(_mono_capture(delta_ms=1.8), 48_000)

    combined = sync_measure.aggregate_measurements([a, b])

    assert combined.ok is False
    assert "repeatability_low" in combined.warnings
