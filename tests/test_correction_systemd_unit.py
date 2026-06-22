# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = ROOT / "deploy" / "jasper-correction-web.service"
INSTALL_SH = ROOT / "deploy" / "install.sh"
NGINX_CONF = ROOT / "deploy" / "nginx-jasper.conf"


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


def test_correction_location_allows_large_capture_upload():
    """A capture WAV (~1-2 MB) exceeds nginx's 1 MB default, so without a
    raised client_max_body_size the upload 413s before reaching the backend
    (real hardware bug 2026-06-04). Guard that the /correction/ location keeps a
    limit >= the backend's own MAX_WAV_BODY_BYTES, so the app — not a raw nginx
    413 — enforces the real cap with a clean error.
    """
    conf = NGINX_CONF.read_text()
    start = conf.index("location /correction/")
    end = conf.index("location ", start + 1)  # next location block
    block = conf[start:end]
    m = re.search(r"client_max_body_size\s+(\d+)m\s*;", block)
    assert m, "/correction/ location must set client_max_body_size (Nm)"
    nginx_bytes = int(m.group(1)) * 1024 * 1024

    from jasper.web.correction_setup import MAX_WAV_BODY_BYTES
    assert nginx_bytes >= MAX_WAV_BODY_BYTES, (
        "nginx client_max_body_size must be >= backend MAX_WAV_BODY_BYTES"
    )


def test_install_sh_creates_correction_state_dirs():
    body = INSTALL_SH.read_text()
    assert "install -d -m 2770 -g jasper \\" in body
    for path in [
        "/var/lib/jasper/correction",
        "/var/lib/jasper/correction/sweeps",
        "/var/lib/jasper/correction/captures",
        "/var/lib/jasper/correction/sessions",
        "/var/lib/jasper/correction/calibration_mics",
        "/var/lib/jasper/correction/tones",
    ]:
        assert path in body
