# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the room-correction wizard at /correction/.

The page started as the Phase 0 mic-permission skeleton and has grown
into the full correction wizard, so this file pins both browser-facing
HTML/JS contracts and real HTTP dispatch:

  1. Page render — hostname substitutes through, sample-rate constant
     reaches the JS, the WiiM-style placement advice is present, the
     CA-download link is present.
  2. Healthz returns plain-text "ok" so systemd / curl probes work.
  3. End-to-end via a real ThreadingHTTPServer to confirm the routes
     dispatch from real HTTP — same shape as test_voice_setup.

Keep the existing test names where possible so future-me can grep for
the original Phase 0 pins.
"""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest

from pathlib import Path

from jasper.web import correction_setup

from ._web_test_helpers import request_with_csrf

# The page's behaviour was relocated VERBATIM into a static ES module when
# /correction/ migrated to the canonical design system (chrome-only restyle).
# Render-surface assertions that used to look for inline JS now read the
# module; the intent (the behaviour ships to the browser) is unchanged.
_CORRECTION_MODULE = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "assets" / "correction" / "js" / "main.js"
)


def _module_js() -> str:
    return _CORRECTION_MODULE.read_text()


# ---------- Page render ----------------------------------------------------


def test_render_page_substitutes_hostname():
    body = correction_setup._render_page("acoustic-lab.local").decode()
    assert "acoustic-lab.local" in body
    # The hostname appears in the cert-download link href; if it didn't
    # substitute the page would show literal "__HOSTNAME__".
    assert "__HOSTNAME__" not in body


def test_render_page_substitutes_required_sample_rate():
    body = _module_js()  # behaviour relocated to the static ES module
    # The constant lands in the JS as a numeric literal — check it shows
    # up. The JS bails on any other rate. (The migration baked it in as a
    # literal; the old __REQUIRED_SR__ Python substitution is gone, but the
    # module's relocation-note comment names it, so don't assert its absence.)
    assert "var REQUIRED_SR = 48000;" in body


def test_render_page_no_unfilled_placeholders():
    """Defensive: catch any future placeholder that gets added to
    _PAGE_HTML but forgotten in _render_page."""
    body = correction_setup._render_page("jts.local").decode()
    assert "__STYLE__" not in body
    assert "__HOSTNAME__" not in body
    assert "__REQUIRED_SR__" not in body
    assert "__CSRF_META__" not in body
    assert "__CSRF_FETCH_HELPERS__" not in body
    assert "__TARGET_PROFILE_OPTIONS__" not in body
    assert "__CORRECTION_STRATEGY_OPTIONS__" not in body


def test_render_page_embeds_csrf_meta_and_fetch_helpers():
    # The CSRF meta tag stays in the page (canonical_page renders it); the
    # fetch helpers moved into the shared ES module, which now IMPORTS
    # csrfHeaders/jsonHeaders from /assets/shared/js/http.js rather than
    # inlining them. Assert both surfaces keep the X-CSRF-Token contract.
    body = correction_setup._render_page("jts.local", "csrf-token").decode()
    assert 'meta name="jts-csrf" content="csrf-token"' in body
    js = _module_js()
    assert 'from "/assets/shared/js/http.js"' in js
    assert "jsonHeaders" in js and "csrfHeaders" in js
    assert "headers: jsonHeaders()" in js
    assert "headers: csrfHeaders({'Content-Type': 'audio/wav'})" in js


def test_render_page_delegates_correction_when_bonded_follower(monkeypatch):
    monkeypatch.setattr(correction_setup, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(
        correction_setup,
        "bonded_follower_leader_web_url",
        lambda path="/": "http://jts3.local/correction/",
    )

    body = correction_setup._render_page("jts4.local", "csrf-token").decode()

    assert "Room correction is controlled by the pair leader" in body
    assert "http://jts3.local/correction/" in body
    assert "/assets/correction/js/main.js" not in body
    assert 'meta name="jts-csrf" content="csrf-token"' in body


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


def test_render_page_home_link_returns_to_plain_http():
    """The correction app itself runs under HTTPS, but the rest of the
    JTS wizard surface is deliberately plain HTTP. Its Home affordance
    must use an absolute HTTP URL so it does not inherit the HTTPS
    origin and hit nginx's 443 catch-all."""
    body = correction_setup._render_page("jts.local").decode()
    # Migrated to the canonical sticky header: the back affordance is the
    # round .icon-button. It must still point at the absolute plain-HTTP root
    # so it does not inherit the HTTPS origin and hit nginx's 443 catch-all.
    assert 'class="icon-button" href="http://jts.local/"' in body
    assert 'href="/"' not in body


def test_read_json_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_setup.BadRequest, match="Content-Length"):
        correction_setup._read_json_body(Handler())


def test_read_wav_body_rejects_invalid_content_length():
    class Handler:
        headers = {"Content-Length": "not-a-number"}
        rfile = io.BytesIO()

    with pytest.raises(correction_setup.BadRequest, match="Content-Length"):
        correction_setup._read_wav_body(Handler())


def test_read_wav_body_rejects_large_or_incomplete_body():
    class TooLarge:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"12345")

    with pytest.raises(correction_setup.BadRequest, match="too large"):
        correction_setup._read_wav_body(TooLarge(), max_bytes=4)

    class Incomplete:
        headers = {"Content-Length": "5"}
        rfile = io.BytesIO(b"123")

    with pytest.raises(correction_setup.BadRequest, match="incomplete"):
        correction_setup._read_wav_body(Incomplete())


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
    body = _module_js()  # behaviour relocated to the static ES module
    assert "echoCancellation: false" in body
    assert "noiseSuppression: false" in body
    assert "autoGainControl: false" in body
    # And the constructor pin for sample rate.
    assert "sampleRate: REQUIRED_SR" in body


def test_render_page_includes_mic_picker_and_calibration_controls():
    # Markup (the picker + model dropdown) stays in the page; the device
    # enumeration + calibration fetch/upload plumbing moved to the module.
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="input-device-select"' in body
    assert 'id="mic-model-select"' in body
    assert "Dayton Audio iMM-6 / iMM-6C" in body
    assert "miniDSP UMIK-1" in body
    js = _module_js()
    assert "enumerateDevices" in js
    assert "audioConstraints.deviceId = {exact: inputDeviceSelect.value}" in js
    assert "calibration/fetch" in js
    assert "calibration/upload" in js
    assert "calibration_id: selectedCalibrationId" in js
    assert "function invalidateLoadedCalibration()" in js
    assert "micSerialInput.addEventListener('input'" in js
    assert "micOrientationSelect.addEventListener('change'" in js
    assert "calibrationSignSelect.addEventListener('change'" in js
    assert "calibrationFileInput.addEventListener('change'" in js


def test_render_page_includes_browser_audio_path_report():
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="browser-audio-report"' in body  # markup stays in the page
    js = _module_js()  # rendering logic moved to the module
    assert "function renderBrowserAudioReport(report)" in js
    assert "renderBrowserAudioLocal(actual, problems)" in js
    assert "browser_audio_report" in js


def test_sanitize_input_device_hashes_browser_ids():
    raw = {
        "device_id": "raw-device-id",
        "requested_device_id": "requested-device-id",
        "actual_device_id": "actual-device-id",
        "label": "USB measurement mic",
        "browser_label": "Dayton Audio USB",
        "sample_rate": 48000,
        "channel_count": 1,
        "echo_cancellation": False,
        "noise_suppression": False,
        "auto_gain_control": False,
        "ignored": "drop me",
    }
    out = correction_setup._sanitize_input_device(raw)
    assert out["label"] == "USB measurement mic"
    assert out["sample_rate"] == 48000.0
    assert out["echo_cancellation"] is False
    assert "ignored" not in out
    assert "raw-device-id" not in str(out)
    assert out["device_id_hash"]
    assert out["requested_device_id_hash"]
    assert out["actual_device_id_hash"]


def test_render_page_reads_back_settings_for_verify():
    """After getUserMedia, the JS must call getSettings() and surface
    a red banner if EC/NS/AGC didn't actually take effect. If this
    check ever falls out, future phases would silently measure with
    Safari's processed audio — which is exactly the wrong thing."""
    body = _module_js()  # behaviour relocated to the static ES module
    assert ".getSettings()" in body
    # All three constraint names appear in the verify section.
    assert "actual.echoCancellation" in body
    assert "actual.noiseSuppression" in body
    assert "actual.autoGainControl" in body


def test_verify_capture_starts_before_server_sweep_request():
    """Verification must arm browser capture before POST /verify triggers
    the server-side sweep. Otherwise the verification recording can miss
    the first part of playback on real hardware."""
    body = _module_js()
    start = body.index("async function startVerify()")
    end = body.index("// Centralised button-state policy", start)
    fn = body[start:end]
    assert fn.index("postMessage('startCapture')") < fn.index(
        "await postJson('verify', {})"
    )
    assert "captureMode = 'discard'" in fn
    assert "postMessage('stopCapture')" in fn


def test_render_page_does_not_loop_mic_back_to_speaker():
    """A naive 'src.connect(node); node.connect(ctx.destination)' would
    play the mic back through the phone speaker. Acceptable on a
    laptop, terrible on a smart speaker that's the room's TARGET (the
    feedback loop would be instant and ear-melting). Keep the comment
    that documents the deliberate omission as a regression pin."""
    body = _module_js()  # behaviour relocated to the static ES module
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
    body = _module_js()  # behaviour relocated to the static ES module
    assert "AudioWorkletProcessor" in body
    assert "AudioWorkletNode" in body
    assert "audioWorklet.addModule" in body


def test_render_page_requests_wake_lock():
    """A 2-minute sweep on iOS Safari without Wake Lock = screen
    locks mid-measurement = AudioContext suspended = capture lost.
    Pin the request here so it's not optimized away later."""
    body = _module_js()  # behaviour relocated to the static ES module
    assert "wakeLock" in body
    assert "screen" in body  # request type


def test_render_page_treats_undefined_constraints_as_ok():
    """iOS Safari often returns `undefined` from getSettings() for
    echoCancellation / noiseSuppression / autoGainControl rather
    than echoing back the requested value. Undefined ≠ true ⇒ the
    feature is off (iOS has these off by default for getUserMedia).
    The page must NOT mark undefined as 'bad' — that was a
    real first-pass-test bug. Pin the corrected behavior."""
    body = _module_js()  # behaviour relocated to the static ES module
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
    # All three control buttons present (start + manual-lock + cancel) — markup.
    assert 'id="autolevel"' in body
    assert 'id="autolevel-lock"' in body
    assert 'id="autolevel-cancel"' in body
    assert "Auto-level" in body
    assert "Lock now" in body
    # JS handlers exist + target the right endpoints (now in the module).
    js = _module_js()
    assert "startAutolevel" in js
    assert "autolevel/start" in js
    assert "autolevel/lock" in js
    # Adaptive target band — computed from measured noise floor at
    # the start of autolevel rather than hard-coded.
    assert "computeTargetBand" in js
    assert "AUTOLEVEL_SNR_DESIRED_LOW" in js
    assert "AUTOLEVEL_SNR_DESIRED_HIGH" in js
    # Preflight noise-floor measurement step is present.
    assert "Measuring room noise" in js


def test_render_page_includes_strategy_and_design_audit_controls():
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="strategy-select"' in body  # picker markup stays in the page
    assert "Balanced" in body
    assert "Assertive" in body
    assert 'id="design-report"' in body
    js = _module_js()  # the wiring + render moved to the module
    assert "strategy_choice: strategyChoice" in js
    assert "renderDesignReport" in js


def test_render_page_includes_results_visualization_controls():
    body = correction_setup._render_page("jts.local").decode()
    # Markup containers + chart controls stay in the page.
    assert 'id="results-summary"' in body
    assert 'id="chart-smoothing"' in body
    assert 'id="chart-show-spread"' in body
    assert 'id="chart-show-filter"' in body
    assert 'id="chart-show-band"' in body
    assert 'id="runtime-integrity-panel"' in body
    assert "spatial spread" in body
    js = _module_js()  # the renderers moved to the module
    assert "renderResultsSummary" in js
    assert "renderRuntimeIntegrity" in js
    assert "recommendedNextAction" in js


def test_render_page_includes_read_only_measurement_reports():
    body = correction_setup._render_page("jts.local").decode()
    # Section containers stay in the page; the report fetch/render/strings
    # moved into the module.
    assert 'id="measurement-reports"' in body
    assert 'id="session-history"' in body
    assert 'id="session-report"' in body
    js = _module_js()
    assert "loadSessionReports" in js
    assert "session-report?id=" in js
    assert "session/delete" in js
    assert "Private raw recordings" in js
    assert "What looks trustworthy" in js


def test_render_page_includes_noise_and_repeat_capture_flow():
    body = correction_setup._render_page("jts.local").decode()
    assert 'id="repeat-main-position"' in body  # markup stays in the page
    assert 'id="repeat-position"' in body
    js = _module_js()  # the capture/upload flow moved to the module
    assert "capturePreSweepNoise" in js
    assert "upload-noise" in js
    assert "repeat-position" in js
    assert "awaiting_repeat_capture" in js


def test_render_page_shows_result_before_drawing_chart():
    """Bug fix pin: drawChart() must run AFTER `resultSection` is
    shown, otherwise the canvas's getBoundingClientRect returns
    0×0 (hidden ancestor) and the chart renders blank. Real user
    bug — got 5 PEQ filters but an empty frequency-response box.
    """
    body = _module_js()  # behaviour relocated to the static ES module
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
    body = _module_js()  # behaviour relocated to the static ES module
    assert "scheduleChartRedraw" in body
    assert "orientationchange" in body


def test_render_page_autolevel_target_band_clamps():
    """Pin the absolute clamps: -30 dBFS floor (don't lock super
    quiet even in dead-silent rooms) and -10 dBFS ceiling (avoid
    pushing the iPhone mic toward clipping). A regression here
    would cause silent off-by-default-target failures we'd only
    catch on hardware."""
    body = _module_js()  # behaviour relocated to the static ES module
    assert "AUTOLEVEL_TARGET_DB_FLOOR = -30" in body
    assert "AUTOLEVEL_TARGET_DB_CEILING = -10" in body


def test_render_page_amp_message_is_generic_not_tpa3255():
    """First pass said 'TPA3255 amp knob' — wrong because (a) users
    don't know what that is, and (b) they might be on a different
    amp. Generic 'turn up your amplifier' is the right wording.
    Pin the wording so a future revision doesn't accidentally
    reintroduce the brand-specific text."""
    # The amp wording lives in the autolevel status copy, which moved into
    # the module; the brand-specific text must not reappear in either surface.
    body = correction_setup._render_page("jts.local").decode()
    js = _module_js()
    combined = (body + js).lower()
    assert "turn up your amplifier" in combined or "turn up your amp" in combined
    assert "TPA3255" not in body
    assert "TPA3255" not in js


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
    body = _module_js()  # behaviour relocated to the static ES module
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


def test_e2e_bonded_follower_rejects_correction_mutation(monkeypatch):
    monkeypatch.setattr(correction_setup, "bonded_follower_active", lambda: True)
    server, base = _start_server()
    try:
        resp = request_with_csrf(
            base,
            "/apply",
            json.dumps({}).encode("utf-8"),
            content_type="application/json",
            expect_status=409,
        )
        payload = json.loads(resp.read().decode("utf-8"))
        assert "controlled on the pair leader" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_start_safety_refusal_returns_422(monkeypatch):
    from jasper.correction.runtime_safety import CorrectionRuntimeSafetyError

    def fake_start(handler):
        raise CorrectionRuntimeSafetyError("flat sweep is unsafe")

    monkeypatch.setattr(correction_setup, "_handle_start", fake_start)
    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/start",
            b"{}",
            content_type="application/json",
            expect_status=422,
        )
        body = json.loads(e.read().decode())
        assert "flat sweep is unsafe" in body["error"]
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


def test_e2e_calibration_upload_parses_and_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    server, base = _start_server()
    try:
        payload = json.dumps({
            "filename": "lab.txt",
            "content": "20 -1\n100 0\n1000 1\n",
            "model": "other",
            "label": "Lab mic",
            "sign_convention": "correction",
        }).encode()
        resp = request_with_csrf(
            base,
            "/calibration/upload",
            payload,
            content_type="application/json",
        )
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["calibration"]["provider"] == "manual_upload"
        assert data["calibration"]["point_count"] == 3
        assert data["calibration"]["calibration_id"]
        assert data["preview"]["freqs_hz"][0] == 20.0
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_calibration_upload_bad_file_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    server, base = _start_server()
    try:
        payload = json.dumps({
            "filename": "bad.txt",
            "content": "this is not a calibration file",
            "model": "other",
            "label": "Lab mic",
        }).encode()
        e = request_with_csrf(
            base,
            "/calibration/upload",
            payload,
            content_type="application/json",
            expect_status=400,
        )
        body = json.loads(e.read().decode())
        assert "at least 2 rows" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_invalid_json_returns_400():
    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/calibration/upload",
            b"{not json",
            content_type="application/json",
            expect_status=400,
        )
        body = json.loads(e.read().decode())
        assert "invalid JSON" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_calibration_fetch_upstream_failure_returns_502(monkeypatch):
    from jasper.correction import calibration

    def fake_fetch_vendor_calibration(**kwargs):
        raise calibration.CalibrationUpstreamError("miniDSP unavailable")

    monkeypatch.setattr(
        calibration,
        "fetch_vendor_calibration",
        fake_fetch_vendor_calibration,
    )
    server, base = _start_server()
    try:
        payload = json.dumps({
            "model": "minidsp_umik2",
            "serial": "810-8494",
        }).encode()
        e = request_with_csrf(
            base,
            "/calibration/fetch",
            payload,
            content_type="application/json",
            expect_status=502,
        )
        body = json.loads(e.read().decode())
        assert body["error"] == "miniDSP unavailable"
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_upload_quality_failure_returns_422(tmp_path, monkeypatch):
    from jasper.correction import quality
    from jasper.correction.session import SessionState

    report = quality.CaptureQuality(
        sample_rate=48000,
        duration_s=1.0,
        peak_dbfs=0.0,
        rms_dbfs=-3.0,
        clipped_fraction=0.1,
        issues=(
            quality.QualityIssue(
                code="capture_clipped",
                severity="fail",
                message="capture clipped; lower speaker volume and re-measure",
            ),
        ),
    )
    report_dict = report.to_dict()
    report_dict["capture_kind"] = "measurement"
    report_dict["position_index"] = 0
    report_dict["artifact_path"] = "captures/p0.wav"

    class FakeSession:
        session_id = "quality-fail"
        state = SessionState.AWAITING_CAPTURE
        current_position = 0
        total_positions = 1
        capture_quality: list[dict] = []
        verify_quality = None
        measured_curve = None
        target_curve = None
        predicted_curve = None
        verify_curve = None
        verify_metrics = None
        peqs = []
        design_report = None
        confidence_report = None

        def capture_path_for_position(self, position: int):
            return tmp_path / f"p{position}.wav"

        async def on_capture_uploaded(self, path):
            self.state = SessionState.FAILED
            self.capture_quality = [report_dict]
            raise quality.CaptureQualityError(report)

    fake = FakeSession()
    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: fake,
    )

    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/upload-capture",
            b"not really a wav",
            content_type="audio/wav",
            expect_status=422,
        )
        body = json.loads(e.read().decode())
        assert "capture quality failed" in body["error"]
        assert body["state"] == "failed"
        assert body["capture_quality"][0]["capture_kind"] == "measurement"
        assert body["capture_quality"][0]["issues"][0]["code"] == (
            "capture_clipped"
        )
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_correction_posts_require_csrf():
    server, base = _start_server()
    try:
        req = urllib.request.Request(
            f"{base}/calibration/upload",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            assert e.code == 403
        else:
            raise AssertionError("expected HTTP 403")
    finally:
        server.shutdown()
        server.server_close()


def test_sync_analyze_rejects_oversized_capture_before_body_read():
    handler_cls = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_cls.__new__(handler_cls)
    handler.headers = Message()
    handler.headers["Content-Length"] = str(2 * 1024 * 1024 + 1)
    handler.rfile = io.BytesIO(b"")
    sent: dict = {}

    def _send_json(payload, status=200):
        sent["payload"] = payload
        sent["status"] = int(status)

    handler._send_json = _send_json

    handler._dispatch_sync("/sync/analyze")

    assert sent["status"] == 400
    assert "WAV body too large" in sent["payload"]["error"]


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


# --- Bug 1 regression: calibration↔device mismatch backstop -----------------
# A vendor measurement-mic calibration applied to phone-built-in-mic audio
# silently invalidates the measurement. The browser blocks it, but this
# server-side gate is the reliable backstop. Reproduces the cmm31555 iMM-6C
# run on 2026-06-04 where input_device.browser_label was "iPhone Microphone".
import types  # noqa: E402


def _cal(provider):
    return types.SimpleNamespace(provider=provider)


def test_calibration_device_mismatch_blocks_vendor_mic_on_builtin():
    for label in ("iPhone Microphone", "iPad Microphone", "MacBook Pro Microphone",
                  "Built-in Microphone", "Default"):
        msg = correction_setup._calibration_device_mismatch(
            _cal("dayton_audio"), {"browser_label": label}
        )
        assert msg is not None, label
        assert "USB" in msg
    # miniDSP is also an external-only provider
    assert correction_setup._calibration_device_mismatch(
        _cal("minidsp"), {"browser_label": "iPhone Microphone"}
    ) is not None


def test_calibration_device_mismatch_allows_real_usb_mic():
    for label in ("iMM-6C", "USB Audio Device", "UMIK-1", "Microphone 2"):
        assert correction_setup._calibration_device_mismatch(
            _cal("dayton_audio"), {"browser_label": label}
        ) is None, label


def test_calibration_device_mismatch_ignores_manual_and_absent():
    # Manual "other" upload: we can't assume it isn't a phone curve — don't gate.
    assert correction_setup._calibration_device_mismatch(
        _cal("other"), {"browser_label": "iPhone Microphone"}
    ) is None
    # No calibration / no device → nothing to check.
    assert correction_setup._calibration_device_mismatch(
        None, {"browser_label": "iPhone Microphone"}
    ) is None
    assert correction_setup._calibration_device_mismatch(
        _cal("dayton_audio"), None
    ) is None


def test_render_page_emits_registry_model_aliases():
    # Inference is registry-driven: each model option carries data-aliases
    # from SUPPORTED_MODELS so the frontend has no hardcoded mic map to drift.
    body = correction_setup._render_page("jts.local").decode()
    assert 'value="dayton_imm6"' in body
    assert 'data-aliases="iMM-6"' in body
    assert 'data-aliases="umik-2"' in body


# --- Audio-safety regression: autolevel volume must be restored even when ----
# apply()/reset() raises. Autolevel ramps main_volume well above the listening
# level for measurement SNR; if a failed apply/reset skipped the restore, the
# next song would play back at the (loud) measurement level. The restore now
# lives in a finally so the exception can't strand the speaker loud.
def _locked_autolevel_session(raises_on, *, original=-20.0):
    """Fake session whose apply/reset raises, with a LOCKED autolevel that
    ramped main_volume up to a measurement level above `original`."""
    from jasper.correction.session import AutolevelData, AutolevelStatus, SessionState

    class _FakeSession:
        session_id = "vol-strand"
        state = SessionState.READY
        config_path = None

        def __init__(self):
            self.autolevel = AutolevelData(
                status=AutolevelStatus.LOCKED,
                original_main_volume_db=original,
                locked_main_volume_db=-8.0,
            )

        async def apply(self, set_cb, camilla_get_config=None):
            if raises_on == "apply":
                raise RuntimeError("CamillaDSP reload failed")

        async def reset(self, set_cb, **kwargs):
            if raises_on == "reset":
                raise RuntimeError("reset reload failed")

    return _FakeSession()


def _volume_recording_cam(restored):
    class _FakeCam:
        async def set_config_file_path(self, path, best_effort=False):
            return True

        async def get_config_file_path(self, best_effort=True):
            return None

        async def set_volume_db(self, db, best_effort=False):
            restored.append(db)

    return _FakeCam()


def test_apply_restores_listening_volume_when_apply_raises(monkeypatch):
    restored: list[float] = []
    sess = _locked_autolevel_session("apply", original=-20.0)
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_setup, "_camilla", lambda: _volume_recording_cam(restored)
    )

    with pytest.raises(RuntimeError):
        correction_setup._handle_apply(None)

    # The apply exception propagated, but the finally still restored volume.
    assert restored == [-20.0]


def test_reset_restores_listening_volume_when_reset_raises(monkeypatch):
    restored: list[float] = []
    sess = _locked_autolevel_session("reset", original=-18.0)
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: sess)
    monkeypatch.setattr(
        correction_setup, "_camilla", lambda: _volume_recording_cam(restored)
    )

    with pytest.raises(RuntimeError):
        correction_setup._handle_reset(None)

    assert restored == [-18.0]


def test_maybe_restore_main_volume_swallows_restore_failure():
    # The restore runs inside apply/reset's finally; a failed restore must not
    # raise (which would mask the original apply/reset error).
    from jasper.correction.session import (
        AutolevelData,
        AutolevelStatus,
        SessionState,
    )

    class _FailingCam:
        async def set_volume_db(self, db, best_effort=False):
            raise RuntimeError("CamillaDSP websocket down")

    sess = types.SimpleNamespace(
        state=SessionState.APPLIED,  # settled, so we reach the (failing) restore
        autolevel=AutolevelData(
            status=AutolevelStatus.LOCKED, original_main_volume_db=-20.0
        ),
    )
    # Must not raise.
    correction_setup._maybe_restore_main_volume(sess, _FailingCam())


def test_maybe_restore_skips_while_measurement_still_active():
    # A reset rejected during a sweep (the server refuses it; see PR #737's
    # SessionBusyError guard) leaves the session mid-measurement. The restore
    # must NOT drop the ramped sweep level underneath the active measurement.
    from jasper.correction.session import (
        AutolevelData,
        AutolevelStatus,
        SessionState,
    )

    restored: list[float] = []
    for active in (
        SessionState.PREPARING,
        SessionState.SWEEPING,
        SessionState.ANALYZING,
        SessionState.VERIFYING,
    ):
        sess = types.SimpleNamespace(
            state=active,
            autolevel=AutolevelData(
                status=AutolevelStatus.LOCKED, original_main_volume_db=-20.0
            ),
        )
        correction_setup._maybe_restore_main_volume(
            sess, _volume_recording_cam(restored)
        )
    assert restored == []  # skipped in every active state


def test_maybe_restore_runs_once_the_workflow_has_settled():
    # The normal post-apply / post-reset case still restores the listening
    # level — the guard only fences the mid-measurement states.
    from jasper.correction.session import (
        AutolevelData,
        AutolevelStatus,
        SessionState,
    )

    for settled in (
        SessionState.APPLIED,
        SessionState.IDLE,
        SessionState.FAILED,
    ):
        restored: list[float] = []
        sess = types.SimpleNamespace(
            state=settled,
            autolevel=AutolevelData(
                status=AutolevelStatus.LOCKED, original_main_volume_db=-20.0
            ),
        )
        correction_setup._maybe_restore_main_volume(
            sess, _volume_recording_cam(restored)
        )
        assert restored == [-20.0], settled


def test_needs_noise_capture_offers_cancel_in_ui():
    # The stranded-noise-capture dead-end: needs_noise_capture waits on an
    # automatic browser upload that can fail (denied mic / backgrounded tab),
    # so the UI must offer Cancel there — pairs with the server-side watchdog.
    js = _module_js()
    block = js.split("var cancellableStates = [", 1)[1].split("]", 1)[0]
    assert "'needs_noise_capture'" in block


def test_e2e_reset_while_busy_returns_409(monkeypatch):
    # A reset rejected because a sweep/analysis is in flight is a state
    # conflict, not a server error — the dispatch maps SessionBusyError to 409
    # (a stale/buggy client hitting /reset mid-sweep; the UI never does).
    from jasper.correction.session import SessionBusyError, SessionState

    class FakeSession:
        session_id = "busy-reset"
        state = SessionState.SWEEPING

        async def reset(self, set_cb):
            raise SessionBusyError(
                "cannot reset while sweeping — analysis is in progress"
            )

    monkeypatch.setattr(
        correction_setup, "_get_or_create_session", lambda: FakeSession(),
    )

    server, base = _start_server()
    try:
        e = request_with_csrf(
            base, "/reset", b"{}",
            content_type="application/json", expect_status=409,
        )
        body = json.loads(e.read().decode())
        assert "in progress" in body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_reset_safety_refusal_returns_422(monkeypatch):
    from jasper.correction.runtime_safety import CorrectionRuntimeSafetyError

    def fake_reset(handler):
        raise CorrectionRuntimeSafetyError("no legal graph is available")

    monkeypatch.setattr(correction_setup, "_handle_reset", fake_reset)
    server, base = _start_server()
    try:
        e = request_with_csrf(
            base,
            "/reset",
            b"{}",
            content_type="application/json",
            expect_status=422,
        )
        body = json.loads(e.read().decode())
        assert "no legal graph" in body["error"]
    finally:
        server.shutdown()
        server.server_close()
