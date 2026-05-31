"""Hardware-free tests for the /wake/ wizard (jasper.web.wake_setup).

Covers the canonical-design migration (app.css link, .app-header, CSRF
surfaces, ES-module script, no legacy chrome) and the preserved
request/response behaviour (model save writes the env file + restarts
voice; the layer/sensitivity routes proxy to jasper-control; CSRF is
enforced). Network (jasper-control proxy) and the daemon restart are
mocked, mirroring the other web-wizard tests.

The handler tests drive the *real* Handler class — instantiated via
``__new__`` to skip BaseHTTPRequestHandler's socket setup — with a fake
request I/O surface stamped onto the instance. Driving the real class
(rather than a stand-in with copied methods) is what exercises the
nested ``_handle_*`` dispatch helpers.
"""
from __future__ import annotations

import io
import json

from jasper.web import _common, wake_setup


# A bundled registry entry is always "available" on a dev box (no .onnx on
# disk needed); a non-bundled one is not. Tests that need a saveable model
# must use a bundled entry, mirroring what a fresh install ships.
def _bundled_entry():
    for e in wake_setup.wake_models.REGISTRY:
        if e.bundled:
            return e
    raise AssertionError("registry has no bundled entry to test with")


# ----------------------------------------------------------------------
# Render — canonical design system markers.
# ----------------------------------------------------------------------

CSRF = "x" * 43


def _render(state=None, *, status_msg=""):
    return wake_setup._index_html(
        state or {}, CSRF, status_msg=status_msg,
    ).decode()


def test_render_links_canonical_stylesheet():
    assert "/assets/app.css" in _render()


def test_render_links_page_css():
    assert "/assets/wake/wake.css" in _render()


def test_render_emits_app_header():
    html = _render()
    assert 'class="app-header"' in html
    assert 'class="app-header__title"' in html
    # Back affordance to home, using the shared icon sprite.
    assert "#icon-back" in html


def test_render_carries_csrf_meta_and_form_field():
    html = _render()
    # canonical_page emits the meta tag for the JS module to read.
    assert 'name="jts-csrf"' in html
    # The model-picker form carries the hidden field for the form POST.
    assert _common.CSRF_FORM_FIELD in html


def test_render_loads_es_module_not_inline_script():
    html = _render()
    assert '<script type="module" src="/assets/wake/js/main.js">' in html
    # No legacy inline behaviour should survive on the migrated page: the old
    # page baked the detection-card JS and the submit handler into the
    # document. Both now live in the ES module.
    assert "pollDetection" not in html
    assert "addEventListener" not in html


def test_render_has_no_legacy_chrome():
    html = _render()
    # wrap_page's PAGE_STYLE (max-width:620px body) and the clickable-div
    # switch must be gone; the toggle is the shared checkbox markup.
    assert "max-width: 620px" not in html
    assert 'class="switch"' not in html


def test_render_uses_canonical_toggle_for_each_layer():
    html = _render()
    for key in ("aec", "raw", "dtln"):
        assert f'id="layer-{key}"' in html
    # toggle_html renders the shared checkbox toggle.
    assert 'class="toggle"' in html


def test_render_form_posts_to_save_with_primary_button():
    html = _render()
    assert '<form method="post" action="save"' in html
    assert "btn btn--primary" in html


def test_render_lists_registry_models():
    html = _render()
    # Every registered model surfaces as a radio row.
    for entry in wake_setup.wake_models.REGISTRY:
        assert f'value="{entry.key}"' in html


def test_render_custom_row_when_active_model_off_registry():
    html = _render({"JASPER_WAKE_MODEL": "/abs/path/to/custom.onnx"})
    assert "Custom:" in html
    assert "custom.onnx" in html


# ----------------------------------------------------------------------
# Pure save-logic — behaviour preserved.
# ----------------------------------------------------------------------


def test_apply_save_rejects_empty_selection():
    new, err = wake_setup._apply_save({}, {})
    assert err is not None
    assert new == {}


def test_apply_save_rejects_custom_token():
    new, err = wake_setup._apply_save({"model": "__custom__"}, {})
    assert err is not None


def test_apply_save_rejects_unknown_model():
    new, err = wake_setup._apply_save({"model": "nope-not-real"}, {})
    assert err is not None


def test_apply_save_rejects_undownloaded_model():
    # A non-bundled model whose .onnx is absent on disk is rejected with a
    # re-deploy hint (this is the jarvis_v2 default on a fresh dev box).
    nonbundled = next(
        (e for e in wake_setup.wake_models.REGISTRY if not e.bundled), None
    )
    if nonbundled is None:
        return  # registry is all-bundled; nothing to assert
    new, err = wake_setup._apply_save({"model": nonbundled.key}, {})
    assert err is not None
    assert "deploy" in err


def test_apply_save_preserves_existing_threshold():
    entry = _bundled_entry()
    current = {"JASPER_WAKE_THRESHOLD": "0.42"}
    new, err = wake_setup._apply_save({"model": entry.key}, current)
    assert err is None
    assert new["JASPER_WAKE_MODEL"] == entry.model
    # The slider's value (written separately) must survive a model save.
    assert new["JASPER_WAKE_THRESHOLD"] == "0.42"


# ----------------------------------------------------------------------
# HTTP handler — routes + behaviour, with proxy/restart mocked.
# ----------------------------------------------------------------------


def _make_request(method, path, *, body=b"", headers=None, cookie=None):
    """Build a real Handler instance (skipping the socket-binding __init__)
    with a fake request I/O surface, so the nested _handle_* dispatch runs.

    Returns (handler, captured) where captured records status/headers/body.
    """
    cfg = {
        "state_path": _make_request.state_path,
        "control_base": "http://127.0.0.1:8780",
    }
    handler_cls = wake_setup._make_handler(cfg)
    h = handler_cls.__new__(handler_cls)  # bypass BaseHTTPRequestHandler.__init__

    hdrs = dict(headers or {})
    if body:
        hdrs.setdefault("Content-Length", str(len(body)))
    if cookie:
        hdrs["Cookie"] = cookie

    captured = {"status": None, "headers": {}, "body": b""}

    class _Wfile:
        def write(self, data):
            captured["body"] += data

    h.command = method
    h.path = path
    h.rfile = io.BytesIO(body)
    h.wfile = _Wfile()
    h.headers = hdrs
    h.client_address = ("127.0.0.1", 0)

    def _send_response(code, message=None):
        captured["status"] = code

    def _send_header(key, value):
        captured["headers"][key] = value

    def _send_error(code, message=None):
        captured["status"] = code

    h.send_response = _send_response
    h.send_header = _send_header
    h.end_headers = lambda: None
    h.send_error = _send_error
    h.address_string = lambda: "test-client"
    h.log_message = lambda *a, **k: None

    return h, captured


def test_public_surface_present():
    assert callable(wake_setup._index_html)
    assert callable(wake_setup.make_server)
    assert callable(wake_setup.main)


def test_get_root_renders_canonical_page(tmp_path):
    _make_request.state_path = str(tmp_path / "wake_model.env")
    h, cap = _make_request("GET", "/")
    h.do_GET()
    assert cap["status"] == 200
    assert b"/assets/app.css" in cap["body"]
    assert b"app-header" in cap["body"]


def test_get_detection_json_proxies_aec(tmp_path, monkeypatch):
    _make_request.state_path = str(tmp_path / "wake_model.env")
    captured = {}

    def fake_proxy_get(path, *, control_base, timeout):
        captured["path"] = path
        return 200, b'{"mode":"auto","bridge_active":true}'

    monkeypatch.setattr(wake_setup, "proxy_get", fake_proxy_get)
    h, cap = _make_request("GET", "/detection.json")
    h.do_GET()
    assert captured["path"] == "/aec"
    assert cap["status"] == 200
    assert b'"mode":"auto"' in cap["body"]


def test_get_unknown_path_404(tmp_path):
    _make_request.state_path = str(tmp_path / "wake_model.env")
    h, cap = _make_request("GET", "/nope")
    h.do_GET()
    assert cap["status"] == 404


def test_post_unknown_path_404(tmp_path):
    _make_request.state_path = str(tmp_path / "wake_model.env")
    h, cap = _make_request("POST", "/nope")
    h.do_POST()
    assert cap["status"] == 404


def test_post_save_bad_csrf_rejected(tmp_path):
    _make_request.state_path = str(tmp_path / "wake_model.env")
    body = b"csrf_token=" + b"a" * 64 + b"&model=hey_jarvis"
    h, cap = _make_request(
        "POST", "/save",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cookie=f"{_common.CSRF_COOKIE_NAME}=" + "b" * 64,
    )
    h.do_POST()
    assert cap["status"] == 403


def test_post_layer_proxies_to_control(tmp_path, monkeypatch):
    """A valid /layer/raw POST reaches _apply_layer with the parsed flag.

    CSRF is bypassed (verify_csrf mocked True) so the test stays focused on
    routing + body parsing + the proxy call shape."""
    _make_request.state_path = str(tmp_path / "wake_model.env")
    monkeypatch.setattr(wake_setup, "verify_csrf", lambda *a, **k: True)
    captured = {}

    def fake_apply_layer(layer, enabled, *, control_base):
        captured["layer"] = layer
        captured["enabled"] = enabled
        return 200, b'{"ok":true}'

    monkeypatch.setattr(wake_setup, "_apply_layer", fake_apply_layer)
    h, cap = _make_request(
        "POST", "/layer/raw",
        body=json.dumps({"enabled": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    h.do_POST()
    assert captured == {"layer": "raw", "enabled": True}
    assert cap["status"] == 200


def test_post_layer_unknown_layer_400(tmp_path, monkeypatch):
    _make_request.state_path = str(tmp_path / "wake_model.env")
    monkeypatch.setattr(wake_setup, "verify_csrf", lambda *a, **k: True)
    h, cap = _make_request(
        "POST", "/layer/bogus",
        body=json.dumps({"enabled": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    h.do_POST()
    assert cap["status"] == 400


def test_post_sensitivity_validates_range(tmp_path, monkeypatch):
    _make_request.state_path = str(tmp_path / "wake_model.env")
    monkeypatch.setattr(wake_setup, "verify_csrf", lambda *a, **k: True)
    h, cap = _make_request(
        "POST", "/sensitivity",
        body=json.dumps({"value": 5.0}).encode(),
        headers={"Content-Type": "application/json"},
    )
    h.do_POST()
    assert cap["status"] == 400  # out of [0, 1]


def test_post_save_writes_env_and_restarts(tmp_path, monkeypatch):
    """Happy path: a valid (bundled) model selection writes wake_model.env
    and kicks the voice daemon, then redirects with a flash."""
    _make_request.state_path = str(tmp_path / "wake_model.env")
    monkeypatch.setattr(wake_setup, "verify_csrf", lambda *a, **k: True)
    restarted = {"n": 0}
    monkeypatch.setattr(
        wake_setup, "restart_voice_daemon",
        lambda *a, **k: restarted.__setitem__("n", restarted["n"] + 1),
    )
    entry = _bundled_entry()
    h, cap = _make_request(
        "POST", "/save",
        body=("model=" + entry.key).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    h.do_POST()
    assert cap["status"] == 303
    assert restarted["n"] == 1
    written = (tmp_path / "wake_model.env").read_text()
    assert entry.model in written
