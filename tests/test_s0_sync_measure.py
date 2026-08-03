# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import _sync_measure_audio


ROOT = Path(__file__).resolve().parents[1]


def load_s0_sync_measure():
    path = ROOT / "scripts" / "s0-sync-measure.py"
    spec = importlib.util.spec_from_file_location("s0_sync_measure", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


s0_sync_measure = load_s0_sync_measure()


def _write_pcm_wav(
    path: Path,
    frames: list[tuple[int, ...]],
    *,
    sample_width: int = 2,
    sample_rate: int = 48_000,
) -> None:
    dtype = {1: "u1", 2: "<i2", 4: "<i4"}[sample_width]
    payload = np.asarray(frames, dtype=dtype).tobytes()
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(len(frames[0]))
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(payload)


def test_s0_wav_decode_delegates_without_changing_normalized_downmix(tmp_path):
    wav_path = tmp_path / "stereo.wav"
    _write_pcm_wav(wav_path, [(32_767, -32_768), (1_000, 3_000)])

    samples, sample_rate = s0_sync_measure.read_wav_mono(wav_path)

    assert sample_rate == 48_000
    assert samples.dtype == np.float64
    assert samples.tolist() == pytest.approx([-0.5 / 32_768.0, 2_000 / 32_768.0])
    assert s0_sync_measure.find_energy_onsets is _sync_measure_audio.find_energy_onsets


def test_s0_wav_decode_retains_32_bit_support_and_error_text(tmp_path):
    wav_path = tmp_path / "mono-32.wav"
    _write_pcm_wav(wav_path, [(1_073_741_824,), (-1_073_741_824,)], sample_width=4)
    samples, _sample_rate = s0_sync_measure.read_wav_mono(wav_path)
    assert samples.tolist() == pytest.approx([0.5, -0.5])

    unsupported_path = tmp_path / "mono-8.wav"
    _write_pcm_wav(unsupported_path, [(128,)], sample_width=1)
    with pytest.raises(ValueError, match=r"^unsupported sample width 1$"):
        s0_sync_measure.read_wav_mono(unsupported_path)


def test_s0_onsets_retain_centered_energy_window_and_refractory_rule():
    samples = np.zeros(26_000, dtype=np.float64)
    samples[1_000:1_003] = 1.0
    samples[25_000:25_003] = 1.0

    assert s0_sync_measure.find_onsets(samples, 48_000) == [881, 24_882]


def _write_soak_log(tmp_path: Path, buffer_levels: list[str | None]) -> None:
    lines = []
    for index, buffer_level in enumerate(buffer_levels):
        fields = [
            f"ts={index}",
            "host=jts3",
            "camilla_xruns=0",
            "temp_c=40",
            "throttled=0x0",
            "load1=0.2",
            "camilla_pss_kb=5600",
            "snapclient_pss_kb=5000",
            "camilla_state=RUNNING",
            f"rate_adjust={1.0 + index / 1_000_000:.6f}",
        ]
        if buffer_level is not None:
            fields.append(f"buffer_level={buffer_level}")
        lines.append(" ".join(fields))
    (tmp_path / "soak-jts3.log").write_text("\n".join(lines) + "\n")


def _run_soak(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    args = SimpleNamespace(dir=str(tmp_path), min_sharpness=4.0)
    rc = s0_sync_measure.run_soak(args)
    return rc, capsys.readouterr().out


def test_all_zero_buffer_levels_fail_without_crashing(tmp_path, capsys):
    _write_soak_log(tmp_path, ["0", "0"])

    rc, out = _run_soak(tmp_path, capsys)

    assert rc == 2
    assert "buffer=0-0(mean 0.0)" in out
    assert "CLOCK-LOCK gate : FAIL/INCOMPLETE" in out
    assert "S0-SYNC VERDICT: FAIL" in out


def test_positive_then_zero_buffer_level_fails_lock(tmp_path, capsys):
    _write_soak_log(tmp_path, ["1024", "0"])

    rc, out = _run_soak(tmp_path, capsys)

    assert rc == 2
    assert "buffer=0-1024(mean 512.0)" in out
    assert "CHECK" in out
    assert "CLOCK-LOCK gate : FAIL/INCOMPLETE" in out


def test_negative_buffer_level_fails_lock(tmp_path, capsys):
    _write_soak_log(tmp_path, ["-1"])

    rc, out = _run_soak(tmp_path, capsys)

    assert rc == 2
    assert "buffer=-1--1(mean -1.0)" in out
    assert "CHECK" in out


def test_missing_and_unparseable_buffer_levels_are_unavailable(tmp_path, capsys):
    _write_soak_log(tmp_path, [None, "not-a-number"])

    rc, out = _run_soak(tmp_path, capsys)

    assert rc == 2
    assert "buffer=unavailable" in out
    assert "CHECK" in out
    assert "CLOCK-LOCK gate : FAIL/INCOMPLETE" in out


def test_healthy_positive_buffer_levels_pass_pending_acoustics(tmp_path, capsys):
    _write_soak_log(tmp_path, ["1024", "1025"])

    rc, out = _run_soak(tmp_path, capsys)

    assert rc == 0
    assert "buffer=1024-1025(mean 1024.5)" in out
    assert "LOCKED" in out
    assert "CLOCK-LOCK gate : PASS" in out
    assert "S0-SYNC VERDICT: PASS (telemetry)" in out
    assert "acoustic p99 PENDING" in out
