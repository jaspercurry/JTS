# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/_build_wake_feature_bank.py."""
from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np

from jasper.wake_training import feature_bank as shared_features


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "_build_wake_feature_bank.py"
)
_spec = importlib.util.spec_from_file_location("build_wake_feature_bank", _SCRIPT)
assert _spec and _spec.loader, f"could not load spec for {_SCRIPT}"
builder = importlib.util.module_from_spec(_spec)
sys.modules["build_wake_feature_bank"] = builder
_spec.loader.exec_module(builder)


class FakeExtractor:
    name = "fake"

    def __init__(self) -> None:
        self.batches: list[np.ndarray] = []

    def embed_clips(self, clips: np.ndarray, *, batch_size: int, ncpu: int) -> np.ndarray:
        del batch_size, ncpu
        self.batches.append(clips.copy())
        out = np.zeros((clips.shape[0], 16, 96), dtype=np.float32)
        for idx, clip in enumerate(clips):
            out[idx, :, :] = float(clip[-1])
        return out


def _write_wav(path: Path, samples: np.ndarray, *, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.astype(np.int16).tobytes())


def _write_bundle(root: Path, rows: list[dict[str, object]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "bundle.json").write_text(json.dumps({"schema_version": 1}) + "\n")
    with open(root / "manifest.jsonl", "w") as f:
        for row in rows:
            bundle_path = str(row.get("bundle_path") or "")
            wav_path = root / bundle_path if bundle_path else None
            existing_sha = str(row.get("sha256") or "")
            existing_is_hex_sha = (
                len(existing_sha) == 64
                and all(c in "0123456789abcdefABCDEF" for c in existing_sha)
            )
            if wav_path is not None and wav_path.is_file() and not existing_is_hex_sha:
                row = {**row, "sha256": shared_features.sha256(wav_path)}
            f.write(json.dumps({"schema_version": 1, **row}, sort_keys=True) + "\n")


def _row(
    *,
    split: str,
    leg: str,
    filename: str,
    profile: str = "chip_aec_comparison_v1",
) -> dict[str, object]:
    return {
        "utterance_id": f"session-1:{filename}",
        "session_id": "session-1",
        "clip_id": filename,
        "seq": 1,
        "member": "jasper",
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


def test_feature_bank_builds_train_and_eval_arrays(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    train_wav = bundle / "audio/train/music/near/chip_aec_150/train.wav"
    eval_wav = bundle / "audio/eval/music/near/chip_aec_210/eval.wav"
    _write_wav(train_wav, np.array([1, 2, 3], dtype=np.int16))
    _write_wav(eval_wav, np.array([4, 5, 6], dtype=np.int16))
    _write_bundle(
        bundle,
        [
            _row(split="train", leg="chip_aec_150", filename="train"),
            _row(split="eval", leg="chip_aec_210", filename="eval"),
        ],
    )
    extractor = FakeExtractor()
    out = tmp_path / "features"

    summary = builder.build_feature_bank(bundle, out, extractor=extractor, batch_size=1)

    assert summary["counts"]["feature_rows"] == 2
    train_features = np.load(out / "positive_features_train.npy")
    eval_features = np.load(out / "positive_features_eval.npy")
    assert train_features.shape == (1, 16, 96)
    assert eval_features.shape == (1, 16, 96)
    assert float(train_features[0, 0, 0]) == 3.0
    assert float(eval_features[0, 0, 0]) == 6.0
    rows = [
        json.loads(line)
        for line in (out / "feature_manifest.jsonl").read_text().splitlines()
    ]
    assert {(row["split"], row["feature_file"]) for row in rows} == {
        ("train", "positive_features_train.npy"),
        ("eval", "positive_features_eval.npy"),
    }
    assert (out / "feature_bank.json").is_file()


def test_end_aligns_short_clips_and_truncates_long_clips(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    short_wav = bundle / "audio/train/music/near/raw0/short.wav"
    long_wav = bundle / "audio/train/music/near/raw0/long.wav"
    _write_wav(short_wav, np.array([7, 8], dtype=np.int16))
    _write_wav(long_wav, np.arange(32005, dtype=np.int16))
    _write_bundle(
        bundle,
        [
            _row(split="train", leg="raw0", filename="short"),
            _row(split="train", leg="raw0", filename="long"),
        ],
    )
    extractor = FakeExtractor()

    builder.build_feature_bank(bundle, tmp_path / "features", extractor=extractor, batch_size=8)

    batch = extractor.batches[0]
    assert batch.shape == (2, 32000)
    assert batch[0, -2:].tolist() == [7, 8]
    assert np.all(batch[0, :-2] == 0)
    assert batch[1, 0] == np.arange(32005, dtype=np.int16)[-32000]
    assert batch[1, -1] == np.arange(32005, dtype=np.int16)[-1]


def test_filters_leg_and_profile(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    for leg in ("chip_aec_150", "raw0", "usb_raw"):
        _write_wav(
            bundle / f"audio/train/music/near/{leg}/{leg}.wav",
            np.array([1], dtype=np.int16),
        )
    _write_bundle(
        bundle,
        [
            _row(split="train", leg="chip_aec_150", filename="chip_aec_150"),
            _row(split="train", leg="raw0", filename="raw0"),
            _row(
                split="train",
                leg="usb_raw",
                filename="usb_raw",
                profile="usb_mic_comparison_v1",
            ),
        ],
    )

    summary = builder.build_feature_bank(
        bundle,
        tmp_path / "features",
        legs={"chip_aec_150", "usb_raw"},
        profiles={"chip_aec_comparison_v1"},
        extractor=FakeExtractor(),
    )

    assert summary["counts"]["selected_manifest_rows"] == 1
    assert summary["counts"]["selected_by_leg"] == {"chip_aec_150": 1}
    assert np.load(tmp_path / "features" / "positive_features_train.npy").shape[0] == 1


def test_rejects_missing_or_bad_wavs_without_stopping_other_features(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    good = bundle / "audio/train/music/near/raw0/good.wav"
    bad = bundle / "audio/train/music/near/raw0/bad.wav"
    _write_wav(good, np.array([1], dtype=np.int16))
    _write_wav(bad, np.array([1], dtype=np.int16), sample_rate=8000)
    rows = [
        _row(split="train", leg="raw0", filename="good"),
        _row(split="train", leg="raw0", filename="bad"),
        _row(split="train", leg="raw0", filename="missing"),
    ]
    _write_bundle(bundle, rows)

    summary = builder.build_feature_bank(bundle, tmp_path / "features", extractor=FakeExtractor())

    assert summary["counts"]["feature_rows"] == 1
    assert summary["counts"]["rejections"] == 2
    rejections = [
        json.loads(line)
        for line in (tmp_path / "features" / "feature_rejections.jsonl").read_text().splitlines()
    ]
    assert {item["reason"] for item in rejections} == {"wrong_wav_format", "missing_wav"}


def test_rejects_wav_hash_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    wav = bundle / "audio/train/music/near/raw0/changed.wav"
    _write_wav(wav, np.array([1], dtype=np.int16))
    row = _row(split="train", leg="raw0", filename="changed")
    row["sha256"] = "0" * 64
    _write_bundle(bundle, [row])

    summary = builder.build_feature_bank(bundle, tmp_path / "features", extractor=FakeExtractor())

    assert summary["counts"]["feature_rows"] == 0
    assert summary["counts"]["rejections"] == 1
    rejection = json.loads(
        (tmp_path / "features" / "feature_rejections.jsonl").read_text()
    )
    assert rejection["reason"] == "sha256_mismatch"
    assert rejection["expected_sha256"] == "0" * 64
    assert rejection["actual_sha256"] == shared_features.sha256(wav)


def test_force_remove_guard_only_allows_tool_owned_outputs(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    assert builder._safe_to_remove_output(bundle / "feature-bank", bundle_dir=bundle)
    assert not builder._safe_to_remove_output(bundle, bundle_dir=bundle)
    assert not builder._safe_to_remove_output(bundle / "audio" / "train", bundle_dir=bundle)
    assert not builder._safe_to_remove_output(tmp_path / "custom", bundle_dir=bundle)
    assert not builder._safe_to_remove_output(Path.cwd(), bundle_dir=bundle)

    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "feature_bank.json").write_text(json.dumps({
        "schema_version": builder.SCHEMA_VERSION,
        "artifacts": {
            "summary": "feature_bank.json",
            "feature_manifest": "feature_manifest.jsonl",
        },
    }))
    assert builder._safe_to_remove_output(custom, bundle_dir=bundle)
