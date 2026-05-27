"""Programmatic audio-quality analyzer for browser-recorded wake corpus WAVs.

This is the repeatable implementation of
`docs/HANDOFF-wake-corpus-quality.md`: a laptop-side, deterministic
quality pass over clips copied from `/var/lib/jasper/enrollment_positives/`.

It is intentionally separate from `scripts/_audit_wake_corpus.py`.
The audit answers "did recording structurally work?" This analyzer
answers "which clips or legs deserve listening review, and why?"
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import wave
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage, signal


SAMPLE_RATE_HZ = 16000
FULL_SCALE = 32768.0
EPS = 1e-12

LEG_ORDER = (
    "on",
    "off",
    "dtln",
    "raw0",
    "usb_raw",
    "usb_webrtc",
    "usb_dtln",
    "ref",
)

LEG_LABELS = {
    "on": "XVF WebRTC AEC3",
    "off": "XVF raw",
    "dtln": "XVF DTLN",
    "raw0": "XVF raw0",
    "usb_raw": "USB raw",
    "usb_webrtc": "USB WebRTC AEC3",
    "usb_dtln": "USB DTLN",
    "ref": "Reference",
}

CROSS_LEG_PAIRS = (
    ("on", "off"),
    ("dtln", "off"),
    ("raw0", "off"),
    ("usb_webrtc", "usb_raw"),
    ("usb_dtln", "usb_raw"),
    ("usb_raw", "off"),
)

METRIC_FIELDS = (
    "session_id",
    "seq",
    "clip_id",
    "condition",
    "distance",
    "leg",
    "path",
    "sample_rate",
    "channels",
    "sample_width",
    "duration_s",
    "peak_int16",
    "peak_dbfs",
    "true_peak_dbfs",
    "rms_dbfs",
    "crest_db",
    "dc_offset",
    "exact_clip_count",
    "near_clip_0_5db",
    "near_clip_1db",
    "near_clip_3db",
    "flat_top_run",
    "near_zero_run_ms",
    "repeated_sample_run_ms",
    "flatness_p50",
    "flatness_p90",
    "high_ratio_db_p50",
    "high_ratio_db_p90",
    "nyquist_ratio_db_p50",
    "nyquist_ratio_db_p90",
    "spectral_flux_p95",
    "env_mod_peak_hz",
    "env_mod_prom_db",
    "crest_rms_corr",
    "transient_event_count",
    "transient_event_rate_s",
    "max_delta_robust_z",
)


@dataclass(frozen=True)
class ClipRef:
    session_id: str
    seq: int
    clip_id: str
    condition: str
    distance: str
    files: dict[str, str]


@dataclass(frozen=True)
class AnalyzerConfig:
    event_z: float = 10.0
    event_min_jump: float = 0.020
    event_merge_ms: float = 5.0
    coincidence_ms: float = 20.0
    max_alignment_lag_ms: float = 250.0


def _db20(value: float) -> float:
    return 20.0 * math.log10(max(float(value), EPS))


def _median(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return None
    return float(statistics.median(clean))


def _fmt(value: float | None, digits: int = 1) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{path}: failed to read JSON: {e}") from e


def _resolve_wav_path(corpus_dir: Path, path_str: str) -> Path:
    raw = Path(path_str)
    marker = "enrollment_positives"
    if marker in raw.parts:
        idx = raw.parts.index(marker)
        rel_parts = raw.parts[idx + 1:]
        if rel_parts:
            return corpus_dir.joinpath(*rel_parts)
    if raw.is_absolute():
        return raw
    return corpus_dir / raw


def _load_wav(path: Path) -> tuple[int, int, int, np.ndarray, np.ndarray]:
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        sample_width = w.getsampwidth()
        sample_rate = w.getframerate()
        frames = w.getnframes()
        raw = w.readframes(frames)
    if sample_width != 2:
        raise ValueError(f"{path}: unsupported sample width {sample_width}")
    pcm = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels)[:, 0]
    return sample_rate, channels, sample_width, pcm, pcm.astype(np.float64) / FULL_SCALE


def _longest_true_run(mask: np.ndarray) -> int:
    if mask.size == 0 or not bool(mask.any()):
        return 0
    idx = np.flatnonzero(mask)
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, len(idx) - 1]
    return int(np.max(idx[ends] - idx[starts] + 1))


def _cluster_events(
    sample_indices: np.ndarray,
    sample_rate: int,
    *,
    merge_ms: float,
    max_events: int = 50,
) -> list[dict[str, float]]:
    if sample_indices.size == 0:
        return []
    merge_samples = max(1, int(sample_rate * merge_ms / 1000.0))
    idx = np.sort(sample_indices.astype(np.int64))
    clusters: list[tuple[int, int]] = []
    start = prev = int(idx[0])
    for value in idx[1:]:
        current = int(value)
        if current - prev <= merge_samples:
            prev = current
        else:
            clusters.append((start, prev))
            start = prev = current
    clusters.append((start, prev))
    events: list[dict[str, float]] = []
    for start, end in clusters[:max_events]:
        events.append({
            "t_s": (start + end) / (2.0 * sample_rate),
            "duration_ms": (end - start + 1) * 1000.0 / sample_rate,
        })
    return events


def _spectral_metrics(samples: np.ndarray, sample_rate: int) -> dict[str, float | None]:
    if samples.size < 512:
        return {
            "flatness_p50": None,
            "flatness_p90": None,
            "high_ratio_db_p50": None,
            "high_ratio_db_p90": None,
            "nyquist_ratio_db_p50": None,
            "nyquist_ratio_db_p90": None,
            "spectral_flux_p95": None,
        }

    freqs, _, stft = signal.stft(
        samples,
        fs=sample_rate,
        window="hann",
        nperseg=400,
        noverlap=240,
        nfft=512,
        boundary=None,
        padded=False,
    )
    mag = np.abs(stft).T
    if mag.size == 0:
        return {
            "flatness_p50": None,
            "flatness_p90": None,
            "high_ratio_db_p50": None,
            "high_ratio_db_p90": None,
            "nyquist_ratio_db_p50": None,
            "nyquist_ratio_db_p90": None,
            "spectral_flux_p95": None,
        }

    power = mag**2 + 1e-18
    frame_energy = power.sum(axis=1)
    keep = frame_energy > np.percentile(frame_energy, 35)
    if int(keep.sum()) < 3:
        keep = frame_energy > 0
    power = power[keep]
    mag = mag[keep]

    flatness = np.exp(np.mean(np.log(power), axis=1)) / np.mean(power, axis=1)
    low = (freqs >= 80) & (freqs < 3000)
    high = (freqs >= 3000) & (freqs <= 7500)
    nyquist = (freqs >= 7200) & (freqs <= 8000)
    total = (freqs >= 80) & (freqs <= 8000)
    high_ratio = 10.0 * np.log10(
        (power[:, high].sum(axis=1) + 1e-18)
        / (power[:, low].sum(axis=1) + 1e-18)
    )
    nyquist_ratio = 10.0 * np.log10(
        (power[:, nyquist].sum(axis=1) + 1e-18)
        / (power[:, total].sum(axis=1) + 1e-18)
    )
    flux_p95: float | None = None
    if mag.shape[0] >= 2:
        normalized = mag / (np.linalg.norm(mag, axis=1, keepdims=True) + 1e-12)
        flux = np.sqrt(np.sum(np.diff(normalized, axis=0) ** 2, axis=1))
        flux_p95 = float(np.percentile(flux, 95))

    return {
        "flatness_p50": float(np.percentile(flatness, 50)),
        "flatness_p90": float(np.percentile(flatness, 90)),
        "high_ratio_db_p50": float(np.percentile(high_ratio, 50)),
        "high_ratio_db_p90": float(np.percentile(high_ratio, 90)),
        "nyquist_ratio_db_p50": float(np.percentile(nyquist_ratio, 50)),
        "nyquist_ratio_db_p90": float(np.percentile(nyquist_ratio, 90)),
        "spectral_flux_p95": flux_p95,
    }


def _envelope_metrics(samples: np.ndarray, sample_rate: int) -> dict[str, float | None]:
    frame = max(1, int(0.010 * sample_rate))
    usable = (len(samples) // frame) * frame
    env_peak_hz: float | None = None
    env_prom_db: float | None = None
    if usable >= frame * 50:
        envelope = np.sqrt(
            np.mean(samples[:usable].reshape(-1, frame) ** 2, axis=1) + 1e-18
        )
        envelope = envelope - float(np.mean(envelope))
        spectrum = np.abs(np.fft.rfft(envelope * np.hanning(len(envelope))))
        freqs = np.fft.rfftfreq(len(envelope), d=0.010)
        band = (freqs >= 1.0) & (freqs <= 10.0)
        if bool(band.any()):
            values = spectrum[band]
            band_freqs = freqs[band]
            peak_idx = int(np.argmax(values))
            env_peak_hz = float(band_freqs[peak_idx])
            env_prom_db = _db20(
                (float(values[peak_idx]) + 1e-12)
                / (float(np.median(values)) + 1e-12)
            )

    win = int(0.250 * sample_rate)
    hop = int(0.050 * sample_rate)
    rms_values: list[float] = []
    crest_values: list[float] = []
    if len(samples) >= win and hop > 0:
        for start in range(0, len(samples) - win + 1, hop):
            seg = samples[start:start + win]
            rms = float(np.sqrt(np.mean(seg * seg) + 1e-18))
            peak = float(np.max(np.abs(seg))) if seg.size else 0.0
            if rms > 1e-6 and peak > 1e-6:
                rms_values.append(_db20(rms))
                crest_values.append(_db20(peak / rms))
    crest_rms_corr: float | None = None
    if (
        len(rms_values) >= 4
        and float(np.std(rms_values)) > 1e-6
        and float(np.std(crest_values)) > 1e-6
    ):
        crest_rms_corr = float(np.corrcoef(rms_values, crest_values)[0, 1])

    return {
        "env_mod_peak_hz": env_peak_hz,
        "env_mod_prom_db": env_prom_db,
        "crest_rms_corr": crest_rms_corr,
    }


def _transient_events(
    samples: np.ndarray,
    sample_rate: int,
    config: AnalyzerConfig,
) -> tuple[list[dict[str, float]], float]:
    if samples.size < 300:
        return [], 0.0
    delta = np.diff(samples)
    size = 257 if len(delta) >= 257 else max(3, len(delta) // 2 * 2 + 1)
    local_median = ndimage.median_filter(delta, size=size, mode="reflect")
    deviation = np.abs(delta - local_median)
    local_mad = ndimage.median_filter(deviation, size=size, mode="reflect")
    robust_z = deviation / (1.4826 * local_mad + 1e-6)
    candidates = np.flatnonzero(
        (robust_z > config.event_z)
        & (deviation > config.event_min_jump)
    )
    events = _cluster_events(
        candidates,
        sample_rate,
        merge_ms=config.event_merge_ms,
    )
    return events, float(np.max(robust_z)) if robust_z.size else 0.0


def _alignment(
    left: np.ndarray,
    right: np.ndarray,
    sample_rate: int,
    *,
    max_lag_ms: float,
) -> dict[str, float | None]:
    if left.size < 100 or right.size < 100:
        return {"lag_ms": None, "confidence": None}
    n = min(left.size, right.size)
    left = left[:n] - float(np.mean(left[:n]))
    right = right[:n] - float(np.mean(right[:n]))
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm <= 1e-9 or right_norm <= 1e-9:
        return {"lag_ms": None, "confidence": None}
    corr = signal.correlate(left, right, mode="full", method="fft")
    lags = signal.correlation_lags(left.size, right.size, mode="full")
    max_lag = int(sample_rate * max_lag_ms / 1000.0)
    keep = np.abs(lags) <= max_lag
    if not bool(keep.any()):
        return {"lag_ms": None, "confidence": None}
    corr = corr[keep]
    lags = lags[keep]
    best = int(np.argmax(np.abs(corr)))
    confidence = float(np.abs(corr[best]) / (left_norm * right_norm + EPS))
    return {
        "lag_ms": float(lags[best]) * 1000.0 / sample_rate,
        "confidence": confidence,
    }


def _flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if row["sample_rate"] != SAMPLE_RATE_HZ:
        flags.append("bad_sample_rate")
    if row["channels"] != 1:
        flags.append("bad_channels")
    if row["sample_width"] != 2:
        flags.append("bad_sample_width")
    if row["exact_clip_count"] > 0:
        flags.append("exact_clip")
    if row["peak_dbfs"] > -1.0:
        flags.append("peak_gt_-1dbfs")
    if row["near_clip_0_5db"] > 5:
        flags.append("near_clip_mass")
    if row["flat_top_run"] >= 3 and row["peak_dbfs"] > -6.0:
        flags.append("flat_top_suspect")
    if abs(row["dc_offset"]) > 0.001:
        flags.append("dc_offset")
    if row["crest_db"] < 8.0:
        flags.append("low_crest_compressed")
    if row["crest_db"] > 26.0:
        flags.append("high_crest_impulse_or_quiet")
    if row["near_zero_run_ms"] > 30.0:
        flags.append("long_near_zero_run")
    if row["repeated_sample_run_ms"] > 10.0:
        flags.append("repeated_samples")
    if row["transient_event_rate_s"] > 2.0 or row["transient_event_count"] >= 4:
        flags.append("transient_candidates")
    corr = row.get("crest_rms_corr")
    if corr is not None and corr < -0.3:
        flags.append("agc_corr_suspect")
    prom = row.get("env_mod_prom_db")
    if prom is not None and prom >= 12.0 and (corr is None or corr < -0.25):
        flags.append("env_modulation_suspect")
    nyq = row.get("nyquist_ratio_db_p90")
    if nyq is not None and nyq > -25.0:
        flags.append("nyquist_edge_energy")
    return flags


def analyze_wav(
    *,
    corpus_dir: Path,
    clip: ClipRef,
    leg: str,
    path_str: str,
    config: AnalyzerConfig,
) -> tuple[dict[str, Any], np.ndarray]:
    path = _resolve_wav_path(corpus_dir, path_str)
    sample_rate, channels, sample_width, pcm, samples = _load_wav(path)
    duration_s = len(samples) / sample_rate if sample_rate else 0.0
    abs_pcm = np.abs(pcm.astype(np.int32))
    peak_int = int(np.max(abs_pcm)) if abs_pcm.size else 0
    peak = peak_int / FULL_SCALE
    rms = float(np.sqrt(np.mean(samples * samples) + 1e-18))
    true_peak = float(np.max(np.abs(signal.resample_poly(samples, 4, 1)))) if samples.size else 0.0

    near_counts: dict[str, int] = {}
    for db in (0.5, 1.0, 3.0):
        threshold = FULL_SCALE * (10.0 ** (-db / 20.0))
        key = f"near_clip_{str(db).replace('.', '_')}db"
        near_counts[key] = int(np.sum(abs_pcm >= threshold))

    flat_threshold = peak_int * (10.0 ** (-0.1 / 20.0)) if peak_int else 0
    flat_top_run = _longest_true_run(abs_pcm >= flat_threshold) if peak_int else 0
    near_zero_run = _longest_true_run(abs_pcm <= 2)
    repeated_mask = np.r_[False, (np.diff(pcm.astype(np.int32)) == 0) & (abs_pcm[1:] > 2)]
    repeated_run = _longest_true_run(repeated_mask)
    events, max_z = _transient_events(samples, sample_rate, config)

    row: dict[str, Any] = {
        "session_id": clip.session_id,
        "seq": clip.seq,
        "clip_id": clip.clip_id,
        "condition": clip.condition,
        "distance": clip.distance,
        "leg": leg,
        "path": str(path),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "duration_s": duration_s,
        "peak_int16": peak_int,
        "peak_dbfs": _db20(peak) if peak_int else -120.0,
        "true_peak_dbfs": _db20(true_peak) if true_peak > 0.0 else -120.0,
        "rms_dbfs": _db20(rms),
        "crest_db": _db20(peak / rms) if rms > 0.0 and peak > 0.0 else 0.0,
        "dc_offset": float(np.mean(samples)) if samples.size else 0.0,
        "exact_clip_count": int(np.sum((pcm == 32767) | (pcm == -32768))),
        **near_counts,
        "flat_top_run": flat_top_run,
        "near_zero_run_ms": near_zero_run * 1000.0 / sample_rate if sample_rate else 0.0,
        "repeated_sample_run_ms": repeated_run * 1000.0 / sample_rate if sample_rate else 0.0,
        **_spectral_metrics(samples, sample_rate),
        **_envelope_metrics(samples, sample_rate),
        "transient_event_count": len(events),
        "transient_event_rate_s": len(events) / duration_s if duration_s else 0.0,
        "max_delta_robust_z": max_z,
        "events": events,
    }
    row["flags"] = _flags(row)
    row["review_priority"] = (
        30 * int("exact_clip" in row["flags"])
        + 20 * int("peak_gt_-1dbfs" in row["flags"])
        + 15 * int("transient_candidates" in row["flags"])
        + 10 * int("agc_corr_suspect" in row["flags"])
        + 5 * len(row["flags"])
        + min(20.0, row["transient_event_rate_s"] * 2.0)
    )
    return row, samples


def _coincident_event_count(
    events_a: list[dict[str, float]],
    events_b: list[dict[str, float]],
    *,
    window_ms: float,
) -> int:
    if not events_a or not events_b:
        return 0
    window_s = window_ms / 1000.0
    b_times = [e["t_s"] for e in events_b]
    count = 0
    for event in events_a:
        if any(abs(event["t_s"] - b_time) <= window_s for b_time in b_times):
            count += 1
    return count


def _cross_leg_rows(
    rows_by_clip: dict[tuple[str, int], dict[str, dict[str, Any]]],
    samples_by_clip: dict[tuple[str, int], dict[str, np.ndarray]],
    config: AnalyzerConfig,
) -> list[dict[str, Any]]:
    cross_rows: list[dict[str, Any]] = []
    for (session_id, seq), legs in sorted(rows_by_clip.items()):
        samples = samples_by_clip[(session_id, seq)]
        for processed, baseline in CROSS_LEG_PAIRS:
            if processed not in legs or baseline not in legs:
                continue
            left = legs[processed]
            right = legs[baseline]
            alignment = _alignment(
                samples[processed],
                samples[baseline],
                SAMPLE_RATE_HZ,
                max_lag_ms=config.max_alignment_lag_ms,
            )
            cross_rows.append({
                "session_id": session_id,
                "seq": seq,
                "pair": f"{processed}-{baseline}",
                "processed_leg": processed,
                "baseline_leg": baseline,
                "alignment_lag_ms": alignment["lag_ms"],
                "alignment_confidence": alignment["confidence"],
                "rms_delta_db": left["rms_dbfs"] - right["rms_dbfs"],
                "peak_delta_db": left["peak_dbfs"] - right["peak_dbfs"],
                "crest_delta_db": left["crest_db"] - right["crest_db"],
                "high_ratio_delta_db": (
                    (left.get("high_ratio_db_p50") or 0.0)
                    - (right.get("high_ratio_db_p50") or 0.0)
                ),
                "nyquist_ratio_delta_db": (
                    (left.get("nyquist_ratio_db_p50") or 0.0)
                    - (right.get("nyquist_ratio_db_p50") or 0.0)
                ),
                "transient_delta": (
                    left["transient_event_count"] - right["transient_event_count"]
                ),
                "coincident_events": _coincident_event_count(
                    left["events"],
                    right["events"],
                    window_ms=config.coincidence_ms,
                ),
            })
    return cross_rows


def _load_clips(
    corpus_dir: Path,
    *,
    session_ids: set[str] | None,
    latest: int | None,
) -> tuple[list[dict[str, Any]], list[ClipRef]]:
    metadata_dir = corpus_dir / "metadata"
    if not corpus_dir.is_dir():
        raise ValueError(f"{corpus_dir} is not a directory")
    if not metadata_dir.is_dir():
        raise ValueError(f"{metadata_dir} is not a directory")
    session_paths = sorted(metadata_dir.glob("enroll_*.json"))
    sessions = [_read_json(path) for path in session_paths]
    if session_ids is not None:
        sessions = [s for s in sessions if str(s.get("session_id")) in session_ids]
    if latest is not None:
        sessions = sorted(sessions, key=lambda s: str(s.get("session_id", "")))[-latest:]
    clips: list[ClipRef] = []
    for session in sessions:
        session_id = str(session.get("session_id", ""))
        for raw_clip in session.get("clips") or []:
            if raw_clip.get("deleted"):
                continue
            files = raw_clip.get("files") or {}
            if not isinstance(files, dict) or not files:
                continue
            clips.append(ClipRef(
                session_id=session_id,
                seq=int(raw_clip.get("seq", 0)),
                clip_id=str(raw_clip.get("clip_id", "")),
                condition=str(raw_clip.get("condition", "")),
                distance=str(raw_clip.get("distance", "")),
                files={str(k): str(v) for k, v in files.items()},
            ))
    return sessions, clips


def analyze_corpus(
    corpus_dir: Path,
    output_dir: Path,
    *,
    session_ids: set[str] | None = None,
    latest: int | None = None,
    config: AnalyzerConfig | None = None,
) -> dict[str, Any]:
    config = config or AnalyzerConfig()
    sessions, clips = _load_clips(corpus_dir, session_ids=session_ids, latest=latest)
    if not sessions:
        raise ValueError("no matching sessions found")
    if not clips:
        raise ValueError("no matching clips found")

    output_dir.mkdir(parents=True, exist_ok=True)
    metric_rows: list[dict[str, Any]] = []
    rows_by_clip: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    samples_by_clip: dict[tuple[str, int], dict[str, np.ndarray]] = defaultdict(dict)
    issues: list[str] = []

    for clip in clips:
        for leg in LEG_ORDER:
            if leg not in clip.files:
                continue
            try:
                row, samples = analyze_wav(
                    corpus_dir=corpus_dir,
                    clip=clip,
                    leg=leg,
                    path_str=clip.files[leg],
                    config=config,
                )
            except (OSError, wave.Error, ValueError) as e:
                issues.append(f"{clip.session_id} #{clip.seq} {leg}: {e}")
                continue
            metric_rows.append(row)
            rows_by_clip[(clip.session_id, clip.seq)][leg] = row
            samples_by_clip[(clip.session_id, clip.seq)][leg] = samples

    if not metric_rows:
        raise ValueError("no WAVs could be analyzed")
    cross_rows = _cross_leg_rows(rows_by_clip, samples_by_clip, config)
    summary = _build_summary(
        sessions=sessions,
        clips=clips,
        metric_rows=metric_rows,
        cross_rows=cross_rows,
        issues=issues,
    )

    metrics_path = output_dir / "metrics.csv"
    with metrics_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[*METRIC_FIELDS, "flags", "review_priority"])
        writer.writeheader()
        for row in metric_rows:
            writer.writerow({
                **{field: row.get(field) for field in METRIC_FIELDS},
                "flags": ";".join(row["flags"]),
                "review_priority": row["review_priority"],
            })

    cross_path = output_dir / "cross_leg.csv"
    if cross_rows:
        with cross_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(cross_rows[0].keys()))
            writer.writeheader()
            writer.writerows(cross_rows)
    else:
        cross_path.write_text("")

    events_path = output_dir / "events.json"
    events_path.write_text(json.dumps({
        "config": {
            "event_z": config.event_z,
            "event_min_jump": config.event_min_jump,
            "event_merge_ms": config.event_merge_ms,
            "coincidence_ms": config.coincidence_ms,
            "max_alignment_lag_ms": config.max_alignment_lag_ms,
        },
        "issues": issues,
        "events": [
            {
                "session_id": row["session_id"],
                "seq": row["seq"],
                "clip_id": row["clip_id"],
                "leg": row["leg"],
                "flags": row["flags"],
                "review_priority": row["review_priority"],
                "events": row["events"],
            }
            for row in metric_rows
            if row["events"] or row["flags"]
        ],
    }, indent=2))

    summary_path = output_dir / "summary.md"
    summary_path.write_text(summary)
    return {
        "output_dir": output_dir,
        "metrics_path": metrics_path,
        "cross_path": cross_path,
        "events_path": events_path,
        "summary_path": summary_path,
        "summary": summary,
        "issues": issues,
        "metric_rows": metric_rows,
        "cross_rows": cross_rows,
    }


def _build_summary(
    *,
    sessions: list[dict[str, Any]],
    clips: list[ClipRef],
    metric_rows: list[dict[str, Any]],
    cross_rows: list[dict[str, Any]],
    issues: list[str],
) -> str:
    lines: list[str] = []
    lines.append("# Wake Corpus Quality Summary")
    lines.append("")
    lines.append(
        "Deterministic first-pass analysis. Use this to prioritize "
        "listening review, not to auto-reject corpus clips."
    )
    lines.append("")
    lines.append(f"- Sessions: {len(sessions)}")
    lines.append(f"- Clips: {len(clips)}")
    lines.append(f"- WAVs analyzed: {len(metric_rows)}")
    lines.append(f"- Issues while reading WAVs: {len(issues)}")
    lines.append("")
    lines.append("## Sessions")
    for session in sessions:
        session_id = str(session.get("session_id", "?"))
        enabled = ", ".join(str(x) for x in session.get("enabled_legs", [])) or "legacy"
        live_clips = [c for c in session.get("clips") or [] if not c.get("deleted")]
        lines.append(f"- `{session_id}`: {len(live_clips)} clip(s), legs: {enabled}")
    lines.append("")
    lines.append("## Per-Leg Medians")
    lines.append("")
    lines.append(
        "| Session | Leg | n | RMS dBFS | Peak dBFS | Crest dB | "
        "Events/s | HB p50 | Nyq p50 | Flags |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for session_id in sorted({r["session_id"] for r in metric_rows}):
        for leg in LEG_ORDER:
            rows = [r for r in metric_rows if r["session_id"] == session_id and r["leg"] == leg]
            if not rows:
                continue
            flags = Counter(flag for row in rows for flag in row["flags"])
            flag_text = ", ".join(
                f"{flag}:{count}" for flag, count in flags.most_common(4)
            ) or "-"
            lines.append(
                f"| `{session_id}` | {LEG_LABELS.get(leg, leg)} | {len(rows)} "
                f"| {_fmt(_median([r['rms_dbfs'] for r in rows]))} "
                f"| {_fmt(_median([r['peak_dbfs'] for r in rows]))} "
                f"| {_fmt(_median([r['crest_db'] for r in rows]))} "
                f"| {_fmt(_median([r['transient_event_rate_s'] for r in rows]), 2)} "
                f"| {_fmt(_median([r.get('high_ratio_db_p50') for r in rows]))} "
                f"| {_fmt(_median([r.get('nyquist_ratio_db_p50') for r in rows]))} "
                f"| {flag_text} |"
            )
    lines.append("")
    if cross_rows:
        lines.append("## Cross-Leg Deltas")
        lines.append("")
        lines.append(
            "| Pair | n | RMS delta | Peak delta | Crest delta | "
            "HB delta | Nyq delta | Event delta | Align conf |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for pair in sorted({r["pair"] for r in cross_rows}):
            rows = [r for r in cross_rows if r["pair"] == pair]
            lines.append(
                f"| `{pair}` | {len(rows)} "
                f"| {_fmt(_median([r['rms_delta_db'] for r in rows]), 1)} "
                f"| {_fmt(_median([r['peak_delta_db'] for r in rows]), 1)} "
                f"| {_fmt(_median([r['crest_delta_db'] for r in rows]), 1)} "
                f"| {_fmt(_median([r['high_ratio_delta_db'] for r in rows]), 1)} "
                f"| {_fmt(_median([r['nyquist_ratio_delta_db'] for r in rows]), 1)} "
                f"| {_fmt(_median([r['transient_delta'] for r in rows]), 1)} "
                f"| {_fmt(_median([r.get('alignment_confidence') for r in rows]), 2)} |"
            )
        lines.append("")
    lines.append("## Highest-Priority WAVs")
    lines.append("")
    top = sorted(
        metric_rows,
        key=lambda r: (r["review_priority"], r["transient_event_rate_s"], r["peak_dbfs"]),
        reverse=True,
    )[:20]
    if not top:
        lines.append("- None.")
    for row in top:
        flags = ", ".join(row["flags"]) or "-"
        lines.append(
            f"- `{row['session_id']}` #{row['seq']} {LEG_LABELS.get(row['leg'], row['leg'])}: "
            f"priority {_fmt(row['review_priority'], 1)}, flags {flags}, "
            f"peak {_fmt(row['peak_dbfs'])} dBFS, rms {_fmt(row['rms_dbfs'])} dBFS, "
            f"crest {_fmt(row['crest_db'])} dB, events/s "
            f"{_fmt(row['transient_event_rate_s'], 2)}"
        )
    if issues:
        lines.append("")
        lines.append("## Read Issues")
        for issue in issues:
            lines.append(f"- {issue}")
    lines.append("")
    lines.append("## Interpretation Notes")
    lines.append("")
    lines.append("- Exact clipping and near-clipping are strong evidence.")
    lines.append(
        "- Envelope/AGC features are weak on 1-3 s music clips; treat them "
        "as review hints until calibrated on paired AGC-on/off data."
    )
    lines.append(
        "- Transient candidates are local-MAD candidates. LPC residual "
        "confirmation is the next planned upgrade."
    )
    lines.append(
        "- Cross-leg deltas are most meaningful between sibling legs "
        "from the same mic path, such as `usb_webrtc-usb_raw`."
    )
    return "\n".join(lines) + "\n"


def _default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("logs") / "wake-corpus-quality" / stamp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "corpus_dir",
        nargs="?",
        type=Path,
        default=Path("data/enrollment_positives"),
        help="Corpus root copied from /var/lib/jasper/enrollment_positives.",
    )
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="Analyze only this session id. May be repeated.",
    )
    parser.add_argument(
        "--latest",
        nargs="?",
        const=1,
        type=int,
        default=None,
        help="Analyze the latest N session(s) by session id; default N=1.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for metrics.csv, cross_leg.csv, events.json, summary.md.",
    )
    parser.add_argument("--event-z", type=float, default=10.0)
    parser.add_argument("--event-min-jump", type=float, default=0.020)
    parser.add_argument("--event-merge-ms", type=float, default=5.0)
    parser.add_argument("--coincidence-ms", type=float, default=20.0)
    args = parser.parse_args(argv)

    if args.session and args.latest is not None:
        parser.error("--session and --latest are mutually exclusive")
    output_dir = args.output_dir or _default_output_dir()
    config = AnalyzerConfig(
        event_z=args.event_z,
        event_min_jump=args.event_min_jump,
        event_merge_ms=args.event_merge_ms,
        coincidence_ms=args.coincidence_ms,
    )
    try:
        result = analyze_corpus(
            args.corpus_dir,
            output_dir,
            session_ids=set(args.session) if args.session else None,
            latest=args.latest,
            config=config,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(result["summary"])
    print(f"Artifacts written to: {result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
