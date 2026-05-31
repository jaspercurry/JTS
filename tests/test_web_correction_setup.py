"""Hardware-free tests for the /correction/ wizard (canonical design system).

The room-correction page is HARDWARE/BROWSER-CRITICAL — the real measurement
flow (getUserMedia, the sweep, CamillaDSP apply) only runs on the Pi. These
tests cover the parts that can be checked off-device: the page renders the
canonical document shell, the relocated behaviour ships as an ES module (no
inline IIFE remains), the routes still resolve, and the CSRF guard still
fires. Network / CamillaDSP / session imports are lazy inside the handlers,
so a static render needs no hardware.
"""
from __future__ import annotations

import io

from jasper.web import correction_setup


# ---------------------------------------------------------------------------
# Page render — canonical shell.
# ---------------------------------------------------------------------------


def _render() -> str:
    return correction_setup._render_page(
        "jts.local",
        csrf_token="tok-correction-123456789012345678901234",
    ).decode("utf-8")


def test_render_uses_canonical_shell():
    html = _render()
    assert html.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in html
    assert "app-header" in html
    assert 'name="jts-csrf"' in html
    assert "tok-correction-123456789012345678901234" in html


def test_render_links_page_css_and_module():
    html = _render()
    assert "/assets/correction/correction.css?v=" in html
    assert "/assets/correction/js/main.js" in html
    assert 'type="module"' in html


def test_render_has_no_inline_script_iife():
    """The page behaviour was relocated into the ES module; the legacy
    inline <script> IIFE must be gone (gating: no inline JS on a migrated
    page)."""
    html = _render()
    assert "(function () {" not in html
    # Old hand-rolled shell + injection markers must be gone too.
    assert "__STYLE__" not in html
    assert "__DIALOG_HELPERS__" not in html
    assert "__CSRF_FETCH_HELPERS__" not in html


def test_render_carries_required_sample_rate_for_module():
    """The module reads the required capture rate off the page rather than
    hardcoding it; the server stays the source of truth."""
    html = _render()
    assert f'data-required-sr="{correction_setup.REQUIRED_SAMPLE_RATE}"' in html


def test_render_back_link_is_absolute_http():
    """/correction/ is HTTPS but the dashboard at / is plain HTTP, so the
    Home affordance must be an absolute http:// link, not a relative '/'."""
    html = _render()
    assert 'href="http://jts.local/"' in html


def test_render_preserves_workflow_anchors():
    """Every DOM id the relocated module drives must still be present in the
    server-rendered shell."""
    html = _render()
    for anchor in (
        'id="start"',
        'id="current-correction"',
        'id="input-device-select"',
        'id="mic-model-select"',
        'id="constraints"',
        'id="measure-section"',
        'id="state-badge"',
        'id="run-measurement"',
        'id="autolevel"',
        'id="apply-correction"',
        'id="verify-correction"',
        'id="reset-correction"',
        'id="chart"',
        'id="peq-list"',
        'id="measurement-reports"',
        'id="session-report"',
    ):
        assert anchor in html, anchor


def test_render_keeps_cert_install_disclosure():
    """The Safari "Not Private" cert-install help is load-bearing for the
    HTTPS-only page and must survive the restyle."""
    html = _render()
    assert "/jts-root-ca.crt" in html
    assert "Certificate Trust Settings" in html


def test_render_escapes_hostname():
    html = correction_setup._render_page(
        'evil"<x>', csrf_token="tok-correction-123456789012345678901234",
    ).decode("utf-8")
    assert 'evil"<x>' not in html
    assert "&quot;" in html or "&lt;x&gt;" in html


# ---------------------------------------------------------------------------
# Routing — behaviour preserved.
# ---------------------------------------------------------------------------


def _drive(path: str, method: str = "GET", *, headers=None, body: bytes = b""):
    """Construct the wizard's Handler without binding a socket and drive a
    single request through it. Returns the raw response bytes."""
    Handler = correction_setup._make_handler({"hostname": "jts.local"})

    request_line = f"{method} {path} HTTP/1.1\r\n".encode()
    header_lines = b"Host: jts.local\r\n"
    if body:
        header_lines += f"Content-Length: {len(body)}\r\n".encode()
    for k, v in (headers or {}).items():
        header_lines += f"{k}: {v}\r\n".encode()
    raw = request_line + header_lines + b"\r\n" + body

    rfile = io.BytesIO(raw)
    wfile = io.BytesIO()

    handler = Handler.__new__(Handler)
    handler.rfile = rfile
    handler.wfile = wfile
    handler.client_address = ("127.0.0.1", 0)
    handler.server = None
    handler.raw_requestline = rfile.readline()
    handler.parse_request()
    handler.protocol_version = "HTTP/1.1"
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return wfile.getvalue()


def test_get_root_renders_html():
    resp = _drive("/")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"/assets/app.css" in resp
    assert b"/assets/correction/js/main.js" in resp


def test_get_healthz_ok():
    resp = _drive("/healthz")
    assert b"200" in resp.split(b"\r\n", 1)[0]
    assert b"ok" in resp


def test_unknown_get_route_404():
    resp = _drive("/nope")
    assert b"404" in resp.split(b"\r\n", 1)[0]


def test_post_without_csrf_is_rejected():
    """Every state-changing POST must fail CSRF before doing any work — the
    resilience guard must survive the restyle."""
    resp = _drive("/start", method="POST", body=b"{}")
    assert b"403" in resp.split(b"\r\n", 1)[0]


def test_unknown_post_route_404_before_csrf():
    """Unknown POST paths 404 without revealing CSRF state (route-check
    precedes the CSRF check)."""
    resp = _drive("/bogus", method="POST", body=b"{}")
    assert b"404" in resp.split(b"\r\n", 1)[0]


def test_known_post_routes_reach_csrf_guard():
    """Lock the full POST surface so the migration can't silently drop a
    route: each known route reaches the CSRF guard (403 without a token),
    proving it is still registered."""
    known = {
        "/start", "/next-position", "/repeat-position", "/verify",
        "/test-tone", "/autolevel/start", "/autolevel/lock",
        "/autolevel/cancel", "/upload-noise", "/upload-capture",
        "/calibration/fetch", "/calibration/upload", "/apply", "/reset",
        "/session/delete",
    }
    for route in known:
        resp = _drive(route, method="POST", body=b"{}")
        assert b"403" in resp.split(b"\r\n", 1)[0], (
            f"{route} should reach the CSRF guard (403)"
        )


# ---------------------------------------------------------------------------
# Public surface unchanged.
# ---------------------------------------------------------------------------


def test_make_server_smoke():
    srv = correction_setup.make_server(("127.0.0.1", 0), hostname="jts.local")
    try:
        assert srv is not None
    finally:
        srv.server_close()


def test_public_surface_present():
    assert callable(correction_setup.make_server)
    assert callable(correction_setup.main)
    assert callable(correction_setup._render_page)
    assert callable(correction_setup._make_handler)
