from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = ROOT / "deploy" / "jasper-correction-web.service"
INSTALL_SH = ROOT / "deploy" / "install.sh"


def test_correction_web_waits_for_time_sync_before_https_lookup():
    body = UNIT_PATH.read_text()
    assert (
        "After=jasper-correction-web.socket network-online.target "
        "time-sync.target"
    ) in body
    assert "Wants=network-online.target time-sync.target" in body


def test_correction_web_explicit_write_paths_cover_state_dirs():
    body = UNIT_PATH.read_text()
    assert "ReadWritePaths=/var/lib/jasper /var/lib/camilladsp" in body
    assert "UMask=0077" in body


def test_install_sh_creates_correction_state_dirs():
    body = INSTALL_SH.read_text()
    assert "install -d -m 0750 \\" in body
    for path in [
        "/var/lib/jasper/correction",
        "/var/lib/jasper/correction/sweeps",
        "/var/lib/jasper/correction/captures",
        "/var/lib/jasper/correction/sessions",
        "/var/lib/jasper/correction/calibration_mics",
    ]:
        assert path in body
