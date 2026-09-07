# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the shared multi-room broadband click generator."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import textwrap
import wave

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "_make_click_track.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("make_click_track", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


click_track = _load_helper()


def test_raw_and_wav_modes_emit_identical_bounded_pcm(tmp_path: Path) -> None:
    raw_path = tmp_path / "click.raw"
    wav_path = tmp_path / "click.wav"

    click_track.render_click_track(raw_path, container="raw")
    click_track.render_click_track(wav_path, container="wav")

    raw = raw_path.read_bytes()
    expected_frames = click_track.SAMPLE_RATE * click_track.REPETITIONS
    assert len(raw) == (
        expected_frames * click_track.CHANNELS * click_track.SAMPLE_WIDTH_BYTES
    )
    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getnchannels() == click_track.CHANNELS
        assert wav.getsampwidth() == click_track.SAMPLE_WIDTH_BYTES
        assert wav.getframerate() == click_track.SAMPLE_RATE
        assert wav.getnframes() == expected_frames
        assert wav.readframes(expected_frames) == raw

    first_second = raw[: click_track.SAMPLE_RATE * 4]
    second_second = raw[click_track.SAMPLE_RATE * 4 : click_track.SAMPLE_RATE * 8]
    assert first_second == second_second
    left, right = zip(*struct.iter_unpack("<hh", first_second[: 96 * 4]))
    assert left == right
    expected_peak = int(32767 * (10 ** (click_track.AMPLITUDE_DBFS / 20)))
    assert max(abs(sample) for sample in left) == expected_peak
    assert set(first_second[96 * 4 :]) == {0}


def test_cli_runs_from_foreign_cwd(tmp_path: Path) -> None:
    output = tmp_path / "generated.raw"
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--format",
            "raw",
            "--output",
            str(output),
        ],
        cwd=foreign_cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert output.stat().st_size == click_track.SAMPLE_RATE * 4 * 60
    assert "60.0s @48k S16 stereo" in result.stdout


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("remote_status", (0, 37))
def test_bench_callers_stream_helper_over_ssh_and_propagate_failure(
    tmp_path: Path,
    remote_status: int,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("multiroom-spike.sh", "_make_click_track.py", "_lib.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "click-finished"
    output = tmp_path / "remote.wav"
    log = tmp_path / "ssh.log"
    _write_executable(
        fake_bin / "ssh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            cmd="${*: -1}"
            printf '%s\n' "$cmd" >> "$FAKE_SSH_LOG"
            if [[ -f "$FAKE_CLICK_FINISHED" ]]; then
                exit 23
            fi
            if [[ "$cmd" == "command -v sox >/dev/null 2>&1" ]]; then
                exit 1
            fi
            if [[ "$cmd" == *"python3 - --format "* ]]; then
                if [[ "$FAKE_REMOTE_CLICK_STATUS" != 0 ]]; then
                    exit "$FAKE_REMOTE_CLICK_STATUS"
                fi
                python3 - --format wav --output "$FAKE_CLICK_OUTPUT"
                touch "$FAKE_CLICK_FINISHED"
            fi
            """
        ),
    )
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PI_HOST": "leader.invalid",
            "PI_USER": "pi",
            "FAKE_SSH_LOG": str(log),
            "FAKE_CLICK_FINISHED": str(marker),
            "FAKE_CLICK_OUTPUT": str(output),
            "FAKE_REMOTE_CLICK_STATUS": str(remote_status),
        }
    )

    result = subprocess.run(
        ["bash", str(scripts / "multiroom-spike.sh"), "--setup", "--leader",
         "leader.invalid"],
        cwd=foreign_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == (23 if remote_status == 0 else remote_status)
    assert "--format wav" in log.read_text(encoding="utf-8")
    if remote_status:
        assert not output.exists()
        return
    assert output.exists()
    with wave.open(str(output), "rb") as wav:
        assert wav.getnframes() == click_track.SAMPLE_RATE * 60


def test_bench_callers_fail_when_local_helper_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("multiroom-spike.sh", "_lib.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "ssh",
        '#!/usr/bin/env bash\ncase "${*: -1}" in *sox*) exit 1;; *) exit 0;; esac\n',
    )
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PI_HOST": "leader.invalid",
            "PI_USER": "pi",
        }
    )

    result = subprocess.run(
        ["bash", str(scripts / "multiroom-spike.sh"), "--setup", "--leader",
         "leader.invalid"],
        cwd=foreign_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "_make_click_track.py" in result.stderr
