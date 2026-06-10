#!/usr/bin/env python3
"""Build openWakeWord-compatible positive feature banks from a corpus bundle.

This is the second Phase 0 slice for custom wake-word training. It consumes
the artifact produced by ``scripts/export-wake-corpus-bundle.sh`` and emits
positive feature arrays that later LiveKit/openWakeWord injection tooling can
append to a synthetic positive bank.

The tool is intentionally offline-only. It does not train, score, touch Pi
runtime state, run playback/capture, or write any microphone/chip settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import types
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _require_numpy() -> None:
    if np is None:
        raise RuntimeError(
            "numpy is required to build wake feature banks. Run inside the "
            "JTS environment or install numpy in the selected Python."
        )


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
        _install_openwakeword_custom_verifier_stub()
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


def _install_openwakeword_custom_verifier_stub() -> None:
    """Keep openwakeword's sklearn-heavy verifier import out of this tool."""
    stub = types.ModuleType("openwakeword.custom_verifier_model")
    stub.train_custom_verifier = None
    sys.modules.setdefault("openwakeword.custom_verifier_model", stub)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _feature_frame_count(total_samples: int) -> int:
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


def _audio_path(bundle_dir: Path, row: dict[str, Any]) -> Path:
    bundle_path = str(row.get("bundle_path") or "")
    if bundle_path:
        return bundle_dir / bundle_path
    src_path = str(row.get("src_path") or "")
    if not src_path:
        raise ValueError("manifest row has neither bundle_path nor src_path")
    return Path(src_path)


def _read_wav_int16(path: Path) -> tuple[np.ndarray | None, dict[str, Any] | None]:
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


def _end_align(samples: np.ndarray, *, total_samples: int) -> np.ndarray:
    if samples.shape[0] >= total_samples:
        return samples[-total_samples:].astype(np.int16, copy=True)
    out = np.zeros(total_samples, dtype=np.int16)
    out[-samples.shape[0]:] = samples
    return out


def _prepare_clip(
    *,
    bundle_dir: Path,
    row: dict[str, Any],
    total_samples: int,
) -> tuple[PreparedClip | None, dict[str, Any] | None]:
    try:
        audio_path = _audio_path(bundle_dir, row)
    except ValueError as e:
        return None, _rejection_for_row(row, reason=str(e), audio_path="")
    if not audio_path.is_file():
        return None, _rejection_for_row(row, reason="missing_wav", audio_path=str(audio_path))

    expected_sha256 = str(row.get("sha256") or "")
    if not expected_sha256:
        return None, _rejection_for_row(
            row,
            reason="sha256_missing",
            audio_path=str(audio_path),
        )
    try:
        actual_sha256 = _sha256(audio_path)
    except OSError as e:
        return None, _rejection_for_row(
            row,
            reason=f"sha256_read_failed:{e}",
            audio_path=str(audio_path),
        )
    if actual_sha256 != expected_sha256:
        rejection = _rejection_for_row(
            row,
            reason="sha256_mismatch",
            audio_path=str(audio_path),
        )
        rejection["expected_sha256"] = expected_sha256
        rejection["actual_sha256"] = actual_sha256
        return None, rejection

    samples, info_or_error = _read_wav_int16(audio_path)
    if samples is None:
        rejection = _rejection_for_row(
            row,
            reason=str(info_or_error.get("reason", "wav_unusable")),
            audio_path=str(audio_path),
        )
        rejection.update(info_or_error)
        return None, rejection

    info = info_or_error or {}
    prepared = _end_align(samples, total_samples=total_samples)
    return PreparedClip(
        row=row,
        audio_path=audio_path,
        samples=prepared,
        source_frames=int(info.get("frames") or samples.shape[0]),
    ), None


def _rejection_for_row(
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


def _row_matches(
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


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w") as f:
        for row in rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            f.write(json.dumps(clean, sort_keys=True) + "\n")


def _chunks(values: list[PreparedClip], size: int) -> list[list[PreparedClip]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def _extract_split_features(
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

    chunks: list[np.ndarray] = []
    feature_rows: list[dict[str, Any]] = []
    feature_index = 0
    for batch in _chunks(prepared, batch_size):
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
        chunks.append(features)
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

    return np.concatenate(chunks, axis=0), feature_rows


def build_feature_bank(
    bundle_dir: Path,
    output_dir: Path,
    *,
    splits: set[str] | None = None,
    legs: set[str] | None = None,
    profiles: set[str] | None = None,
    total_samples: int = DEFAULT_TOTAL_SAMPLES,
    batch_size: int = 32,
    ncpu: int = 1,
    extractor: FeatureExtractor | None = None,
    device: str = "cpu",
    melspec_model_path: Path | None = None,
    embedding_model_path: Path | None = None,
) -> dict[str, Any]:
    bundle_dir = bundle_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    _require_numpy()
    manifest_path = bundle_dir / "manifest.jsonl"
    bundle_json_path = bundle_dir / "bundle.json"
    if not manifest_path.is_file():
        raise ValueError(f"{manifest_path} not found")
    if not bundle_json_path.is_file():
        raise ValueError(f"{bundle_json_path} not found")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if ncpu <= 0:
        raise ValueError("ncpu must be positive")

    splits = splits or set(DEFAULT_SPLITS)
    invalid_splits = splits - set(DEFAULT_SPLITS)
    if invalid_splits:
        raise ValueError(f"unsupported split(s): {', '.join(sorted(invalid_splits))}")
    feature_frames = _feature_frame_count(total_samples)

    bundle_summary = _read_json(bundle_json_path)
    source_rows = _read_jsonl(manifest_path)
    selected_rows = [
        row
        for row in source_rows
        if _row_matches(row, splits=splits, legs=legs, profiles=profiles)
    ]

    prepared_by_split: dict[str, list[PreparedClip]] = {split: [] for split in sorted(splits)}
    rejections: list[dict[str, Any]] = []
    for row in selected_rows:
        prepared, rejection = _prepare_clip(
            bundle_dir=bundle_dir,
            row=row,
            total_samples=total_samples,
        )
        if prepared is not None:
            prepared_by_split[str(row.get("split") or TRAIN_SPLIT)].append(prepared)
        if rejection is not None:
            rejections.append(rejection)

    if extractor is None:
        extractor = OpenWakeWordFeatureExtractor(
            ncpu=ncpu,
            device=device,
            melspec_model_path=melspec_model_path,
            embedding_model_path=embedding_model_path,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    all_feature_rows: list[dict[str, Any]] = []
    feature_files: dict[str, str] = {}
    feature_counts: dict[str, int] = {}
    for split in sorted(splits):
        features, feature_rows = _extract_split_features(
            split=split,
            prepared=prepared_by_split[split],
            extractor=extractor,
            batch_size=batch_size,
            ncpu=ncpu,
            expected_feature_frames=feature_frames,
        )
        file_name = f"positive_features_{split}.npy"
        np.save(output_dir / file_name, features)
        feature_files[split] = file_name
        feature_counts[split] = int(features.shape[0])
        for row in feature_rows:
            row["feature_file"] = file_name
            row["feature_shape"] = [feature_frames, EMBEDDING_DIM]
        all_feature_rows.extend(feature_rows)

    _write_jsonl(output_dir / "feature_manifest.jsonl", all_feature_rows)
    _write_jsonl(output_dir / "feature_rejections.jsonl", rejections)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_bundle": {
            "path": str(bundle_dir),
            "bundle_schema_version": bundle_summary.get("schema_version"),
            "manifest_sha256": _sha256(manifest_path),
            "bundle_json_sha256": _sha256(bundle_json_path),
        },
        "output_dir": str(output_dir),
        "selection": {
            "splits": sorted(splits),
            "legs": sorted(legs) if legs is not None else None,
            "profiles": sorted(profiles) if profiles is not None else None,
        },
        "extraction": {
            "extractor": getattr(extractor, "name", type(extractor).__name__),
            "sample_rate_hz": EXPECTED_SAMPLE_RATE,
            "total_samples": total_samples,
            "alignment": "end_aligned",
            "feature_shape": [feature_frames, EMBEDDING_DIM],
            "inference_framework": "onnx",
            "device": device,
            "ncpu": ncpu,
            "batch_size": batch_size,
        },
        "counts": {
            "source_manifest_rows": len(source_rows),
            "selected_manifest_rows": len(selected_rows),
            "feature_rows": len(all_feature_rows),
            "rejections": len(rejections),
            "features_by_split": feature_counts,
            "selected_by_split": _count_by(selected_rows, "split"),
            "selected_by_profile": _count_by(selected_rows, "profile"),
            "selected_by_leg": _count_by(selected_rows, "leg"),
            "selected_by_condition": _count_by(selected_rows, "condition"),
            "selected_by_distance": _count_by(selected_rows, "distance"),
        },
        "artifacts": {
            "summary": "feature_bank.json",
            "feature_manifest": "feature_manifest.jsonl",
            "feature_rejections": "feature_rejections.jsonl",
            "features": feature_files,
        },
    }
    (output_dir / "feature_bank.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return summary


def _parse_repeatable_csv(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    out: set[str] = set()
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                out.add(item)
    return out or None


def _default_output_dir(bundle_dir: Path) -> Path:
    return bundle_dir / "feature-bank"


def _non_empty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def _safe_to_remove_output(path: Path, *, bundle_dir: Path) -> bool:
    resolved = path.expanduser().resolve()
    bundle_resolved = bundle_dir.expanduser().resolve()
    blocked = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
        bundle_resolved,
    }
    if resolved in blocked:
        return False
    if bundle_resolved in resolved.parents:
        rel = resolved.relative_to(bundle_resolved)
        return bool(rel.parts and rel.parts[0] == "feature-bank")
    return _looks_like_feature_bank_output(resolved)


def _looks_like_feature_bank_output(path: Path) -> bool:
    marker = path / "feature_bank.json"
    if not marker.is_file():
        return False
    try:
        data = _read_json(marker)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    artifacts = data.get("artifacts")
    return (
        data.get("schema_version") == SCHEMA_VERSION
        and isinstance(artifacts, dict)
        and artifacts.get("summary") == "feature_bank.json"
        and artifacts.get("feature_manifest") == "feature_manifest.jsonl"
    )


def _print_summary(summary: dict[str, Any]) -> str:
    lines = [
        "Wake feature bank build",
        "=" * 60,
        f"  output        : {summary['output_dir']}",
        f"  selected rows : {summary['counts']['selected_manifest_rows']}",
        f"  features      : {summary['counts']['feature_rows']}",
        f"  rejections    : {summary['counts']['rejections']}",
        f"  shape         : {tuple(summary['extraction']['feature_shape'])}",
        "",
        "  by split:",
    ]
    for split, count in summary["counts"]["features_by_split"].items():
        lines.append(f"    {split:<8} {count}")
    lines.append("")
    lines.append("  by leg:")
    for leg, count in summary["counts"]["selected_by_leg"].items():
        lines.append(f"    {leg:<24} {count}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle_dir", help="Wake corpus bundle directory")
    ap.add_argument(
        "output_dir",
        nargs="?",
        help="Output directory (default: <bundle_dir>/feature-bank)",
    )
    ap.add_argument(
        "--split",
        action="append",
        help="Split to include; repeat or comma-separate. Defaults to train and eval.",
    )
    ap.add_argument("--leg", action="append", help="Leg token to include; repeat or comma-separate")
    ap.add_argument("--profile", action="append", help="Profile to include; repeat or comma-separate")
    ap.add_argument(
        "--total-samples",
        type=int,
        default=DEFAULT_TOTAL_SAMPLES,
        help="End-aligned training window in samples (default: 32000 = 2 seconds)",
    )
    ap.add_argument("--batch-size", type=int, default=32, help="Feature extraction batch size")
    ap.add_argument("--ncpu", type=int, default=1, help="openWakeWord CPU thread count")
    ap.add_argument("--device", choices=["cpu", "gpu"], default="cpu", help="openWakeWord device")
    ap.add_argument("--melspec-model", help="Path to melspectrogram.onnx")
    ap.add_argument("--embedding-model", help="Path to embedding_model.onnx")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Replace a non-empty output directory after safety checks",
    )
    args = ap.parse_args(argv)

    bundle_dir = Path(args.bundle_dir)
    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir(bundle_dir)
    if _non_empty(output_dir):
        if not args.force:
            print(f"output directory is not empty: {output_dir} (pass --force)", file=sys.stderr)
            return 2
        if not _safe_to_remove_output(output_dir, bundle_dir=bundle_dir):
            print(f"refusing to remove unsafe output directory: {output_dir}", file=sys.stderr)
            return 2
        shutil.rmtree(output_dir)

    try:
        summary = build_feature_bank(
            bundle_dir,
            output_dir,
            splits=_parse_repeatable_csv(args.split) or set(DEFAULT_SPLITS),
            legs=_parse_repeatable_csv(args.leg),
            profiles=_parse_repeatable_csv(args.profile),
            total_samples=args.total_samples,
            batch_size=args.batch_size,
            ncpu=args.ncpu,
            device=args.device,
            melspec_model_path=Path(args.melspec_model) if args.melspec_model else None,
            embedding_model_path=Path(args.embedding_model) if args.embedding_model else None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_print_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
