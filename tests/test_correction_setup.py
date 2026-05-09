"""Tests for the room-correction wizard at /correction/.

Phase 0 has very little server-side logic (the action is in the
browser), so the test surface is small:

  1. Page render — hostname substitutes through, sample-rate constant
     reaches the JS, the WiiM-style placement advice is present, the
     CA-download link is present.
  2. Healthz returns plain-text "ok" so systemd / curl probes work.
  3. End-to-end via a real ThreadingHTTPServer to confirm the routes
     dispatch from real HTTP — same shape as test_voice_setup.

When Phase 1 lands sweep / capture / apply routes, this file extends
with their shape (POST validation, SSE framing, etc.) — keep the
existing test names so future-me can grep for the Phase 0 pin.
"""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from jasper.web import correction_setup


# ---------- Page render ----------------------------------------------------


def test_render_page_substitutes_hostname():
    body = correction_setup._render_page("acoustic-lab.local").decode()
    assert "acoustic-lab.local" in body
    # The hostname appears in the cert-download link href; if it didn't
    # substitute the page would show literal "__HOSTNAME__".
    assert "__HOSTNAME__" not in body


def test_render_page_substitutes_required_sample_rate():
    body = correction_setup._render_page("jts.local").decode()
    # The constant lands in the JS as a numeric literal — check it shows
    # up. The JS bails on any other rate.
    assert "var REQUIRED_SR = 48000;" in body
    assert "__REQUIRED_SR__" not in body


def test_render_page_no_unfilled_placeholders():
    """Defensive: catch any future placeholder that gets added to
    _PAGE_HTML but forgotten in _render_page."""
    body = correction_setup._render_page("jts.local").decode()
    assert "__STYLE__" not in body
    assert "__HOSTNAME__" not in body
    assert "__REQUIRED_SR__" not in body


def test_render_page_includes_ca_download_link():
    """The cert-trust dance is the load-bearing first-time-user step;
    the fallback link to the CA must always be visible. Pin the URL
    shape so a stylesheet refactor doesn't accidentally drop the
    anchor."""
    body = correction_setup._render_page("jts.local").decode()
    assert 'href="http://jts.local/jts-root-ca.crt"' in body
    # Mention "Certificate Trust Settings" so the user knows the right
    # iOS panel name.
    assert "Certificate Trust Settings" in body


def test_render_page_includes_placement_advice():
    """The WiiM-style 'lay flat, bottom toward speakers, no case' is
    the only mic-positioning guidance we can give on iOS (no mic-
    selection API). Pin it so a copy edit doesn't accidentally drop
    it."""
    body = correction_setup._render_page("jts.local").decode()
    assert "screen up" in body or "screen-up" in body
    assert "bottom edge" in body
    assert "no case" in body or "out of any case" in body


def test_render_page_requests_constraints_explicitly():
    """getUserMedia must request EC/NS/AGC off — Safari will sometimes
    ignore the constraint, but we have to ASK first. Verify the JS
    actually sets these. Without this, even a correctly-implemented
    Safari would give us processed audio."""
    body = correction_setup._render_page("jts.local").decode()
    assert "echoCancellation: false" in body
    assert "noiseSuppression: false" in body
    assert "autoGainControl: false" in body
    # And the constructor pin for sample rate.
    assert "sampleRate: REQUIRED_SR" in body


def test_render_page_reads_back_settings_for_verify():
    """After getUserMedia, the JS must call getSettings() and surface
    a red banner if EC/NS/AGC didn't actually take effect. If this
    check ever falls out, future phases would silently measure with
    Safari's processed audio — which is exactly the wrong thing."""
    body = correction_setup._render_page("jts.local").decode()
    assert ".getSettings()" in body
    # All three constraint names appear in the verify section.
    assert "actual.echoCancellation" in body
    assert "actual.noiseSuppression" in body
    assert "actual.autoGainControl" in body


def test_render_page_does_not_loop_mic_back_to_speaker():
    """A naive 'src.connect(node); node.connect(ctx.destination)' would
    play the mic back through the phone speaker. Acceptable on a
    laptop, terrible on a smart speaker that's the room's TARGET (the
    feedback loop would be instant and ear-melting). Keep the comment
    that documents the deliberate omission as a regression pin."""
    body = correction_setup._render_page("jts.local").decode()
    # node.connect(ctx.destination) MUST NOT appear.
    assert "node.connect(ctx.destination)" not in body
    # The anti-feedback comment must be there to flag the omission as
    # deliberate to a future drive-by editor.
    assert "feedback loop" in body


def test_render_page_serves_audioworklet_inline():
    """Phase 0 ships an inline AudioWorklet via Blob URL. Important
    invariant: the worklet pattern (not ScriptProcessorNode) carries
    into Phase 1 sweep capture, where worklet timing matters. If a
    future change replaces the worklet with ScriptProcessorNode, the
    sweep capture refactor breaks."""
    body = correction_setup._render_page("jts.local").decode()
    assert "AudioWorkletProcessor" in body
    assert "AudioWorkletNode" in body
    assert "audioWorklet.addModule" in body


def test_render_page_requests_wake_lock():
    """A 2-minute sweep on iOS Safari without Wake Lock = screen
    locks mid-measurement = AudioContext suspended = capture lost.
    Pin the request here so it's not optimized away later."""
    body = correction_setup._render_page("jts.local").decode()
    assert "wakeLock" in body
    assert "screen" in body  # request type


# ---------- End-to-end via the actual HTTP server --------------------------


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = correction_setup.make_server(
        "127.0.0.1", 0, hostname="jts.local",
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def test_e2e_get_index_serves_html():
    server, base = _start_server()
    try:
        resp = urllib.request.urlopen(f"{base}/")
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith(
            "text/html",
        )
        body = resp.read().decode()
        assert "Room correction" in body
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_healthz_returns_plain_ok():
    """systemd's `Type=notify` could replace this later, but for now a
    simple HTTP-200 / "ok" body is what makes `curl jts.local/correction/healthz`
    a valid liveness probe — and also lets jasper-doctor add a
    correction-subsystem check without parsing JSON."""
    server, base = _start_server()
    try:
        resp = urllib.request.urlopen(f"{base}/healthz")
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith(
            "text/plain",
        )
        assert resp.read() == b"ok\n"
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_unknown_path_404s():
    server, base = _start_server()
    try:
        try:
            urllib.request.urlopen(f"{base}/nope")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:
            raise AssertionError("expected 404 for unknown path")
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_trailing_slash_index_serves_same_html():
    """Defensive: nginx strips its /correction/ prefix and forwards as
    GET / — but a future client (curl, jasper-doctor, integration test)
    might hit GET // or GET /. Both should serve the page."""
    server, base = _start_server()
    try:
        for path in ("/", ""):
            resp = urllib.request.urlopen(f"{base}{path}")
            assert resp.status == 200
            assert b"Room correction" in resp.read()
    finally:
        server.shutdown()
        server.server_close()
