# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.web.nav import entry

from . import nginx_site
from .systemd_unit_helpers import assignments_for, value_for, values_for


ROOT = Path(__file__).resolve().parents[1]


def test_chat_web_is_socket_nginx_and_entrypoint_wired():
    socket_unit = (ROOT / "deploy" / "jasper-chat-web.socket").read_text()
    service_unit = (ROOT / "deploy" / "jasper-chat-web.service").read_text()
    nginx = nginx_site.conf_text("full")
    pyproject = (ROOT / "pyproject.toml").read_text()
    install_units = (
        ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
    ).read_text()

    assert "127.0.0.1:8787" in assignments_for(socket_unit, "ListenStream")
    assert value_for(service_unit, "Type") == "notify"
    assert value_for(service_unit, "WatchdogSec") == "30s"
    assert value_for(service_unit, "User") == "jasper-web"
    assert value_for(service_unit, "Group") == "jasper"
    assert value_for(service_unit, "UMask") == "0007"
    assert value_for(service_unit, "ExecStart") == (
        "/opt/jasper/.venv/bin/jasper-chat-web --host 127.0.0.1 --port 8787"
    )
    assert "/var/lib/jasper" in values_for(service_unit, "ReadWritePaths")
    for key, expected in (
        ("ProtectSystem", "strict"),
        ("ProtectHome", "true"),
        ("PrivateTmp", "true"),
        ("NoNewPrivileges", "true"),
        ("CapabilityBoundingSet", ""),
        ("SystemCallFilter", "@system-service"),
    ):
        assert value_for(service_unit, key) == expected
    assert "location /assistant/chat/" in nginx
    assert "location = /assistant/chat { return 308 /assistant/chat/; }" in nginx
    assert "proxy_pass http://127.0.0.1:8787/;" in nginx
    assert 'jasper-chat-web = "jasper.web.chat_setup:main"' in pyproject
    assert "jasper-chat-web" in install_units
    assert 'systemctl restart "${unit}.socket"' in install_units
    assert entry("/assistant/chat/").label == "Chat history"
