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


def test_render_page_treats_undefined_constraints_as_ok():
    """iOS Safari often returns `undefined` from getSettings() for
    echoCancellation / noiseSuppression / autoGainControl rather
    than echoing back the requested value. Undefined ≠ true ⇒ the
    feature is off (iOS has these off by default for getUserMedia).
    The page must NOT mark undefined as 'bad' — that was a
    real first-pass-test bug. Pin the corrected behavior."""
    body = correction_setup._render_page("jts.local").decode()
    # Helper function exists.
    assert "isAudioProcessingOff" in body
    # Only TRUE counts as a problem (not 'truthy', because
    # undefined is falsy and would otherwise be misclassified).
    assert "actual.echoCancellation === true" in body
    assert "actual.noiseSuppression === true" in body
    assert "actual.autoGainControl === true" in body


def test_render_page_includes_autolevel_controls():
    """The leveling step is now AUTOMATIC — server ramps main_volume
    while client watches mic and posts /autolevel/lock when in
    target range. Pin the UI presence + JS plumbing.

    Also pins the MANUAL Lock button as a reliable override when the
    auto-detect can't reach the target band (real first-user-test
    finding — speaker-to-iPhone-at-couch path attenuation can leave
    the mic below the lock band even at max safe volume)."""
    body = correction_setup._render_page("jts.local").decode()
    # All three control buttons present (start + manual-lock + cancel).
    assert 'id="autolevel"' in body
    assert 'id="autolevel-lock"' in body
    assert 'id="autolevel-cancel"' in body
    assert "Auto-level" in body
    assert "Lock now" in body
    # JS handlers exist + target the right endpoints.
    assert "startAutolevel" in body
    assert "autolevel/start" in body
    assert "autolevel/lock" in body
    # Adaptive target band — computed from measured noise floor at
    # the start of autolevel rather than hard-coded.
    assert "computeTargetBand" in body
    assert "AUTOLEVEL_SNR_DESIRED_LOW" in body
    assert "AUTOLEVEL_SNR_DESIRED_HIGH" in body
    # Preflight noise-floor measurement step is present.
    assert "Measuring room noise" in body


def test_render_page_shows_result_before_drawing_chart():
    """Bug fix pin: drawChart() must run AFTER `resultSection` is
    shown, otherwise the canvas's getBoundingClientRect returns
    0×0 (hidden ancestor) and the chart renders blank. Real user
    bug — got 5 PEQ filters but an empty frequency-response box.
    """
    body = correction_setup._render_page("jts.local").decode()
    # The fix marker comment must stay so a refactor doesn't silently
    # reintroduce the old order.
    assert "show resultSection BEFORE drawing the chart" in body
    # And the drawChart defensive guard must reject zero-size
    # bounding rects.
    assert "drawChart skipped" in body


def test_render_page_redraws_chart_on_resize():
    """Phone rotation / external display change should re-render
    the chart at the new canvas size, not stretch the old bitmap.
    Pin the resize + orientationchange listeners."""
    body = correction_setup._render_page("jts.local").decode()
    assert "scheduleChartRedraw" in body
    assert "orientationchange" in body


def test_render_page_autolevel_target_band_clamps():
    """Pin the absolute clamps: -30 dBFS floor (don't lock super
    quiet even in dead-silent rooms) and -10 dBFS ceiling (avoid
    pushing the iPhone mic toward clipping). A regression here
    would cause silent off-by-default-target failures we'd only
    catch on hardware."""
    body = correction_setup._render_page("jts.local").decode()
    assert "AUTOLEVEL_TARGET_DB_FLOOR = -30" in body
    assert "AUTOLEVEL_TARGET_DB_CEILING = -10" in body


def test_render_page_amp_message_is_generic_not_tpa3255():
    """First pass said 'TPA3255 amp knob' — wrong because (a) users
    don't know what that is, and (b) they might be on a different
    amp. Generic 'turn up your amplifier' is the right wording.
    Pin the wording so a future revision doesn't accidentally
    reintroduce the brand-specific text."""
    body = correction_setup._render_page("jts.local").decode()
    assert "turn up your amplifier" in body.lower() or \
        "turn up your amp" in body.lower()
    assert "TPA3255" not in body


def test_render_page_placement_advice_says_head_height():
    """First-pass instructions said 'on the seat' which is wrong —
    the cushion absorbs sound and the listener's head is what we
    care about. Pin the corrected wording."""
    body = correction_setup._render_page("jts.local").decode()
    assert "head will be" in body or "head height" in body
    # Negative pin — the bad wording shouldn't come back.
    assert "on the seat" not in body


def test_render_page_continue_button_hidden_outside_needs_next_position():
    """Bug a user hit: Continue button stayed visible during the next
    sweep and a double-tap fired /next-position from the wrong
    state. Fix is the central applyButtonPolicy that hides everything
    by default and re-shows per state."""
    body = correction_setup._render_page("jts.local").decode()
    assert "applyButtonPolicy" in body
    # Default is hidden + disabled.
    assert "continueBtn.classList.add('hidden')" in body
    assert "continueBtn.disabled = false" in body
    # Re-shown only in needs_next_position branch.
    assert "needs_next_position" in body


def test_render_page_cert_section_is_optional_not_a_warning():
    """First-pass UX framed the cert install as 'cert trouble?' which
    misled users into thinking it was a fallback. The corrected
    framing is 'optional: silence the warning' — explicit that the
    page works without it."""
    body = correction_setup._render_page("jts.local").decode()
    assert "Optional: silence" in body
    # Negative pin.
    assert "Cert trust trouble" not in body


# ---------- Test-tone backend (jasper.correction.playback) ------------------


def test_test_tone_wav_is_generated_and_cached(tmp_path):
    """First call generates the WAV; second call reuses the cache
    file (no re-generation). Cache key is the parameter tuple."""
    from jasper.correction import playback
    p1 = playback._ensure_tone_wav(
        freq_hz=1000, duration_s=2.0, dbfs=-18.0,
        sample_rate=48000, cache_dir=tmp_path,
    )
    assert p1.exists()
    mtime1 = p1.stat().st_mtime
    # Second call → same path, cache hit.
    p2 = playback._ensure_tone_wav(
        freq_hz=1000, duration_s=2.0, dbfs=-18.0,
        sample_rate=48000, cache_dir=tmp_path,
    )
    assert p2 == p1
    assert p2.stat().st_mtime == mtime1


def test_test_tone_wav_audio_correctness(tmp_path):
    """The generated WAV should:
      - have the expected duration (within sample-rate resolution)
      - contain a single dominant frequency at the requested freq
      - peak amplitude near the requested dBFS (within fade-edge dip)
    """
    import numpy as np
    from jasper.correction import playback, sweep

    wav_path = playback._ensure_tone_wav(
        freq_hz=1000, duration_s=1.0, dbfs=-12.0,
        sample_rate=48000, cache_dir=tmp_path,
    )
    sig, sr = sweep.read_wav_mono(wav_path)
    assert sr == 48000
    # Length tolerance: ±10 samples for fade-rounding.
    assert abs(len(sig) - 48000) < 10
    # Peak amplitude target: 10**(-12/20) = 0.251. Allow a bit of
    # margin for fade-edge dip.
    expected_peak = 10 ** (-12.0 / 20)
    actual_peak = float(np.max(np.abs(sig)))
    assert actual_peak <= expected_peak + 0.005
    assert actual_peak > expected_peak * 0.9
    # FFT — the peak bin should be at ~1000 Hz.
    spectrum = np.abs(np.fft.rfft(sig))
    freqs_bin = np.fft.rfftfreq(len(sig), d=1.0 / sr)
    peak_idx = int(np.argmax(spectrum))
    assert abs(freqs_bin[peak_idx] - 1000) < 2  # within 2 Hz


# ---------- End-to-end via the actual HTTP server --------------------------


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = correction_setup.make_server(
        ("127.0.0.1", 0), hostname="jts.local",
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
