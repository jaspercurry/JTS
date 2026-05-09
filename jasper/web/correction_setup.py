"""Room correction wizard at /correction/.

Phase 0 — skeleton. Serves a page that requests mic permission, opens
an AudioContext at 48 kHz, runs an AudioWorklet RMS meter, and reads
back `getSettings()` to verify Safari actually honored the
`echoCancellation: false` / `noiseSuppression: false` /
`autoGainControl: false` constraints. Big red banner if it didn't.

This is the foundation Phase 1 will extend — the same page becomes
the sweep launcher; the same AudioWorklet pattern becomes the sweep
capture; the same getSettings-verify becomes a hard refusal to start
the measurement when constraints are unhonored.

Why a separate service from jasper-web (which serves /spotify and
/voice): future phases pull in numpy / scipy / pyfar / pyrato — heavy
deps that should not be in the Python process serving Spotify OAuth
and voice-provider config. Mirrors the jasper-dial-web split (heavy
USB / esptool deps in their own process). See
docs/HANDOFF-correction.md for the full architecture and the
sequenced phase plan.

URL surface (after nginx strips the /correction/ prefix):
  GET  /                          page render
  GET  /healthz                   liveness (text/plain "ok")

Phase 1+ adds:
  POST /start                     begin measurement session
  GET  /events?session=…          SSE progress stream
  POST /upload-capture            captured WAV from AudioWorklet
  POST /apply                     SetConfig + Reload
  GET  /export.frd?session=…      REW-compatible export
  ...
"""
from __future__ import annotations

import argparse
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ._common import PAGE_STYLE

logger = logging.getLogger(__name__)


# Required AudioContext sample rate. CamillaDSP runs at 48 kHz; if
# the AudioContext lands on anything else the sweep + capture math
# is off, so we refuse to proceed. iOS 17.5 had an
# AudioWorklet+44.1 kHz regression that distorted output — pinning
# 48 kHz here heads that off.
REQUIRED_SAMPLE_RATE = 48000


# Page CSS additions on top of the shared PAGE_STYLE — the level-meter
# bar and the "constraint check" status table.
_CORRECTION_PAGE_STYLE = PAGE_STYLE + """
  .level-bar-track {
    height: 28px; background: #e8e8e8; border-radius: 4px;
    overflow: hidden; margin: 0.5em 0 0.2em;
  }
  .level-bar-fill {
    height: 100%; background: linear-gradient(to right,
      #1db954 0%, #1db954 70%, #ff9800 88%, #d44 100%);
    transition: width 0.05s linear; width: 0%;
  }
  .level-readout {
    font-variant-numeric: tabular-nums; color: #666; font-size: 0.9em;
  }

  .constraint-table {
    width: 100%; border-collapse: collapse; margin: 1em 0;
    font-size: 0.93em;
  }
  .constraint-table th, .constraint-table td {
    text-align: left; padding: 0.4em 0.6em; border-bottom: 1px solid #eee;
  }
  .constraint-table th { color: #555; font-weight: 600; }
  .constraint-table .ok { color: #1db954; font-weight: 600; }
  .constraint-table .bad { color: #d44; font-weight: 600; }

  .err-banner {
    background: #ffe8e8; border: 1px solid #d99;
    border-radius: 6px; padding: 0.7em 0.9em; margin: 1em 0;
    color: #800;
  }
  .err-banner.hidden { display: none; }

  details.advice {
    background: #f4f4f4; border-radius: 6px; padding: 0.5em 0.8em 0.7em;
    margin: 1em 0;
  }
  details.advice summary { cursor: pointer; font-weight: 600; }
  details.advice ol { margin: 0.5em 0; }

  .hidden { display: none; }
"""


# Static page template. Substitution is plain string-replace because
# Jinja / string.Template is overkill for three placeholders and the
# JS body has `{...}` braces that would collide with `.format()` if
# we ever switched. The placeholders:
#   __STYLE__         page CSS (shared + correction-specific)
#   __HOSTNAME__      JASPER_HOSTNAME for the cert-download fallback link
#   __REQUIRED_SR__   the AudioContext sample rate we pin (48000)
#
# Stored as `str` (not `bytes`) so the unicode characters in the
# user-facing copy (em dashes, arrows, check / x marks) round-trip
# cleanly. _render_page() does the single .encode() at the boundary.
_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Room correction — JTS speaker</title>
<style>__STYLE__</style>
</head>
<body>
<h1>Room correction</h1>
<p class="sub">Phase 0 — verify your phone can capture mic audio at the right sample rate. Future updates will add the actual sweep and the correction filters (see docs/HANDOFF-correction.md).</p>

<details class="advice" open>
  <summary>Place your phone correctly before tapping Start</summary>
  <ol>
    <li>Lay the phone <strong>flat, screen up</strong>, on the seat where you usually listen.</li>
    <li>Point the <strong>bottom edge</strong> (the speaker / mic end) toward the speakers.</li>
    <li>Take it out of any case for the most accurate measurement.</li>
    <li>Keep the room quiet — close windows, mute other devices, no talking.</li>
  </ol>
  <p class="hint">This mirrors what WiiM RoomFit and HouseCurve recommend. iOS Safari doesn't expose a mic-selection API, so the bottom-mic-toward-speakers trick is the only way to get a consistent capture orientation.</p>
</details>

<button id="start" type="button">Start mic capture</button>

<div id="constraints" class="hidden" aria-live="polite">
  <h2>Capture settings</h2>
  <p class="hint">iOS Safari is allowed to silently ignore audio constraints (WebKit Bug 179411). The Phase 1 sweep capture will <em>refuse to run</em> until every row below reads <span class="ok">✓ ok</span>.</p>
  <table class="constraint-table">
    <thead><tr><th>Setting</th><th>Requested</th><th>Actual</th><th>Status</th></tr></thead>
    <tbody id="constraint-rows"></tbody>
  </table>
  <div id="err-banner" class="err-banner hidden"></div>

  <h2>Live mic level</h2>
  <p class="hint">RMS computed in the AudioWorklet thread, posted at ~20 Hz. Tap the table or talk into the bottom of the phone — the bar should respond within 50 ms.</p>
  <div class="level-bar-track" aria-label="microphone level">
    <div id="level-bar-fill" class="level-bar-fill"></div>
  </div>
  <div class="level-readout">RMS: <span id="level-db">—</span> dBFS</div>
</div>

<p class="hint" style="margin-top:2em">
  Cert trust trouble? <a href="http://__HOSTNAME__/jts-root-ca.crt">Download the JTS root CA</a> on plain HTTP, then in iOS go to Settings → General → VPN &amp; Device Management → install the profile, then Settings → General → About → Certificate Trust Settings → toggle JTS Speaker on.
</p>

<script>
(function () {
  'use strict';

  var REQUIRED_SR = __REQUIRED_SR__;

  var startBtn = document.getElementById('start');
  var constraintsBlock = document.getElementById('constraints');
  var rowsTbody = document.getElementById('constraint-rows');
  var errBanner = document.getElementById('err-banner');
  var levelBar = document.getElementById('level-bar-fill');
  var levelReadout = document.getElementById('level-db');

  function renderConstraints(actual, problems) {
    var rows = [
      ['sampleRate', REQUIRED_SR + ' Hz', actual.sampleRate + ' Hz',
       actual.sampleRate === REQUIRED_SR],
      ['echoCancellation', 'false', String(actual.echoCancellation),
       actual.echoCancellation === false],
      ['noiseSuppression', 'false', String(actual.noiseSuppression),
       actual.noiseSuppression === false],
      ['autoGainControl', 'false', String(actual.autoGainControl),
       actual.autoGainControl === false],
      ['channelCount', '1', String(actual.channelCount),
       actual.channelCount === 1]
    ];
    rowsTbody.innerHTML = '';
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td>' + r[0] + '</td>' +
        '<td>' + r[1] + '</td>' +
        '<td>' + r[2] + '</td>' +
        '<td class="' + (r[3] ? 'ok' : 'bad') + '">' +
          (r[3] ? '✓ ok' : '✗ bad') +
        '</td>';
      rowsTbody.appendChild(tr);
    });
    if (problems.length > 0) {
      errBanner.textContent =
        'Capture settings did not match what we requested: ' +
        problems.join(', ') +
        '. Phase 1 sweep capture will refuse to start in this state.';
      errBanner.classList.remove('hidden');
    } else {
      errBanner.classList.add('hidden');
    }
  }

  async function start() {
    startBtn.disabled = true;
    startBtn.textContent = 'Capturing…';

    // AudioContext must be created inside a user-gesture handler on
    // iOS Safari. The constructor option pins the rate to what we'll
    // need for sweep playback at parity in Phase 1.
    var ctx;
    try {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      ctx = new Ctor({sampleRate: REQUIRED_SR});
    } catch (e) {
      alert('Could not create AudioContext: ' + e.message);
      startBtn.disabled = false;
      startBtn.textContent = 'Start mic capture';
      return;
    }

    var stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          sampleRate: REQUIRED_SR,
          channelCount: 1
        },
        video: false
      });
    } catch (e) {
      alert('Microphone permission denied or unavailable: ' + e.message);
      startBtn.disabled = false;
      startBtn.textContent = 'Start mic capture';
      return;
    }

    constraintsBlock.classList.remove('hidden');

    // Read back what we ACTUALLY got. Safari often ignores audio
    // constraints (WebKit Bug 179411 historically). The verify-not-
    // trust pattern is load-bearing for this whole feature.
    var settings = stream.getAudioTracks()[0].getSettings();
    var actual = {
      sampleRate: settings.sampleRate || ctx.sampleRate,
      echoCancellation: settings.echoCancellation,
      noiseSuppression: settings.noiseSuppression,
      autoGainControl: settings.autoGainControl,
      channelCount: settings.channelCount || 1
    };
    var problems = [];
    if (actual.sampleRate !== REQUIRED_SR) problems.push('sampleRate');
    if (actual.echoCancellation) problems.push('echoCancellation enabled');
    if (actual.noiseSuppression) problems.push('noiseSuppression enabled');
    if (actual.autoGainControl) problems.push('autoGainControl enabled');
    renderConstraints(actual, problems);

    // Inline AudioWorklet — RMS in the worklet thread, posted to
    // the main thread at ~20 Hz. Same pattern carries into Phase 1
    // (sweep capture). Worklet source is loaded from a Blob URL so we
    // don't need a second route on the server for a tiny file.
    var workletSrc =
      'class M extends AudioWorkletProcessor {' +
        'constructor(){super();this.r=0;this.n=0;}' +
        'process(inp){' +
          'var ch=inp[0]&&inp[0][0];if(!ch)return true;' +
          'var s=0;for(var i=0;i<ch.length;i++)s+=ch[i]*ch[i];' +
          'this.r+=s;this.n+=ch.length;' +
          'if(this.n>=2400){' +  // ~50 ms at 48 kHz
            'var rms=Math.sqrt(this.r/this.n);' +
            'this.port.postMessage(rms);' +
            'this.r=0;this.n=0;' +
          '}return true;' +
        '}}' +
      'registerProcessor("m",M);';
    var blobUrl = URL.createObjectURL(
      new Blob([workletSrc], {type: 'application/javascript'})
    );
    try {
      await ctx.audioWorklet.addModule(blobUrl);
    } catch (e) {
      alert('AudioWorklet load failed: ' + e.message);
      return;
    }
    var src = ctx.createMediaStreamSource(stream);
    var node = new AudioWorkletNode(ctx, 'm');
    node.port.onmessage = function (ev) {
      var rms = ev.data;
      var db = rms > 0 ? 20 * Math.log10(rms) : -120;
      // Map -60..0 dB to 0..100% bar width.
      var pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
      levelBar.style.width = pct.toFixed(1) + '%';
      levelReadout.textContent = db.toFixed(1);
    };
    src.connect(node);
    // Don't connect node → destination — we don't want to
    // play the mic back into the speaker (instant feedback loop,
    // especially bad on a smart speaker that's the listener's
    // TARGET).

    startBtn.textContent = 'Capturing (mic level live below)';

    // Wake Lock during capture — prevents the screen from
    // locking mid-measurement. Requires HTTPS (we have it) and
    // Safari 16.4+. Best-effort; older Safari just doesn't have the
    // API.
    try {
      if ('wakeLock' in navigator) {
        await navigator.wakeLock.request('screen');
      }
    } catch (e) {
      console.warn('wakeLock not granted', e);
    }
  }

  startBtn.addEventListener('click', function () { start(); });
})();
</script>
</body>
</html>
"""


def _render_page(hostname: str) -> bytes:
    """Render the static page with hostname + sample rate substituted in.
    String-replace rather than `.format()` because the JS body has
    `{...}` braces that would collide with Python format syntax, and
    pulling in Jinja for three placeholders is overkill. Single
    .encode() at the end keeps the unicode round-trip simple."""
    return (
        _PAGE_HTML
        .replace("__STYLE__", _CORRECTION_PAGE_STYLE)
        .replace("__HOSTNAME__", hostname)
        .replace("__REQUIRED_SR__", str(REQUIRED_SAMPLE_RATE))
    ).encode("utf-8")


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                self._send_html(_render_page(cfg["hostname"]))
                return
            if path == "/healthz":
                # Plain text — systemd / curl friendly. No JSON
                # dep needed for a one-word response.
                body = b"ok\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def make_server(
    host: str, port: int, *, hostname: str = "jts.local",
) -> ThreadingHTTPServer:
    cfg = {"hostname": hostname}
    return ThreadingHTTPServer((host, port), _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-correction-web",
        description="Room correction wizard at /correction/ for the JTS speaker",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_CORRECTION_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_CORRECTION_WEB_PORT", "8770")),
    )
    parser.add_argument(
        "--hostname",
        default=os.environ.get("JASPER_HOSTNAME", "jts.local"),
        help="speaker hostname used in the cert-download fallback link",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(args.host, args.port, hostname=args.hostname)
    logger.info(
        "jasper-correction-web listening on http://%s:%d (hostname=%s)",
        args.host, args.port, args.hostname,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
