# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/_prepare_wake_training_workdir.py."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "_prepare_wake_training_workdir.py"
)
_spec = importlib.util.spec_from_file_location("prepare_wake_training_workdir", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
prep = importlib.util.module_from_spec(_spec)
sys.modules["prepare_wake_training_workdir"] = prep
_spec.loader.exec_module(prep)


def _feature_row(
    *,
    split: str,
    feature_index: int,
    feature_file: str,
    leg: str = "chip_aec_150",
    profile: str = "chip_aec_comparison_v1",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "split": split,
        "feature_index": feature_index,
        "feature_file": feature_file,
        "manifest_line": feature_index + 1,
        "utterance_id": f"session-1:{split}:{feature_index}",
        "session_id": "session-1",
        "clip_id": f"{split}-{feature_index}",
        "seq": feature_index + 1,
        "member": "jasper",
        "profile": profile,
        "condition": "music",
        "distance": "near",
        "leg": leg,
        "leg_label": leg,
        "device_id": "xvf3800",
        "native_stream": leg,
        "source_channel": "fixed",
        "processing": "hardware_aec",
        "profile_role": "production_wake",
        "wake_input": True,
        "source_sha256": f"sha-{split}-{feature_index}",
        "source_duration_sec": 1.2,
        "alignment": "end_aligned",
    }


def _write_feature_bank(
    root: Path,
    *,
    train: np.ndarray | None = None,
    eval_: np.ndarray | None = None,
    train_rows: list[dict[str, object]] | None = None,
    eval_rows: list[dict[str, object]] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    train = (
        np.arange(2 * 16 * 96, dtype=np.float32).reshape(2, 16, 96)
        if train is None
        else train
    )
    eval_ = (
        np.arange(1 * 16 * 96, dtype=np.float32).reshape(1, 16, 96) + 10000
        if eval_ is None
        else eval_
    )
    np.save(root / "positive_features_train.npy", train)
    np.save(root / "positive_features_eval.npy", eval_)

    train_rows = train_rows if train_rows is not None else [
        _feature_row(
            split="train",
            feature_index=idx,
            feature_file="positive_features_train.npy",
            leg="chip_aec_150" if idx == 0 else "chip_aec_210",
        )
        for idx in range(train.shape[0])
    ]
    eval_rows = eval_rows if eval_rows is not None else [
        _feature_row(
            split="eval",
            feature_index=idx,
            feature_file="positive_features_eval.npy",
            leg="chip_aec_210",
        )
        for idx in range(eval_.shape[0])
    ]
    with open(root / "feature_manifest.jsonl", "w") as f:
        for row in [*train_rows, *eval_rows]:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    (root / "feature_bank.json").write_text(json.dumps({
        "schema_version": 1,
        "extraction": {"feature_shape": [16, 96]},
        "artifacts": {
            "summary": "feature_bank.json",
            "feature_manifest": "feature_manifest.jsonl",
            "features": {
                "train": "positive_features_train.npy",
                "eval": "positive_features_eval.npy",
            },
        },
    }) + "\n")


def test_prepares_livekit_positive_files_with_weighted_train_only(tmp_path: Path) -> None:
    feature_bank = tmp_path / "feature-bank"
    _write_feature_bank(feature_bank)
    out = tmp_path / "workdir"

    summary = prep.prepare_training_workdir(
        feature_bank,
        out,
        target_phrase="hey jarvis",
        model_name="hey_jarvis_jts",
        positive_weight=3,
    )

    train = np.load(out / "feature_data" / "positive_features_train.npy")
    test = np.load(out / "feature_data" / "positive_features_test.npy")
    assert train.shape == (6, 16, 96)
    assert test.shape == (1, 16, 96)
    source_train = np.load(feature_bank / "positive_features_train.npy")
    assert np.array_equal(train[0], source_train[0])
    assert np.array_equal(train[1], source_train[0])
    assert np.array_equal(train[2], source_train[0])
    assert np.array_equal(train[3], source_train[1])
    assert summary["counts"]["source_train_rows"] == 2
    assert summary["counts"]["trainer_train_rows"] == 6
    assert summary["counts"]["trainer_test_rows"] == 1
    assert summary["trainer_contract"]["positive_test"] == (
        "feature_data/positive_features_test.npy"
    )
    assert summary["trainer_contract"]["source_eval_split_maps_to_trainer_test"] is True

    manifest_rows = [
        json.loads(line)
        for line in (out / "real_positive_manifest.jsonl").read_text().splitlines()
    ]
    assert len(manifest_rows) == 7
    assert [row["repeat_index"] for row in manifest_rows[:3]] == [0, 1, 2]
    assert {row["trainer_split"] for row in manifest_rows} == {"train", "test"}
    test_rows = [row for row in manifest_rows if row["trainer_split"] == "test"]
    assert test_rows[0]["source_split"] == "eval"
    assert test_rows[0]["positive_weight"] == 1
    assert test_rows[0]["feature_manifest_line"] == 3
    assert test_rows[0]["source_manifest_line"] == 1

    injection = json.loads((out / "real_positive_injection.json").read_text())
    assert injection["target_phrases"] == ["hey jarvis"]
    assert injection["feature_files"]["positive_train"] == (
        "feature_data/positive_features_train.npy"
    )
    assert (out / "README.md").is_file()
    assert (out / "training_workdir.json").is_file()
    assert "feature_data/positive_features_train.npy" in summary["artifacts"]["sha256"]


def test_rejects_manifest_feature_count_mismatch(tmp_path: Path) -> None:
    feature_bank = tmp_path / "feature-bank"
    _write_feature_bank(
        feature_bank,
        train_rows=[
            _feature_row(
                split="train",
                feature_index=0,
                feature_file="positive_features_train.npy",
            )
        ],
    )

    try:
        prep.prepare_training_workdir(feature_bank, tmp_path / "workdir")
    except ValueError as e:
        assert "has 1 train rows" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected manifest count mismatch")


def test_rejects_manifest_feature_file_mismatch(tmp_path: Path) -> None:
    feature_bank = tmp_path / "feature-bank"
    _write_feature_bank(
        feature_bank,
        eval_rows=[
            _feature_row(
                split="eval",
                feature_index=0,
                feature_file="unexpected.npy",
            )
        ],
    )

    try:
        prep.prepare_training_workdir(feature_bank, tmp_path / "workdir")
    except ValueError as e:
        assert "points at 'unexpected.npy'" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected manifest feature file mismatch")


def test_rejects_bad_feature_shape(tmp_path: Path) -> None:
    feature_bank = tmp_path / "feature-bank"
    _write_feature_bank(
        feature_bank,
        train=np.zeros((1, 8, 96), dtype=np.float32),
        train_rows=[
            _feature_row(
                split="train",
                feature_index=0,
                feature_file="positive_features_train.npy",
            )
        ],
    )
    feature_summary = json.loads((feature_bank / "feature_bank.json").read_text())
    feature_summary["extraction"]["feature_shape"] = [8, 96]
    (feature_bank / "feature_bank.json").write_text(json.dumps(feature_summary) + "\n")

    try:
        prep.prepare_training_workdir(feature_bank, tmp_path / "workdir")
    except ValueError as e:
        assert "expected [16, 96]" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected feature shape rejection")


def test_rejects_empty_eval_positives(tmp_path: Path) -> None:
    feature_bank = tmp_path / "feature-bank"
    _write_feature_bank(
        feature_bank,
        eval_=np.empty((0, 16, 96), dtype=np.float32),
        eval_rows=[],
    )

    try:
        prep.prepare_training_workdir(feature_bank, tmp_path / "workdir")
    except ValueError as e:
        assert "no eval positives" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected empty eval rejection")


def test_force_remove_guard_only_allows_tool_owned_outputs(tmp_path: Path) -> None:
    feature_bank = tmp_path / "feature-bank"
    assert prep._safe_to_remove_output(
        feature_bank / "training-workdir",
        feature_bank_dir=feature_bank,
    )
    assert not prep._safe_to_remove_output(feature_bank, feature_bank_dir=feature_bank)
    assert not prep._safe_to_remove_output(tmp_path, feature_bank_dir=feature_bank)
    assert not prep._safe_to_remove_output(tmp_path / "custom", feature_bank_dir=feature_bank)
    assert not prep._safe_to_remove_output(Path.cwd(), feature_bank_dir=feature_bank)

    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "training_workdir.json").write_text(json.dumps({
        "schema_version": prep.SCHEMA_VERSION,
        "tool": "prepare-wake-training-workdir",
        "output_dir": str(custom),
        "artifacts": {
            "summary": "training_workdir.json",
            "real_positive_manifest": "real_positive_manifest.jsonl",
        },
    }))
    assert prep._safe_to_remove_output(custom, feature_bank_dir=feature_bank)

    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / "training_workdir.json").write_bytes(
        (custom / "training_workdir.json").read_bytes()
    )
    assert not prep._safe_to_remove_output(copied, feature_bank_dir=feature_bank)

    alias = tmp_path / "alias"
    alias.symlink_to(custom, target_is_directory=True)
    assert not prep._safe_to_remove_output(alias, feature_bank_dir=feature_bank)


def test_cli_refuses_unsafe_force_path(tmp_path: Path, capsys) -> None:
    feature_bank = tmp_path / "feature-bank"
    _write_feature_bank(feature_bank)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "unrelated.txt").write_text("nope\n")

    rc = prep.main([str(feature_bank), str(unsafe), "--force"])

    assert rc == 2
    assert "refusing to remove unsafe output directory" in capsys.readouterr().err


def test_training_workdir_marker_predicate_rejects_wrong_contract() -> None:
    valid = {
        "schema_version": prep.SCHEMA_VERSION,
        "tool": "prepare-wake-training-workdir",
        "artifacts": {
            "summary": "training_workdir.json",
            "real_positive_manifest": "real_positive_manifest.jsonl",
        },
    }
    assert prep._looks_like_training_workdir_output(valid)
    for invalid in (
        {**valid, "schema_version": 2},
        {**valid, "tool": "other-tool"},
        {**valid, "artifacts": {"summary": "training_workdir.json"}},
    ):
        assert not prep._looks_like_training_workdir_output(invalid)


def test_cli_force_symlink_refusal_never_calls_rmtree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    feature_bank = tmp_path / "feature-bank"
    _write_feature_bank(feature_bank)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep\n")
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    def unexpected_rmtree(_path: Path) -> None:
        raise AssertionError("rmtree must not run for a final symlink")

    monkeypatch.setattr(prep.shutil, "rmtree", unexpected_rmtree)
    rc = prep.main([str(feature_bank), str(alias), "--force"])

    assert rc == 2
    assert (target / "keep.txt").read_text() == "keep\n"
