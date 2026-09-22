# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from jasper import service_units
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
    calls = []

    class FakeCompletedProcess:
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
            "not json",
            json.dumps(["not", "an", "object"]),
        ])

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeCompletedProcess()

    monkeypatch.setattr(service_units.subprocess, "run", fake_run)

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
    argv, kwargs = calls[0]
    assert argv[argv.index("--since") + 1] == "2026-06-02T10:00:00Z"
    assert argv[argv.index("--until") + 1] == "2026-06-02T10:01:00Z"
    assert "--output-fields=__REALTIME_TIMESTAMP,_SYSTEMD_UNIT,PRIORITY,MESSAGE" in argv
    assert argv[argv.index("-u") + 1] == "jasper-camilla.service"
    assert kwargs["timeout"] == 20


def test_journal_summary_preserves_unavailable_returncode(monkeypatch) -> None:
    def fail(*_a, **_kw):
        raise service_units.JournalctlUnavailable("denied", returncode=2)

    monkeypatch.setattr(system_soak, "run_journalctl_json", fail)

    assert system_soak._summarize_journal("start", "end", ["a.service"]) == {
        "available": False,
        "error": "denied",
        "returncode": 2,
    }


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
