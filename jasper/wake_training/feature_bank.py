"""Shared feature-bank utilities for offline wake-word training.

This module is the stable contract underneath the Phase 0 wake-training
scripts. It handles bundle manifest reads, source WAV validation, SHA-256
verification, end-aligned openWakeWord windows, batched embedding extraction,
and JSONL artifact writing.

It is intentionally offline-only and side-effect-light. Importing this module
does not load openWakeWord, initialize ONNX Runtime, access audio hardware, run
playback/capture, launch cloud jobs, or mutate Pi runtime state.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import types
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

try:
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - exercised by CLI help paths.
    np = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH_BYTES = 2
DEFAULT_TOTAL_SAMPLES = 32000  # 2.0 s -> 16 openWakeWord embedding frames.
TRAIN_SPLIT = "train"
EVAL_SPLIT = "eval"
DEFAULT_SPLITS = (TRAIN_SPLIT, EVAL_SPLIT)
EMBEDDING_DIM = 96


@dataclass(frozen=True)
class PreparedClip:
    row: dict[str, Any]
    audio_path: Path
    samples: np.ndarray
    source_frames: int


class FeatureExtractor(Protocol):
    name: str

    def embed_clips(self, clips: np.ndarray, *, batch_size: int, ncpu: int) -> np.ndarray:
        """Return features with shape ``(N, feature_frames, 96)``."""


class OpenWakeWordFeatureExtractor:
    """Small wrapper around openWakeWord's AudioFeatures batch API."""

    name = "openwakeword.utils.AudioFeatures"

    def __init__(
        self,
        *,
        ncpu: int,
        device: str,
        melspec_model_path: Path | None,
        embedding_model_path: Path | None,
    ) -> None:
        install_openwakeword_custom_verifier_stub()
        try:
            from openwakeword.utils import AudioFeatures
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "openwakeword feature extraction is unavailable. Install "
                "openwakeword==0.6.0 with ONNX assets, or run this in the "
                "JTS runtime environment. If the package assets are not "
                "staged, pass --melspec-model and --embedding-model."
            ) from e

        kwargs: dict[str, str | int] = {
            "sr": EXPECTED_SAMPLE_RATE,
            "ncpu": ncpu,
            "inference_framework": "onnx",
            "device": device,
        }
        if melspec_model_path is not None:
            kwargs["melspec_model_path"] = str(melspec_model_path)
        if embedding_model_path is not None:
            kwargs["embedding_model_path"] = str(embedding_model_path)
        try:
            self._features = AudioFeatures(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "failed to initialize openWakeWord AudioFeatures. Check that "
                "melspectrogram.onnx and embedding_model.onnx exist and that "
                "onnxruntime can load them."
            ) from e

    def embed_clips(self, clips: np.ndarray, *, batch_size: int, ncpu: int) -> np.ndarray:
        return self._features.embed_clips(clips, batch_size=batch_size, ncpu=ncpu)


def require_numpy() -> None:
    if np is None:
        raise RuntimeError(
            "numpy is required to build wake feature banks. Run inside the "
            "JTS environment or install numpy in the selected Python."
        )


def install_openwakeword_custom_verifier_stub() -> None:
    """Keep openwakeword's sklearn-heavy verifier import out of this tool."""
    stub = types.ModuleType("openwakeword.custom_verifier_model")
    stub.train_custom_verifier = None
    sys.modules.setdefault("openwakeword.custom_verifier_model", stub)


def read_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError(f"{path}:{lineno} does not contain a JSON object")
            data["_manifest_line"] = lineno
            rows.append(data)
    return rows


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def feature_frame_count(total_samples: int) -> int:
    if total_samples <= 0:
        raise ValueError("total_samples must be positive")
    melspec_frames = int(math.ceil(total_samples / 160 - 3))
    frames = (melspec_frames - 76) // 8 + 1
    if frames < 1:
        raise ValueError(
            "total_samples is too short for the openWakeWord embedding model; "
            "need enough audio to produce at least 76 mel frames"
        )
    return frames


def audio_path(bundle_dir: Path, row: dict[str, Any]) -> Path:
    bundle_path = str(row.get("bundle_path") or "")
    if bundle_path:
        return bundle_dir / bundle_path
    src_path = str(row.get("src_path") or "")
    if not src_path:
        raise ValueError("manifest row has neither bundle_path nor src_path")
    return Path(src_path)


def read_wav_int16(path: Path) -> tuple[np.ndarray | None, dict[str, Any] | None]:
    try:
        with wave.open(str(path), "rb") as w:
            sample_rate = int(w.getframerate())
            channels = int(w.getnchannels())
            sample_width = int(w.getsampwidth())
            frames = int(w.getnframes())
            raw = w.readframes(frames)
    except (OSError, EOFError, wave.Error) as e:
        return None, {"reason": f"wav_read_failed:{e}"}

    issues: list[str] = []
    if sample_rate != EXPECTED_SAMPLE_RATE:
        issues.append(f"sample_rate:{sample_rate}")
    if channels != EXPECTED_CHANNELS:
        issues.append(f"channels:{channels}")
    if sample_width != EXPECTED_SAMPLE_WIDTH_BYTES:
        issues.append(f"sample_width:{sample_width}")
    if frames <= 0:
        issues.append("empty_wav")
    if issues:
        return None, {
            "reason": "wrong_wav_format",
            "wav_issues": issues,
            "sample_rate_hz": sample_rate,
            "channels": channels,
            "sample_width_bytes": sample_width,
            "frames": frames,
        }

    return np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True), {
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "frames": frames,
    }


def end_align(samples: np.ndarray, *, total_samples: int) -> np.ndarray:
    if samples.shape[0] >= total_samples:
        return samples[-total_samples:].astype(np.int16, copy=True)
    out = np.zeros(total_samples, dtype=np.int16)
    out[-samples.shape[0]:] = samples
    return out


def prepare_clip(
    *,
    bundle_dir: Path,
    row: dict[str, Any],
    total_samples: int,
) -> tuple[PreparedClip | None, dict[str, Any] | None]:
    try:
        source_path = audio_path(bundle_dir, row)
    except ValueError as e:
        return None, rejection_for_row(row, reason=str(e), audio_path="")
    if not source_path.is_file():
        return None, rejection_for_row(row, reason="missing_wav", audio_path=str(source_path))

    expected_sha256 = str(row.get("sha256") or "")
    if not expected_sha256:
        return None, rejection_for_row(
            row,
            reason="sha256_missing",
            audio_path=str(source_path),
        )
    try:
        actual_sha256 = sha256(source_path)
    except OSError as e:
        return None, rejection_for_row(
            row,
            reason=f"sha256_read_failed:{e}",
            audio_path=str(source_path),
        )
    if actual_sha256 != expected_sha256:
        rejection = rejection_for_row(
            row,
            reason="sha256_mismatch",
            audio_path=str(source_path),
        )
        rejection["expected_sha256"] = expected_sha256
        rejection["actual_sha256"] = actual_sha256
        return None, rejection

    samples, info_or_error = read_wav_int16(source_path)
    if samples is None:
        rejection = rejection_for_row(
            row,
            reason=str(info_or_error.get("reason", "wav_unusable")),
            audio_path=str(source_path),
        )
        rejection.update(info_or_error)
        return None, rejection

    info = info_or_error or {}
    prepared = end_align(samples, total_samples=total_samples)
    return PreparedClip(
        row=row,
        audio_path=source_path,
        samples=prepared,
        source_frames=int(info.get("frames") or samples.shape[0]),
    ), None


def rejection_for_row(
    row: dict[str, Any],
    *,
    reason: str,
    audio_path: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "reason": reason,
        "audio_path": audio_path,
        "manifest_line": row.get("_manifest_line"),
        "utterance_id": row.get("utterance_id"),
        "session_id": row.get("session_id"),
        "clip_id": row.get("clip_id"),
        "split": row.get("split"),
        "profile": row.get("profile"),
        "leg": row.get("leg"),
        "sha256": row.get("sha256"),
    }


def row_matches(
    row: dict[str, Any],
    *,
    splits: set[str],
    legs: set[str] | None,
    profiles: set[str] | None,
) -> bool:
    if str(row.get("split") or "") not in splits:
        return False
    if legs is not None and str(row.get("leg") or "") not in legs:
        return False
    if profiles is not None and str(row.get("profile") or "") not in profiles:
        return False
    return True


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w") as f:
        for row in rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            f.write(json.dumps(clean, sort_keys=True) + "\n")


def chunks(values: list[PreparedClip], size: int) -> list[list[PreparedClip]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def extract_split_features(
    *,
    split: str,
    prepared: list[PreparedClip],
    extractor: FeatureExtractor,
    batch_size: int,
    ncpu: int,
    expected_feature_frames: int,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    if not prepared:
        return np.empty((0, expected_feature_frames, EMBEDDING_DIM), dtype=np.float32), []

    feature_chunks: list[np.ndarray] = []
    feature_rows: list[dict[str, Any]] = []
    feature_index = 0
    for batch in chunks(prepared, batch_size):
        audio_batch = np.stack([item.samples for item in batch]).astype(np.int16, copy=False)
        features = np.asarray(
            extractor.embed_clips(audio_batch, batch_size=len(batch), ncpu=ncpu),
            dtype=np.float32,
        )
        if features.ndim != 3:
            raise ValueError(
                f"extractor returned shape {features.shape}; expected (N, frames, 96)"
            )
        if features.shape[0] != len(batch):
            raise ValueError(
                f"extractor returned {features.shape[0]} rows for {len(batch)} clips"
            )
        if features.shape[1] != expected_feature_frames or features.shape[2] != EMBEDDING_DIM:
            raise ValueError(
                "extractor returned incompatible feature shape "
                f"{features.shape[1:]}; expected "
                f"({expected_feature_frames}, {EMBEDDING_DIM})"
            )
        feature_chunks.append(features)
        for offset, item in enumerate(batch):
            row = item.row
            feature_rows.append({
                "schema_version": SCHEMA_VERSION,
                "split": split,
                "feature_index": feature_index + offset,
                "manifest_line": row.get("_manifest_line"),
                "utterance_id": row.get("utterance_id"),
                "session_id": row.get("session_id"),
                "clip_id": row.get("clip_id"),
                "seq": row.get("seq"),
                "member": row.get("member"),
                "label_kind": row.get("label_kind"),
                "phrase": row.get("phrase"),
                "transcript": row.get("transcript"),
                "profile": row.get("profile"),
                "condition": row.get("condition"),
                "distance": row.get("distance"),
                "leg": row.get("leg"),
                "leg_label": row.get("leg_label"),
                "device_id": row.get("device_id"),
                "native_stream": row.get("native_stream"),
                "source_channel": row.get("source_channel"),
                "processing": row.get("processing"),
                "profile_role": row.get("profile_role"),
                "wake_input": row.get("wake_input"),
                "source_sha256": row.get("sha256"),
                "source_duration_sec": row.get("duration_sec"),
                "source_frames": item.source_frames,
                "audio_path": str(item.audio_path),
                "alignment": "end_aligned",
            })
        feature_index += len(batch)

    return np.concatenate(feature_chunks, axis=0), feature_rows


def parse_repeatable_csv(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    out: set[str] = set()
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                out.add(item)
    return out or None
