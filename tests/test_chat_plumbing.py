# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chat_web_is_socket_nginx_and_entrypoint_wired():
    socket_unit = (ROOT / "deploy" / "jasper-chat-web.socket").read_text()
    service_unit = (ROOT / "deploy" / "jasper-chat-web.service").read_text()
    nginx = (ROOT / "deploy" / "nginx-jasper.conf").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    install_units = (
        ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
    ).read_text()
    landing = (ROOT / "deploy" / "index.html").read_text()

    assert "ListenStream=127.0.0.1:8787" in socket_unit
    assert "Type=notify" in service_unit
    assert "WatchdogSec=30s" in service_unit
    assert "User=jasper-web" in service_unit
    assert "Group=jasper" in service_unit
    assert "UMask=0007" in service_unit
    assert (
        "ExecStart=/opt/jasper/.venv/bin/jasper-chat-web "
        "--host 127.0.0.1 --port 8787"
    ) in service_unit
    for hardening in (
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/jasper",
        "ProtectHome=true",
        "PrivateTmp=true",
        "NoNewPrivileges=true",
        "CapabilityBoundingSet=",
        "SystemCallFilter=@system-service",
    ):
        assert hardening in service_unit
    assert "location /chat/" in nginx
    assert "location = /chat { return 308 /chat/; }" in nginx
    assert "proxy_pass http://127.0.0.1:8787/;" in nginx
    assert 'jasper-chat-web = "jasper.web.chat_setup:main"' in pyproject
    assert "jasper-chat-web" in install_units
    assert 'systemctl restart "${unit}.socket"' in install_units
    assert 'href="/chat/"' in landing
