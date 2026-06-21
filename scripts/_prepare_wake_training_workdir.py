#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare a trainer workdir from JTS wake real-positive feature banks.

This is the third Phase 0 slice for custom wake-word training. It consumes
the output from ``scripts/build-wake-feature-bank.sh`` and stages the JTS
real-positive features into the LiveKit/openWakeWord naming convention:

    feature_data/positive_features_train.npy
    feature_data/positive_features_test.npy

The tool also writes an audit manifest that records every source row and every
intentional train-row repeat used for real-positive up-weighting.

It is intentionally offline-only. It does not generate synthetic data, build
negative feature banks, train a model, launch cloud jobs, or alter Pi runtime
state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - exercised by CLI help paths.
    np = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
EXPECTED_FEATURE_FRAMES = 16
EXPECTED_EMBEDDING_DIM = 96
SOURCE_TRAIN_SPLIT = "train"
SOURCE_EVAL_SPLIT = "eval"
TRAINER_TRAIN_SPLIT = "train"
TRAINER_TEST_SPLIT = "test"
DEFAULT_POSITIVE_WEIGHT = 3
DEFAULT_TARGET_PHRASE = "hey jarvis"


def _require_numpy() -> None:
    if np is None:
        raise RuntimeError(
            "numpy is required to prepare wake training workdirs. Run inside "
            "the JTS environment or install numpy in the selected Python."
        )


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


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w") as f:
        for row in rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            f.write(json.dumps(clean, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_output_dir(feature_bank_dir: Path) -> Path:
    return feature_bank_dir / "training-workdir"


def _non_empty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def _safe_to_remove_output(path: Path, *, feature_bank_dir: Path) -> bool:
    resolved = path.expanduser().resolve()
    feature_bank_resolved = feature_bank_dir.expanduser().resolve()
    blocked = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
        feature_bank_resolved,
    }
    if resolved in blocked:
        return False
    if feature_bank_resolved in resolved.parents:
        rel = resolved.relative_to(feature_bank_resolved)
        return bool(rel.parts and rel.parts[0] == "training-workdir")
    return _looks_like_training_workdir_output(resolved)


def _looks_like_training_workdir_output(path: Path) -> bool:
    marker = path / "training_workdir.json"
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
        and artifacts.get("summary") == "training_workdir.json"
        and artifacts.get("real_positive_manifest") == "real_positive_manifest.jsonl"
    )


def _feature_file_for(summary: dict[str, Any], split: str) -> str:
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("feature_bank.json missing artifacts object")
    features = artifacts.get("features")
    if not isinstance(features, dict):
        raise ValueError("feature_bank.json missing artifacts.features object")
    file_name = features.get(split)
    if not isinstance(file_name, str) or not file_name:
        raise ValueError(f"feature_bank.json missing feature file for split {split!r}")
    return file_name


def _load_feature_array(path: Path, *, split: str) -> np.ndarray:
    _require_numpy()
    try:
        features = np.asarray(np.load(path), dtype=np.float32)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"failed to load {split} features from {path}: {e}") from e
    if features.ndim != 3:
        raise ValueError(
            f"{path} has shape {features.shape}; expected (N, 16, 96)"
        )
    if features.shape[1:] != (EXPECTED_FEATURE_FRAMES, EXPECTED_EMBEDDING_DIM):
        raise ValueError(
            f"{path} has feature shape {features.shape[1:]}; expected "
            f"({EXPECTED_FEATURE_FRAMES}, {EXPECTED_EMBEDDING_DIM})"
        )
    return features


def _manifest_rows_by_split(rows: list[dict[str, Any]], split: str) -> list[dict[str, Any]]:
    filtered = [row for row in rows if str(row.get("split") or "") == split]
    return sorted(filtered, key=lambda row: _int_value(row.get("feature_index"), default=0))


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_manifest_matches_features(
    *,
    rows: list[dict[str, Any]],
    features: np.ndarray,
    split: str,
    expected_file: str,
) -> None:
    if len(rows) != int(features.shape[0]):
        raise ValueError(
            f"feature_manifest.jsonl has {len(rows)} {split} rows but "
            f"{expected_file} has {int(features.shape[0])} rows"
        )
    for expected_idx, row in enumerate(rows):
        actual_idx = _int_value(row.get("feature_index"), default=-1)
        if actual_idx != expected_idx:
            raise ValueError(
                f"{split} manifest row order mismatch: expected feature_index "
                f"{expected_idx}, got {actual_idx}"
            )
        if str(row.get("feature_file") or "") != expected_file:
            raise ValueError(
                f"{split} manifest row {expected_idx} points at "
                f"{row.get('feature_file')!r}, expected {expected_file!r}"
            )


def _source_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "feature_manifest_line": row.get("_manifest_line"),
        "source_manifest_line": row.get("manifest_line"),
        "source_feature_index": row.get("feature_index"),
        "utterance_id": row.get("utterance_id"),
        "session_id": row.get("session_id"),
        "clip_id": row.get("clip_id"),
        "seq": row.get("seq"),
        "member": row.get("member"),
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
        "source_sha256": row.get("source_sha256"),
        "source_duration_sec": row.get("source_duration_sec"),
        "alignment": row.get("alignment"),
    }


def _training_manifest_rows(
    *,
    rows: list[dict[str, Any]],
    source_split: str,
    trainer_split: str,
    source_feature_file: str,
    trainer_feature_file: str,
    positive_weight: int,
    repeat_train: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    trainer_index = 0
    repeats = positive_weight if repeat_train else 1
    for row in rows:
        source_index = _int_value(row.get("feature_index"), default=0)
        for repeat_index in range(repeats):
            out.append({
                "schema_version": SCHEMA_VERSION,
                "trainer_split": trainer_split,
                "trainer_feature_file": trainer_feature_file,
                "trainer_feature_index": trainer_index,
                "source_split": source_split,
                "source_feature_file": source_feature_file,
                "source_feature_index": source_index,
                "repeat_index": repeat_index,
                "positive_weight": positive_weight if repeat_train else 1,
                **_source_row(row),
            })
            trainer_index += 1
    return out


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _write_readme(path: Path, *, target_phrase: str, model_name: str) -> None:
    path.write_text(
        "\n".join([
            "# JTS Wake Training Workdir",
            "",
            "This directory was generated by `prepare-wake-training-workdir.sh`.",
            "",
            "It contains JTS real-positive openWakeWord embedding arrays staged",
            "for a later LiveKit/openWakeWord training run. It is not a complete",
            "training dataset: synthetic positives, negatives, background banks,",
            "training, export, and evaluation are intentionally separate steps.",
            "",
            f"- model_name: `{model_name}`",
            f"- target_phrase: `{target_phrase}`",
            "- feature shape: `(N, 16, 96)`",
            "- train positives: `feature_data/positive_features_train.npy`",
            "- test positives: `feature_data/positive_features_test.npy`",
            "- audit manifest: `real_positive_manifest.jsonl`",
            "",
        ])
        + "\n"
    )


def prepare_training_workdir(
    feature_bank_dir: Path,
    output_dir: Path,
    *,
    target_phrase: str = DEFAULT_TARGET_PHRASE,
    model_name: str | None = None,
    positive_weight: int = DEFAULT_POSITIVE_WEIGHT,
) -> dict[str, Any]:
    _require_numpy()
    feature_bank_dir = feature_bank_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if positive_weight < 1 or positive_weight > 20:
        raise ValueError("positive_weight must be between 1 and 20")
    if not target_phrase.strip():
        raise ValueError("target_phrase must not be empty")
    model_name = (model_name or _model_name_from_phrase(target_phrase)).strip()
    if not model_name:
        raise ValueError("model_name must not be empty")

    summary_path = feature_bank_dir / "feature_bank.json"
    manifest_path = feature_bank_dir / "feature_manifest.jsonl"
    if not summary_path.is_file():
        raise ValueError(f"{summary_path} not found")
    if not manifest_path.is_file():
        raise ValueError(f"{manifest_path} not found")

    feature_bank = _read_json(summary_path)
    if feature_bank.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported feature_bank.json schema_version: "
            f"{feature_bank.get('schema_version')!r}"
        )
    extraction = feature_bank.get("extraction")
    if isinstance(extraction, dict):
        feature_shape = extraction.get("feature_shape")
        if feature_shape and feature_shape != [EXPECTED_FEATURE_FRAMES, EXPECTED_EMBEDDING_DIM]:
            raise ValueError(
                f"feature_bank.json extraction.feature_shape is {feature_shape!r}; "
                f"expected [{EXPECTED_FEATURE_FRAMES}, {EXPECTED_EMBEDDING_DIM}]"
            )

    train_source_file = _feature_file_for(feature_bank, SOURCE_TRAIN_SPLIT)
    eval_source_file = _feature_file_for(feature_bank, SOURCE_EVAL_SPLIT)
    train_source_path = feature_bank_dir / train_source_file
    eval_source_path = feature_bank_dir / eval_source_file
    train_features = _load_feature_array(train_source_path, split=SOURCE_TRAIN_SPLIT)
    eval_features = _load_feature_array(eval_source_path, split=SOURCE_EVAL_SPLIT)

    manifest_rows = _read_jsonl(manifest_path)
    train_rows = _manifest_rows_by_split(manifest_rows, SOURCE_TRAIN_SPLIT)
    eval_rows = _manifest_rows_by_split(manifest_rows, SOURCE_EVAL_SPLIT)
    _validate_manifest_matches_features(
        rows=train_rows,
        features=train_features,
        split=SOURCE_TRAIN_SPLIT,
        expected_file=train_source_file,
    )
    _validate_manifest_matches_features(
        rows=eval_rows,
        features=eval_features,
        split=SOURCE_EVAL_SPLIT,
        expected_file=eval_source_file,
    )
    if train_features.shape[0] == 0:
        raise ValueError("feature bank has no train positives")
    if eval_features.shape[0] == 0:
        raise ValueError("feature bank has no eval positives")

    feature_data_dir = output_dir / "feature_data"
    feature_data_dir.mkdir(parents=True, exist_ok=True)
    output_train_file = "feature_data/positive_features_train.npy"
    output_test_file = "feature_data/positive_features_test.npy"
    train_output_path = output_dir / output_train_file
    test_output_path = output_dir / output_test_file

    weighted_train_features = np.repeat(train_features, positive_weight, axis=0)
    np.save(train_output_path, weighted_train_features.astype(np.float32, copy=False))
    np.save(test_output_path, eval_features.astype(np.float32, copy=False))

    train_manifest = _training_manifest_rows(
        rows=train_rows,
        source_split=SOURCE_TRAIN_SPLIT,
        trainer_split=TRAINER_TRAIN_SPLIT,
        source_feature_file=train_source_file,
        trainer_feature_file=output_train_file,
        positive_weight=positive_weight,
        repeat_train=True,
    )
    test_manifest = _training_manifest_rows(
        rows=eval_rows,
        source_split=SOURCE_EVAL_SPLIT,
        trainer_split=TRAINER_TEST_SPLIT,
        source_feature_file=eval_source_file,
        trainer_feature_file=output_test_file,
        positive_weight=positive_weight,
        repeat_train=False,
    )
    real_positive_manifest = train_manifest + test_manifest
    _write_jsonl(output_dir / "real_positive_manifest.jsonl", real_positive_manifest)

    config_fragment = {
        "schema_version": SCHEMA_VERSION,
        "kind": "jts_real_positive_injection",
        "trainer": "livekit-wakeword",
        "model_name": model_name,
        "target_phrases": [target_phrase],
        "feature_files": {
            "positive_train": output_train_file,
            "positive_test": output_test_file,
        },
        "real_positive_weight": positive_weight,
        "note": (
            "This is a JTS config fragment, not a complete LiveKit config. "
            "Merge these positive feature files into a trainer workdir that "
            "also contains synthetic positives, negatives, background banks, "
            "and evaluation config."
        ),
    }
    (output_dir / "real_positive_injection.json").write_text(
        json.dumps(config_fragment, indent=2, sort_keys=True) + "\n"
    )
    _write_readme(output_dir / "README.md", target_phrase=target_phrase, model_name=model_name)

    result = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "prepare-wake-training-workdir",
        "output_dir": str(output_dir),
        "trainer_contract": {
            "trainer": "livekit-wakeword",
            "compatible_with": ["openWakeWord"],
            "feature_shape": [EXPECTED_FEATURE_FRAMES, EXPECTED_EMBEDDING_DIM],
            "positive_train": output_train_file,
            "positive_test": output_test_file,
            "source_eval_split_maps_to_trainer_test": True,
            "runtime_unchanged": True,
        },
        "configuration": {
            "target_phrase": target_phrase,
            "model_name": model_name,
            "positive_weight": positive_weight,
        },
        "source_feature_bank": {
            "path": str(feature_bank_dir),
            "feature_bank_sha256": _sha256(summary_path),
            "feature_manifest_sha256": _sha256(manifest_path),
            "features": {
                SOURCE_TRAIN_SPLIT: {
                    "path": train_source_file,
                    "sha256": _sha256(train_source_path),
                    "rows": int(train_features.shape[0]),
                },
                SOURCE_EVAL_SPLIT: {
                    "path": eval_source_file,
                    "sha256": _sha256(eval_source_path),
                    "rows": int(eval_features.shape[0]),
                },
            },
        },
        "counts": {
            "source_train_rows": int(train_features.shape[0]),
            "source_eval_rows": int(eval_features.shape[0]),
            "trainer_train_rows": int(weighted_train_features.shape[0]),
            "trainer_test_rows": int(eval_features.shape[0]),
            "manifest_rows": len(real_positive_manifest),
            "source_by_profile": _count_by(train_rows + eval_rows, "profile"),
            "source_by_leg": _count_by(train_rows + eval_rows, "leg"),
            "source_by_condition": _count_by(train_rows + eval_rows, "condition"),
            "source_by_distance": _count_by(train_rows + eval_rows, "distance"),
        },
        "artifacts": {
            "summary": "training_workdir.json",
            "config_fragment": "real_positive_injection.json",
            "real_positive_manifest": "real_positive_manifest.jsonl",
            "readme": "README.md",
            "features": {
                TRAINER_TRAIN_SPLIT: output_train_file,
                TRAINER_TEST_SPLIT: output_test_file,
            },
            "sha256": {
                "real_positive_injection.json": _sha256(
                    output_dir / "real_positive_injection.json"
                ),
                "real_positive_manifest.jsonl": _sha256(
                    output_dir / "real_positive_manifest.jsonl"
                ),
                output_train_file: _sha256(train_output_path),
                output_test_file: _sha256(test_output_path),
            },
        },
        "limitations": [
            "does not generate synthetic positives",
            "does not build negative or background feature banks",
            "does not train, export, evaluate, or register a model",
            "does not alter Pi runtime state",
        ],
        "next_steps": [
            "merge these real-positive arrays into a complete LiveKit training workdir",
            "run a tiny off-Pi train/eval loop",
            "compare the exported ONNX against the incumbent model on held-out JTS audio",
        ],
    }
    (output_dir / "training_workdir.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return result


def _model_name_from_phrase(phrase: str) -> str:
    raw = phrase.strip().lower()
    out = "".join(c if c.isalnum() else "_" for c in raw)
    out = "_".join(part for part in out.split("_") if part)
    return out or "wake_word"


def _print_summary(summary: dict[str, Any]) -> str:
    counts = summary["counts"]
    lines = [
        "Wake training workdir prep",
        "=" * 60,
        f"  output        : {summary['output_dir']}",
        f"  model         : {summary['configuration']['model_name']}",
        f"  phrase        : {summary['configuration']['target_phrase']}",
        f"  positive wt   : {summary['configuration']['positive_weight']}x train only",
        f"  source train  : {counts['source_train_rows']}",
        f"  source eval   : {counts['source_eval_rows']}",
        f"  trainer train : {counts['trainer_train_rows']}",
        f"  trainer test  : {counts['trainer_test_rows']}",
        "",
        "  by leg:",
    ]
    for leg, count in counts["source_by_leg"].items():
        lines.append(f"    {leg:<24} {count}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("feature_bank_dir", help="Feature bank directory")
    ap.add_argument(
        "output_dir",
        nargs="?",
        help="Output directory (default: <feature_bank_dir>/training-workdir)",
    )
    ap.add_argument(
        "--target-phrase",
        default=DEFAULT_TARGET_PHRASE,
        help=f"Wake phrase label for the config fragment (default: {DEFAULT_TARGET_PHRASE!r})",
    )
    ap.add_argument(
        "--model-name",
        help="Model name for the config fragment (default: slugified target phrase)",
    )
    ap.add_argument(
        "--positive-weight",
        type=int,
        default=DEFAULT_POSITIVE_WEIGHT,
        help=(
            "Integer train-positive repeat factor for real-positive up-weighting "
            f"(default: {DEFAULT_POSITIVE_WEIGHT}; eval/test remains unweighted)"
        ),
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Replace a non-empty output directory after safety checks",
    )
    args = ap.parse_args(argv)

    feature_bank_dir = Path(args.feature_bank_dir)
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else _default_output_dir(feature_bank_dir)
    )
    if _non_empty(output_dir):
        if not args.force:
            print(f"output directory is not empty: {output_dir} (pass --force)", file=sys.stderr)
            return 2
        if not _safe_to_remove_output(output_dir, feature_bank_dir=feature_bank_dir):
            print(f"refusing to remove unsafe output directory: {output_dir}", file=sys.stderr)
            return 2
        shutil.rmtree(output_dir)

    try:
        summary = prepare_training_workdir(
            feature_bank_dir,
            output_dir,
            target_phrase=args.target_phrase,
            model_name=args.model_name,
            positive_weight=args.positive_weight,
        )
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_print_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
