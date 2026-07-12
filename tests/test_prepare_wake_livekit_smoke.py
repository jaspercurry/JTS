# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/_prepare_wake_livekit_smoke.py."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "_prepare_wake_livekit_smoke.py"
)
_spec = importlib.util.spec_from_file_location("prepare_wake_livekit_smoke", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
smoke = importlib.util.module_from_spec(_spec)
sys.modules["prepare_wake_livekit_smoke"] = smoke
_spec.loader.exec_module(smoke)


def _write_training_workdir(root: Path) -> None:
    feature_dir = root / "feature_data"
    feature_dir.mkdir(parents=True, exist_ok=True)
    train = np.arange(3 * 16 * 96, dtype=np.float32).reshape(3, 16, 96)
    test = (np.arange(2 * 16 * 96, dtype=np.float32).reshape(2, 16, 96) + 5000)
    np.save(feature_dir / "positive_features_train.npy", train)
    np.save(feature_dir / "positive_features_test.npy", test)
    (root / "training_workdir.json").write_text(json.dumps({
        "schema_version": 1,
        "configuration": {
            "model_name": "hey_jarvis_jts",
            "target_phrase": "hey jarvis",
        },
        "artifacts": {
            "summary": "training_workdir.json",
            "features": {
                "train": "feature_data/positive_features_train.npy",
                "test": "feature_data/positive_features_test.npy",
            },
        },
    }) + "\n")


def test_prepares_livekit_smoke_workdir_with_placeholder_negatives(tmp_path: Path) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)
    out = tmp_path / "smoke"

    summary = smoke.prepare_livekit_smoke(training, out, steps=12, seed=7)

    model_name = "hey_jarvis_jts_smoke"
    model_dir = out / "livekit-output" / model_name
    assert np.load(model_dir / "positive_features_train.npy").shape == (3, 16, 96)
    assert np.load(model_dir / "positive_features_test.npy").shape == (2, 16, 96)
    assert np.load(model_dir / "negative_features_train.npy").shape == (16, 16, 96)
    assert np.load(model_dir / "negative_features_test.npy").shape == (16, 16, 96)
    assert summary["negative_features"]["train_source"] == "synthetic_embedding_placeholder"
    assert summary["negative_features"]["quality_evidence"] is False
    assert summary["livekit"]["model_type"] == "conv_attention"
    assert summary["livekit"]["model_size"] == "tiny"
    assert summary["livekit"]["steps"] == 12
    assert summary["run_livekit"]["requested"] is False
    assert summary["run_livekit"]["commands"][0] == [
        "livekit-wakeword",
        "train",
        str(out / "livekit_smoke_config.yaml"),
    ]
    config_text = (out / "livekit_smoke_config.yaml").read_text()
    assert not config_text.lstrip().startswith("{")
    assert f'model_name: "{model_name}"' in config_text
    assert '  - "hey jarvis"' in config_text
    assert f'output_dir: "{out / "livekit-output"}"' in config_text
    assert "batch_n_per_class:\n" in config_text
    assert "  positive: 3\n" in config_text
    assert "  adversarial_negative: 4\n" in config_text
    assert "  ACAV100M_sample: 0\n" in config_text
    assert "  background_noise: 0\n" in config_text
    assert (out / "README.md").is_file()
    assert (out / "livekit_smoke.json").is_file()


def test_accepts_operator_supplied_negative_features(tmp_path: Path) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)
    neg_train = tmp_path / "neg_train.npy"
    neg_test = tmp_path / "neg_test.npy"
    np.save(neg_train, np.ones((5, 16, 96), dtype=np.float32))
    np.save(neg_test, np.ones((6, 16, 96), dtype=np.float32))

    summary = smoke.prepare_livekit_smoke(
        training,
        tmp_path / "smoke",
        negative_train_features=neg_train,
        negative_test_features=neg_test,
    )

    assert summary["negative_features"]["train_source"] == "operator_supplied"
    assert summary["negative_features"]["test_source"] == "operator_supplied"
    assert summary["negative_features"]["quality_evidence"] is True
    model_dir = Path(summary["livekit"]["model_dir"])
    assert np.load(model_dir / "negative_features_train.npy").shape == (5, 16, 96)
    assert np.load(model_dir / "negative_features_test.npy").shape == (6, 16, 96)


def test_rejects_bad_negative_feature_shape(tmp_path: Path) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)
    neg_train = tmp_path / "neg_train.npy"
    np.save(neg_train, np.ones((5, 8, 96), dtype=np.float32))

    try:
        smoke.prepare_livekit_smoke(
            training,
            tmp_path / "smoke",
            negative_train_features=neg_train,
        )
    except ValueError as e:
        assert "expected (N, 16, 96)" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected bad shape rejection")


def test_rejects_too_few_steps(tmp_path: Path) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)

    try:
        smoke.prepare_livekit_smoke(training, tmp_path / "smoke", steps=3)
    except ValueError as e:
        assert "at least 10" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected step-count rejection")


def test_rejects_bad_negative_counts(tmp_path: Path) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)

    try:
        smoke.prepare_livekit_smoke(training, tmp_path / "smoke", negative_train_count=-1)
    except ValueError as e:
        assert "negative_train_count must be positive" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected negative count rejection")


def test_run_livekit_failure_still_writes_summary(tmp_path: Path, monkeypatch) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)
    out = tmp_path / "smoke"

    def fake_run(config_path: Path):
        return (
            [{
                "command": ["livekit-wakeword", "train", str(config_path)],
                "returncode": 127,
                "elapsed_sec": 0.0,
                "error": "missing executable",
            }],
            127,
        )

    monkeypatch.setattr(smoke, "_run_livekit_commands", fake_run)

    try:
        smoke.prepare_livekit_smoke(training, out, run_livekit=True)
    except RuntimeError as e:
        assert "failed with 127" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected run_livekit failure")

    summary = json.loads((out / "livekit_smoke.json").read_text())
    assert summary["run_livekit"]["requested"] is True
    assert summary["run_livekit"]["succeeded"] is False
    assert summary["run_livekit"]["results"][0]["returncode"] == 127


def test_force_remove_guard_only_allows_tool_owned_outputs(tmp_path: Path) -> None:
    training = tmp_path / "training"
    assert smoke._safe_to_remove_output(training / "livekit-smoke", training_workdir=training)
    assert not smoke._safe_to_remove_output(training, training_workdir=training)
    assert not smoke._safe_to_remove_output(tmp_path, training_workdir=training)
    assert not smoke._safe_to_remove_output(tmp_path / "custom", training_workdir=training)
    assert not smoke._safe_to_remove_output(Path.cwd(), training_workdir=training)

    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "livekit_smoke.json").write_text(json.dumps({
        "schema_version": smoke.SCHEMA_VERSION,
        "tool": "prepare-wake-livekit-smoke",
        "output_dir": str(custom),
        "artifacts": {
            "summary": "livekit_smoke.json",
            "config": "livekit_smoke_config.yaml",
        },
    }))
    assert smoke._safe_to_remove_output(custom, training_workdir=training)

    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / "livekit_smoke.json").write_bytes(
        (custom / "livekit_smoke.json").read_bytes()
    )
    assert not smoke._safe_to_remove_output(copied, training_workdir=training)

    alias = tmp_path / "alias"
    alias.symlink_to(custom, target_is_directory=True)
    assert not smoke._safe_to_remove_output(alias, training_workdir=training)


def test_cli_refuses_unsafe_force_path(tmp_path: Path, capsys) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "unrelated.txt").write_text("nope\n")

    rc = smoke.main([str(training), str(unsafe), "--force"])

    assert rc == 2
    assert "refusing to remove unsafe output directory" in capsys.readouterr().err


def test_livekit_marker_predicate_rejects_wrong_contract() -> None:
    valid = {
        "schema_version": smoke.SCHEMA_VERSION,
        "tool": "prepare-wake-livekit-smoke",
        "artifacts": {
            "summary": "livekit_smoke.json",
            "config": "livekit_smoke_config.yaml",
        },
    }
    assert smoke._looks_like_livekit_smoke_output(valid)
    for invalid in (
        {**valid, "schema_version": 2},
        {**valid, "tool": "other-tool"},
        {**valid, "artifacts": {"summary": "livekit_smoke.json"}},
    ):
        assert not smoke._looks_like_livekit_smoke_output(invalid)


def test_cli_force_symlink_refusal_never_calls_rmtree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    training = tmp_path / "training"
    _write_training_workdir(training)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep\n")
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    def unexpected_rmtree(_path: Path) -> None:
        raise AssertionError("rmtree must not run for a final symlink")

    monkeypatch.setattr(smoke.shutil, "rmtree", unexpected_rmtree)
    rc = smoke.main([str(training), str(alias), "--force"])

    assert rc == 2
    assert (target / "keep.txt").read_text() == "keep\n"
