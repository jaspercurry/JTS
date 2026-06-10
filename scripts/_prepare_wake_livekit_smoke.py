#!/usr/bin/env python3
"""Prepare a tiny LiveKit wakeword smoke-training workdir.

This consumes the output from ``scripts/prepare-wake-training-workdir.sh`` and
creates a complete LiveKit-compatible model-output directory:

    <output>/livekit-output/<model_name>/
      positive_features_train.npy
      positive_features_test.npy
      negative_features_train.npy
      negative_features_test.npy

By default the negative arrays are deterministic embedding-space placeholders.
That is enough to prove the train/export/eval mechanics, but it is not model
quality evidence. Real negative-hours data remains a later pipeline stage.

The tool can optionally run ``livekit-wakeword train/export/eval`` when the
LiveKit CLI and its training dependencies are installed. It never touches Pi
runtime state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - exercised by CLI help paths.
    np = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
EXPECTED_FEATURE_SHAPE = (16, 96)
DEFAULT_STEPS = 20
DEFAULT_SEED = 20260609
DEFAULT_TARGET_FP_PER_HOUR = 1.0


def _require_numpy() -> None:
    if np is None:
        raise RuntimeError(
            "numpy is required to prepare a wake LiveKit smoke workdir. "
            "Run inside the JTS environment or install numpy."
        )


def _read_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_output_dir(training_workdir: Path) -> Path:
    return training_workdir / "livekit-smoke"


def _non_empty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def _safe_to_remove_output(path: Path, *, training_workdir: Path) -> bool:
    resolved = path.expanduser().resolve()
    training_resolved = training_workdir.expanduser().resolve()
    blocked = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
        training_resolved,
    }
    if resolved in blocked:
        return False
    if training_resolved in resolved.parents:
        rel = resolved.relative_to(training_resolved)
        return bool(rel.parts and rel.parts[0] == "livekit-smoke")
    return _looks_like_livekit_smoke_output(resolved)


def _looks_like_livekit_smoke_output(path: Path) -> bool:
    marker = path / "livekit_smoke.json"
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
        and artifacts.get("summary") == "livekit_smoke.json"
        and artifacts.get("config") == "livekit_smoke_config.yaml"
    )


def _feature_file(summary: dict[str, Any], split: str) -> str:
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("training_workdir.json missing artifacts object")
    features = artifacts.get("features")
    if not isinstance(features, dict):
        raise ValueError("training_workdir.json missing artifacts.features object")
    value = features.get(split)
    if not isinstance(value, str) or not value:
        raise ValueError(f"training_workdir.json missing feature file for split {split!r}")
    return value


def _load_features(path: Path, *, label: str) -> np.ndarray:
    _require_numpy()
    try:
        features = np.asarray(np.load(path), dtype=np.float32)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"failed to load {label} features from {path}: {e}") from e
    if features.ndim != 3 or features.shape[1:] != EXPECTED_FEATURE_SHAPE:
        raise ValueError(
            f"{path} has shape {features.shape}; expected (N, 16, 96)"
        )
    if features.shape[0] == 0:
        raise ValueError(f"{label} features are empty")
    return features


def _copy_or_load_external(
    path: Path | None,
    *,
    fallback: np.ndarray,
    label: str,
) -> tuple[np.ndarray, str]:
    if path is None:
        return fallback.astype(np.float32, copy=False), "synthetic_embedding_placeholder"
    return _load_features(path.expanduser().resolve(), label=label), "operator_supplied"


def _synthetic_negatives(
    positives: np.ndarray,
    *,
    count: int,
    seed: int,
) -> np.ndarray:
    """Create deterministic embedding-space smoke negatives.

    These are intentionally not realistic negatives. They are separated from
    positives enough to keep the LiveKit smoke train/eval loop quick and
    deterministic.
    """
    _require_numpy()
    rng = np.random.default_rng(seed)
    mean = positives.mean(axis=0, keepdims=True)
    std = positives.std(axis=0, keepdims=True)
    std = np.maximum(std, 0.05)
    noise = rng.normal(loc=0.0, scale=1.0, size=(count, *EXPECTED_FEATURE_SHAPE))
    return (mean - 3.0 * std + 0.25 * noise).astype(np.float32)


def _model_name_from_training(summary: dict[str, Any], override: str | None) -> str:
    if override:
        return _safe_token(override)
    config = summary.get("configuration")
    if isinstance(config, dict):
        model_name = str(config.get("model_name") or "")
        if model_name:
            return _safe_token(f"{model_name}_smoke")
    return "wakeword_smoke"


def _target_phrase_from_training(summary: dict[str, Any], override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    config = summary.get("configuration")
    if isinstance(config, dict):
        phrase = str(config.get("target_phrase") or "").strip()
        if phrase:
            return phrase
    return "hey jarvis"


def _safe_token(value: str) -> str:
    raw = value.strip().lower()
    out = "".join(c if c.isalnum() else "_" for c in raw)
    return "_".join(part for part in out.split("_") if part) or "wakeword_smoke"


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"unsupported YAML scalar type: {type(value).__name__}")


def _yaml_lines(value: Any, *, indent: int = 0) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError("YAML object keys must be non-empty strings")
            if isinstance(item, dict):
                if item:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_yaml_lines(item, indent=indent + 2))
                else:
                    lines.append(f"{pad}{key}: {{}}")
            elif isinstance(item, list):
                if item:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_yaml_lines(item, indent=indent + 2))
                else:
                    lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.extend(_yaml_lines(item, indent=indent + 2))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return lines or [f"{pad}[]"]
    return [f"{pad}{_yaml_scalar(value)}"]


def _write_config(
    path: Path,
    *,
    model_name: str,
    target_phrase: str,
    data_dir: Path,
    output_dir: Path,
    steps: int,
    batch_positive: int,
    batch_negative: int,
    model_type: str,
    model_size: str,
    target_fp_per_hour: float,
) -> None:
    config = {
        "model_name": model_name,
        "target_phrases": [target_phrase],
        "data_dir": str(data_dir),
        "output_dir": str(output_dir),
        "n_samples": 0,
        "n_samples_val": 0,
        "n_background_samples": 0,
        "n_background_samples_val": 0,
        "augmentation": {
            "clip_duration": 2.0,
            "batch_size": 1,
            "rounds": 1,
            "background_paths": [],
            "rir_paths": [],
        },
        "model": {
            "model_type": model_type,
            "model_size": model_size,
        },
        "steps": steps,
        "learning_rate": 0.001,
        "max_negative_weight": 20.0,
        "target_fp_per_hour": target_fp_per_hour,
        "batch_n_per_class": {
            "positive": batch_positive,
            "adversarial_negative": batch_negative,
            "ACAV100M_sample": 0,
            "background_noise": 0,
        },
    }
    path.write_text("\n".join(_yaml_lines(config)) + "\n")


def _write_readme(path: Path, *, model_name: str, target_phrase: str, run_livekit: bool) -> None:
    run_line = (
        "This run attempted LiveKit train/export/eval."
        if run_livekit
        else "Run the commands below after installing LiveKit training dependencies."
    )
    path.write_text(
        "\n".join([
            "# JTS LiveKit Wakeword Smoke Workdir",
            "",
            "This directory is a mechanical smoke test, not a quality model run.",
            "It proves the feature-file layout, LiveKit config, train/export/eval",
            "commands, and generated artifact contracts.",
            "",
            f"- model_name: `{model_name}`",
            f"- target_phrase: `{target_phrase}`",
            "- config: `livekit_smoke_config.yaml`",
            "- summary: `livekit_smoke.json`",
            "",
            run_line,
            "",
            "```sh",
            "livekit-wakeword train livekit_smoke_config.yaml",
            "livekit-wakeword export livekit_smoke_config.yaml --format onnx",
            "livekit-wakeword eval livekit_smoke_config.yaml",
            "```",
            "",
            "Do not deploy this model. Replace placeholder negatives with real",
            "negative-hours features before interpreting metrics.",
            "",
        ])
        + "\n"
    )


def _run_livekit_commands(config_path: Path) -> tuple[list[dict[str, Any]], int | None]:
    commands = [
        ["livekit-wakeword", "train", str(config_path)],
        ["livekit-wakeword", "export", str(config_path), "--format", "onnx"],
        ["livekit-wakeword", "eval", str(config_path)],
    ]
    results: list[dict[str, Any]] = []
    for command in commands:
        started = time.monotonic()
        try:
            completed = subprocess.run(command, check=False)  # noqa: S603
        except OSError as e:
            results.append({
                "command": command,
                "returncode": 127,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "error": str(e),
            })
            return results, 127
        results.append({
            "command": command,
            "returncode": completed.returncode,
            "elapsed_sec": round(time.monotonic() - started, 3),
        })
        if completed.returncode != 0:
            return results, completed.returncode
    return results, None


def prepare_livekit_smoke(
    training_workdir: Path,
    output_dir: Path,
    *,
    target_phrase: str | None = None,
    model_name: str | None = None,
    steps: int = DEFAULT_STEPS,
    seed: int = DEFAULT_SEED,
    negative_train_features: Path | None = None,
    negative_test_features: Path | None = None,
    negative_train_count: int | None = None,
    negative_test_count: int | None = None,
    model_type: str = "conv_attention",
    model_size: str = "tiny",
    target_fp_per_hour: float = DEFAULT_TARGET_FP_PER_HOUR,
    run_livekit: bool = False,
) -> dict[str, Any]:
    _require_numpy()
    training_workdir = training_workdir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if steps < 10:
        raise ValueError("steps must be at least 10 for the 3-phase LiveKit trainer")
    if model_type not in {"dnn", "rnn", "conv_attention"}:
        raise ValueError("model_type must be one of dnn, rnn, conv_attention")
    if model_size not in {"tiny", "small", "medium", "large"}:
        raise ValueError("model_size must be one of tiny, small, medium, large")
    if target_fp_per_hour <= 0:
        raise ValueError("target_fp_per_hour must be positive")
    if negative_train_count is not None and negative_train_count <= 0:
        raise ValueError("negative_train_count must be positive")
    if negative_test_count is not None and negative_test_count <= 0:
        raise ValueError("negative_test_count must be positive")

    summary_path = training_workdir / "training_workdir.json"
    if not summary_path.is_file():
        raise ValueError(f"{summary_path} not found")
    training_summary = _read_json(summary_path)
    if training_summary.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported training_workdir.json schema_version: "
            f"{training_summary.get('schema_version')!r}"
        )

    source_train_file = _feature_file(training_summary, "train")
    source_test_file = _feature_file(training_summary, "test")
    positive_train = _load_features(training_workdir / source_train_file, label="positive_train")
    positive_test = _load_features(training_workdir / source_test_file, label="positive_test")

    negative_train_count = negative_train_count or max(positive_train.shape[0] * 2, 16)
    negative_test_count = negative_test_count or max(positive_test.shape[0] * 4, 16)
    negative_train, negative_train_source = _copy_or_load_external(
        negative_train_features,
        fallback=_synthetic_negatives(
            positive_train,
            count=negative_train_count,
            seed=seed,
        ),
        label="negative_train",
    )
    negative_test, negative_test_source = _copy_or_load_external(
        negative_test_features,
        fallback=_synthetic_negatives(
            positive_test,
            count=negative_test_count,
            seed=seed + 1,
        ),
        label="negative_test",
    )

    model_name_resolved = _model_name_from_training(training_summary, model_name)
    target_phrase_resolved = _target_phrase_from_training(training_summary, target_phrase)
    data_dir = output_dir / "livekit-data"
    livekit_output_dir = output_dir / "livekit-output"
    model_dir = livekit_output_dir / model_name_resolved
    model_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    np.save(model_dir / "positive_features_train.npy", positive_train)
    np.save(model_dir / "positive_features_test.npy", positive_test)
    np.save(model_dir / "negative_features_train.npy", negative_train)
    np.save(model_dir / "negative_features_test.npy", negative_test)

    config_path = output_dir / "livekit_smoke_config.yaml"
    batch_positive = max(1, min(4, int(positive_train.shape[0])))
    batch_negative = max(1, min(4, int(negative_train.shape[0])))
    _write_config(
        config_path,
        model_name=model_name_resolved,
        target_phrase=target_phrase_resolved,
        data_dir=data_dir,
        output_dir=livekit_output_dir,
        steps=steps,
        batch_positive=batch_positive,
        batch_negative=batch_negative,
        model_type=model_type,
        model_size=model_size,
        target_fp_per_hour=target_fp_per_hour,
    )
    _write_readme(
        output_dir / "README.md",
        model_name=model_name_resolved,
        target_phrase=target_phrase_resolved,
        run_livekit=run_livekit,
    )

    livekit_results: list[dict[str, Any]] = []
    livekit_failure: int | None = None
    if run_livekit:
        livekit_results, livekit_failure = _run_livekit_commands(config_path)

    result = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "prepare-wake-livekit-smoke",
        "output_dir": str(output_dir),
        "training_workdir": {
            "path": str(training_workdir),
            "summary_sha256": _sha256(summary_path),
            "positive_train": source_train_file,
            "positive_test": source_test_file,
        },
        "livekit": {
            "config": "livekit_smoke_config.yaml",
            "data_dir": str(data_dir),
            "output_dir": str(livekit_output_dir),
            "model_dir": str(model_dir),
            "model_name": model_name_resolved,
            "target_phrase": target_phrase_resolved,
            "model_type": model_type,
            "model_size": model_size,
            "steps": steps,
            "target_fp_per_hour": target_fp_per_hour,
            "batch_n_per_class": {
                "positive": batch_positive,
                "adversarial_negative": batch_negative,
                "ACAV100M_sample": 0,
                "background_noise": 0,
            },
        },
        "counts": {
            "positive_train": int(positive_train.shape[0]),
            "positive_test": int(positive_test.shape[0]),
            "negative_train": int(negative_train.shape[0]),
            "negative_test": int(negative_test.shape[0]),
        },
        "negative_features": {
            "train_source": negative_train_source,
            "test_source": negative_test_source,
            "quality_evidence": (
                negative_train_source == "operator_supplied"
                and negative_test_source == "operator_supplied"
            ),
        },
        "artifacts": {
            "summary": "livekit_smoke.json",
            "config": "livekit_smoke_config.yaml",
            "readme": "README.md",
            "features": {
                "positive_train": f"livekit-output/{model_name_resolved}/positive_features_train.npy",
                "positive_test": f"livekit-output/{model_name_resolved}/positive_features_test.npy",
                "negative_train": f"livekit-output/{model_name_resolved}/negative_features_train.npy",
                "negative_test": f"livekit-output/{model_name_resolved}/negative_features_test.npy",
            },
            "sha256": {
                "livekit_smoke_config.yaml": _sha256(config_path),
                f"livekit-output/{model_name_resolved}/positive_features_train.npy": _sha256(
                    model_dir / "positive_features_train.npy"
                ),
                f"livekit-output/{model_name_resolved}/positive_features_test.npy": _sha256(
                    model_dir / "positive_features_test.npy"
                ),
                f"livekit-output/{model_name_resolved}/negative_features_train.npy": _sha256(
                    model_dir / "negative_features_train.npy"
                ),
                f"livekit-output/{model_name_resolved}/negative_features_test.npy": _sha256(
                    model_dir / "negative_features_test.npy"
                ),
            },
        },
        "run_livekit": {
            "requested": run_livekit,
            "succeeded": livekit_failure is None if run_livekit else None,
            "commands": [
                ["livekit-wakeword", "train", str(config_path)],
                ["livekit-wakeword", "export", str(config_path), "--format", "onnx"],
                ["livekit-wakeword", "eval", str(config_path)],
            ],
            "results": livekit_results,
        },
        "limitations": [
            "smoke-only harness; generated placeholder negatives are not real negative-hours data",
            "does not generate synthetic positive audio",
            "does not run cloud jobs unless the operator explicitly passes --run-livekit",
            "does not register, deploy, or activate a model",
            "does not alter Pi runtime state",
        ],
        "next_steps": [
            "run with --run-livekit on an off-Pi host with livekit-wakeword training extras installed",
            "replace placeholder negatives with real negative-hours feature banks before interpreting metrics",
            "compare exported ONNX against the incumbent on held-out JTS corpus audio",
        ],
    }
    (output_dir / "livekit_smoke.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    if livekit_failure is not None:
        raise RuntimeError(f"livekit-wakeword smoke command failed with {livekit_failure}")
    return result


def _print_summary(summary: dict[str, Any]) -> str:
    livekit = summary["livekit"]
    counts = summary["counts"]
    lines = [
        "Wake LiveKit smoke workdir",
        "=" * 60,
        f"  output        : {summary['output_dir']}",
        f"  model         : {livekit['model_name']}",
        f"  phrase        : {livekit['target_phrase']}",
        f"  config        : {summary['artifacts']['config']}",
        f"  model dir     : {livekit['model_dir']}",
        f"  positives     : train={counts['positive_train']} test={counts['positive_test']}",
        f"  negatives     : train={counts['negative_train']} test={counts['negative_test']}",
        f"  neg source    : {summary['negative_features']['train_source']}",
        f"  run livekit   : {summary['run_livekit']['requested']}",
        "",
        "  commands:",
    ]
    for command in summary["run_livekit"]["commands"]:
        lines.append(f"    {' '.join(command)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("training_workdir", help="Training workdir from prepare-wake-training-workdir")
    ap.add_argument(
        "output_dir",
        nargs="?",
        help="Output directory (default: <training_workdir>/livekit-smoke)",
    )
    ap.add_argument("--target-phrase", help="Override target phrase")
    ap.add_argument("--model-name", help="Override LiveKit model name")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS, help="Tiny trainer step count")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Synthetic negative seed")
    ap.add_argument("--negative-train-features", help="Optional real negative train .npy")
    ap.add_argument("--negative-test-features", help="Optional real negative test .npy")
    ap.add_argument("--negative-train-count", type=int, help="Synthetic train negative count")
    ap.add_argument("--negative-test-count", type=int, help="Synthetic test negative count")
    ap.add_argument(
        "--model-type",
        choices=["dnn", "rnn", "conv_attention"],
        default="conv_attention",
        help="LiveKit model type for the smoke run",
    )
    ap.add_argument(
        "--model-size",
        choices=["tiny", "small", "medium", "large"],
        default="tiny",
        help="LiveKit model size for the smoke run",
    )
    ap.add_argument(
        "--target-fp-per-hour",
        type=float,
        default=DEFAULT_TARGET_FP_PER_HOUR,
        help="LiveKit evaluation target FPPH for the smoke config",
    )
    ap.add_argument(
        "--run-livekit",
        action="store_true",
        help="After preparing files, run livekit-wakeword train/export/eval locally",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Replace a non-empty output directory after safety checks",
    )
    args = ap.parse_args(argv)

    training_workdir = Path(args.training_workdir)
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else _default_output_dir(training_workdir)
    )
    if _non_empty(output_dir):
        if not args.force:
            print(f"output directory is not empty: {output_dir} (pass --force)", file=sys.stderr)
            return 2
        if not _safe_to_remove_output(output_dir, training_workdir=training_workdir):
            print(f"refusing to remove unsafe output directory: {output_dir}", file=sys.stderr)
            return 2
        shutil.rmtree(output_dir)

    try:
        summary = prepare_livekit_smoke(
            training_workdir,
            output_dir,
            target_phrase=args.target_phrase,
            model_name=args.model_name,
            steps=args.steps,
            seed=args.seed,
            negative_train_features=(
                Path(args.negative_train_features) if args.negative_train_features else None
            ),
            negative_test_features=(
                Path(args.negative_test_features) if args.negative_test_features else None
            ),
            negative_train_count=args.negative_train_count,
            negative_test_count=args.negative_test_count,
            model_type=args.model_type,
            model_size=args.model_size,
            target_fp_per_hour=args.target_fp_per_hour,
            run_livekit=args.run_livekit,
        )
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(_print_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
