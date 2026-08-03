# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI coverage for the operator-facing AEC3 sweep config tool."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from jasper.aec_sweep import aec3_sweep_config_payload
from jasper.cli import aec_sweep_config


def test_restart_bridge_resets_failure_then_restarts(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(aec_sweep_config.subprocess, "run", fake_run)

    aec_sweep_config._restart_bridge()

    assert [call[0] for call in calls] == [
        ["systemctl", "reset-failed", aec_sweep_config.BRIDGE_UNIT],
        ["systemctl", "restart", aec_sweep_config.BRIDGE_UNIT],
    ]
    assert calls[0][1]["check"] is False
    assert calls[0][1]["timeout"] == 5
    assert calls[1][1]["check"] is True
    assert calls[1][1]["timeout"] == 30


def test_validate_and_apply_commands_cover_write_and_optional_restart(
    monkeypatch, tmp_path, capsys,
):
    source = tmp_path / "sweep.json"
    source.write_text(json.dumps(aec3_sweep_config_payload()))
    installed = tmp_path / "installed.json"
    restart = []
    monkeypatch.setattr(
        aec_sweep_config,
        "_restart_bridge",
        lambda: restart.append(True),
    )

    assert aec_sweep_config.main(["validate", str(source)]) == 0
    assert aec_sweep_config.main([
        "apply",
        str(source),
        "--path",
        str(installed),
        "--restart-bridge",
    ]) == 0

    output = capsys.readouterr().out
    assert f"ok: {source}" in output
    assert f"installed {installed}" in output
    assert f"restarted {aec_sweep_config.BRIDGE_UNIT}" in output
    assert restart == [True]
    installed_payload = json.loads(installed.read_text())
    assert installed_payload["version"] == 1
    assert [item["leg"] for item in installed_payload["variants"]] == [
        item["leg"] for item in aec3_sweep_config_payload()["variants"]
    ]


def test_invalid_json_and_restart_failure_return_operator_error(
    monkeypatch, tmp_path, capsys,
):
    source = tmp_path / "invalid.json"
    source.write_text("{")
    assert aec_sweep_config.main(["validate", str(source)]) == 2
    assert "error:" in capsys.readouterr().err

    source.write_text(json.dumps(aec3_sweep_config_payload()))
    monkeypatch.setattr(
        aec_sweep_config,
        "_restart_bridge",
        lambda: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["systemctl", "restart"])
        ),
    )
    assert aec_sweep_config.main([
        "apply", str(source), "--path", str(tmp_path / "out.json"),
        "--restart-bridge",
    ]) == 2
    assert "error:" in capsys.readouterr().err
