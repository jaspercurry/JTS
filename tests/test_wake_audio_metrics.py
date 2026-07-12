# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the shared wake-analysis RMS metric and staged consumer."""
from __future__ import annotations

import importlib.util
import math
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from scripts._wake_audio_metrics import rms_amplitude


_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"


def _load_script(filename: str):
    name = f"da0224_{filename.removesuffix('.py').lstrip('_')}"
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_rms_amplitude_empty_is_zero() -> None:
    assert rms_amplitude(np.array([], dtype=np.int16)) == 0.0


@pytest.mark.parametrize(
    "samples",
    (
        np.array([3, 4], dtype=np.int16),
        np.array([-32768, 32767], dtype=np.int16),
        np.array([-(2**31), 2**31 - 1], dtype=np.int32),
        np.array([0.25, -0.5, 1.0], dtype=np.float32),
    ),
)
def test_rms_amplitude_matches_float64_reference_without_integer_overflow(
    samples: np.ndarray,
) -> None:
    expected = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    actual = rms_amplitude(samples)

    assert actual == expected
    assert math.isfinite(actual)


def test_rms_amplitude_preserves_nonfinite_propagation() -> None:
    assert math.isnan(rms_amplitude(np.array([1.0, np.nan])))
    assert math.isinf(rms_amplitude(np.array([1.0, np.inf])))


def test_identical_rms_consumers_alias_the_shared_function() -> None:
    consumers = (
        ("_audit_wake_events.py", "rms"),
        ("_offline_wake_count.py", "_rms"),
        ("_waveform_fusion_experiment.py", "_rms"),
        ("aec_erle_analyze.py", "_rms"),
    )
    for filename, attribute in consumers:
        module = _load_script(filename)
        assert getattr(module, attribute) is rms_amplitude


def test_audit_corpus_and_waveform_dbfs_consume_shared_rms(tmp_path: Path) -> None:
    audit = _load_script("_audit_wake_corpus.py")
    fusion = _load_script("_waveform_fusion_experiment.py")
    wav_path = tmp_path / "constant.wav"
    samples = np.full(160, 1200, dtype=np.int16)
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())

    assert audit._load_wav(wav_path).rms == rms_amplitude(samples)
    assert fusion._dbfs_from_rms(samples) == pytest.approx(
        20.0 * math.log10(rms_amplitude(samples) / 32768.0),
    )
    assert fusion._dbfs_from_rms(np.array([], dtype=np.int16)) == -100.0


def test_wake_rate_harness_validates_and_stages_shared_helper() -> None:
    source = (_SCRIPTS / "wake-rate-test.sh").read_text(encoding="utf-8")

    assert 'LOCAL_METRICS="$REPO_ROOT/scripts/_wake_audio_metrics.py"' in source
    assert 'if [[ ! -f "$LOCAL_METRICS" ]]' in source
    assert (
        'scp -q "$LOCAL_METRICS" '
        '"${PI_USER}@${PI_HOST}:/tmp/_wake_audio_metrics.py"'
    ) in source


def test_offline_counter_runs_standalone_with_staged_sibling(tmp_path: Path) -> None:
    counter = tmp_path / "_offline_wake_count.py"
    helper = tmp_path / "_wake_audio_metrics.py"
    shutil.copy2(_SCRIPTS / counter.name, counter)
    shutil.copy2(_SCRIPTS / helper.name, helper)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(counter), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run openWakeWord" in result.stdout
