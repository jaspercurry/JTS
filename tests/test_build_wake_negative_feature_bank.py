# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/_build_wake_negative_feature_bank.py."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

from tests.wake_feature_bank_fixtures import (
    FakeExtractor,
    write_bundle as _write_bundle,
    write_wav as _write_wav,
)


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "_build_wake_negative_feature_bank.py"
)
_spec = importlib.util.spec_from_file_location("build_wake_negative_feature_bank", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
negative_builder = importlib.util.module_from_spec(_spec)
sys.modules["build_wake_negative_feature_bank"] = negative_builder
_spec.loader.exec_module(negative_builder)


def _row(
    *,
    split: str,
    leg: str,
    filename: str,
    label_kind: str = "hard_negative",
    phrase: str = "hey harvest",
    profile: str = "chip_aec_comparison_v1",
) -> dict[str, object]:
    return {
        "utterance_id": f"session-1:{filename}",
        "session_id": "session-1",
        "clip_id": filename,
        "seq": 1,
        "member": "jasper",
        "label_kind": label_kind,
        "phrase": phrase,
        "transcript": phrase,
        "profile": profile,
        "split": split,
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
        "duration_sec": 0.25,
        "sample_rate_hz": 16000,
        "channels": 1,
        "sample_width_bytes": 2,
        "frames": 4000,
        "sha256": filename,
        "bundle_path": f"audio/{split}/music/near/{leg}/{filename}.wav",
        "src_path": "",
    }


def test_negative_feature_bank_builds_train_and_eval_arrays(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    train_wav = bundle / "audio/train/music/near/chip_aec_150/train.wav"
    eval_wav = bundle / "audio/eval/music/near/chip_aec_210/eval.wav"
    _write_wav(train_wav, np.array([1, 2, 3], dtype=np.int16))
    _write_wav(eval_wav, np.array([4, 5, 6], dtype=np.int16))
    _write_bundle(
        bundle,
        [
            _row(
                split="train",
                leg="chip_aec_150",
                filename="train",
                label_kind="hard_negative",
                phrase="hey harvest",
            ),
            _row(
                split="eval",
                leg="chip_aec_210",
                filename="eval",
                label_kind="ambient_negative",
                phrase="living room music",
            ),
        ],
    )
    out = tmp_path / "negative-features"

    summary = negative_builder.build_negative_feature_bank(
        bundle,
        out,
        extractor=FakeExtractor(),
        batch_size=1,
    )

    assert summary["kind"] == "negative_feature_bank"
    assert summary["counts"]["feature_rows"] == 2
    assert summary["counts"]["selected_by_label_kind"] == {
        "ambient_negative": 1,
        "hard_negative": 1,
    }
    assert np.load(out / "negative_features_train.npy").shape == (1, 16, 96)
    assert np.load(out / "negative_features_eval.npy").shape == (1, 16, 96)
    rows = [
        json.loads(line)
        for line in (out / "negative_feature_manifest.jsonl").read_text().splitlines()
    ]
    assert {(row["split"], row["feature_file"], row["training_label"]) for row in rows} == {
        ("train", "negative_features_train.npy", "negative"),
        ("eval", "negative_features_eval.npy", "negative"),
    }
    assert {row["label_kind"] for row in rows} == {"hard_negative", "ambient_negative"}
    assert {row["wake_positive"] for row in rows} == {False}
    assert (out / "negative_feature_bank.json").is_file()


def test_filters_to_hard_negatives(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    for name in ("hard", "ambient", "positive"):
        _write_wav(
            bundle / f"audio/train/music/near/raw0/{name}.wav",
            np.array([1], dtype=np.int16),
        )
    _write_bundle(
        bundle,
        [
            _row(split="train", leg="raw0", filename="hard", label_kind="hard_negative"),
            _row(
                split="train",
                leg="raw0",
                filename="ambient",
                label_kind="ambient_negative",
            ),
            _row(split="train", leg="raw0", filename="positive", label_kind="positive"),
        ],
    )

    summary = negative_builder.build_negative_feature_bank(
        bundle,
        tmp_path / "features",
        label_kinds={"hard_negative"},
        extractor=FakeExtractor(),
    )

    assert summary["counts"]["selected_manifest_rows"] == 1
    assert summary["counts"]["selected_by_label_kind"] == {"hard_negative": 1}
    rows = [
        json.loads(line)
        for line in (tmp_path / "features" / "negative_feature_manifest.jsonl")
        .read_text()
        .splitlines()
    ]
    assert rows[0]["clip_id"] == "hard"


def test_unlabeled_rows_require_explicit_negative_only_override(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    wav = bundle / "audio/train/music/near/raw0/unlabeled.wav"
    _write_wav(wav, np.array([9], dtype=np.int16))
    row = _row(split="train", leg="raw0", filename="unlabeled", label_kind="")
    _write_bundle(bundle, [row])

    default_summary = negative_builder.build_negative_feature_bank(bundle, tmp_path / "default")
    assert default_summary["counts"]["selected_manifest_rows"] == 0
    assert default_summary["extraction"]["extractor"] == "not_used_no_selected_rows"
    assert np.load(tmp_path / "default" / "negative_features_train.npy").shape[0] == 0

    override_summary = negative_builder.build_negative_feature_bank(
        bundle,
        tmp_path / "override",
        allow_unlabeled_as="ambient_negative",
        extractor=FakeExtractor(),
    )
    assert override_summary["counts"]["selected_manifest_rows"] == 1
    assert override_summary["counts"]["selected_by_label_kind"] == {"ambient_negative": 1}


def test_rejects_positive_label_kind_options(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _write_bundle(bundle, [])

    try:
        negative_builder.build_negative_feature_bank(
            bundle,
            tmp_path / "features",
            label_kinds={"positive"},
            extractor=FakeExtractor(),
        )
    except ValueError as e:
        assert "non-wake label" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected positive label kind rejection")

    try:
        negative_builder.build_negative_feature_bank(
            bundle,
            tmp_path / "features2",
            allow_unlabeled_as="wake",
            extractor=FakeExtractor(),
        )
    except ValueError as e:
        assert "non-wake label" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected positive unlabeled override rejection")


def test_force_remove_guard_only_allows_tool_owned_outputs(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    assert negative_builder._safe_to_remove_output(
        bundle / "negative-feature-bank",
        bundle_dir=bundle,
    )
    assert not negative_builder._safe_to_remove_output(bundle, bundle_dir=bundle)
    assert not negative_builder._safe_to_remove_output(
        bundle / "audio" / "train",
        bundle_dir=bundle,
    )
    assert not negative_builder._safe_to_remove_output(tmp_path / "custom", bundle_dir=bundle)
    assert not negative_builder._safe_to_remove_output(Path.cwd(), bundle_dir=bundle)

    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "negative_feature_bank.json").write_text(json.dumps({
        "schema_version": negative_builder.SCHEMA_VERSION,
        "kind": "negative_feature_bank",
        "artifacts": {
            "summary": "negative_feature_bank.json",
            "feature_manifest": "negative_feature_manifest.jsonl",
        },
    }))
    assert negative_builder._safe_to_remove_output(custom, bundle_dir=bundle)
