# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess

from jasper.cli import airplay_mode


def test_apply_uses_active_only_refresh(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(airplay_mode.subprocess, "run", run)

    assert airplay_mode._apply_and_restart() == 0
    assert calls[0][1]["timeout"] == airplay_mode.SHAIRPORT_RESTART_TIMEOUT_SEC
    assert calls[0][0] == ["systemctl", "try-restart", "shairport-sync"]
