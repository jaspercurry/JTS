# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import jasper.control.camilla_health as camilla_health
from jasper.control.camilla_health import CamillaHealth, classify_camilla_line


def test_camilla_probe_uses_bounded_controller_and_reads_device_config(
    monkeypatch,
) -> None:
    constructed: list[tuple[str, int]] = []
    closed = 0

    class Controller:
        def __init__(self, host: str, port: int) -> None:
            constructed.append((host, port))

        async def get_runtime_status(self):
            return {
                "buffer_level": 31,
                "rate_adjust": 1.0001,
                "capture_rate": 48000,
            }

        async def get_config_file_path(self, *, best_effort: bool):
            assert best_effort is True
            return "/tmp/camilla.yml"

        async def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(camilla_health, "CamillaController", Controller)
    monkeypatch.setattr(
        camilla_health,
        "read_camilla_devices_config",
        lambda path: {"chunksize": 256} if path == "/tmp/camilla.yml" else None,
    )

    assert CamillaHealth._read_camilla_state("127.0.0.1", 1234) == {
        "buffer_level": 31,
        "rate_adjust": 1.0001,
        "capture_rate": 48000,
        "config_path": "/tmp/camilla.yml",
        "chunksize": 256,
    }
    assert constructed == [("127.0.0.1", 1234)]
    assert closed == 1


def test_camilla_probe_rejects_incomplete_runtime_snapshot(monkeypatch) -> None:
    class Controller:
        def __init__(self, _host: str, _port: int) -> None:
            pass

        async def get_runtime_status(self):
            return {"buffer_level": 31}

        async def close(self):
            pass

    monkeypatch.setattr(camilla_health, "CamillaController", Controller)

    assert CamillaHealth._read_camilla_state("127.0.0.1", 1234) is None


def test_classify_journal_lines_for_documented_camilla_patterns() -> None:
    short = classify_camilla_line(
        "jasper-camilla",
        "Capture read 768 frames instead of the requested 1024",
    )
    assert short is not None
    assert short["type"] == "camilla_short_read"
    assert short["severity"] == "watch"
    assert short["deficit_frames"] == 256

    underrun = classify_camilla_line(
        "jasper-camilla",
        "PB: Prepare playback after buffer underrun",
    )
    assert underrun is not None
    assert underrun["type"] == "camilla_playback_underrun"
    assert underrun["severity"] == "issue"


def test_tiny_camilla_short_reads_are_ignored_as_recovered_partials() -> None:
    assert (
        classify_camilla_line(
            "jasper-camilla",
            "Capture read 1023 frames instead of the requested 1024",
        )
        is None
    )
    assert (
        classify_camilla_line(
            "jasper-camilla",
            "Capture read 1016 frames instead of the requested 1024",
        )
        is None
    )

    material = classify_camilla_line(
        "jasper-camilla",
        "Capture read 1008 frames instead of the requested 1024",
    )

    assert material is not None
    assert material["type"] == "camilla_short_read"
    assert material["deficit_frames"] == 16
