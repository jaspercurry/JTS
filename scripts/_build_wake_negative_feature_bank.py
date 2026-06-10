#!/usr/bin/env python3
"""Build openWakeWord-compatible negative feature banks from a corpus bundle.

This is the negative-hours companion to ``scripts/build-wake-feature-bank.sh``.
It consumes the same exported wake-corpus bundle shape, verifies source WAV
hashes, extracts ``(N, 16, 96)`` openWakeWord embedding features, and writes
``negative_features_<split>.npy`` arrays for LiveKit/openWakeWord training and
evaluation.

By default, rows must be explicitly labeled as non-wake data
(``negative``, ``hard_negative``, ``ambient_negative``, or ``background``).
For a dedicated negative-only corpus that predates first-class labels, pass
``--allow-unlabeled-as ambient_negative`` or ``--allow-unlabeled-as
hard_negative``. The tool never trains, scores, launches cloud jobs, or touches
Pi runtime state.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _build_wake_feature_bank as feature_builder  # noqa: E402


SCHEMA_VERSION = 1
TRAIN_SPLIT = feature_builder.TRAIN_SPLIT
EVAL_SPLIT = feature_builder.EVAL_SPLIT
DEFAULT_SPLITS = feature_builder.DEFAULT_SPLITS
DEFAULT_TOTAL_SAMPLES = feature_builder.DEFAULT_TOTAL_SAMPLES
EMBEDDING_DIM = feature_builder.EMBEDDING_DIM
DEFAULT_NEGATIVE_LABEL_KINDS = frozenset(
    {"negative", "hard_negative", "ambient_negative", "background"}
)
POSITIVE_LABEL_KINDS = frozenset({"positive", "wake_positive", "wake"})


class _NoopExtractor:
    name = "not_used_no_selected_rows"

    def embed_clips(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("empty negative feature build should not extract features")


def _normalize_kind(value: object) -> str:
    raw = str(value or "").strip().lower()
    out = "".join(c if c.isalnum() else "_" for c in raw)
    return "_".join(part for part in out.split("_") if part)


def _effective_label_kind(row: dict[str, Any], *, allow_unlabeled_as: str | None) -> str:
    raw = (
        row.get("label_kind")
        or row.get("target_kind")
        or row.get("kind")
        or ""
    )
    normalized = _normalize_kind(raw)
    if normalized:
        return normalized
    return allow_unlabeled_as or ""


def _validate_negative_label_kind(kind: str, *, option_name: str) -> str:
    normalized = _normalize_kind(kind)
    if not normalized:
        raise ValueError(f"{option_name} must not be empty")
    if normalized in POSITIVE_LABEL_KINDS:
        raise ValueError(f"{option_name} must be a non-wake label, got {kind!r}")
    return normalized


def _row_matches(
    row: dict[str, Any],
    *,
    splits: set[str],
    legs: set[str] | None,
    profiles: set[str] | None,
    conditions: set[str] | None,
    distances: set[str] | None,
    label_kinds: set[str],
    allow_unlabeled_as: str | None,
) -> bool:
    if not feature_builder._row_matches(
        row,
        splits=splits,
        legs=legs,
        profiles=profiles,
    ):
        return False
    if conditions is not None and str(row.get("condition") or "") not in conditions:
        return False
    if distances is not None and str(row.get("distance") or "") not in distances:
        return False
    kind = _effective_label_kind(row, allow_unlabeled_as=allow_unlabeled_as)
    return kind in label_kinds


def _enrich_negative_rows(
    rows: list[dict[str, Any]],
    *,
    allow_unlabeled_as: str | None,
) -> None:
    for row in rows:
        kind = _effective_label_kind(row, allow_unlabeled_as=allow_unlabeled_as)
        row["label_kind"] = kind
        row["negative_kind"] = kind
        row["wake_positive"] = False


def _sum_duration_hours(rows: list[dict[str, Any]]) -> float:
    total_sec = 0.0
    for row in rows:
        try:
            total_sec += float(row.get("duration_sec") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(total_sec / 3600.0, 6)


def _count_duration_hours_by(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows:
        value = str(row.get(key) or "")
        try:
            duration = float(row.get("duration_sec") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        totals[value] = totals.get(value, 0.0) + duration
    return {key: round(value / 3600.0, 6) for key, value in sorted(totals.items())}


def build_negative_feature_bank(
    bundle_dir: Path,
    output_dir: Path,
    *,
    splits: set[str] | None = None,
    legs: set[str] | None = None,
    profiles: set[str] | None = None,
    conditions: set[str] | None = None,
    distances: set[str] | None = None,
    label_kinds: set[str] | None = None,
    allow_unlabeled_as: str | None = None,
    total_samples: int = DEFAULT_TOTAL_SAMPLES,
    batch_size: int = 32,
    ncpu: int = 1,
    extractor: feature_builder.FeatureExtractor | None = None,
    device: str = "cpu",
    melspec_model_path: Path | None = None,
    embedding_model_path: Path | None = None,
) -> dict[str, Any]:
    bundle_dir = bundle_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    feature_builder._require_numpy()
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

    allow_unlabeled_as = (
        _validate_negative_label_kind(
            allow_unlabeled_as,
            option_name="--allow-unlabeled-as",
        )
        if allow_unlabeled_as
        else None
    )
    label_kinds = label_kinds or set(DEFAULT_NEGATIVE_LABEL_KINDS)
    label_kinds = {
        _validate_negative_label_kind(kind, option_name="--label-kind")
        for kind in label_kinds
    }
    feature_frames = feature_builder._feature_frame_count(total_samples)

    bundle_summary = feature_builder._read_json(bundle_json_path)
    source_rows = feature_builder._read_jsonl(manifest_path)
    selected_rows = [
        row
        for row in source_rows
        if _row_matches(
            row,
            splits=splits,
            legs=legs,
            profiles=profiles,
            conditions=conditions,
            distances=distances,
            label_kinds=label_kinds,
            allow_unlabeled_as=allow_unlabeled_as,
        )
    ]
    _enrich_negative_rows(selected_rows, allow_unlabeled_as=allow_unlabeled_as)

    prepared_by_split: dict[str, list[feature_builder.PreparedClip]] = {
        split: [] for split in sorted(splits)
    }
    rejections: list[dict[str, Any]] = []
    for row in selected_rows:
        prepared, rejection = feature_builder._prepare_clip(
            bundle_dir=bundle_dir,
            row=row,
            total_samples=total_samples,
        )
        if prepared is not None:
            prepared_by_split[str(row.get("split") or TRAIN_SPLIT)].append(prepared)
        if rejection is not None:
            rejection["label_kind"] = row.get("label_kind")
            rejection["negative_kind"] = row.get("negative_kind")
            rejections.append(rejection)

    has_prepared_clips = any(prepared_by_split.values())
    if extractor is None and has_prepared_clips:
        extractor = feature_builder.OpenWakeWordFeatureExtractor(
            ncpu=ncpu,
            device=device,
            melspec_model_path=melspec_model_path,
            embedding_model_path=embedding_model_path,
        )
    elif extractor is None:
        extractor = _NoopExtractor()

    output_dir.mkdir(parents=True, exist_ok=True)
    all_feature_rows: list[dict[str, Any]] = []
    feature_files: dict[str, str] = {}
    feature_counts: dict[str, int] = {}
    for split in sorted(splits):
        features, feature_rows = feature_builder._extract_split_features(
            split=split,
            prepared=prepared_by_split[split],
            extractor=extractor,
            batch_size=batch_size,
            ncpu=ncpu,
            expected_feature_frames=feature_frames,
        )
        file_name = f"negative_features_{split}.npy"
        feature_builder.np.save(output_dir / file_name, features)
        feature_files[split] = file_name
        feature_counts[split] = int(features.shape[0])
        for row in feature_rows:
            row["feature_file"] = file_name
            row["feature_shape"] = [feature_frames, EMBEDDING_DIM]
            row["training_label"] = "negative"
            row["wake_positive"] = False
        all_feature_rows.extend(feature_rows)

    feature_builder._write_jsonl(output_dir / "negative_feature_manifest.jsonl", all_feature_rows)
    feature_builder._write_jsonl(
        output_dir / "negative_feature_rejections.jsonl",
        rejections,
    )

    summary = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "negative_feature_bank",
        "source_bundle": {
            "path": str(bundle_dir),
            "bundle_schema_version": bundle_summary.get("schema_version"),
            "manifest_sha256": feature_builder._sha256(manifest_path),
            "bundle_json_sha256": feature_builder._sha256(bundle_json_path),
        },
        "output_dir": str(output_dir),
        "selection": {
            "splits": sorted(splits),
            "legs": sorted(legs) if legs is not None else None,
            "profiles": sorted(profiles) if profiles is not None else None,
            "conditions": sorted(conditions) if conditions is not None else None,
            "distances": sorted(distances) if distances is not None else None,
            "label_kinds": sorted(label_kinds),
            "allow_unlabeled_as": allow_unlabeled_as,
        },
        "extraction": {
            "extractor": getattr(extractor, "name", type(extractor).__name__),
            "sample_rate_hz": feature_builder.EXPECTED_SAMPLE_RATE,
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
            "selected_by_split": feature_builder._count_by(selected_rows, "split"),
            "selected_by_profile": feature_builder._count_by(selected_rows, "profile"),
            "selected_by_leg": feature_builder._count_by(selected_rows, "leg"),
            "selected_by_condition": feature_builder._count_by(selected_rows, "condition"),
            "selected_by_distance": feature_builder._count_by(selected_rows, "distance"),
            "selected_by_label_kind": feature_builder._count_by(selected_rows, "label_kind"),
            "selected_duration_hours": _sum_duration_hours(selected_rows),
            "selected_duration_hours_by_label_kind": _count_duration_hours_by(
                selected_rows,
                "label_kind",
            ),
            "selected_duration_hours_by_leg": _count_duration_hours_by(selected_rows, "leg"),
        },
        "artifacts": {
            "summary": "negative_feature_bank.json",
            "feature_manifest": "negative_feature_manifest.jsonl",
            "feature_rejections": "negative_feature_rejections.jsonl",
            "features": feature_files,
        },
    }
    (output_dir / "negative_feature_bank.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return summary


def _default_output_dir(bundle_dir: Path) -> Path:
    return bundle_dir / "negative-feature-bank"


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
        return bool(rel.parts and rel.parts[0] == "negative-feature-bank")
    return _looks_like_negative_feature_bank_output(resolved)


def _looks_like_negative_feature_bank_output(path: Path) -> bool:
    marker = path / "negative_feature_bank.json"
    if not marker.is_file():
        return False
    try:
        data = feature_builder._read_json(marker)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    artifacts = data.get("artifacts")
    return (
        data.get("schema_version") == SCHEMA_VERSION
        and data.get("kind") == "negative_feature_bank"
        and isinstance(artifacts, dict)
        and artifacts.get("summary") == "negative_feature_bank.json"
        and artifacts.get("feature_manifest") == "negative_feature_manifest.jsonl"
    )


def _print_summary(summary: dict[str, Any]) -> str:
    lines = [
        "Wake negative feature bank build",
        "=" * 60,
        f"  output         : {summary['output_dir']}",
        f"  selected rows  : {summary['counts']['selected_manifest_rows']}",
        f"  features       : {summary['counts']['feature_rows']}",
        f"  rejections     : {summary['counts']['rejections']}",
        f"  duration hours : {summary['counts']['selected_duration_hours']:.6f}",
        f"  shape          : {tuple(summary['extraction']['feature_shape'])}",
        "",
        "  by split:",
    ]
    for split, count in summary["counts"]["features_by_split"].items():
        lines.append(f"    {split:<8} {count}")
    lines.append("")
    lines.append("  by label kind:")
    for kind, count in summary["counts"]["selected_by_label_kind"].items():
        hours = summary["counts"]["selected_duration_hours_by_label_kind"].get(kind, 0.0)
        lines.append(f"    {kind:<24} {count:<6} {hours:.6f} h")
    lines.append("")
    lines.append("  by leg:")
    for leg, count in summary["counts"]["selected_by_leg"].items():
        lines.append(f"    {leg:<24} {count}")
    return "\n".join(lines)


def _parse_repeatable_csv(values: list[str] | None) -> set[str] | None:
    return feature_builder._parse_repeatable_csv(values)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle_dir", help="Wake corpus bundle directory")
    ap.add_argument(
        "output_dir",
        nargs="?",
        help="Output directory (default: <bundle_dir>/negative-feature-bank)",
    )
    ap.add_argument(
        "--split",
        action="append",
        help="Split to include; repeat or comma-separate. Defaults to train and eval.",
    )
    ap.add_argument("--leg", action="append", help="Leg token to include; repeat or comma-separate")
    ap.add_argument("--profile", action="append", help="Profile to include; repeat or comma-separate")
    ap.add_argument(
        "--condition",
        action="append",
        help="Condition to include; repeat or comma-separate.",
    )
    ap.add_argument(
        "--distance",
        action="append",
        help="Distance bucket to include; repeat or comma-separate.",
    )
    ap.add_argument(
        "--label-kind",
        action="append",
        help=(
            "Negative label kind to include; repeat or comma-separate. "
            "Defaults to negative, hard_negative, ambient_negative, background."
        ),
    )
    ap.add_argument(
        "--allow-unlabeled-as",
        help=(
            "Treat unlabeled manifest rows as this non-wake label kind. Use only "
            "for a dedicated negative-only corpus that predates first-class labels."
        ),
    )
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
        summary = build_negative_feature_bank(
            bundle_dir,
            output_dir,
            splits=_parse_repeatable_csv(args.split) or set(DEFAULT_SPLITS),
            legs=_parse_repeatable_csv(args.leg),
            profiles=_parse_repeatable_csv(args.profile),
            conditions=_parse_repeatable_csv(args.condition),
            distances=_parse_repeatable_csv(args.distance),
            label_kinds=_parse_repeatable_csv(args.label_kind),
            allow_unlabeled_as=args.allow_unlabeled_as,
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
