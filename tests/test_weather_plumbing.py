# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_weather_wizard_is_socket_and_nginx_wired():
    socket_unit = (ROOT / "deploy" / "jasper-web.socket").read_text()
    nginx = (ROOT / "deploy" / "nginx-jasper.conf").read_text()
    web_main = (ROOT / "jasper" / "web" / "__main__.py").read_text()
    assert "ListenStream=127.0.0.1:8779" in socket_unit
    assert "location /weather/" in nginx
    assert "JASPER_WEATHER_WEB_PORT" in web_main
    assert "weather_setup.make_server" in web_main


def test_weather_env_file_is_sourced_by_voice_and_web_units():
    voice_unit = (ROOT / "deploy" / "systemd" / "jasper-voice.service").read_text()
    web_unit = (ROOT / "deploy" / "jasper-web.service").read_text()
    assert "EnvironmentFile=-/var/lib/jasper/weather.env" in voice_unit
    assert "EnvironmentFile=-/var/lib/jasper/weather.env" in web_unit
