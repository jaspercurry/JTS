from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    assert "ReadWritePaths=/var/lib/jasper /var/lib/camilladsp/configs" in service


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
