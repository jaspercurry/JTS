"""Room correction wizard at /correction/.

Phase 1 — single-position end-to-end loop. The user opens the page on
iPhone Safari, sees the mic-permission verify (Phase 0), taps "Run
measurement", the speaker plays a sweep, the iPhone records it, the
backend designs PEQ filters, and CamillaDSP hot-swaps the new config.

Architecture (per docs/HANDOFF-correction.md):
  - stdlib `ThreadingHTTPServer` — same pattern as voice_setup,
    spotify_setup, dial_setup. No FastAPI / ASGI dependency.
  - Single in-memory `MeasurementSession` (jasper.correction.session)
    drives the multi-step state machine.
  - Browser polls GET /status every 500 ms — simpler than SSE in
    stdlib, plenty fast for state transitions that take seconds.
  - Background asyncio loop in a daemon thread bridges the sync HTTP
    handlers to the async session methods.
  - HTTP routes (after nginx strips the /correction/ prefix):
      GET  /                page render
      GET  /healthz         liveness
      GET  /status          session snapshot JSON
      POST /start           open measurement window, play sweep
      POST /upload-capture  body = WAV bytes, runs analysis pipeline
      POST /apply           write YAML, reload CamillaDSP
      POST /reset           roll back to /etc/camilladsp/v1.yml

Phase 2 will add multi-position MMM averaging — the chart payload
shape (`measured`/`target`/`predicted` curves + `peqs` list) is
designed not to change, so the frontend can extend without breaking
the existing flow.

Why a separate service from jasper-web (Spotify + voice settings):
this module imports numpy and scipy (via jasper.correction.*). Those
deps load >100 MB into the Python process at import time on the Pi 5.
Keeping that out of the Spotify-OAuth path matters on a 1 GB Pi.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ._common import PAGE_STYLE

logger = logging.getLogger(__name__)


# 48 kHz, EC=NS=AGC=false — pinned by the iOS verify step. The Phase 1
# sweep math assumes the captured signal is at this rate; on mismatch
# we refuse the upload rather than silently resampling (silent
# resampling would produce a working but wrong correction).
REQUIRED_SAMPLE_RATE = 48000


# Module-level session + bridge to the async loop. Lazy-init on
# first use so importing this module is cheap (lets `python -m
# jasper.web.correction_setup --help` work without spinning up a
# loop).
_session_lock = threading.Lock()
_session = None  # type: ignore[var-annotated]
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Start (or reuse) a single background asyncio loop. The HTTP
    handlers schedule coroutines onto it via
    `run_coroutine_threadsafe`."""
    global _loop, _loop_thread
    with _session_lock:
        if _loop is None or not _loop.is_running():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_loop.run_forever,
                name="jasper-correction-loop",
                daemon=True,
            )
            _loop_thread.start()
    return _loop


def _run_async(coro, *, timeout: float = 60.0):
    """Run a coroutine on the background loop and return its result.

    Long timeout default (60 s) covers sweep playback (10 s) + setup
    margin. Endpoints that should be fast (status / apply / reset)
    pass shorter timeouts.
    """
    fut = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    return fut.result(timeout=timeout)


def _get_or_create_session():
    """Single global session. Reset by /reset (which transitions
    APPLIED → IDLE) or by an explicit /start (which creates a fresh
    one regardless of prior state)."""
    from jasper.correction.session import MeasurementSession
    global _session
    if _session is None:
        _session = MeasurementSession()
    return _session


def _replace_session():
    """Replace the global session with a fresh one. Called by /start
    so the user can re-run measurements without restarting the
    daemon."""
    from jasper.correction.session import MeasurementSession
    global _session
    _session = MeasurementSession()
    return _session


# ----------------------------------------------------------------------
# Page CSS + HTML.
# ----------------------------------------------------------------------


_CORRECTION_PAGE_STYLE = PAGE_STYLE + """
  .level-bar-track { height: 28px; background: #e8e8e8; border-radius: 4px;
                     overflow: hidden; margin: 0.5em 0 0.2em; }
  .level-bar-fill { height: 100%;
                    background: linear-gradient(to right,
                      #1db954 0%, #1db954 70%, #ff9800 88%, #d44 100%);
                    transition: width 0.05s linear; width: 0%; }
  .level-readout { font-variant-numeric: tabular-nums; color: #666;
                   font-size: 0.9em; }

  .constraint-table { width: 100%; border-collapse: collapse;
                      margin: 1em 0; font-size: 0.93em; }
  .constraint-table th, .constraint-table td {
    text-align: left; padding: 0.4em 0.6em; border-bottom: 1px solid #eee; }
  .constraint-table th { color: #555; font-weight: 600; }
  .constraint-table .ok { color: #1db954; font-weight: 600; }
  .constraint-table .bad { color: #d44; font-weight: 600; }

  .err-banner { background: #ffe8e8; border: 1px solid #d99;
                border-radius: 6px; padding: 0.7em 0.9em;
                margin: 1em 0; color: #800; }
  .err-banner.hidden { display: none; }

  details.advice { background: #f4f4f4; border-radius: 6px;
                   padding: 0.5em 0.8em 0.7em; margin: 1em 0; }
  details.advice summary { cursor: pointer; font-weight: 600; }
  details.advice ol { margin: 0.5em 0; }

  .hidden { display: none; }

  /* Measurement workflow UI */
  #measure-section button.primary { background: #1db954; }
  #measure-section button.danger  { background: #d44; }
  #measure-section button:disabled { opacity: 0.5; cursor: not-allowed; }

  .state-badge { display: inline-block; padding: 0.2em 0.7em;
                 border-radius: 4px; font-size: 0.85em;
                 font-weight: 600; background: #e8e8e8; color: #333;
                 font-variant-numeric: tabular-nums; }
  .state-badge.preparing      { background: #fff3cd; color: #806000; }
  .state-badge.sweeping       { background: #cfe5ff; color: #003580; }
  .state-badge.awaiting_capture { background: #cfe5ff; color: #003580; }
  .state-badge.analyzing      { background: #fff3cd; color: #806000; }
  .state-badge.ready          { background: #1db954; color: white; }
  .state-badge.applied        { background: #1db954; color: white; }
  .state-badge.failed         { background: #d44; color: white; }
  .state-badge.idle           { background: #e8e8e8; color: #555; }

  .chart-wrap { width: 100%; max-width: 580px; margin: 1em 0;
                background: white; border: 1px solid #ddd;
                border-radius: 4px; }
  canvas#chart { display: block; width: 100%; height: 240px; }

  .peq-list { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
              font-size: 0.92em; }
  .peq-list table { width: 100%; border-collapse: collapse; }
  .peq-list th, .peq-list td { text-align: right; padding: 0.3em 0.6em;
                                border-bottom: 1px solid #eee; }
  .peq-list th:first-child, .peq-list td:first-child { text-align: left; }
"""


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
<p class="sub">Measure your room from this iPhone, design correction filters, and apply them to the speaker. Phase 1 is single-position; Phase 2 will add 5-position averaging.</p>

<details class="advice" open>
  <summary>Place your phone correctly before starting</summary>
  <ol>
    <li>Lay the phone <strong>flat, screen up</strong>, on the seat where you usually listen.</li>
    <li>Point the <strong>bottom edge</strong> (the speaker / mic end) toward the speakers.</li>
    <li>Take it out of any case for the most accurate measurement.</li>
    <li>Keep the room quiet — close windows, mute other devices, no talking during the sweep.</li>
  </ol>
  <p class="hint">This mirrors WiiM RoomFit and HouseCurve. iOS Safari doesn't expose a mic-selection API, so the bottom-mic-toward-speakers trick is the only way to get a consistent capture orientation.</p>
</details>

<button id="start" type="button">Start mic capture</button>

<div id="constraints" class="hidden" aria-live="polite">
  <h2>Capture settings</h2>
  <p class="hint">iOS Safari may silently ignore audio constraints (WebKit Bug 179411). The measurement refuses to start unless every row reads <span class="ok">✓ ok</span>.</p>
  <table class="constraint-table">
    <thead><tr><th>Setting</th><th>Requested</th><th>Actual</th><th>Status</th></tr></thead>
    <tbody id="constraint-rows"></tbody>
  </table>
  <div id="err-banner" class="err-banner hidden"></div>

  <h2>Live mic level</h2>
  <p class="hint">Talk into the bottom of the phone — the bar should respond within 50 ms.</p>
  <div class="level-bar-track" aria-label="microphone level">
    <div id="level-bar-fill" class="level-bar-fill"></div>
  </div>
  <div class="level-readout">RMS: <span id="level-db">—</span> dBFS</div>
</div>

<div id="measure-section" class="hidden">
  <h2>Measurement</h2>
  <p>Music will pause automatically. The sweep is loud — make sure no one is asleep.</p>
  <p>Status: <span id="state-badge" class="state-badge idle">idle</span>
    <span id="state-detail" class="hint"></span></p>
  <p style="display:flex; gap:0.6em; flex-wrap:wrap">
    <button id="run-measurement" type="button" class="primary" disabled>Run measurement</button>
    <button id="apply-correction" type="button" class="primary hidden">Apply correction</button>
    <button id="reset-correction" type="button" class="danger hidden">Reset to flat</button>
  </p>
  <div id="result-section" class="hidden">
    <h3>Frequency response</h3>
    <div class="chart-wrap"><canvas id="chart"></canvas></div>
    <p class="hint">
      <span style="color:#d44">red</span> = measured,
      <span style="color:#888">gray dashed</span> = target,
      <span style="color:#1db954">green</span> = predicted post-correction.
    </p>
    <h3>Filters designed</h3>
    <div class="peq-list" id="peq-list"></div>
  </div>
</div>

<p class="hint" style="margin-top:2em">
  Cert trust trouble? <a href="http://__HOSTNAME__/jts-root-ca.crt">Download the JTS root CA</a> on plain HTTP, install via Settings → General → VPN &amp; Device Management, then enable in Settings → General → About → Certificate Trust Settings.
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
  var measureSection = document.getElementById('measure-section');
  var stateBadge = document.getElementById('state-badge');
  var stateDetail = document.getElementById('state-detail');
  var runBtn = document.getElementById('run-measurement');
  var applyBtn = document.getElementById('apply-correction');
  var resetBtn = document.getElementById('reset-correction');
  var resultSection = document.getElementById('result-section');
  var canvas = document.getElementById('chart');
  var peqList = document.getElementById('peq-list');

  var ctx = null;
  var workletNode = null;
  var pollTimer = null;
  var sessionId = null;
  var currentState = null;
  var lastResult = null;

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
        '<td>' + r[0] + '</td><td>' + r[1] + '</td><td>' + r[2] + '</td>' +
        '<td class="' + (r[3] ? 'ok' : 'bad') + '">' +
          (r[3] ? '✓ ok' : '✗ bad') + '</td>';
      rowsTbody.appendChild(tr);
    });
    if (problems.length > 0) {
      errBanner.textContent =
        'Capture settings did not match what we requested: ' +
        problems.join(', ') +
        '. The measurement will refuse to start in this state.';
      errBanner.classList.remove('hidden');
      runBtn.disabled = true;
    } else {
      errBanner.classList.add('hidden');
      runBtn.disabled = false;
    }
  }

  async function startMicCapture() {
    startBtn.disabled = true;
    startBtn.textContent = 'Capturing…';

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
    measureSection.classList.remove('hidden');

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

    var workletSrc =
      'class M extends AudioWorkletProcessor {' +
        'constructor(){super();this.r=0;this.n=0;this.cap=false;this.buf=[];' +
          'this.port.onmessage=(e)=>{' +
            'if(e.data===\\'startCapture\\'){this.buf=[];this.cap=true;}' +
            'else if(e.data===\\'stopCapture\\'){' +
              'this.cap=false;' +
              'var total=0;for(var i=0;i<this.buf.length;i++)total+=this.buf[i].length;' +
              'var out=new Float32Array(total);var pos=0;' +
              'for(var i=0;i<this.buf.length;i++){out.set(this.buf[i],pos);pos+=this.buf[i].length;}' +
              'this.port.postMessage({type:\\'capture\\',buffer:out.buffer},[out.buffer]);' +
              'this.buf=[];' +
            '}};}' +
        'process(inp){' +
          'var ch=inp[0]&&inp[0][0];if(!ch)return true;' +
          'var s=0;for(var i=0;i<ch.length;i++)s+=ch[i]*ch[i];' +
          'this.r+=s;this.n+=ch.length;' +
          'if(this.n>=2400){' +
            'var rms=Math.sqrt(this.r/this.n);' +
            'this.port.postMessage({type:\\'rms\\',value:rms});' +
            'this.r=0;this.n=0;' +
          '}' +
          'if(this.cap){' +
            'var copy=new Float32Array(ch.length);copy.set(ch);' +
            'this.buf.push(copy);' +
          '}' +
          'return true;' +
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
    workletNode = new AudioWorkletNode(ctx, 'm');
    workletNode.port.onmessage = function (ev) {
      if (ev.data && ev.data.type === 'rms') {
        var rms = ev.data.value;
        var db = rms > 0 ? 20 * Math.log10(rms) : -120;
        var pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
        levelBar.style.width = pct.toFixed(1) + '%';
        levelReadout.textContent = db.toFixed(1);
      } else if (ev.data && ev.data.type === 'capture') {
        onCaptureReady(ev.data.buffer);
      }
    };
    src.connect(workletNode);
    // No mic→destination connection — would create a feedback loop
    // on the smart speaker that's the listening target.

    startBtn.textContent = 'Capturing (mic level live below)';

    try {
      if ('wakeLock' in navigator) {
        await navigator.wakeLock.request('screen');
      }
    } catch (e) {
      console.warn('wakeLock not granted', e);
    }
  }

  function setStateBadge(state, detail) {
    stateBadge.className = 'state-badge ' + state;
    stateBadge.textContent = state;
    stateDetail.textContent = detail || '';
  }

  function renderPEQs(peqs) {
    if (!peqs || peqs.length === 0) {
      peqList.innerHTML = '<p class="hint">No filters needed — your room\\'s bass is already flat (or close enough). Nothing to apply.</p>';
      return;
    }
    var rows = peqs.map(function (p, i) {
      return '<tr><td>peq_' + (i+1) + '</td>' +
             '<td>' + p.freq_hz.toFixed(1) + ' Hz</td>' +
             '<td>Q ' + p.q.toFixed(2) + '</td>' +
             '<td>' + p.gain_db.toFixed(2) + ' dB</td></tr>';
    }).join('');
    peqList.innerHTML =
      '<table><thead><tr><th>Filter</th><th>Freq</th><th>Q</th><th>Gain</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
  }

  function drawChart(measured, target, predicted) {
    var dpr = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    var c = canvas.getContext('2d');
    c.scale(dpr, dpr);
    c.clearRect(0, 0, rect.width, rect.height);

    // Margins
    var ml = 40, mr = 10, mt = 10, mb = 22;
    var W = rect.width - ml - mr;
    var H = rect.height - mt - mb;

    var fMin = 20, fMax = 20000;
    var dbMin = -20, dbMax = 20;

    function fx(f) { return ml + W * (Math.log2(f / fMin) / Math.log2(fMax / fMin)); }
    function fy(db) { return mt + H * (1 - (db - dbMin) / (dbMax - dbMin)); }

    // Grid
    c.strokeStyle = '#e6e6e6'; c.fillStyle = '#888';
    c.font = '11px sans-serif'; c.lineWidth = 1;
    [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach(function (f) {
      var x = fx(f);
      c.beginPath(); c.moveTo(x, mt); c.lineTo(x, mt + H); c.stroke();
      var label = f >= 1000 ? (f / 1000) + 'k' : '' + f;
      c.fillText(label, x - 8, mt + H + 14);
    });
    [-20, -10, 0, 10, 20].forEach(function (db) {
      var y = fy(db);
      c.beginPath(); c.moveTo(ml, y); c.lineTo(ml + W, y); c.stroke();
      c.fillText(db + ' dB', 2, y + 3);
    });
    // 0 dB emphasis
    c.strokeStyle = '#bbb';
    c.beginPath(); c.moveTo(ml, fy(0)); c.lineTo(ml + W, fy(0)); c.stroke();

    function drawCurve(curve, color, dashed) {
      if (!curve || !curve.freqs_hz) return;
      c.strokeStyle = color;
      c.lineWidth = 2;
      if (dashed) c.setLineDash([4, 4]); else c.setLineDash([]);
      c.beginPath();
      var first = true;
      for (var i = 0; i < curve.freqs_hz.length; i++) {
        var x = fx(curve.freqs_hz[i]);
        var y = fy(curve.magnitude_db[i]);
        if (first) { c.moveTo(x, y); first = false; }
        else c.lineTo(x, y);
      }
      c.stroke();
      c.setLineDash([]);
    }

    drawCurve(target, '#888', true);
    drawCurve(measured, '#d44', false);
    drawCurve(predicted, '#1db954', false);
  }

  // -- Network --

  async function postJson(path, body) {
    var resp = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    });
    if (!resp.ok) {
      var msg = await resp.text();
      throw new Error('POST ' + path + ' → ' + resp.status + ': ' + msg);
    }
    return await resp.json();
  }

  async function fetchStatus() {
    var resp = await fetch('status', {cache: 'no-store'});
    if (!resp.ok) throw new Error('status ' + resp.status);
    return await resp.json();
  }

  // -- WAV encoding --

  function float32ToWav(samples, sampleRate) {
    var len = samples.length;
    var buf = new ArrayBuffer(44 + len * 2);
    var view = new DataView(buf);
    function w8s(off, str) {
      for (var i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i));
    }
    w8s(0, 'RIFF');
    view.setUint32(4, 36 + len * 2, true);
    w8s(8, 'WAVE');
    w8s(12, 'fmt ');
    view.setUint32(16, 16, true);          // fmt chunk size
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, 1, true);           // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);  // byte rate (mono * 2 bytes)
    view.setUint16(32, 2, true);           // block align
    view.setUint16(34, 16, true);          // 16-bit
    w8s(36, 'data');
    view.setUint32(40, len * 2, true);
    var off = 44;
    for (var i = 0; i < len; i++) {
      var s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(off, s * 0x7FFF, true);
      off += 2;
    }
    return new Blob([buf], {type: 'audio/wav'});
  }

  // -- Workflow --

  async function startMeasurement() {
    runBtn.disabled = true;
    applyBtn.classList.add('hidden');
    resetBtn.classList.add('hidden');
    resultSection.classList.add('hidden');
    setStateBadge('preparing', 'pausing music…');
    try {
      var resp = await postJson('start', {});
      sessionId = resp.session_id;
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
      return;
    }
    // Tell the worklet to begin buffering samples for upload. The
    // backend's POST /start has already begun the sweep on the
    // speaker — capture timing aligns naturally because the FFT
    // deconvolution is offset-invariant (it cross-correlates the
    // recorded signal against the known sweep, finding the peak in
    // the result).
    if (workletNode) workletNode.port.postMessage('startCapture');
    pollState();
  }

  async function pollState() {
    if (pollTimer) clearTimeout(pollTimer);
    try {
      var s = await fetchStatus();
      currentState = s.state;
      setStateBadge(s.state, s.error || '');
      if (s.state === 'awaiting_capture') {
        // Sweep finished — stop buffering and ship the WAV.
        if (workletNode) workletNode.port.postMessage('stopCapture');
        // Don't poll again until upload completes — onCaptureReady
        // resumes polling.
        return;
      }
      if (s.state === 'ready') {
        // Result event already arrived; chart may need to be drawn
        // if the WAV upload completed before we got here.
        applyBtn.classList.remove('hidden');
        resetBtn.classList.remove('hidden');
        runBtn.disabled = false;
        // Fetch the latest result via status (which doesn't include
        // curves — we keep them on the last 'result' event we got
        // from the upload response).
        return;
      }
      if (s.state === 'applied') {
        runBtn.disabled = false;
        applyBtn.classList.add('hidden');
        resetBtn.classList.remove('hidden');
        return;
      }
      if (s.state === 'failed') {
        runBtn.disabled = false;
        return;
      }
      // Otherwise keep polling.
      pollTimer = setTimeout(pollState, 500);
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
    }
  }

  async function onCaptureReady(arrayBuffer) {
    var float32 = new Float32Array(arrayBuffer);
    setStateBadge('analyzing', 'uploading capture (' +
      Math.round(float32.length / REQUIRED_SR * 10) / 10 + ' s of audio)…');
    var wav = float32ToWav(float32, REQUIRED_SR);
    try {
      var resp = await fetch('upload-capture', {
        method: 'POST',
        headers: {'Content-Type': 'audio/wav'},
        body: wav
      });
      if (!resp.ok) {
        var msg = await resp.text();
        throw new Error('upload-capture → ' + resp.status + ': ' + msg);
      }
      var data = await resp.json();
      lastResult = data;
      if (data.measured) drawChart(data.measured, data.target, data.predicted);
      renderPEQs(data.peqs || []);
      resultSection.classList.remove('hidden');
      // Resume polling — server should now be in 'ready'.
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
    }
  }

  async function applyCorrection() {
    applyBtn.disabled = true;
    setStateBadge('analyzing', 'applying to CamillaDSP…');
    try {
      await postJson('apply', {});
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
    } finally {
      applyBtn.disabled = false;
    }
  }

  async function resetCorrection() {
    resetBtn.disabled = true;
    setStateBadge('analyzing', 'rolling back to flat…');
    try {
      await postJson('reset', {});
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
    } finally {
      resetBtn.disabled = false;
    }
  }

  startBtn.addEventListener('click', function () { startMicCapture(); });
  runBtn.addEventListener('click', function () { startMeasurement(); });
  applyBtn.addEventListener('click', function () { applyCorrection(); });
  resetBtn.addEventListener('click', function () { resetCorrection(); });
})();
</script>
</body>
</html>
"""


def _render_page(hostname: str) -> bytes:
    return (
        _PAGE_HTML
        .replace("__STYLE__", _CORRECTION_PAGE_STYLE)
        .replace("__HOSTNAME__", hostname)
        .replace("__REQUIRED_SR__", str(REQUIRED_SAMPLE_RATE))
    ).encode("utf-8")


# ----------------------------------------------------------------------
# HTTP route handlers — sync wrappers around async session methods.
# ----------------------------------------------------------------------


def _handle_start(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /start: replace the session, kick off the measurement
    pipeline in the background."""
    from jasper.correction import coordinator, playback
    from jasper.correction.session import SessionState

    sess = _replace_session()

    async def _run_full_measurement() -> None:
        try:
            async with coordinator.measurement_window():
                await sess.prepare_and_play_sweep(
                    playback.play_sweep,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("measurement failed: %s", e)
            # session._fail() already called inside the methods that
            # raised; nothing else to do.

    # Schedule the long-running coroutine on the background loop;
    # don't await — the HTTP request returns immediately and the
    # browser polls /status.
    asyncio.run_coroutine_threadsafe(_run_full_measurement(), _ensure_loop())

    return {"session_id": sess.session_id, "state": sess.state.value}


def _handle_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /status: snapshot the current session."""
    sess = _get_or_create_session()
    return sess.snapshot()


def _handle_upload_capture(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /upload-capture: read the WAV body, write to disk, run
    the analysis pipeline, return the chart data + designed PEQs."""
    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")

    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        raise ValueError("empty body")
    body = handler.rfile.read(length)

    sess.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
    captured_path = sess.cfg.capture_dir / (
        f"capture_{sess.session_id}_{int(time.time())}.wav"
    )
    captured_path.write_bytes(body)

    _run_async(sess.on_capture_uploaded(captured_path), timeout=30.0)

    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "measured": (
            sess.measured_curve.__dict__ if sess.measured_curve else None
        ),
        "target": (
            sess.target_curve.__dict__ if sess.target_curve else None
        ),
        "predicted": (
            sess.predicted_curve.__dict__ if sess.predicted_curve else None
        ),
        "peqs": [p.__dict__ for p in sess.peqs],
    }


def _handle_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /apply: write YAML + reload CamillaDSP."""
    from jasper.camilla import CamillaController

    sess = _get_or_create_session()
    cam = CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    _run_async(sess.apply(_set), timeout=15.0)
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "config_path": (
            str(sess.config_path) if sess.config_path else None
        ),
    }


def _handle_reset(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /reset: roll back to /etc/camilladsp/v1.yml."""
    from jasper.camilla import CamillaController

    sess = _get_or_create_session()
    cam = CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    _run_async(sess.reset(_set), timeout=15.0)
    return {"session_id": sess.session_id, "state": sess.state.value}


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(
            self, payload: dict[str, Any], *, status: int = 200,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, *, status: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                self._send_html(_render_page(cfg["hostname"]))
                return
            if path == "/healthz":
                body = b"ok\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/status":
                try:
                    self._send_json(_handle_status(self))
                except Exception as e:  # noqa: BLE001
                    logger.exception("/status failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/start":
                    self._send_json(_handle_start(self))
                    return
                if path == "/upload-capture":
                    self._send_json(_handle_upload_capture(self))
                    return
                if path == "/apply":
                    self._send_json(_handle_apply(self))
                    return
                if path == "/reset":
                    self._send_json(_handle_reset(self))
                    return
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                self._send_json({"error": str(e)}, status=500)
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
