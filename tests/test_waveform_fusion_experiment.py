from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "_waveform_fusion_experiment.py"
_SPEC = importlib.util.spec_from_file_location("waveform_fusion_experiment", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
fusion = importlib.util.module_from_spec(_SPEC)
sys.modules["waveform_fusion_experiment"] = fusion
_SPEC.loader.exec_module(fusion)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.astype(np.int16).tobytes())


def test_shift_samples_positive_delay_preserves_length():
    samples = np.array([1, 2, 3, 4], dtype=np.float64)

    shifted = fusion.shift_samples(samples, 2)

    assert shifted.tolist() == [0, 0, 1, 2]


def test_shift_samples_negative_delay_preserves_length():
    samples = np.array([1, 2, 3, 4], dtype=np.float64)

    shifted = fusion.shift_samples(samples, -2)

    assert shifted.tolist() == [3, 4, 0, 0]


def test_mix_pair_applies_clipping_guard():
    a = np.full(1600, 32000, dtype=np.int16)
    b = np.full(1600, 32000, dtype=np.int16)

    mixed, guard_gain = fusion.mix_pair(
        a,
        b,
        delay_ms=0,
        weight_a=1.0,
        weight_b=1.0,
        normalization="native",
    )

    assert guard_gain < 1.0
    assert np.max(np.abs(mixed)) <= 32767.01


def test_run_no_score_generates_mix_outputs(tmp_path):
    corpus = tmp_path / "enrollment_positives"
    on = corpus / "aec_on_nomusic" / "clip-on.wav"
    dtln = corpus / "aec_dtln_nomusic" / "clip-dtln.wav"
    samples = np.zeros(1600, dtype=np.int16)
    samples[100:140] = 1000
    _write_wav(on, samples)
    _write_wav(dtln, samples)
    metadata = {
        "session_id": "20260528T000000Z-test",
        "clips": [
            {
                "seq": 1,
                "condition": "music",
                "distance": "far",
                "files": {"on": str(on), "dtln": str(dtln)},
            }
        ],
    }
    metadata_dir = corpus / "metadata"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "enroll_jasper_20260528T000000Z-test.json").write_text(
        json.dumps(metadata)
    )
    out_dir = tmp_path / "fusion-out"

    rc = fusion.main(
        [
            "--corpus-dir",
            str(corpus),
            "--session",
            "20260528T000000Z-test",
            "--out-dir",
            str(out_dir),
            "--no-score",
            "--pair",
            "xvf:on:dtln",
            "--delays-ms",
            "0",
            "--weights",
            "0.5/0.5",
            "--normalization",
            "native",
        ]
    )

    assert rc == 0
    assert (out_dir / "waveform_fusion_scores.csv").is_file()
    assert (out_dir / "summary.md").read_text().startswith("# Wake Waveform Fusion")
    mixes = list((out_dir / "xvf").glob("*.wav"))
    assert len(mixes) == 1
