# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavioural pins for the multi-room spike harness and analyzer.

The spike harness needs real Pis + snapcast to do anything useful, so
its tests exercise the laptop-side argument layer and the analyzer's pure
measurement helpers. The harness pin that matters: ``--reference-ethernet``
must actually be consumed. The flag was documented (script header +
multiroom-spike-measure.py both tell the operator to pass it) but for a while
was parsed into a variable nothing read — an operator's Ethernet best-case run
silently overwrote the WiFi cells in the shared results dir. It now redirects
RESULTS_DIR to a ``-ethernet-reference`` suffix and logs the redirect.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import wave
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from scripts import _sync_measure_audio

_SCRIPT = Path(__file__).parent.parent / "scripts" / "multiroom-spike.sh"
_MEASURE_SCRIPT = _SCRIPT.with_name("multiroom-spike-measure.py")


def _load_measure_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "multiroom_spike_measure",
        _MEASURE_SCRIPT,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MEASURE = _load_measure_script()


def _write_pcm_wav(
    path: Path,
    frames: list[tuple[int, ...]],
    *,
    sample_width: int = 2,
) -> None:
    dtype = {2: "<i2", 4: "<i4"}[sample_width]
    payload = np.asarray(frames, dtype=dtype).tobytes()
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(len(frames[0]))
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(48_000)
        wav_file.writeframes(payload)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    # The harness targets speakers, so _lib.sh requires one to be named
    # (#3498); these runs exit at the argument layer, before any network.
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PI_HOST": "jts9.invalid"},
    )


def test_no_action_prints_usage_and_exits_2():
    result = _run()
    assert result.returncode == 2
    assert "multiroom-spike.sh" in result.stderr
    # No reference run was requested; the redirect must not fire.
    assert "ethernet-reference" not in result.stderr


def test_reference_ethernet_flag_redirects_results_dir():
    """The flag's observable contract: results are rerouted to a
    dedicated ``<results>-ethernet-reference`` dir (announced on
    stderr) so the best-case Ethernet line can't clobber WiFi cells."""
    result = _run("--reference-ethernet")
    # Still no action given -> usage + exit 2, but the redirect runs
    # (and is announced) before the action check.
    assert result.returncode == 2
    assert "ethernet-reference" in result.stderr
    assert "REFERENCE-ETHERNET run" in result.stderr


def test_measure_wav_decode_delegates_without_changing_integer_downmix(tmp_path):
    wav_path = tmp_path / "stereo.wav"
    _write_pcm_wav(wav_path, [(1, 0), (-1, 0), (1_000, 3_000)])

    samples, sample_rate = MEASURE._read_wav_mono(wav_path)

    assert sample_rate == 48_000
    assert samples == [0, -1, 2_000]
    assert MEASURE.find_energy_onsets is _sync_measure_audio.find_energy_onsets


def test_measure_wav_decode_retains_16_bit_policy_and_error_text(tmp_path):
    wav_path = tmp_path / "mono-32.wav"
    _write_pcm_wav(wav_path, [(1,), (-1,)], sample_width=4)

    with pytest.raises(
        ValueError,
        match=r"^expected 16-bit WAV, got sampwidth=4$",
    ):
        MEASURE._read_wav_mono(wav_path)


def test_measure_onsets_retain_trailing_energy_window_and_refractory_rule():
    samples = [0] * 26_000
    samples[1_000:1_003] = [1, 1, 1]
    samples[25_000:25_003] = [1, 1, 1]

    assert MEASURE._find_burst_onsets(samples, 48_000) == [1_000, 25_001]
