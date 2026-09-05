# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

from .test_install_state_group_write import _extract as _extract_bash_function

ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = ROOT / "deploy" / "jasper-correction-web.service"
INSTALL_SH = ROOT / "deploy" / "install.sh"
NGINX_CONF = ROOT / "deploy" / "nginx-jasper.conf"


def test_measurement_view_routes_to_correction_web_on_http_and_https():
    conf = NGINX_CONF.read_text()

    assert conf.count("location /sound/measurements/ {") == 2
    assert conf.count("proxy_pass http://127.0.0.1:8770/measurements/;") == 2


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
    (real hardware bug 2026-06-04). Guard that the /sound/room/ location keeps a
    limit >= the backend's own MAX_WAV_BODY_BYTES, so the app — not a raw nginx
    413 — enforces the real cap with a clean error.
    """
    conf = NGINX_CONF.read_text()
    start = conf.index("location /sound/room/")
    end = conf.index("location ", start + 1)  # next location block
    block = conf[start:end]
    m = re.search(r"client_max_body_size\s+(\d+)m\s*;", block)
    assert m, "/sound/room/ location must set client_max_body_size (Nm)"
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
        # The active_speaker* trees /sound/ and /sound/room/ share; must be
        # created at install time too, or the first root-lane writer mints
        # them root:root 0700 and locks jasper-web out until the next
        # deploy's heal_shared_state_modes runs.
        "/var/lib/jasper/active_speaker",
        "/var/lib/jasper/active_speaker/campaigns",
        "/var/lib/jasper/active_speaker/sessions",
        "/var/lib/jasper/active_speaker_captures",
        "/var/lib/jasper/active_speaker_sweeps",
        "/var/lib/jasper/active_speaker_stimuli",
        "/var/lib/jasper/active_speaker_tone_artifacts",
    ]:
        assert path in body


def test_install_sh_active_speaker_dirs_match_heal_allowlist():
    """A prose pointer between install.sh and heal_shared_state_modes
    doesn't fail CI: reuses the two existing extraction helpers (this
    module's own install.sh function-body slice, and
    test_install_state_group_write._extract for the heal's bash source) so a
    path added to one list and not the other reds here instead of silently
    reviving the fresh-install lockout bug."""
    install_body = INSTALL_SH.read_text()
    func_start = install_body.index("install_camilladsp()")
    func_end = install_body.index("\n}\n", func_start)
    install_paths = set(re.findall(
        r"/var/lib/jasper/active_speaker\S*", install_body[func_start:func_end]
    ))

    heal_body = _extract_bash_function("heal_shared_state_modes")
    heal_paths = {
        p.replace("${STATE_DIR}", "/var/lib/jasper")
        for p in re.findall(r'"d:2770:(\$\{STATE_DIR\}/active_speaker[^"]*)"', heal_body)
    }

    assert install_paths, "install_camilladsp() must still list active_speaker* paths"
    assert install_paths == heal_paths
