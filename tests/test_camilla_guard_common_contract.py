# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ownership/deploy contract for the two Camilla dead-pipe guards."""

import os
from pathlib import Path
import subprocess

import pytest

from tests._camilla_guard_fixtures import (
    write_pipe_config,
    write_runtime_safe_graph_script,
    write_statefile,
)

ROOT = Path(__file__).resolve().parents[1]
PIPE_GUARD = ROOT / "deploy/bin/jasper-camilla-pipe-guard"
GUARDS = (
    PIPE_GUARD,
    ROOT / "deploy/bin/jasper-camilla-crossover-guard",
)


def test_missing_common_library_fails_open_with_each_guard_event(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["JASPER_CAMILLA_GUARD_COMMON_LIB"] = str(tmp_path / "missing-common.sh")
    for guard in GUARDS:
        result = subprocess.run(
            ["bash", str(guard)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
        event = "camilla_crossover_guard" if "crossover" in guard.name else "camilla_pipe_guard"
        assert f"event={event}.skip reason=common_lib_unavailable" in result.stderr


def test_incomplete_common_library_fails_open_before_dead_pipe(tmp_path: Path) -> None:
    common = tmp_path / "incomplete.sh"
    common.write_text("# readable and sourceable, but missing the API\n")
    config = tmp_path / "bonded.yml"
    fifo = tmp_path / "absent-fifo"
    config.write_text(f'devices:\n  playback:\n    filename: "{fifo}"\n')
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f'config_path: "{config}"\n')

    for guard in GUARDS:
        env = dict(os.environ)
        env["JASPER_CAMILLA_GUARD_COMMON_LIB"] = str(common)
        env["JASPER_GROUPING_SNAPFIFO"] = str(fifo)
        if "crossover" in guard.name:
            env["JASPER_CAMILLA2_STATEFILE"] = str(statefile)
            event = "camilla_crossover_guard"
        else:
            env["JASPER_CAMILLA_STATEFILE"] = str(statefile)
            event = "camilla_pipe_guard"
        result = subprocess.run(
            ["bash", str(guard)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
        assert f"event={event}.skip reason=common_lib_invalid" in result.stderr
        assert "command not found" not in result.stderr


@pytest.mark.parametrize("guard", GUARDS, ids=lambda path: path.name)
def test_dead_pipe_repair_spawns_the_runtime_contract_once_across_a_restart_burst(
    tmp_path: Path, guard: Path,
) -> None:
    """Both camilla units are Restart=always/RestartSec=2, so a start that
    keeps failing runs the guard once per start. The repair re-points the
    statefile at a graph that is no longer pipe-shaped, so every later start in
    the burst takes an early exit instead of re-spawning the interpreter.
    """
    fifo = tmp_path / "snapfifo"  # never created: the measured dead-pipe mode
    statefile = write_statefile(tmp_path, write_pipe_config(tmp_path, fifo))
    repaired = tmp_path / "base.yml"
    repaired.write_text(
        "devices:\n  playback:\n    type: Alsa\n    channels: 2\n"
        '    device: "outputd_content_playback"\n    format: S16_LE\n'
    )
    calls = tmp_path / "runtime-calls"
    env = dict(os.environ)
    env["JASPER_CAMILLA_STATEFILE"] = str(statefile)
    env["JASPER_CAMILLA2_STATEFILE"] = str(statefile)
    env["JASPER_CAMILLA_BASE_CONFIG"] = str(repaired)
    env["JASPER_GROUPING_SNAPFIFO"] = str(fifo)
    env["JASPER_RUNTIME_SAFE_GRAPH"] = str(
        write_runtime_safe_graph_script(tmp_path, success_status="select_flat")
    )
    env["JASPER_FAKE_RUNTIME_CALLS"] = str(calls)

    for _ in range(3):
        result = subprocess.run(
            ["bash", str(guard)],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        assert result.returncode == 0

    assert str(repaired) in statefile.read_text()
    assert calls.read_text().count("call") == 1
