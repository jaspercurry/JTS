#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jasper.wake_training import feature_bank as fb


SCHEMA_VERSION = fb.SCHEMA_VERSION
DEFAULT_TOTAL_SAMPLES = fb.DEFAULT_TOTAL_SAMPLES
TRAIN_SPLIT = fb.TRAIN_SPLIT
EVAL_SPLIT = fb.EVAL_SPLIT
DEFAULT_SPLITS = fb.DEFAULT_SPLITS
EMBEDDING_DIM = fb.EMBEDDING_DIM
FeatureExtractor = fb.FeatureExtractor


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
    fb.require_numpy()
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
    feature_frames = fb.feature_frame_count(total_samples)

    bundle_summary = fb.read_json(bundle_json_path)
    source_rows = fb.read_jsonl(manifest_path)
    selected_rows = [
        row
        for row in source_rows
        if fb.row_matches(row, splits=splits, legs=legs, profiles=profiles)
    ]

    prepared_by_split: dict[str, list[fb.PreparedClip]] = {
        split: [] for split in sorted(splits)
    }
    rejections: list[dict[str, Any]] = []
    for row in selected_rows:
        prepared, rejection = fb.prepare_clip(
            bundle_dir=bundle_dir,
            row=row,
            total_samples=total_samples,
        )
        if prepared is not None:
            prepared_by_split[str(row.get("split") or TRAIN_SPLIT)].append(prepared)
        if rejection is not None:
            rejections.append(rejection)

    if extractor is None:
        extractor = fb.OpenWakeWordFeatureExtractor(
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
        features, feature_rows = fb.extract_split_features(
            split=split,
            prepared=prepared_by_split[split],
            extractor=extractor,
            batch_size=batch_size,
            ncpu=ncpu,
            expected_feature_frames=feature_frames,
        )
        file_name = f"positive_features_{split}.npy"
        fb.np.save(output_dir / file_name, features)
        feature_files[split] = file_name
        feature_counts[split] = int(features.shape[0])
        for row in feature_rows:
            row["feature_file"] = file_name
            row["feature_shape"] = [feature_frames, EMBEDDING_DIM]
        all_feature_rows.extend(feature_rows)

    fb.write_jsonl(output_dir / "feature_manifest.jsonl", all_feature_rows)
    fb.write_jsonl(output_dir / "feature_rejections.jsonl", rejections)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_bundle": {
            "path": str(bundle_dir),
            "bundle_schema_version": bundle_summary.get("schema_version"),
            "manifest_sha256": fb.sha256(manifest_path),
            "bundle_json_sha256": fb.sha256(bundle_json_path),
        },
        "output_dir": str(output_dir),
        "selection": {
            "splits": sorted(splits),
            "legs": sorted(legs) if legs is not None else None,
            "profiles": sorted(profiles) if profiles is not None else None,
        },
        "extraction": {
            "extractor": getattr(extractor, "name", type(extractor).__name__),
            "sample_rate_hz": fb.EXPECTED_SAMPLE_RATE,
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
            "selected_by_split": fb.count_by(selected_rows, "split"),
            "selected_by_profile": fb.count_by(selected_rows, "profile"),
            "selected_by_leg": fb.count_by(selected_rows, "leg"),
            "selected_by_condition": fb.count_by(selected_rows, "condition"),
            "selected_by_distance": fb.count_by(selected_rows, "distance"),
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
        data = fb.read_json(marker)
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
            splits=fb.parse_repeatable_csv(args.split) or set(DEFAULT_SPLITS),
            legs=fb.parse_repeatable_csv(args.leg),
            profiles=fb.parse_repeatable_csv(args.profile),
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
