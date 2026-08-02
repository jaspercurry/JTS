# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import subprocess
import sys
import wave
from array import array
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "_wav_stats.py"
SPEC = importlib.util.spec_from_file_location("wav_stats", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
wav_stats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wav_stats
SPEC.loader.exec_module(wav_stats)


def _write_wav(path: Path, samples: list[int], *, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(array("h", samples).tobytes())


def test_read_wav_stats_reports_first_channel_pcm16(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    _write_wav(path, [1000, 30_000, -1000, -30_000], channels=2)

    stats = wav_stats.read_wav_stats(path)

    assert stats.frames == 2
    assert stats.channels == 2
    assert stats.mean == 0
    assert stats.rms == 1000
    assert stats.peak == 1000
    assert stats.duration_seconds == 2 / 16_000


def test_cli_sorts_directory_and_reports_invalid_wav(tmp_path: Path) -> None:
    _write_wav(tmp_path / "b.wav", [200, -200])
    _write_wav(tmp_path / "a.wav", [100, -100])
    (tmp_path / "broken.wav").write_bytes(b"not a wav")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--strict", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout.index("a.wav") < result.stdout.index("b.wav")
    assert "broken.wav: ERROR" in result.stderr


def test_capture_scripts_share_the_wav_stats_cli() -> None:
    for name in (
        "capture-chip-mic.sh",
        "capture-reference-condition.sh",
        "chip-aec-capture-comparison.sh",
    ):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "_wav_stats.py" in text
