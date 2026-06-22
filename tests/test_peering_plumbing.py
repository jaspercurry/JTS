# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_peering_env_file_is_sourced_by_voice_and_control_units():
    """Both jasper-voice and jasper-control must source peering.env.

    `Config.peering_enabled` is parsed from `JASPER_PEERING`, which the
    `/rooms/` writes to `/var/lib/jasper/peering.env`. jasper-control
    runs the peering daemon; jasper-voice is its UDS client and short-circuits
    every wake-arbitration call when `peering_enabled` is False
    (`_peering_send` in `jasper/voice_daemon.py`). If either unit fails to
    source the file, that daemon never sees the wizard's `JASPER_PEERING=on`
    and peering silently stays off even after the household enabled it — the
    exact regression this guards (jasper-voice was missing the line). Mirrors
    `test_weather_plumbing.py`'s env-file plumbing assertion.
    """
    voice_unit = (
        ROOT / "deploy" / "systemd" / "jasper-voice.service"
    ).read_text()
    control_unit = (
        ROOT / "deploy" / "systemd" / "jasper-control.service"
    ).read_text()
    assert "EnvironmentFile=-/var/lib/jasper/peering.env" in voice_unit
    assert "EnvironmentFile=-/var/lib/jasper/peering.env" in control_unit
