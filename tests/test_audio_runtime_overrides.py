# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jasper.cli.audio_config import main as audio_config_main
from jasper.audio_runtime_overrides import (
    clear_runtime_override,
    load_runtime_overrides,
    runtime_overrides_path,
    set_runtime_override,
)


def test_set_load_and_clear_runtime_override(tmp_path):
    path = tmp_path / "audio_runtime_overrides.json"
    now = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)

    updated = set_runtime_override(
        key="JASPER_CAMILLA_TARGET_LEVEL",
        value="1792",
        reason="jts5 rough-audio soak",
        path=path,
        ttl_seconds=3600,
        allowed_keys={"JASPER_CAMILLA_TARGET_LEVEL"},
        now=now,
    )

    assert updated.values()["JASPER_CAMILLA_TARGET_LEVEL"] == "1792"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["kind"] == "jts_audio_runtime_overrides"
    assert raw["overrides"]["JASPER_CAMILLA_TARGET_LEVEL"]["reason"] == (
        "jts5 rough-audio soak"
    )

    loaded = load_runtime_overrides(
        path,
        allowed_keys={"JASPER_CAMILLA_TARGET_LEVEL"},
        now=now,
    )
    assert loaded.values() == {"JASPER_CAMILLA_TARGET_LEVEL": "1792"}
    assert loaded.warnings == ()

    cleared = clear_runtime_override(
        "JASPER_CAMILLA_TARGET_LEVEL",
        path,
        allowed_keys={"JASPER_CAMILLA_TARGET_LEVEL"},
    )
    assert cleared.values() == {}


def test_runtime_override_requires_reason_and_supported_key(tmp_path):
    path = tmp_path / "audio_runtime_overrides.json"

    with pytest.raises(ValueError, match="reason"):
        set_runtime_override(
            key="JASPER_CAMILLA_TARGET_LEVEL",
            value="1792",
            reason="",
            path=path,
            allowed_keys={"JASPER_CAMILLA_TARGET_LEVEL"},
        )
    with pytest.raises(ValueError, match="unsupported"):
        set_runtime_override(
            key="NOPE",
            value="1",
            reason="test",
            path=path,
            allowed_keys={"JASPER_CAMILLA_TARGET_LEVEL"},
        )


def test_runtime_override_path_honors_env(monkeypatch, tmp_path):
    path = tmp_path / "audio_runtime_overrides.json"

    assert runtime_overrides_path({}) == "/var/lib/jasper/audio_runtime_overrides.json"
    monkeypatch.setenv("JASPER_AUDIO_RUNTIME_OVERRIDES_PATH", str(path))

    assert runtime_overrides_path() == str(path)


def test_runtime_override_expiry_and_malformed_entries_warn(tmp_path):
    path = tmp_path / "audio_runtime_overrides.json"
    path.write_text(
        json.dumps(
            {
                "kind": "jts_audio_runtime_overrides",
                "schema_version": 1,
                "overrides": {
                    "JASPER_CAMILLA_TARGET_LEVEL": {
                        "value": "1792",
                        "reason": "expired trial",
                        "expires_at": "2026-06-30T11:00:00Z",
                    },
                    "JASPER_OUTPUTD_PERIOD_FRAMES": {
                        "value": "384",
                        "reason": "",
                    },
                    "UNKNOWN": {
                        "value": "1",
                        "reason": "bad key",
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = load_runtime_overrides(
        path,
        allowed_keys={
            "JASPER_CAMILLA_TARGET_LEVEL",
            "JASPER_OUTPUTD_PERIOD_FRAMES",
        },
        now=datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
    )

    assert loaded.values() == {}
    assert any("expired" in warning for warning in loaded.warnings)
    assert any("needs a reason" in warning for warning in loaded.warnings)
    assert any("unsupported key" in warning for warning in loaded.warnings)


def test_audio_config_override_cli_roundtrip(tmp_path, capsys):
    path = tmp_path / "audio_runtime_overrides.json"

    assert audio_config_main([
        "overrides-set",
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
        "768",
        "--reason",
        "short soak",
        "--ttl-seconds",
        "600",
        "--overrides",
        str(path),
    ]) == 0
    set_out = capsys.readouterr().out
    assert "JASPER_OUTPUTD_DAC_BUFFER_FRAMES" in set_out

    assert audio_config_main(["overrides-list", "--overrides", str(path)]) == 0
    list_out = capsys.readouterr().out
    assert '"value": "768"' in list_out

    assert audio_config_main([
        "overrides-clear",
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES",
        "--overrides",
        str(path),
    ]) == 0
    clear_out = capsys.readouterr().out
    assert "JASPER_OUTPUTD_DAC_BUFFER_FRAMES" not in clear_out


def test_audio_config_override_cli_rejects_coupling_key(tmp_path):
    path = tmp_path / "audio_runtime_overrides.json"

    with pytest.raises(SystemExit) as exc:
        audio_config_main([
            "overrides-set",
            "JASPER_FANIN_CAMILLA_COUPLING",
            "fifo",
            "--reason",
            "coupling transitions need ordered reconcile",
            "--overrides",
            str(path),
        ])

    assert exc.value.code == 2
    assert not path.exists()


def test_audio_config_override_cli_default_path_honors_env(
    monkeypatch,
    tmp_path,
    capsys,
):
    path = tmp_path / "audio_runtime_overrides.json"
    monkeypatch.setenv("JASPER_AUDIO_RUNTIME_OVERRIDES_PATH", str(path))

    assert audio_config_main([
        "overrides-set",
        "JASPER_OUTPUTD_PERIOD_FRAMES",
        "384",
        "--reason",
        "env-path smoke",
    ]) == 0
    capsys.readouterr()

    loaded = load_runtime_overrides(
        path,
        allowed_keys={"JASPER_OUTPUTD_PERIOD_FRAMES"},
    )
    assert loaded.values() == {"JASPER_OUTPUTD_PERIOD_FRAMES": "384"}
