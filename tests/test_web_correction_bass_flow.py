# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The read-only bass-management display flow (revision plan §3.3 / P5)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from jasper.web import correction_bass_flow as flow


ROOT = Path(__file__).resolve().parents[1]


def test_render_page_is_a_canonical_page_with_the_bass_module():
    html = flow.render_page("jts.local", "tok123").decode()
    # Canonical page shell (CSRF meta, app.css) and the bass section-tab active.
    assert 'name="jts-csrf"' in html
    assert '/assets/app.css' in html
    assert 'aria-current="page" href="/correction/bass/"' in html
    assert "Bass management" in html
    # The static ES module is loaded (no inline script behaviour on the page).
    assert '<script type="module" src="/assets/correction/js/bass/main.js">' in html
    # A pointer to the Room tab where the bass-region measurement lives.
    assert "/correction/room/" in html


def test_render_page_escapes_hostname_in_back_link():
    html = flow.render_page('js"><b>x', "tok").decode()
    assert '"><b>x' not in html  # the raw injection is escaped


def test_bass_module_uses_shared_get_json():
    source = (ROOT / "deploy/assets/correction/js/bass/main.js").read_text()
    assert "import { getJSON } from '/assets/shared/js/http.js';" in source
    assert "getJSON('/bass/status')" in source
    assert "await fetch(" not in source
    assert ".json()" not in source


def _state(monkeypatch, **kwargs):
    from jasper.bass_management import BassManagementState

    defaults = dict(
        corner_hz=None, owner=None, sub_present=False, mains_highpass_enabled=False,
    )
    defaults.update(kwargs)
    monkeypatch.setattr(
        "jasper.bass_management.resolve_bass_management",
        lambda: BassManagementState(**defaults),
    )


def test_status_payload_not_configured(monkeypatch):
    _state(monkeypatch)  # no bass management
    payload, status = flow.handle_status()
    assert status == HTTPStatus.OK
    assert payload["configured"] is False
    assert payload["corner_hz"] is None
    assert payload["owner_label"] is None


def test_status_payload_active_speaker_local(monkeypatch):
    from jasper.bass_management import OWNER_ACTIVE_SPEAKER_LOCAL

    _state(
        monkeypatch,
        corner_hz=80.0,
        owner=OWNER_ACTIVE_SPEAKER_LOCAL,
        sub_present=True,
        mains_highpass_enabled=True,
    )
    payload, status = flow.handle_status()
    assert status == HTTPStatus.OK
    assert payload["configured"] is True
    assert payload["corner_hz"] == 80.0
    assert payload["sub_present"] is True
    assert payload["mains_highpass_enabled"] is True
    # A homeowner-facing owner label is derived (not the raw owner id).
    assert payload["owner_label"] == "This speaker's own subwoofer output"


def test_status_payload_wireless_sub(monkeypatch):
    from jasper.bass_management import OWNER_WIRELESS_SUB

    _state(
        monkeypatch,
        corner_hz=90.0,
        owner=OWNER_WIRELESS_SUB,
        sub_present=True,
        mains_highpass_enabled=False,
    )
    payload, _ = flow.handle_status()
    assert payload["configured"] is True
    assert payload["owner_label"] == (
        "A wireless subwoofer in this speaker's group"
    )
    assert payload["mains_highpass_enabled"] is False


def test_status_payload_fourth_quadrant_gap_is_reported(monkeypatch):
    """The known active-endpoint + wireless-only-sub gap rides the payload so
    the page can render the honest 'not applied on this speaker' copy instead
    of claiming a high-pass the box does not run."""
    from jasper.bass_management import (
        MAINS_HP_UNWIRED_ACTIVE_ENDPOINT,
        OWNER_WIRELESS_SUB,
    )

    _state(
        monkeypatch,
        corner_hz=90.0,
        owner=OWNER_WIRELESS_SUB,
        sub_present=True,
        mains_highpass_enabled=False,
        mains_highpass_unwired_reason=MAINS_HP_UNWIRED_ACTIVE_ENDPOINT,
    )
    payload, _ = flow.handle_status()
    assert payload["mains_highpass_enabled"] is False
    assert (
        payload["mains_highpass_unwired_reason"]
        == MAINS_HP_UNWIRED_ACTIVE_ENDPOINT
    )


def test_status_payload_is_display_only_no_control_keys(monkeypatch):
    """The wizard is read-only: the payload carries no apply/set/write affordance."""
    from jasper.bass_management import OWNER_ACTIVE_SPEAKER_LOCAL

    _state(
        monkeypatch, corner_hz=80.0, owner=OWNER_ACTIVE_SPEAKER_LOCAL,
        sub_present=True, mains_highpass_enabled=True,
    )
    payload, _ = flow.handle_status()
    assert set(payload) == {
        "corner_hz",
        "owner",
        "sub_present",
        "mains_highpass_enabled",
        "mains_highpass_unwired_reason",
        "owner_label",
        "configured",
    }


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
        assert set(body) >= {"corner_hz", "configured", "owner_label"}
    finally:
        server.shutdown()
        server.server_close()
