# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for wake feature-bank tests."""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from jasper.wake_training import feature_bank as shared_features


class FakeExtractor:
    name = "fake"

    def __init__(self) -> None:
        self.batches: list[np.ndarray] = []

    def embed_clips(
        self,
        clips: np.ndarray,
        *,
        batch_size: int,
        ncpu: int,
    ) -> np.ndarray:
        del batch_size, ncpu
        self.batches.append(clips.copy())
        out = np.zeros((clips.shape[0], 16, 96), dtype=np.float32)
        for idx, clip in enumerate(clips):
            out[idx, :, :] = float(clip[-1])
        return out


def write_wav(
    path: Path,
    samples: np.ndarray,
    *,
    sample_rate: int = 16000,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.astype(np.int16).tobytes())


def write_bundle(root: Path, rows: list[dict[str, object]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "bundle.json").write_text(json.dumps({"schema_version": 1}) + "\n")
    with open(root / "manifest.jsonl", "w") as manifest:
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
            manifest.write(
                json.dumps({"schema_version": 1, **row}, sort_keys=True) + "\n"
            )
