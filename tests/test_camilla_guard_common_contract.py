# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ownership/deploy contract for the two Camilla dead-pipe guards."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "deploy/lib/jasper-camilla-guard-common.sh"
GUARDS = (
    ROOT / "deploy/bin/jasper-camilla-pipe-guard",
    ROOT / "deploy/bin/jasper-camilla-crossover-guard",
)


def test_guards_source_one_common_probe_and_repair_owner() -> None:
    common = COMMON.read_text()
    assert "camilla_guard_repair_statefile()" in common
    assert "camilla_guard_check_playback_pipe_or_repair()" in common
    for guard in GUARDS:
        source = guard.read_text()
        assert "jasper-camilla-guard-common.sh" in source
        assert "source \"$COMMON_LIB\"" in source
        assert "camilla_guard_check_playback_pipe_or_repair" in source
        assert "camilla_guard_repair_statefile()" not in source


def test_installer_stages_common_guard_library() -> None:
    installer = (ROOT / "deploy/lib/install/systemd-units.sh").read_text()
    function_start = installer.index("install_local_audio_graph_unit_files() {")
    function_end = installer.index("\n}\n", function_start)
    function = installer[function_start:function_end]
    lib_pos = function.index("deploy/lib/jasper-camilla-guard-common.sh")
    assert lib_pos < function.index("for row in")
    rows = installer.split("JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS=(", 1)[1].split(
        "\n)\n", 1,
    )[0]
    assert "deploy/bin/jasper-camilla-pipe-guard" in rows
    assert "deploy/bin/jasper-camilla-crossover-guard" in rows
    assert installer.count("deploy/lib/jasper-camilla-guard-common.sh") == 1


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
