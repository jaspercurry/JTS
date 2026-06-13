"""Stereo-pair acoustic sync measurement primitives.

This module is intentionally independent of the web wizard and Snapcast
RPC. It owns deterministic marker generation, correlation-based arrival
estimation, and the positive-only delay recommendation that a leader can
later apply through CamillaDSP.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

SAMPLE_RATE = 48_000
CHANNELS = ("left", "right")
MARKER_DURATION_S = 0.080
LEFT_MARKER_OFFSET_S = 0.500
RIGHT_MARKER_OFFSET_S = 1.500
TOTAL_DURATION_S = 2.250
MIN_CONFIDENCE = 0.35
MAX_PLAYBACK_START_DELAY_S = 1.400
PAIR_SEARCH_CANDIDATES = 8


@dataclass(frozen=True)
class SyncMeasurement:
    left_arrival_s: float
    right_arrival_s: float
    delta_ms: float
    confidence: float
    ok: bool
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "left_arrival_s": round(self.left_arrival_s, 6),
            "right_arrival_s": round(self.right_arrival_s, 6),
            "delta_ms": round(self.delta_ms, 3),
            "confidence": round(self.confidence, 3),
            "ok": self.ok,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DelayRecommendation:
    left_delay_ms: float
    right_delay_ms: float

    def to_dict(self) -> dict:
        return {
            "left_delay_ms": round(self.left_delay_ms, 3),
            "right_delay_ms": round(self.right_delay_ms, 3),
        }


def marker_wave(sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Return the mono marker waveform used for both channels."""
    n = int(round(MARKER_DURATION_S * sample_rate))
    t = np.arange(n, dtype=np.float64) / sample_rate
    f0 = 700.0
    f1 = 3200.0
    k = (f1 - f0) / MARKER_DURATION_S
    phase = 2.0 * np.pi * (f0 * t + 0.5 * k * t * t)
    marker = np.sin(phase)
    marker *= np.hanning(n)
    peak = float(np.max(np.abs(marker)))
    if peak <= 0.0:
        raise ValueError("degenerate marker")
    return marker / peak


def render_marker_stereo(sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Stereo float64 marker track: left marker, then right marker."""
    marker = marker_wave(sample_rate)
    total = int(round(TOTAL_DURATION_S * sample_rate))
    stereo = np.zeros((total, 2), dtype=np.float64)
    for channel, offset in (("left", LEFT_MARKER_OFFSET_S),
                            ("right", RIGHT_MARKER_OFFSET_S)):
        start = int(round(offset * sample_rate))
        stereo[start:start + marker.size, CHANNELS.index(channel)] = marker
    return stereo


def write_marker_wav(path: str | Path, sample_rate: int = SAMPLE_RATE) -> None:
    """Write the stereo marker track as 16-bit PCM WAV."""
    stereo = render_marker_stereo(sample_rate)
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm.tobytes())


def marker_wav_bytes(sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    stereo = render_marker_stereo(sample_rate)
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(buf, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm.tobytes())
    return buf.getvalue()


def read_wav_mono(data: bytes) -> tuple[np.ndarray, int]:
    """Decode 16-bit/32-bit integer WAV bytes to mono float64 samples."""
    with wave.open(io.BytesIO(data), "rb") as f:
        channels = f.getnchannels()
        width = f.getsampwidth()
        sample_rate = f.getframerate()
        raw = f.readframes(f.getnframes())
    if channels < 1:
        raise ValueError("WAV has no channels")
    if width == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 4:
        arr = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV sample width: {width}")
    arr = arr.reshape((-1, channels))
    return np.mean(arr, axis=1), sample_rate


def _best_peak(
    corr: np.ndarray, *, start: int, stop: int,
) -> tuple[int, float]:
    start = max(0, start)
    stop = min(corr.size, stop)
    if stop <= start:
        raise ValueError("empty correlation search window")
    window = corr[start:stop]
    local = int(np.argmax(window))
    idx = start + local
    return idx, float(window[local])


def _correlate_abs_window(
    x: np.ndarray,
    marker: np.ndarray,
    *,
    start: int,
    stop: int,
) -> tuple[int, np.ndarray]:
    """Return absolute correlation for the bounded valid-index window.

    ``start``/``stop`` are indices in the full ``valid`` correlation
    space, not sample slice bounds. The segment slice includes the marker
    tail needed for ``mode="valid"`` to produce exactly ``stop - start``
    correlation points.
    """
    valid_len = x.size - marker.size + 1
    start = max(0, min(start, valid_len))
    stop = max(0, min(stop, valid_len))
    if stop <= start:
        raise ValueError("empty correlation search window")
    segment = x[start:stop + marker.size - 1]
    return start, np.abs(np.correlate(segment, marker, mode="valid"))


def _peak_candidates(
    corr: np.ndarray,
    *,
    count: int = PAIR_SEARCH_CANDIDATES,
    min_distance: int = 1,
) -> list[tuple[int, float]]:
    """Return separated peak candidates ordered by descending amplitude."""
    if corr.size == 0:
        return []
    order = np.argsort(corr)[::-1]
    out: list[tuple[int, float]] = []
    for raw_idx in order:
        idx = int(raw_idx)
        if any(abs(idx - prev) < min_distance for prev, _ in out):
            continue
        out.append((idx, float(corr[idx])))
        if len(out) >= count:
            break
    return out


def _find_marker_pair(
    x: np.ndarray,
    marker: np.ndarray,
    sample_rate: int,
    *,
    search_radius_s: float,
    playback_start_search_s: float,
) -> tuple[int, float, int, float, float]:
    """Find a left/right marker pair by known spacing, not wall-clock offsets.

    The browser begins capture before it POSTs ``/sync/play``. Aplay
    startup and Snapcast scheduling can therefore shift both markers later
    than their nominal 0.5 s / 1.5 s offsets. Search for plausible left
    peaks across that shared startup delay, then search a bounded right
    window around the expected pair spacing for each candidate.
    """
    valid_len = x.size - marker.size + 1
    if valid_len <= 0:
        raise ValueError("capture is shorter than marker")

    radius = int(round(search_radius_s * sample_rate))
    expected_spacing = int(round(
        (RIGHT_MARKER_OFFSET_S - LEFT_MARKER_OFFSET_S) * sample_rate
    ))
    left_start = int(round((LEFT_MARKER_OFFSET_S - search_radius_s) * sample_rate))
    left_stop = int(round(
        (
            LEFT_MARKER_OFFSET_S
            + playback_start_search_s
            + search_radius_s
        ) * sample_rate
    ))
    # Do not accept a left candidate so late that there is no bounded
    # right-marker search window left in the upload.
    left_stop = min(left_stop, valid_len - expected_spacing + radius)
    left_base, left_corr = _correlate_abs_window(
        x, marker, start=left_start, stop=left_stop,
    )

    best: tuple[float, int, float, int, float, np.ndarray] | None = None
    min_distance = max(1, marker.size // 2)
    for local_left, left_peak in _peak_candidates(
        left_corr, min_distance=min_distance,
    ):
        left_idx = left_base + local_left
        right_base, right_corr = _correlate_abs_window(
            x,
            marker,
            start=left_idx + expected_spacing - radius,
            stop=left_idx + expected_spacing + radius,
        )
        local_right, right_peak = _best_peak(
            right_corr, start=0, stop=right_corr.size,
        )
        right_idx = right_base + local_right
        pair_score = min(left_peak, right_peak)
        if best is None or pair_score > best[0]:
            best = (
                pair_score,
                left_idx,
                left_peak,
                right_idx,
                right_peak,
                right_corr,
            )

    if best is None:
        raise ValueError("could not find a stereo marker pair")
    _, left_idx, left_peak, right_idx, right_peak, right_corr = best
    baseline = float(np.median(np.concatenate((left_corr, right_corr)))) + 1e-9
    return left_idx, left_peak, right_idx, right_peak, baseline


def analyze_capture(
    samples: np.ndarray,
    sample_rate: int,
    *,
    search_radius_s: float = 0.180,
    playback_start_search_s: float = MAX_PLAYBACK_START_DELAY_S,
) -> SyncMeasurement:
    """Estimate L/R acoustic arrival delta from one mono capture.

    ``delta_ms`` is ``right_arrival - left_arrival``. Positive means the
    right marker arrived later; the recommended compensation delays the
    left channel by that amount.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim != 1:
        raise ValueError("samples must be mono")
    if sample_rate <= 0 or x.size < int(1.8 * sample_rate):
        raise ValueError("capture is too short for a stereo sync marker")
    x = x - float(np.mean(x))
    marker = marker_wave(sample_rate)
    left_idx, left_peak, right_idx, right_peak, baseline = _find_marker_pair(
        x,
        marker,
        sample_rate,
        search_radius_s=search_radius_s,
        playback_start_search_s=playback_start_search_s,
    )
    confidence = min(left_peak, right_peak) / (max(left_peak, right_peak) + baseline)
    warnings: list[str] = []
    if confidence < MIN_CONFIDENCE:
        warnings.append("low_confidence")
    if left_idx >= right_idx:
        warnings.append("marker_order_inverted")

    observed_spacing_s = (right_idx - left_idx) / sample_rate
    expected_spacing_s = RIGHT_MARKER_OFFSET_S - LEFT_MARKER_OFFSET_S
    delta_ms = (observed_spacing_s - expected_spacing_s) * 1000.0
    return SyncMeasurement(
        left_arrival_s=left_idx / sample_rate,
        right_arrival_s=right_idx / sample_rate,
        delta_ms=delta_ms,
        confidence=confidence,
        ok=not warnings,
        warnings=tuple(warnings),
    )


def analyze_wav_bytes(data: bytes) -> SyncMeasurement:
    samples, sample_rate = read_wav_mono(data)
    return analyze_capture(samples, sample_rate)


def recommend_channel_delays(delta_ms: float) -> DelayRecommendation:
    """Positive-only delay recommendation from ``right - left`` arrival."""
    if delta_ms >= 0.0:
        return DelayRecommendation(left_delay_ms=delta_ms, right_delay_ms=0.0)
    return DelayRecommendation(left_delay_ms=0.0, right_delay_ms=-delta_ms)


def aggregate_measurements(items: Iterable[SyncMeasurement]) -> SyncMeasurement:
    good = [m for m in items if m.ok]
    if not good:
        raise ValueError("no valid sync measurements")
    deltas = np.array([m.delta_ms for m in good], dtype=np.float64)
    median_delta = float(np.median(deltas))
    spread = float(np.max(np.abs(deltas - median_delta))) if deltas.size else 0.0
    confidence = min(m.confidence for m in good)
    warnings = []
    if spread > 0.35:
        warnings.append("repeatability_low")
    return SyncMeasurement(
        left_arrival_s=float(np.median([m.left_arrival_s for m in good])),
        right_arrival_s=float(np.median([m.right_arrival_s for m in good])),
        delta_ms=median_delta,
        confidence=confidence,
        ok=not warnings,
        warnings=tuple(warnings),
    )
