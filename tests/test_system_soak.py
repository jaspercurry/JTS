# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from jasper.cli import system_soak


def test_parse_duration_units() -> None:
    assert system_soak.parse_duration("30s") == 30
    assert system_soak.parse_duration("5m") == 300
    assert system_soak.parse_duration("1h") == 3600
    assert system_soak.parse_duration("42") == 42


def test_tracked_units_cover_resident_usb_mic_export_path() -> None:
    units = set(system_soak._tracked_units())

    assert {
        "jasper-aec-bridge.service",
        "jasper-usbgadget.service",
        "jasper-usbmic.service",
        "jasper-usbnet-dhcp.service",
    } <= units
    assert "jasper-usbmic-apply.service" not in units


def test_journal_summary_counts_without_storing_messages(monkeypatch) -> None:
    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = "\n".join([
            json.dumps({
                "_SYSTEMD_UNIT": "jasper-camilla.service",
                "PRIORITY": "6",
                "MESSAGE": "Capture read 0 bytes instead of requested 1024",
            }),
            json.dumps({
                "_SYSTEMD_UNIT": "jasper-camilla.service",
                "PRIORITY": "4",
                "MESSAGE": ["non-string", "message"],
            }),
        ])

    monkeypatch.setattr(
        system_soak.subprocess,
        "run",
        lambda *a, **kw: FakeProc(),
    )

    summary = system_soak._summarize_journal(
        "2026-06-02T10:00:00Z",
        "2026-06-02T10:01:00Z",
        ["jasper-camilla.service"],
    )

    assert summary["available"] is True
    assert summary["entries"] == 2
    camilla = summary["by_unit"]["jasper-camilla.service"]
    assert camilla["priorities"] == {"6": 1, "4": 1}
    assert camilla["message_bytes"] > 0
    assert "Capture read" not in json.dumps(summary)


def test_run_soak_writes_versioned_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        system_soak,
        "_tracked_units",
        lambda: ["jasper-voice.service"],
    )
    monkeypatch.setattr(
        system_soak,
        "_sample_units",
        lambda **kw: [{
            "unit": "jasper-voice.service",
            "active_state": "active",
            "memory_current_bytes": 123,
        }],
    )
    monkeypatch.setattr(
        system_soak,
        "_sample_status_sockets",
        lambda: {"voice": {"path": "/run/jasper/voice.sock", "status": None}},
    )
    monkeypatch.setattr(
        system_soak,
        "_summarize_journal",
        lambda *a, **kw: {"available": True, "entries": 0, "by_unit": {}},
    )

    path = system_soak.run_soak(
        duration_sec=0,
        interval_sec=30,
        include_pss=False,
        include_journal=True,
        output_dir=tmp_path,
        profile="idle",
    )

    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert payload["profile"] == "idle"
    assert payload["include_pss"] is False
    assert payload["journal"]["entries"] == 0
    assert payload["samples"][0]["units"][0]["unit"] == "jasper-voice.service"


def test_main_rejects_tiny_interval(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        system_soak.main(["--duration", "10s", "--interval", "1s"])
    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert "--interval must be at least 5s" in captured.err
