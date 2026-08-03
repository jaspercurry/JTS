# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess

from jasper.cli import airplay_mode
from jasper.web import airplay_setup


def test_apply_uses_active_only_refresh(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(airplay_mode.subprocess, "run", run)

    assert airplay_mode._apply_and_restart() == 0
    assert calls[0][1]["timeout"] == airplay_mode.SHAIRPORT_RESTART_TIMEOUT_SEC
    assert calls[0][0] == ["systemctl", "try-restart", "shairport-sync"]


def test_cli_and_web_share_fail_safe_mode_resolution(tmp_path, monkeypatch):
    path = tmp_path / "airplay_mode.env"
    path.write_text("JASPER_AIRPLAY_FREE_RUNNING=unexpected\n")
    monkeypatch.setattr(airplay_mode, "MODE_ENV_FILE", str(path))

    assert airplay_mode._read_mode() == "synced"
    assert airplay_setup._current_mode(str(path)) == "synced"

    path.write_text("JASPER_AIRPLAY_FREE_RUNNING=yes\n")
    assert airplay_mode._read_mode() == "free-running"
    assert airplay_setup._current_mode(str(path)) == "free-running"
