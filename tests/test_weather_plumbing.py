# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from .systemd_unit_helpers import assignments_for

ROOT = Path(__file__).resolve().parents[1]


def test_weather_env_file_is_sourced_by_voice_and_web_units():
    voice_unit = (ROOT / "deploy" / "systemd" / "jasper-voice.service").read_text()
    web_unit = (ROOT / "deploy" / "jasper-web.service").read_text()
    assert "-/var/lib/jasper/weather.env" in assignments_for(
        voice_unit, "EnvironmentFile"
    )
    assert "-/var/lib/jasper/weather.env" in assignments_for(
        web_unit, "EnvironmentFile"
    )
