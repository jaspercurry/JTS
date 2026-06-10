"""Tests for jasper.wake_training.feature_bank shared utilities."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from jasper.wake_training import feature_bank


class FakeExtractor:
    name = "fake"

    def embed_clips(self, clips: np.ndarray, *, batch_size: int, ncpu: int) -> np.ndarray:
        del batch_size, ncpu
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


def test_prepare_clip_verifies_hash_and_end_aligns(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    wav = bundle / "audio/train/raw0.wav"
    _write_wav(wav, np.array([1, 2, 3], dtype=np.int16))
    row = {
        "schema_version": 1,
        "split": "train",
        "clip_id": "clip-1",
        "bundle_path": "audio/train/raw0.wav",
        "sha256": feature_bank.sha256(wav),
    }

    prepared, rejection = feature_bank.prepare_clip(
        bundle_dir=bundle,
        row=row,
        total_samples=8,
    )

    assert rejection is None
    assert prepared is not None
    assert prepared.source_frames == 3
    assert prepared.samples.tolist() == [0, 0, 0, 0, 0, 1, 2, 3]


def test_prepare_clip_reports_hash_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    wav = bundle / "audio/train/raw0.wav"
    _write_wav(wav, np.array([1], dtype=np.int16))
    row = {
        "schema_version": 1,
        "split": "train",
        "clip_id": "clip-1",
        "bundle_path": "audio/train/raw0.wav",
        "sha256": "0" * 64,
    }

    prepared, rejection = feature_bank.prepare_clip(
        bundle_dir=bundle,
        row=row,
        total_samples=8,
    )

    assert prepared is None
    assert rejection is not None
    assert rejection["reason"] == "sha256_mismatch"
    assert rejection["actual_sha256"] == feature_bank.sha256(wav)


def test_extract_split_features_validates_shape_and_preserves_metadata() -> None:
    prepared = feature_bank.PreparedClip(
        row={
            "clip_id": "clip-1",
            "split": "train",
            "label_kind": "hard_negative",
            "phrase": "hey harvest",
            "leg": "raw0",
            "sha256": "a" * 64,
        },
        audio_path=Path("/tmp/raw0.wav"),
        samples=np.array([0, 9], dtype=np.int16),
        source_frames=2,
    )

    features, rows = feature_bank.extract_split_features(
        split="train",
        prepared=[prepared],
        extractor=FakeExtractor(),
        batch_size=8,
        ncpu=1,
        expected_feature_frames=16,
    )

    assert features.shape == (1, 16, 96)
    assert float(features[0, 0, 0]) == 9.0
    assert rows[0]["label_kind"] == "hard_negative"
    assert rows[0]["phrase"] == "hey harvest"
    assert rows[0]["source_sha256"] == "a" * 64


def test_parse_repeatable_csv() -> None:
    assert feature_bank.parse_repeatable_csv(["raw0, chip", "usb"]) == {
        "raw0",
        "chip",
        "usb",
    }
    assert feature_bank.parse_repeatable_csv(None) is None
