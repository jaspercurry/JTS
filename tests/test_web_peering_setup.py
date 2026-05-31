"""Tests for the /peers/ wizard after its migration to the canonical look.

1. The page renders canonical design-system bytes (links /assets/app.css,
   carries the shared .app-header, embeds the CSRF meta tag) and links its
   page CSS -- and has no inline <script> (the page has no client JS).
2. The migration was presentation-only: the server-rendered POST /save flow
   (write peering.env -> restart both daemons) and the public module surface
   (_render_page, make_server, main, _make_handler) are unchanged, and the
   structured-log / fail-soft paths still fire.
"""
from __future__ import annotations

import http
import types
from email.message import Message
from io import BytesIO

from jasper.web import peering_setup


def _render(on: str = "off", room: str = "kitchen", primary: str = "",
            flash: str = "", peers=None) -> str:
    """Render the page with state injected via monkeypatch-free shims.

    Patches the module-level state readers so the render is deterministic
    and never touches the real /var/lib/jasper files.
    """
    state = {"JASPER_PEERING": on, "JASPER_PEER_ROOM": room}
    if primary:
        state["JASPER_PEER_PRIMARY"] = primary
    orig_load = peering_setup._load_state
    orig_pid = peering_setup._peer_id
    orig_status = peering_setup._fetch_peer_status
    peering_setup._load_state = lambda path: dict(state)
    peering_setup._peer_id = lambda *a, **k: "abcd1234-self"
    peering_setup._fetch_peer_status = lambda *a, **k: {"peers": peers or []}
    try:
        return peering_setup._render_page(
            state_path="/tmp/does-not-matter.env",
            csrf_token="tok-abcdefghijklmnopqrstuvwx",
            status_msg=flash,
        ).decode()
    finally:
        peering_setup._load_state = orig_load
        peering_setup._peer_id = orig_pid
        peering_setup._fetch_peer_status = orig_status


def test_peering_page_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    # legacy wrapper marker must be gone
    assert "max-width: 620px" not in out
    assert "class=\"sub\"" not in out


def test_peering_page_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Speaker peering</h1>' in out
    assert '<use href="#icon-back"></use>' in out


def test_peering_page_embeds_csrf_meta():
    out = _render()
    assert 'meta name="jts-csrf"' in out
    assert 'content="tok-abcdefghijklmnopqrstuvwx"' in out


def test_peering_page_links_page_css():
    out = _render()
    assert "/assets/peering/peering.css?v=" in out


def test_peering_page_has_no_inline_script():
    # This page has no client-side behaviour: no ES module, no <script>.
    out = _render()
    assert "<script" not in out


def test_peering_form_preserves_csrf_field_and_save_action():
    out = _render()
    assert 'action="/save"' in out
    assert 'method="post"' in out
    assert 'type="hidden" name="csrf_token"' in out


def test_peering_form_keeps_native_boolean_fields():
    # The toggles must still submit enabled=1 / primary=1 on the POST.
    out = _render(on="on", primary="1")
    assert 'type="checkbox" name="enabled" value="1" checked' in out
    assert 'type="checkbox" name="primary" value="1" checked' in out
    assert 'name="room"' in out
    # No clickable-div switch, ever (gating-test invariant).
    assert 'class="switch"' not in out


def test_peering_form_uses_canonical_vocabulary():
    out = _render()
    assert 'class="field"' in out
    assert 'class="form-actions"' in out
    assert 'class="btn btn--primary"' in out


def test_peering_status_card_uses_info_card():
    out = _render(on="on")
    assert 'class="info-card"' in out
    assert 'class="deflist"' in out
    assert "var(--status-ok)" in out  # ON tone


def test_peering_off_hides_discovered_section():
    out = _render(on="off")
    assert "Discovered peers" not in out


def test_peering_on_shows_peers():
    out = _render(on="on", peers=[
        {"peer_id": "ffff0000-other", "room": "bedroom",
         "address": "192.168.1.9", "primary": True},
    ])
    assert "Discovered peers" in out
    assert "bedroom" in out
    assert "192.168.1.9" in out
    assert "ffff0000" in out  # short id
    assert "primary</span>" in out


def test_peering_on_empty_peers_shows_placeholder():
    out = _render(on="on", peers=[])
    assert "No sibling peers visible yet" in out


def test_peering_peer_name_is_escaped():
    out = _render(on="on", peers=[
        {"peer_id": "ffff0000-x", "room": '<b>evil</b>',
         "address": "10.0.0.1", "primary": False},
    ])
    assert "<b>evil</b>" not in out
    assert "&lt;b&gt;evil&lt;/b&gt;" in out


def test_peering_blank_flash_renders_no_banner():
    assert 'class="banner' not in _render(flash="")


def test_peering_saved_flash_renders_ok_banner():
    out = _render(flash="Saved. Speakers restarting; refresh in a few seconds.")
    assert "banner--ok" in out


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for driving do_GET/do_POST."""

    def __init__(self, path: str, body: bytes = b"", cookies: str = "") -> None:
        self.path = path
        self.headers = Message()
        self.headers["Content-Length"] = str(len(body))
        self.headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookies:
            self.headers["Cookie"] = cookies
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status = None
        self.sent_headers = []
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = int(status)

    def send_response_only(self, status):
        self.status = int(status)

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def send_error(self, status, *a, **k):
        self.status = int(status)

    def address_string(self):
        return "127.0.0.1"

    def log_message(self, *a, **k):
        pass

    def header_values(self, name):
        return [v for n, v in self.sent_headers if n.lower() == name.lower()]


def test_public_surface_is_stable():
    assert callable(peering_setup.make_server)
    assert callable(peering_setup.main)
    assert callable(peering_setup._render_page)
    assert callable(peering_setup._make_handler)


def test_get_root_renders_canonical_page(monkeypatch):
    monkeypatch.setattr(peering_setup, "_load_state", lambda path: {})
    monkeypatch.setattr(peering_setup, "_peer_id", lambda *a, **k: "id-1")
    handler_cls = peering_setup._make_handler("/tmp/x.env")
    h = _FakeHandler("/")
    handler_cls.do_GET(h)
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out


def test_post_unknown_route_404s():
    handler_cls = peering_setup._make_handler("/tmp/x.env")
    h = _FakeHandler("/nope", body=b"")
    handler_cls.do_POST(h)
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_save_writes_and_restarts(monkeypatch):
    token = "y" * 64
    calls = {"write": [], "voice": 0, "control": 0, "logged": []}

    monkeypatch.setattr(peering_setup, "_load_state", lambda path: {})
    monkeypatch.setattr(
        peering_setup, "write_env_file",
        lambda path, values, mode=0o644: calls["write"].append(dict(values)),
    )
    monkeypatch.setattr(
        peering_setup, "restart_voice_daemon",
        lambda: calls.__setitem__("voice", calls["voice"] + 1),
    )
    monkeypatch.setattr(
        peering_setup, "_restart_jasper_control",
        lambda: calls.__setitem__("control", calls["control"] + 1),
    )
    monkeypatch.setattr(
        peering_setup.logger, "info",
        lambda *a, **k: calls["logged"].append(a),
    )

    handler_cls = peering_setup._make_handler("/tmp/x.env")
    body = ("csrf_token=" + token + "&enabled=1&room=kitchen").encode()
    h = _FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler_cls.do_POST(h)

    assert h.status == int(http.HTTPStatus.SEE_OTHER)
    assert h.header_values("Location") == ["/"]
    assert calls["write"] and calls["write"][0]["JASPER_PEERING"] == "on"
    assert calls["write"][0]["JASPER_PEER_ROOM"] == "kitchen"
    assert calls["voice"] == 1
    assert calls["control"] == 1
    # structured-log event preserved
    assert any("event=peering.wizard.save" in str(a[0]) for a in calls["logged"])


def test_post_save_oserror_renders_500_body(monkeypatch):
    token = "z" * 64

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(peering_setup, "_load_state", lambda path: {})
    monkeypatch.setattr(peering_setup, "_peer_id", lambda *a, **k: "id-1")
    monkeypatch.setattr(peering_setup, "write_env_file", boom)
    # If we reach a restart the fail-soft path was skipped -> fail loud.
    monkeypatch.setattr(
        peering_setup, "restart_voice_daemon",
        lambda: (_ for _ in ()).throw(AssertionError("restart on OSError")),
    )

    handler_cls = peering_setup._make_handler("/tmp/x.env")
    body = ("csrf_token=" + token + "&enabled=1&room=kitchen").encode()
    h = _FakeHandler("/save", body=body, cookies="jts_csrf=" + token)
    handler_cls.do_POST(h)

    assert h.status == int(http.HTTPStatus.INTERNAL_SERVER_ERROR)
    out = h.wfile.getvalue().decode()
    assert "Save failed" in out
    assert "/assets/app.css?v=" in out  # re-renders the canonical page


def test_post_save_rejects_bad_csrf(monkeypatch):
    monkeypatch.setattr(peering_setup, "_load_state", lambda path: {})
    handler_cls = peering_setup._make_handler("/tmp/x.env")
    # form token != cookie token -> 403, no write
    body = b"csrf_token=" + b"a" * 64 + b"&enabled=1"
    h = _FakeHandler("/save", body=body, cookies="jts_csrf=" + "b" * 64)
    handler_cls.do_POST(h)
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
