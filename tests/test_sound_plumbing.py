# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from . import nginx_site
from .systemd_unit_helpers import assignments_for, values_for

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("port", "location", "env_var", "make_server_symbol"),
    [
        (8784, "/sound/", "JASPER_SOUND_WEB_PORT", "sound_setup.make_server"),
        (
            8779,
            "/assistant/weather/",
            "JASPER_WEATHER_WEB_PORT",
            "weather_setup.make_server",
        ),
    ],
    ids=["sound", "weather"],
)
def test_wizard_is_socket_and_nginx_wired(port, location, env_var, make_server_symbol):
    socket_unit = (ROOT / "deploy" / "jasper-web.socket").read_text()
    nginx = nginx_site.conf_text("full")
    web_main = (ROOT / "jasper" / "web" / "__main__.py").read_text()

    assert f"127.0.0.1:{port}" in assignments_for(socket_unit, "ListenStream")
    assert f"location {location}" in nginx
    assert env_var in web_main
    assert make_server_symbol in web_main


def test_sound_wizard_landing_page_and_read_write_paths():
    landing = (ROOT / "deploy" / "index.html").read_text()
    service = (ROOT / "deploy" / "jasper-web.service").read_text()

    assert "/sound/" in landing
    # The /sound/ EQ editor writes CamillaDSP configs, so jasper-web's
    # ReadWritePaths must cover that dir. (Order-robust: WS1 Phase 4a inserted
    # /var/lib/jasper-secrets into this list.)
    rwpaths = values_for(service, "ReadWritePaths")
    assert "/var/lib/jasper" in rwpaths and "/var/lib/camilladsp/configs" in rwpaths


def test_sound_setup_import_keeps_numpy_out_of_cold_start():
    code = (
        "import sys; "
        "import jasper.web.sound_setup; "
        "raise SystemExit(1 if 'numpy' in sys.modules or 'scipy' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0
