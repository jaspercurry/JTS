# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The read-only bass-management display flow (revision plan §3.3 / P5)."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from pathlib import Path

import pytest

from jasper.web import correction_bass_flow as flow
from jasper.active_speaker import baseline_profile
from tests.test_bass_extension_dynamic import _descriptor


ROOT = Path(__file__).resolve().parents[1]


def test_render_page_is_a_canonical_page_with_the_bass_module():
    html = flow.render_page("jts.local", "tok123").decode()
    # Canonical page shell (CSRF meta, app.css).
    assert 'name="jts-csrf"' in html
    assert '/assets/app.css' in html
    assert "Bass management" in html
    # The static ES module is loaded (no inline script behaviour on the page).
    assert '<script type="module" src="/assets/correction/js/bass/main.js">' in html


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


@pytest.mark.parametrize("configured", [False, True])
def test_status_payload_includes_native_bass_descriptor(monkeypatch, configured):
    _corner(monkeypatch)
    descriptor = asdict(_descriptor()) if configured else {}
    monkeypatch.setattr(baseline_profile, "applied_bass_extension", lambda: descriptor)

    payload, status = flow.handle_status()
    assert status == HTTPStatus.OK
    assert payload["bass_extension"] == (descriptor or None)


def test_status_payload_bass_extension_section_is_fail_soft(monkeypatch):
    """A broken bass-extension read must not take down the long-shipped
    bass-management payload it shares a page with — the section is null,
    everything else stays intact."""
    _corner(monkeypatch, 80.0)

    def boom():
        raise RuntimeError("profile read failed")

    monkeypatch.setattr(baseline_profile, "applied_bass_extension", boom)

    payload, status = flow.handle_status()
    assert status == HTTPStatus.OK
    assert payload["bass_extension"] is None
    assert payload["bass_extension_error"] == "unreadable"
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
