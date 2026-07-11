#!/usr/bin/env python3
"""Analyze a synchronized correction diagnostic bundle."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy.io import wavfile
from scipy.signal import periodogram, stft


def dbfs(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else -120.0


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def finite_json(value: object) -> object:
    """Replace non-finite diagnostics with null before strict JSON output."""

    if isinstance(value, dict):
        return {str(key): finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite_json(item) for item in value]
    if isinstance(value, tuple):
        return [finite_json(item) for item in value]
    if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
        return None
    return value


def max_int_field(rows: list[dict[str, Any]], key: str) -> int:
    return max((int(row.get(key) or 0) for row in rows), default=0)


def volume_values(rows: list[dict[str, Any]]) -> list[float]:
    return [
        float(row["main_volume_db"])
        for row in rows
        if finite_number(row.get("main_volume_db")) is not None
    ]


def projection_rms(samples: np.ndarray, sample_rate: int, frequency: float) -> float:
    phase = 2.0 * np.pi * frequency * np.arange(len(samples)) / sample_rate
    cosine = float(np.mean(samples * np.cos(phase)))
    sine = float(np.mean(samples * np.sin(phase)))
    return math.sqrt(2.0) * math.hypot(cosine, sine)


def sweep_characteristics(samples: np.ndarray, sample_rate: int) -> dict[str, object]:
    """Identify an upward ESS ridge without trusting the server's metadata."""
    frequencies, times, spectrum = stft(
        samples,
        fs=sample_rate,
        window="hann",
        nperseg=4096,
        noverlap=3072,
        boundary=None,
    )
    band = (frequencies >= 15.0) & (frequencies <= min(22000.0, sample_rate / 2))
    magnitude = np.abs(spectrum[band])
    band_frequencies = frequencies[band]
    if magnitude.size == 0 or magnitude.shape[1] < 8:
        return {"detected": False, "reason": "insufficient time-frequency data"}
    peak_bins = np.argmax(magnitude, axis=0)
    ridge_hz = band_frequencies[peak_bins]
    frame_energy = np.sqrt(np.mean(np.square(magnitude), axis=0))
    energetic = frame_energy >= np.percentile(frame_energy, 35.0)
    ridge_hz = ridge_hz[energetic]
    ridge_times = times[energetic]
    if len(ridge_hz) < 8:
        return {"detected": False, "reason": "too few energetic sweep frames"}
    log_frequency = np.log10(np.maximum(ridge_hz, 1.0))
    correlation = float(np.corrcoef(ridge_times, log_frequency)[0, 1])
    low_hz = float(np.percentile(ridge_hz, 10.0))
    high_hz = float(np.percentile(ridge_hz, 90.0))
    span_octaves = float(math.log2(high_hz / low_hz)) if low_hz > 0 else 0.0
    positive_steps = float(np.mean(np.diff(log_frequency) >= -0.015))
    detected = correlation >= 0.8 and span_octaves >= 4.0
    return {
        "detected": detected,
        "direction": "up" if correlation > 0 else "down",
        "ridge_log_frequency_time_correlation": correlation,
        "ridge_nondecreasing_fraction": positive_steps,
        "ridge_10th_percentile_hz": low_hz,
        "ridge_90th_percentile_hz": high_hz,
        "ridge_span_octaves": span_octaves,
        "analyzed_frame_count": int(len(ridge_hz)),
    }


def emit_result(bundle: Path, result: dict[str, object]) -> None:
    rendered = (
        json.dumps(finite_json(result), indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    )
    output = bundle / "analysis.json"
    output.write_text(rendered, encoding="utf-8")
    output.chmod(0o600)
    print(rendered, end="")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    bundle = args.bundle

    manifest_path = bundle / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )
    expected_tone_hz = float(manifest.get("tone_frequency_hz") or 1000.0)
    safe_cap_volume_db = float(manifest.get("safe_cap_volume_db") or -4.0)
    speaker = load_jsonl(bundle / "speaker_timeline.jsonl")
    speaker_rows = [
        row for row in speaker if finite_number(row.get("t_epoch_s")) is not None
    ]
    speaker_errors = sum(1 for row in speaker if row.get("error"))
    observed_volumes = volume_values(speaker_rows)
    audio_path = bundle / "umik_raw_2ch_float32.wav"
    if not audio_path.exists():
        emit_result(
            bundle,
            {
                "bundle": str(bundle),
                "state_only": bool(manifest.get("state_only")),
                "stimulus": {
                    "detected": any(
                        finite_number(row.get("correction_input_rms_dbfs")) is not None
                        and float(row["correction_input_rms_dbfs"]) > -90.0
                        for row in speaker_rows
                    ),
                    "reason": "raw microphone audio was not captured",
                },
                "speaker_timeline": {
                    "sample_count": len(speaker_rows),
                    "error_count": speaker_errors,
                },
            },
        )
        return 0

    sample_rate, audio = wavfile.read(audio_path)
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, np.newaxis]
    if audio.ndim != 2 or audio.shape[1] < 1:
        raise ValueError(f"expected one or more audio channels, got {audio.shape}")
    blocks = load_jsonl(bundle / "umik_blocks.jsonl")
    if not blocks:
        emit_result(
            bundle,
            {
                "bundle": str(bundle),
                "sample_rate_hz": int(sample_rate),
                "channels": int(audio.shape[1]),
                "stimulus": {
                    "detected": False,
                    "reason": "microphone block timeline is empty",
                },
            },
        )
        return 0
    active_speaker = [
        row
        for row in speaker_rows
        if isinstance(row.get("correction_input_rms_dbfs"), (int, float))
        and float(row["correction_input_rms_dbfs"]) > -40.0
    ]
    if not active_speaker:
        clips = sum(sum(int(value) for value in row["clip_samples"]) for row in blocks)
        callback_errors = sorted(
            {str(row["callback_status"]) for row in blocks if row["callback_status"]}
        )
        result = {
            "bundle": str(bundle),
            "sample_rate_hz": int(sample_rate),
            "channels": int(audio.shape[1]),
            "duration_s": len(audio) / sample_rate,
            "channels_bit_identical": bool(np.array_equal(audio[:, 0], audio[:, 1])),
            "stimulus": {
                "detected": False,
                "reason": "no correction-lane playback found in speaker timeline",
            },
            "recording": {
                "max_200ms_rms_dbfs": max(
                    float(value) for row in blocks for value in row["rms_dbfs"]
                ),
                "max_sample_peak_dbfs": max(
                    float(value) for row in blocks for value in row["peak_dbfs"]
                ),
            },
            "clipping": {
                "umik_clip_samples_at_0_999": clips,
                "callback_errors": callback_errors,
                "camilla_max_clipped_samples": max_int_field(
                    speaker_rows, "camilla_clipped_samples"
                ),
                "outputd_max_clipped_samples": max_int_field(
                    speaker_rows, "outputd_clipped_samples"
                ),
            },
            "speaker_timeline_error_count": speaker_errors,
        }
        emit_result(bundle, result)
        return 0
    tone_start = min(float(row["t_epoch_s"]) for row in active_speaker)
    tone_end = max(float(row["t_epoch_s"]) for row in active_speaker)

    active_blocks: list[tuple[dict[str, Any], np.ndarray]] = []
    cap_blocks: list[tuple[dict[str, Any], np.ndarray, float]] = []
    volume_pairs: list[tuple[float, float]] = []
    for row in blocks:
        epoch = float(row["t_epoch_s"])
        if not (tone_start - 0.25 <= epoch <= tone_end + 0.25):
            continue
        start = int(row["frame_start"])
        end = int(row["frame_end"])
        samples = audio[start:end, 0]
        active_blocks.append((row, samples))
        nearest = min(
            speaker_rows,
            key=lambda item: abs(float(item["t_epoch_s"]) - epoch),
        )
        volume = nearest.get("main_volume_db")
        if isinstance(volume, (int, float)):
            tone_rms = projection_rms(samples, sample_rate, expected_tone_hz)
            volume_pairs.append((float(volume), dbfs(tone_rms)))
            if float(volume) >= safe_cap_volume_db:
                cap_blocks.append((row, samples, float(volume)))

    if not active_blocks:
        emit_result(
            bundle,
            {
                "bundle": str(bundle),
                "sample_rate_hz": int(sample_rate),
                "channels": int(audio.shape[1]),
                "stimulus": {
                    "detected": False,
                    "reason": "speaker activity did not overlap recorded mic blocks",
                },
                "speaker_timeline_error_count": speaker_errors,
            },
        )
        return 0

    active = np.concatenate([samples for _, samples in active_blocks])
    sweep = sweep_characteristics(active, sample_rate)
    if sweep.get("detected") is True:
        before_blocks = [
            row for row in blocks if float(row["t_epoch_s"]) < tone_start - 1.0
        ]
        noise_rms = [float(row["rms_dbfs"][0]) for row in before_blocks]
        active_rms = [
            float(value) for row, _ in active_blocks for value in row["rms_dbfs"]
        ]
        clips = sum(sum(int(value) for value in row["clip_samples"]) for row in blocks)
        callback_errors = sorted(
            {str(row["callback_status"]) for row in blocks if row["callback_status"]}
        )
        max_peak = max(float(value) for row in blocks for value in row["peak_dbfs"])
        result = {
            "bundle": str(bundle),
            "sample_rate_hz": int(sample_rate),
            "channels": int(audio.shape[1]),
            "duration_s": len(audio) / sample_rate,
            "channels_bit_identical": bool(np.array_equal(audio[:, 0], audio[:, 1])),
            "stimulus": {
                "detected": True,
                "type": "upward_exponential_sweep",
                "speaker_active_start_epoch_s": tone_start,
                "speaker_active_end_epoch_s": tone_end,
                "duration_s": tone_end - tone_start,
                **sweep,
                "max_200ms_rms_dbfs": max(active_rms),
                "max_sample_peak_dbfs": max_peak,
                "median_signal_above_noise_db": (
                    float(np.median(active_rms)) - float(np.median(noise_rms))
                    if noise_rms
                    else None
                ),
            },
            "noise": {
                "median_200ms_rms_dbfs": (
                    float(np.median(noise_rms)) if noise_rms else None
                ),
                "sample_count": len(noise_rms),
            },
            "clipping": {
                "umik_clip_samples_at_0_999": clips,
                "max_sample_peak_dbfs": max_peak,
                "callback_errors": callback_errors,
                "camilla_max_clipped_samples": max_int_field(
                    speaker_rows, "camilla_clipped_samples"
                ),
                "outputd_max_clipped_samples": max_int_field(
                    speaker_rows, "outputd_clipped_samples"
                ),
            },
            "speaker_gain": {
                "observed_min_db": min(observed_volumes) if observed_volumes else None,
                "observed_max_db": max(observed_volumes) if observed_volumes else None,
                "unique_values_db": sorted(set(observed_volumes)),
            },
            "graph_paths": sorted(
                {str(row.get("active_config_path") or "") for row in speaker}
            ),
        }
        emit_result(bundle, result)
        return 0
    frequencies, power = periodogram(active, fs=sample_rate, window="hann")
    search = (frequencies >= expected_tone_hz * 0.8) & (
        frequencies <= expected_tone_hz * 1.2
    )
    if not np.any(search):
        raise ValueError("tone search band has no FFT bins")
    dominant_frequency = float(frequencies[search][np.argmax(power[search])])

    cap_total = [
        dbfs(float(np.sqrt(np.mean(np.square(samples)))))
        for _, samples, _ in cap_blocks
    ]
    cap_tone = [
        dbfs(projection_rms(samples, sample_rate, dominant_frequency))
        for _, samples, _ in cap_blocks
    ]
    cap_thd = []
    for _, samples, _ in cap_blocks:
        fundamental = projection_rms(samples, sample_rate, dominant_frequency)
        harmonics = [
            projection_rms(samples, sample_rate, dominant_frequency * order)
            for order in range(2, 6)
        ]
        cap_thd.append(
            math.sqrt(sum(value * value for value in harmonics)) / fundamental
            if fundamental > 1e-12
            else float("nan")
        )

    regression = None
    usable_pairs = [(v, m) for v, m in volume_pairs if v >= -40.0]
    volume_bins = {
        str(volume): {
            "sample_count": sum(1 for value, _ in volume_pairs if value == volume),
            "median_tone_rms_dbfs": float(
                np.median([mic for value, mic in volume_pairs if value == volume])
            ),
        }
        for volume in sorted({value for value, _ in volume_pairs})
    }
    if len(usable_pairs) >= 3:
        x = np.asarray([item[0] for item in usable_pairs])
        y = np.asarray([item[1] for item in usable_pairs])
        slope, intercept = np.polyfit(x, y, 1)
        predicted = slope * x + intercept
        ss_res = float(np.sum(np.square(y - predicted)))
        ss_tot = float(np.sum(np.square(y - np.mean(y))))
        regression = {
            "slope_db_mic_per_db_dsp": float(slope),
            "intercept_db": float(intercept),
            "r_squared": 1.0 - ss_res / ss_tot if ss_tot else 1.0,
            "sample_count": len(usable_pairs),
        }

    before_blocks = [
        row for row in blocks if float(row["t_epoch_s"]) < tone_start - 1.0
    ]
    noise_rms = [float(row["rms_dbfs"][0]) for row in before_blocks]
    noise_median = float(np.median(noise_rms)) if noise_rms else None
    clips = sum(sum(int(value) for value in row["clip_samples"]) for row in blocks)
    callback_errors = sorted(
        {str(row["callback_status"]) for row in blocks if row["callback_status"]}
    )
    max_peak = max(float(value) for row in blocks for value in row["peak_dbfs"])
    max_rms = max(float(value) for row in active_blocks for value in row[0]["rms_dbfs"])
    exact_duplicate = bool(np.array_equal(audio[:, 0], audio[:, 1]))

    result = {
        "bundle": str(bundle),
        "sample_rate_hz": int(sample_rate),
        "channels": int(audio.shape[1]),
        "duration_s": len(audio) / sample_rate,
        "channels_bit_identical": exact_duplicate,
        "tone": {
            "tone_frequency_hz": expected_tone_hz,
            "speaker_active_start_epoch_s": tone_start,
            "speaker_active_end_epoch_s": tone_end,
            "duration_s": tone_end - tone_start,
            "dominant_frequency_hz": dominant_frequency,
            "max_200ms_rms_dbfs": max_rms,
            "max_sample_peak_dbfs": max_peak,
        },
        "noise": {
            "median_200ms_rms_dbfs": noise_median,
            "sample_count": len(noise_rms),
        },
        "safe_cap": {
            "tone_frequency_hz": expected_tone_hz,
            "observed_speaker_volume_threshold_db": safe_cap_volume_db,
            "block_count": len(cap_blocks),
            "median_total_rms_dbfs": float(np.median(cap_total)) if cap_total else None,
            "max_total_rms_dbfs": max(cap_total) if cap_total else None,
            "median_tone_rms_dbfs": float(np.median(cap_tone)) if cap_tone else None,
            "max_tone_rms_dbfs": max(cap_tone) if cap_tone else None,
            "median_thd_percent_harmonics_2_to_5": (
                float(np.median(cap_thd)) * 100.0 if cap_thd else None
            ),
            "median_signal_above_noise_db": (
                float(np.median(cap_total)) - noise_median
                if cap_total and noise_median is not None
                else None
            ),
            "shortfall_to_window_low_db": (
                float(manifest.get("window_low_dbfs") or -20.0)
                - float(np.median(cap_total))
                if cap_total
                else None
            ),
            "shortfall_to_pre_window_db": (
                float(manifest.get("pre_window_low_dbfs") or -23.75)
                - float(np.median(cap_total))
                if cap_total
                else None
            ),
        },
        "gain_linearity": regression,
        "clipping": {
            "umik_clip_samples_at_0_999": clips,
            "max_sample_peak_dbfs": max_peak,
            "callback_errors": callback_errors,
            "camilla_max_clipped_samples": max_int_field(
                speaker_rows, "camilla_clipped_samples"
            ),
            "outputd_max_clipped_samples": max_int_field(
                speaker_rows, "outputd_clipped_samples"
            ),
        },
        "speaker_gain": {
            "tone_frequency_hz": expected_tone_hz,
            "observed_min_db": min(observed_volumes) if observed_volumes else None,
            "observed_max_db": max(observed_volumes) if observed_volumes else None,
            "unique_values_db": sorted(set(observed_volumes)),
            "mic_by_observed_volume_db": volume_bins,
        },
        "speaker_timeline_error_count": speaker_errors,
    }
    emit_result(bundle, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
