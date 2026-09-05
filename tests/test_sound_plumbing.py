# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _location_block(nginx: str, location: str) -> str:
    match = re.search(
        rf"(?ms)^    {re.escape(location)} \{{\n(?P<body>.*?)^    \}}",
        nginx,
    )
    assert match is not None, f"missing nginx block: {location}"
    return match.group(0)


def test_sound_wizard_is_socket_nginx_and_web_wired():
    socket_unit = (ROOT / "deploy" / "jasper-web.socket").read_text()
    nginx = (ROOT / "deploy" / "nginx-jasper.conf").read_text()
    web_main = (ROOT / "jasper" / "web" / "__main__.py").read_text()
    landing = (ROOT / "deploy" / "index.html").read_text()
    service = (ROOT / "deploy" / "jasper-web.service").read_text()

    assert "ListenStream=127.0.0.1:8784" in socket_unit
    assert "location /sound/" in nginx
    assert "JASPER_SOUND_WEB_PORT" in web_main
    assert "sound_setup.make_server" in web_main
    assert "/sound/" in landing
    # The /sound/ EQ editor writes CamillaDSP configs, so jasper-web's
    # ReadWritePaths must cover that dir. (Order-robust: WS1 Phase 4a inserted
    # /var/lib/jasper-secrets into this list.)
    rwpaths = next(
        ln for ln in service.splitlines() if ln.startswith("ReadWritePaths=")
    )
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


def test_both_profiles_keep_stale_active_speaker_stop_ingress_direct():
    stale_paths = (
        "/sound/active-speaker/stop",
        "/sound/active-speaker/commission-ramp-abort",
        "/sound/active-speaker/summed-test/stop",
    )
    handler_source = (ROOT / "jasper" / "web" / "sound_setup.py").read_text()
    for path in stale_paths:
        assert f'"{path.removeprefix("/sound")}"' in handler_source

    for filename in ("nginx-jasper.conf", "nginx-jasper-streambox.conf"):
        nginx = (ROOT / "deploy" / filename).read_text()
        compat = _location_block(nginx, "location /sound/")

        assert "proxy_pass http://127.0.0.1:8784/;" in compat
        assert "return 302" not in compat
