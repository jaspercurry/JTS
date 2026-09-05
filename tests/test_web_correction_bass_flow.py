# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The read-only bass-management display flow (revision plan §3.3 / P5)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest

from jasper.web import correction_bass_flow as flow


ROOT = Path(__file__).resolve().parents[1]


def test_render_page_is_a_canonical_page_with_the_bass_module():
    html = flow.render_page("jts.local", "tok123").decode()
    # Canonical page shell (CSRF meta, app.css) and the bass section-tab active.
    assert 'name="jts-csrf"' in html
    assert '/assets/app.css' in html
    assert 'aria-current="page" href="/sound/bass/"' in html
    assert "Bass management" in html
    # The static ES module is loaded (no inline script behaviour on the page).
    assert '<script type="module" src="/assets/correction/js/bass/main.js">' in html
    # A pointer to the Room tab where the bass-region measurement lives.
    assert "/sound/room/" in html


def test_render_page_escapes_hostname_in_back_link():
    html = flow.render_page('js"><b>x', "tok").decode()
    assert '"><b>x' not in html  # the raw injection is escaped


def test_bass_module_uses_shared_get_json():
    source = (ROOT / "deploy/assets/correction/js/bass/main.js").read_text()
    assert "import { getJSON } from '/assets/shared/js/http.js';" in source
    assert "getJSON('status')" in source
    assert "getJSON('/bass/status')" not in source
    assert "await fetch(" not in source
    assert ".json()" not in source


def _corner(monkeypatch, corner_hz=None):
    monkeypatch.setattr(
        "jasper.output_topology.bass_management_corner_hz", lambda: corner_hz
    )


@pytest.mark.parametrize("corner_hz", [None, 80.0])
def test_status_payload_mirrors_the_live_corner(monkeypatch, corner_hz):
    _corner(monkeypatch, corner_hz)
    payload, status = flow.handle_status()
    assert status == HTTPStatus.OK
    assert payload["corner_hz"] == corner_hz
    assert payload["configured"] is (corner_hz is not None)


def test_status_payload_is_display_only_no_control_keys(monkeypatch):
    """The wizard is read-only: the payload carries no apply/set/write affordance."""
    _corner(monkeypatch, 80.0)
    payload, _ = flow.handle_status()
    assert set(payload) == {"corner_hz", "configured", "bass_extension"}


def test_status_payload_includes_bass_extension_section(monkeypatch):
    """The Bass Extension status (a separate, not-yet-launched feature) rides
    the same /bass/status payload as the long-shipped bass-management
    section, verbatim from bass_extension_state_summary()."""
    import jasper.bass_extension.profile as profile_mod

    _corner(monkeypatch)  # the corner is irrelevant here
    summary = {"commissioned": True, "status": "accepted"}
    monkeypatch.setattr(profile_mod, "bass_extension_state_summary", lambda: summary)

    payload, status = flow.handle_status()
    assert status == HTTPStatus.OK
    assert payload["bass_extension"] == summary


def test_status_payload_bass_extension_section_is_fail_soft(monkeypatch):
    """A broken bass-extension read must not take down the long-shipped
    bass-management payload it shares a page with — the section is null,
    everything else stays intact."""
    import jasper.bass_extension.profile as profile_mod

    _corner(monkeypatch, 80.0)

    def boom():
        raise RuntimeError("profile read failed")

    monkeypatch.setattr(profile_mod, "bass_extension_state_summary", boom)

    payload, status = flow.handle_status()
    assert status == HTTPStatus.OK
    assert payload["bass_extension"] is None
    assert payload["configured"] is True
    assert payload["corner_hz"] == 80.0


def test_bass_flow_registered_on_the_correction_server(monkeypatch, tmp_path):
    """End-to-end over loopback HTTP: /bass renders and /bass/status returns
    the display JSON — proving the route is in the read allowlist + dispatch."""
    import json
    import threading
    import urllib.request

    from jasper.web import correction_setup

    server = correction_setup.make_server(("127.0.0.1", 0), hostname="jts.local")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/bass", timeout=5
        )
        assert page.status == 200
        assert b"Bass management" in page.read()

        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/bass/status", timeout=5
        )
        body = json.loads(resp.read())
        assert resp.status == 200
        assert set(body) >= {"corner_hz", "configured"}
    finally:
        server.shutdown()
        server.server_close()
