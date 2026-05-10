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


def _replace_session(
    *,
    total_positions: int = 1,
    target_choice: str = "flat",
):
    """Replace the global session with a fresh one. Called by /start
    so the user can re-run measurements without restarting the
    daemon. Phase 2 takes total_positions + target_choice from the
    body so the new session is configured before its first sweep."""
    from jasper.correction.session import MeasurementSession
    global _session
    _session = MeasurementSession(
        total_positions=total_positions,
        target_choice=target_choice,
    )
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
<p class="sub">Measure your room from this iPhone, design correction filters, and apply them to the speaker.</p>

<details class="advice" open>
  <summary>Where to put the phone</summary>
  <ol>
    <li><strong>Hold or prop the phone where your head will be when listening</strong> — sitting on the couch / chair, at ear height. <em>Not</em> on the cushion below your head; the cushion absorbs sound your ears would receive.</li>
    <li>Phone <strong>flat, screen up</strong>, with the <strong>bottom edge</strong> (speaker / mic end) pointing toward the speakers.</li>
    <li>Take it out of any case if it has one.</li>
    <li>Keep the room quiet during the sweep — close windows, mute other devices, no talking.</li>
  </ol>
  <p class="hint">iOS doesn't let us pick which mic to use; pointing the bottom edge at the speakers gets the most consistent capture. Holding the phone at ear height (rather than putting it on the cushion) means we're measuring what you actually hear.</p>
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

  <div style="background:#f4f4f4; border-radius:6px; padding:0.6em 0.8em; margin:0.8em 0;">
    <label for="positions-select">Positions to measure</label>
    <select id="positions-select" form="dummy">
      <option value="1">1 — quick (single position)</option>
      <option value="5" selected>5 — recommended (MMM averaging)</option>
      <option value="3">3 — compromise</option>
    </select>
    <p class="hint" style="margin-top:0.3em">5 positions across your couch / listening area give a much better correction than a single point. We'll guide you through each one.</p>

    <label for="target-select" style="margin-top:0.6em">Target curve</label>
    <select id="target-select" form="dummy">
      <option value="flat" selected>Flat — neutral, accurate</option>
      <option value="warm">Warm — Harman-style downward tilt + sub-bass shelf</option>
      <option value="bright">Bright — slight upward tilt</option>
    </select>
  </div>

  <p>Status: <span id="state-badge" class="state-badge idle">idle</span>
    <span id="state-detail" class="hint"></span></p>

  <div id="position-prompt" class="hidden" style="background:#fff3cd; border-radius:6px; padding:0.7em 0.9em; margin:0.5em 0;">
    <p style="margin:0; font-weight:600">Move phone to position <span id="position-current">2</span> of <span id="position-total">5</span>.</p>
    <p class="hint" style="margin-top:0.3em">Move ~30 cm from the previous position — left, right, forward, or back, head-height. Same orientation: phone flat, bottom edge pointing at the speakers. Tap Continue when ready.</p>
  </div>

  <p style="display:flex; gap:0.6em; flex-wrap:wrap">
    <button id="autolevel" type="button" class="secondary" disabled>Auto-level</button>
    <button id="autolevel-lock" type="button" class="primary hidden">Lock now</button>
    <button id="autolevel-cancel" type="button" class="danger hidden">Cancel</button>
    <button id="run-measurement" type="button" class="primary" disabled>Run measurement</button>
    <button id="continue-position" type="button" class="primary hidden">Continue to next position</button>
    <button id="apply-correction" type="button" class="primary hidden">Apply correction</button>
    <button id="verify-correction" type="button" class="primary hidden">Verify with re-measurement</button>
    <button id="reset-correction" type="button" class="danger hidden">Reset to flat</button>
  </p>
  <p class="hint" style="margin-top:0.4em">Before measuring, tap <strong>Auto-level</strong>. The speaker plays a 1 kHz tone while we gradually raise the volume from quiet to a measurement-friendly level (capped at −6 dB software volume — your amp's analog gain is still the final say). When the iPhone mic hears it in the target range, we lock automatically. If the volume sounds right to <em>you</em> first, tap <strong>Lock now</strong>. Takes ~6 seconds at most.</p>
  <div id="autolevel-status" class="hidden" style="background:#fff3cd; border-radius:6px; padding:0.7em 0.9em; margin:0.5em 0;">
    <p style="margin:0; font-weight:600" id="autolevel-line">Auto-leveling…</p>
    <p class="hint" style="margin-top:0.3em" id="autolevel-detail"></p>
  </div>
  <div id="result-section" class="hidden">
    <h3>Frequency response</h3>
    <div class="chart-wrap"><canvas id="chart"></canvas></div>
    <p class="hint">
      <span style="color:#d44">red</span> = measured (averaged across positions),
      <span style="color:#888">gray dashed</span> = target,
      <span style="color:#1db954">green</span> = predicted post-correction.
      After Verify: <span style="color:#a050d0">purple dashed</span> = post-correction measurement.
    </p>
    <p id="verify-summary" class="hint hidden"></p>
    <h3>Filters designed</h3>
    <div class="peq-list" id="peq-list"></div>
  </div>
</div>

<details style="margin-top:2em; background:#f4f4f4; border-radius:6px; padding:0.5em 0.8em 0.7em">
  <summary style="cursor:pointer; font-weight:600">Optional: silence Safari's "Not Private" warning on future visits</summary>
  <p>You're seeing this page because you tapped through Safari's "Not Private" warning — that's fine and the page works correctly. The warning appears on every visit unless you install this speaker's certificate as a trusted authority on this device.</p>
  <ol>
    <li>Tap <a href="http://__HOSTNAME__/jts-root-ca.crt">Download the JTS root CA</a> (plain HTTP — necessary because HTTPS isn't trusted yet). Safari prompts <em>"This website is trying to download a configuration profile."</em> Tap <strong>Allow</strong>.</li>
    <li>Open the <strong>Settings</strong> app. A new entry near the top says <em>"Profile Downloaded — JTS Speaker Local CA"</em>. Tap it → <strong>Install</strong> → enter passcode → <strong>Install</strong> → <strong>Done</strong>.</li>
    <li>Go to <strong>Settings → General → About → Certificate Trust Settings</strong>. Toggle <strong>JTS Speaker Local CA</strong> on. Tap <strong>Continue</strong> through the warning Apple shows for any non-public CA.</li>
  </ol>
  <p class="hint">To remove later: Settings → General → VPN &amp; Device Management → JTS Speaker Local CA → Remove Profile.</p>
</details>

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
  var autolevelBtn = document.getElementById('autolevel');
  var autolevelLockBtn = document.getElementById('autolevel-lock');
  var autolevelCancelBtn = document.getElementById('autolevel-cancel');
  var autolevelStatus = document.getElementById('autolevel-status');
  var autolevelLine = document.getElementById('autolevel-line');
  var autolevelDetail = document.getElementById('autolevel-detail');
  var runBtn = document.getElementById('run-measurement');
  var continueBtn = document.getElementById('continue-position');
  var applyBtn = document.getElementById('apply-correction');
  var verifyBtn = document.getElementById('verify-correction');
  var resetBtn = document.getElementById('reset-correction');
  var positionsSelect = document.getElementById('positions-select');
  var targetSelect = document.getElementById('target-select');
  var positionPrompt = document.getElementById('position-prompt');
  var positionCurrent = document.getElementById('position-current');
  var positionTotal = document.getElementById('position-total');
  var resultSection = document.getElementById('result-section');
  var canvas = document.getElementById('chart');
  var peqList = document.getElementById('peq-list');
  var verifySummary = document.getElementById('verify-summary');

  var ctx = null;
  var workletNode = null;
  var pollTimer = null;
  var sessionId = null;
  var currentState = null;
  var lastResult = null;
  var lastVerify = null;
  var inVerifyMode = false;
  // Latest mic RMS in dBFS, updated by the AudioWorklet at ~20 Hz.
  // The autolevel loop reads this to decide when the speaker level
  // has reached the target range.
  var latestMicRmsDb = -120;
  var autolevelRmsBuffer = [];  // recent dB samples for smoothing

  // For the three audio-processing flags (echoCancellation,
  // noiseSuppression, autoGainControl), iOS Safari often returns
  // `undefined` from getSettings() rather than echoing back the
  // value we requested. That's NOT a bug — it means Safari simply
  // doesn't surface the setting (and on iOS, these features are
  // off by default for getUserMedia anyway). We treat undefined
  // and false as both "constraint was honored." Only TRUE counts
  // as bad.
  function isAudioProcessingOff(value) {
    return value === false || value === undefined || value === null;
  }

  function renderConstraints(actual, problems) {
    var rows = [
      ['sampleRate', REQUIRED_SR + ' Hz', actual.sampleRate + ' Hz',
       actual.sampleRate === REQUIRED_SR],
      ['echoCancellation', 'false',
       (actual.echoCancellation === undefined ? 'undefined (= off)' : String(actual.echoCancellation)),
       isAudioProcessingOff(actual.echoCancellation)],
      ['noiseSuppression', 'false',
       (actual.noiseSuppression === undefined ? 'undefined (= off)' : String(actual.noiseSuppression)),
       isAudioProcessingOff(actual.noiseSuppression)],
      ['autoGainControl', 'false',
       (actual.autoGainControl === undefined ? 'undefined (= off)' : String(actual.autoGainControl)),
       isAudioProcessingOff(actual.autoGainControl)],
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
      autolevelBtn.disabled = true;
    } else {
      errBanner.classList.add('hidden');
      runBtn.disabled = false;
      autolevelBtn.disabled = false;
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
    // Only TRUE counts as a problem — undefined / null mean Safari
    // didn't echo the value back, which on iOS means the feature
    // is off (it's off by default for getUserMedia on iOS anyway).
    if (actual.echoCancellation === true) problems.push('echoCancellation enabled');
    if (actual.noiseSuppression === true) problems.push('noiseSuppression enabled');
    if (actual.autoGainControl === true) problems.push('autoGainControl enabled');
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
        // Live meter
        var pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
        levelBar.style.width = pct.toFixed(1) + '%';
        levelReadout.textContent = db.toFixed(1);
        // Stash for the autolevel loop (which polls this).
        latestMicRmsDb = db;
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
    // Defensive: a hidden canvas (display:none ancestor) reports
    // 0×0. Drawing into it silently produces an empty chart. Bail
    // and log — caller is responsible for re-invoking after the
    // canvas becomes visible.
    if (rect.width < 10 || rect.height < 10) {
      console.warn('drawChart skipped — canvas not laid out yet ' +
        '(' + rect.width + '×' + rect.height + ')');
      return;
    }
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
    // Phase 2: post-correction verify pass overlay (purple dashed).
    if (lastVerify) {
      drawCurve(lastVerify, '#a050d0', true);
    }
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
    continueBtn.classList.add('hidden');
    applyBtn.classList.add('hidden');
    verifyBtn.classList.add('hidden');
    resetBtn.classList.add('hidden');
    resultSection.classList.add('hidden');
    positionPrompt.classList.add('hidden');
    verifySummary.classList.add('hidden');
    lastVerify = null;
    inVerifyMode = false;
    setStateBadge('preparing', 'pausing music…');
    try {
      var totalPositions = parseInt(positionsSelect.value, 10) || 1;
      var targetChoice = targetSelect.value || 'flat';
      var resp = await postJson('start', {
        total_positions: totalPositions,
        target_choice: targetChoice
      });
      sessionId = resp.session_id;
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
      return;
    }
    if (workletNode) workletNode.port.postMessage('startCapture');
    pollState();
  }

  async function continueToNextPosition() {
    // Hide + disable Continue immediately so a double-tap can't fire
    // a second /next-position before the server transitions out of
    // NEEDS_NEXT_POSITION. (A user hit this in first-pass testing
    // — race between a double-tap and the worklet's stopCapture
    // → upload → state-transition cycle.)
    continueBtn.classList.add('hidden');
    continueBtn.disabled = true;
    positionPrompt.classList.add('hidden');
    setStateBadge('preparing', 'pausing music…');
    try {
      await postJson('next-position', {});
    } catch (e) {
      setStateBadge('failed', e.message);
      // pollState will reapply the button policy on next tick —
      // user can retry from the new state.
      return;
    }
    if (workletNode) workletNode.port.postMessage('startCapture');
    pollState();
  }

  // Auto-level: how much SNR (dB) above the measured room noise
  // floor we want the tone to sit at when we lock. 20-30 dB above
  // noise gives 15-25 dB SNR on the sweep (which is 6 dB quieter
  // than the tone source). Clamped on both ends:
  //   - lower clamp -30 dBFS: don't lock at very quiet absolute
  //     levels even in dead-silent rooms (capture would still work
  //     but the user wouldn't believe a measurement happened).
  //   - upper clamp -10 dBFS: avoid pushing the iPhone mic near
  //     its clipping ceiling.
  //
  // Previous hard-coded -20..-10 target was unreachable in normal
  // rooms (user's "decently loud voice at 10 cm" peaked at -25 dBFS
  // — a speaker tone at couch distance would land around -25 to
  // -35 dBFS at best). Adaptive band picks a target that's
  // physically achievable for whatever noise floor you've got.
  var AUTOLEVEL_SNR_DESIRED_LOW = 20;   // 20 dB above noise = minimum
  var AUTOLEVEL_SNR_DESIRED_HIGH = 30;  // 30 dB above noise = ideal
  var AUTOLEVEL_TARGET_DB_FLOOR = -30;  // lower clamp (absolute)
  var AUTOLEVEL_TARGET_DB_CEILING = -10; // upper clamp (absolute)

  function computeTargetBand(noiseFloorDb) {
    var high = Math.min(
      noiseFloorDb + AUTOLEVEL_SNR_DESIRED_HIGH,
      AUTOLEVEL_TARGET_DB_CEILING,
    );
    var low = Math.max(
      noiseFloorDb + AUTOLEVEL_SNR_DESIRED_LOW,
      AUTOLEVEL_TARGET_DB_FLOOR,
    );
    // In very noisy rooms the clamps can collide. Force a minimum
    // 5 dB window so a momentary RMS spike can satisfy the lock
    // condition.
    if (low > high - 5) low = high - 5;
    return { low: low, high: high };
  }

  async function startAutolevel() {
    autolevelBtn.disabled = true;
    runBtn.disabled = true;
    autolevelStatus.classList.remove('hidden');
    autolevelLockBtn.classList.remove('hidden');
    autolevelCancelBtn.classList.remove('hidden');
    autolevelRmsBuffer = [];

    // Step 1: measure ambient noise floor for ~500 ms BEFORE the
    // tone starts. This gives us a real number for "what counts as
    // quiet in this room right now", which we then use to pick a
    // target SNR band that's actually achievable. Hard-coded bands
    // from the previous version were unreachable in rooms where
    // the speaker-to-listener path attenuated more than I'd
    // assumed (real complaint from first-user test).
    autolevelLine.textContent = 'Measuring room noise…';
    autolevelDetail.textContent = '';
    var noiseSamples = [];
    var noiseSampler = setInterval(function () {
      if (latestMicRmsDb > -100) noiseSamples.push(latestMicRmsDb);
    }, 30);
    await new Promise(function (r) { setTimeout(r, 500); });
    clearInterval(noiseSampler);
    var noiseFloorDb;
    if (noiseSamples.length >= 3) {
      var nsum = 0;
      for (var ni = 0; ni < noiseSamples.length; ni++) nsum += noiseSamples[ni];
      noiseFloorDb = nsum / noiseSamples.length;
    } else {
      // Couldn't measure (mic stream not ready?). Fall back to a
      // reasonable assumption.
      noiseFloorDb = -50;
    }
    var targetBand = computeTargetBand(noiseFloorDb);
    autolevelDetail.textContent =
      'Noise floor ' + noiseFloorDb.toFixed(0) + ' dBFS — target ' +
      targetBand.low.toFixed(0) + ' to ' + targetBand.high.toFixed(0) +
      ' dBFS. Tap Lock now if the tone sounds like a comfortable measurement level.';

    var lockSent = false;
    var sendLock = function (reason) {
      if (lockSent) return;
      lockSent = true;
      console.log('autolevel lock signal:', reason);
      fetch('autolevel/lock', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: '{}'
      }).catch(function (e) { console.warn('lock POST failed', e); });
    };
    // Manual Lock button → send lock signal immediately.
    var prevLockHandler = autolevelLockBtn.onclick;
    autolevelLockBtn.onclick = function () { sendLock('manual'); };
    var prevCancelHandler = autolevelCancelBtn.onclick;
    autolevelCancelBtn.onclick = function () { cancelAutolevel(); };

    // Watch the latest mic RMS at 50 ms granularity. As soon as the
    // smoothed (last ~250 ms) RMS lands in the target range, send
    // auto-lock. Target band is the adaptive one computed above.
    var watcher = setInterval(function () {
      if (lockSent) return;
      var db = latestMicRmsDb;
      if (db <= -100) return;
      autolevelRmsBuffer.push(db);
      if (autolevelRmsBuffer.length > 5) autolevelRmsBuffer.shift();
      var sum = 0;
      for (var i = 0; i < autolevelRmsBuffer.length; i++) sum += autolevelRmsBuffer[i];
      var avg = sum / autolevelRmsBuffer.length;
      if (autolevelRmsBuffer.length >= 3 &&
          avg >= targetBand.low &&
          avg <= targetBand.high) {
        sendLock('mic ' + avg.toFixed(1) + ' dBFS in band ' +
          targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0));
      }
    }, 50);

    try {
      await postJson('autolevel/start', {});
    } catch (e) {
      clearInterval(watcher);
      autolevelLockBtn.onclick = prevLockHandler;
      autolevelCancelBtn.onclick = prevCancelHandler;
      autolevelLine.textContent = 'Auto-level failed: ' + e.message;
      autolevelLockBtn.classList.add('hidden');
      autolevelCancelBtn.classList.add('hidden');
      autolevelBtn.disabled = false;
      runBtn.disabled = false;
      return;
    }

    // Poll /status every 200 ms until autolevel reaches terminal.
    var pollOnce = async function () {
      try {
        var s = await fetchStatus();
        if (!s.autolevel) return true;
        var al = s.autolevel;
        var volStr = (al.current_main_volume_db !== null && al.current_main_volume_db !== undefined)
          ? al.current_main_volume_db.toFixed(1) : '?';
        autolevelLine.textContent = 'Auto-leveling at main_volume=' + volStr +
          ' dB · mic ' + latestMicRmsDb.toFixed(1) + ' dBFS · target ' +
          targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0) +
          (lockSent ? ' · lock sent' : '');
        if (al.status === 'ramping') return false;
        if (al.status === 'locked') {
          autolevelLine.textContent = '✓ Locked — speaker at ' +
            al.locked_main_volume_db.toFixed(1) + ' dB. Ready to measure.';
          autolevelDetail.textContent = '';
        } else if (al.status === 'maxed_out') {
          autolevelLine.textContent =
            'Tone reached the software volume ceiling (−6 dB) at ' +
            latestMicRmsDb.toFixed(1) + ' dBFS — target was ' +
            targetBand.low.toFixed(0) + '..' + targetBand.high.toFixed(0) + '.';
          autolevelDetail.textContent =
            'If the tone you just heard sounded like a reasonable measurement level, tap Auto-level again and use Lock now. Otherwise, turn up your amplifier and retry.';
        } else if (al.status === 'cancelled') {
          autolevelLine.textContent = 'Auto-level cancelled — speaker volume restored.';
          autolevelDetail.textContent = '';
        } else if (al.status === 'error') {
          autolevelLine.textContent = 'Auto-level error.';
          autolevelDetail.textContent = al.error || '(no details)';
        }
        return true;
      } catch (e) {
        autolevelLine.textContent = 'Status fetch failed: ' + e.message;
        return true;
      }
    };

    while (true) {
      var done = await pollOnce();
      if (done) break;
      await new Promise(function (r) { setTimeout(r, 200); });
    }

    clearInterval(watcher);
    autolevelLockBtn.onclick = prevLockHandler;
    autolevelCancelBtn.onclick = prevCancelHandler;
    autolevelLockBtn.classList.add('hidden');
    autolevelCancelBtn.classList.add('hidden');
    autolevelBtn.disabled = false;
    runBtn.disabled = false;
  }

  async function cancelAutolevel() {
    try {
      await postJson('autolevel/cancel', {});
    } catch (e) {
      autolevelLine.textContent = 'Cancel POST failed: ' + e.message;
    }
  }

  async function startVerify() {
    verifyBtn.disabled = true;
    inVerifyMode = true;
    setStateBadge('verifying', 'pausing music for re-measurement…');
    try {
      await postJson('verify', {});
    } catch (e) {
      setStateBadge('failed', e.message);
      verifyBtn.disabled = false;
      inVerifyMode = false;
      return;
    }
    if (workletNode) workletNode.port.postMessage('startCapture');
    verifyBtn.disabled = false;
    pollState();
  }

  // Centralised button-state policy. Every transition through
  // pollState first hides ALL action buttons, then re-shows the
  // ones the current state allows. Without this, stale buttons
  // (e.g. a still-visible Continue button during the next sweep)
  // accept double-clicks and trigger /next-position from the
  // wrong state.
  function applyButtonPolicy(state, autolevelStatus) {
    // Default: everything hidden / disabled.
    positionPrompt.classList.add('hidden');
    continueBtn.classList.add('hidden');
    continueBtn.disabled = false;
    applyBtn.classList.add('hidden');
    applyBtn.disabled = false;
    verifyBtn.classList.add('hidden');
    verifyBtn.disabled = false;
    resetBtn.classList.add('hidden');
    resetBtn.disabled = false;
    autolevelLockBtn.classList.add('hidden');
    autolevelLockBtn.disabled = false;
    autolevelCancelBtn.classList.add('hidden');
    autolevelCancelBtn.disabled = false;
    // Run + Auto-level: enabled only when nothing is in flight.
    // Disabled during measurement, autolevel ramp, etc.
    var idleStates = ['idle', 'ready', 'applied', 'verified', 'failed'];
    var sessionIdle = idleStates.indexOf(state) !== -1;
    var autolevelRamping = autolevelStatus === 'ramping';
    runBtn.disabled = !sessionIdle || autolevelRamping;
    autolevelBtn.disabled = !sessionIdle || autolevelRamping;
    // Per-state additions:
    if (autolevelRamping) {
      // Manual Lock + Cancel always available during the ramp so
      // the user can override the auto-detection (iOS Safari AGC
      // makes the mic-based decision unreliable in some setups).
      autolevelLockBtn.classList.remove('hidden');
      autolevelCancelBtn.classList.remove('hidden');
    }
    if (state === 'needs_next_position') {
      positionPrompt.classList.remove('hidden');
      continueBtn.classList.remove('hidden');
      runBtn.disabled = true;
      autolevelBtn.disabled = true;
    } else if (state === 'ready') {
      applyBtn.classList.remove('hidden');
      resetBtn.classList.remove('hidden');
    } else if (state === 'applied' || state === 'verified') {
      verifyBtn.classList.remove('hidden');
      resetBtn.classList.remove('hidden');
    }
  }

  async function pollState() {
    if (pollTimer) clearTimeout(pollTimer);
    try {
      var s = await fetchStatus();
      currentState = s.state;
      var detail = s.error || '';
      if (s.total_positions > 1 && s.current_position !== undefined &&
          (s.state === 'preparing' || s.state === 'sweeping' ||
           s.state === 'awaiting_capture' || s.state === 'analyzing')) {
        detail = 'position ' + (s.current_position + 1) + ' of ' + s.total_positions;
      }
      setStateBadge(s.state, detail);
      applyButtonPolicy(s.state, s.autolevel ? s.autolevel.status : 'idle');

      if (s.state === 'needs_next_position') {
        positionCurrent.textContent = (s.current_position + 1);
        positionTotal.textContent = s.total_positions;
        return;
      }
      if (s.state === 'awaiting_capture' || s.state === 'awaiting_verify_capture') {
        if (workletNode) workletNode.port.postMessage('stopCapture');
        return;  // upload-capture handler resumes polling
      }
      if (s.state === 'verified' && s.verify_metrics) {
        verifySummary.textContent = 'Post-correction (20–350 Hz): RMS deviation ' +
          s.verify_metrics.rms_db.toFixed(2) + ' dB, max ' +
          s.verify_metrics.max_db.toFixed(2) + ' dB.';
        verifySummary.classList.remove('hidden');
        return;
      }
      if (s.state === 'ready' || s.state === 'applied' || s.state === 'failed') {
        return;
      }
      // Mid-flight states: keep polling.
      pollTimer = setTimeout(pollState, 500);
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
      testToneBtn.disabled = false;
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
      // Verify pass: hold the design curves, overlay the new
      // measurement as `lastVerify`. Otherwise: redraw with the
      // freshly designed curves.
      if (data.verify) {
        lastVerify = data.verify;
      }
      // CRITICAL: show resultSection BEFORE drawing the chart. A
      // hidden canvas has zero bounding-rect dimensions, so
      // canvas.width gets set to 0 and the chart renders empty —
      // which is exactly the "frequency response is blank" bug a
      // user hit on the first measurement. Show, then draw, then
      // request a follow-up frame to redraw in case layout hadn't
      // settled (mobile Safari sometimes lags one frame on
      // display:block transitions).
      if (data.peqs) {
        renderPEQs(data.peqs);
      }
      resultSection.classList.remove('hidden');
      if (data.measured) {
        // Force a layout flush so getBoundingClientRect returns
        // real dimensions on the first draw.
        void canvas.offsetWidth;
        drawChart(data.measured, data.target, data.predicted);
        // Safety redraw next frame.
        requestAnimationFrame(function () {
          drawChart(data.measured, data.target, data.predicted);
        });
      }
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
  continueBtn.addEventListener('click', function () { continueToNextPosition(); });
  applyBtn.addEventListener('click', function () { applyCorrection(); });
  verifyBtn.addEventListener('click', function () { startVerify(); });
  resetBtn.addEventListener('click', function () { resetCorrection(); });
  autolevelBtn.addEventListener('click', function () { startAutolevel(); });
  autolevelCancelBtn.addEventListener('click', function () { cancelAutolevel(); });

  // Redraw chart on resize / orientation change — without this, the
  // canvas's drawing surface stays at the dimensions it had on the
  // first draw and gets CSS-stretched (blurry) on rotation.
  var resizeTimer = null;
  function scheduleChartRedraw() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (lastResult && lastResult.measured) {
        drawChart(lastResult.measured, lastResult.target, lastResult.predicted);
      }
    }, 150);
  }
  window.addEventListener('resize', scheduleChartRedraw);
  window.addEventListener('orientationchange', scheduleChartRedraw);
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


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """Parse JSON body. Empty body → {}."""
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _handle_start(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /start: replace the session, kick off the first sweep.
    Body fields:
      - total_positions: int = 1 (Phase 1 default; UI sends 5 for MMM)
      - target_choice:   str = 'flat' | 'neutral' | 'warm' | 'bright'
    """
    from jasper.correction import coordinator, playback

    body = _read_json_body(handler)
    total_positions = max(1, min(10, int(body.get("total_positions", 1))))
    target_choice = str(body.get("target_choice", "flat"))

    sess = _replace_session(
        total_positions=total_positions,
        target_choice=target_choice,
    )

    async def _run_first_sweep() -> None:
        try:
            async with coordinator.measurement_window():
                await sess.prepare_and_play_sweep(playback.play_sweep)
        except Exception as e:  # noqa: BLE001
            logger.exception("first sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(_run_first_sweep(), _ensure_loop())

    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "total_positions": sess.total_positions,
        "target_choice": sess.target_choice,
    }


def _handle_next_position(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /next-position: play the next sweep for a multi-position
    measurement. Only valid in NEEDS_NEXT_POSITION state.

    The handler intentionally BLOCKS until the background sweep task
    has transitioned state past NEEDS_NEXT_POSITION (typically
    100-500 ms). Without this wait, the HTTP response carries stale
    state, the JS pollState loop sees `needs_next_position` again,
    shows the Continue button, and stops polling — at which point
    the next sweep completes silently and the user is stuck. See
    `MeasurementSession.state_changed_from` for the full rationale.
    """
    from jasper.correction import coordinator, playback
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess.state != SessionState.NEEDS_NEXT_POSITION:
        raise RuntimeError(
            f"cannot advance to next position from state {sess.state.value}"
        )

    async def _run_next_sweep() -> None:
        try:
            async with coordinator.measurement_window():
                await sess.prepare_and_play_sweep(playback.play_sweep)
        except Exception as e:  # noqa: BLE001
            logger.exception("next-position sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(_run_next_sweep(), _ensure_loop())

    # Wait until the background task has actually advanced state.
    # measurement_window setup takes ~100-500 ms (systemctl stop x3
    # + MEASURE_PAUSE UDS) before prepare_and_play_sweep sets
    # PREPARING; allow up to 5 seconds of slack.
    _run_async(
        sess.state_changed_from(SessionState.NEEDS_NEXT_POSITION),
        timeout=6.0,
    )

    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
    }


def _handle_verify(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /verify: re-measure after Apply to see the actual effect
    of the correction. One-position only; result lands in
    verify_curve / verify_metrics. Same stale-state-avoidance wait
    as /next-position."""
    from jasper.correction import coordinator, playback
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()

    async def _run_verify_sweep() -> None:
        try:
            async with coordinator.measurement_window():
                await sess.start_verify_sweep(playback.play_sweep)
        except Exception as e:  # noqa: BLE001
            logger.exception("verify sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(_run_verify_sweep(), _ensure_loop())

    _run_async(
        sess.state_changed_from(
            {SessionState.APPLIED, SessionState.VERIFIED},
        ),
        timeout=6.0,
    )

    return {"session_id": sess.session_id, "state": sess.state.value}


def _handle_autolevel_start(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/start: ramp CamillaDSP main_volume upward
    while a continuous 1 kHz tone plays, until the iPhone client
    POSTs to /autolevel/lock (or the ramp tops out and we report
    `maxed_out`).

    Client behavior:
      1. POST /autolevel/start (kicks off the background task).
      2. Watch the live mic-level meter via AudioWorklet.
      3. When the captured mic RMS lands in the target range
         (default −20 .. −10 dBFS), POST /autolevel/lock.
      4. Poll GET /status; `autolevel.status` becomes `locked`,
         `maxed_out`, `cancelled`, or `error`.
    """
    from jasper.camilla import CamillaController
    from jasper.correction import coordinator, playback
    from jasper.correction.session import AutolevelStatus

    sess = _get_or_create_session()
    if sess.autolevel.status == AutolevelStatus.RAMPING:
        raise RuntimeError("autolevel already in progress")

    cam = CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )

    async def _run_autolevel() -> None:
        try:
            async with coordinator.measurement_window():
                tone_wav = playback._ensure_tone_wav(
                    freq_hz=1000.0,
                    duration_s=15.0,  # safety > max ramp duration
                    dbfs=-6.0,
                    sample_rate=48000,
                )
                player = playback.TonePlayer(tone_wav)

                async def _get_vol() -> float:
                    v = await cam.get_volume_db(best_effort=False)
                    return float(v) if v is not None else 0.0

                async def _set_vol(db: float) -> None:
                    await cam.set_volume_db(db, best_effort=True)

                await sess.run_autolevel(
                    get_main_volume_db=_get_vol,
                    set_main_volume_db=_set_vol,
                    play_continuous_tone=player.play,
                    cancel_tone=player.cancel,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("autolevel run failed: %s", e)

    asyncio.run_coroutine_threadsafe(_run_autolevel(), _ensure_loop())

    # Wait briefly for status to leave IDLE so the response is
    # non-stale (same anti-race pattern as /next-position).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sess.autolevel.status != AutolevelStatus.IDLE:
            break
        time.sleep(0.05)

    return {"started": True, "autolevel": sess.autolevel.snapshot()}


def _handle_autolevel_lock(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/lock: signal the autolevel task to stop
    ramping and freeze main_volume at its current value. The
    locked level is what subsequent sweeps will play through."""
    sess = _get_or_create_session()
    fired = _run_async(sess.lock_autolevel(), timeout=2.0)
    return {"locked": bool(fired), "autolevel": sess.autolevel.snapshot()}


def _handle_autolevel_cancel(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/cancel: abort the autolevel run and restore
    main_volume to whatever it was before the ramp started."""
    sess = _get_or_create_session()
    fired = _run_async(sess.cancel_autolevel(), timeout=2.0)
    return {"cancelled": bool(fired), "autolevel": sess.autolevel.snapshot()}


def _handle_test_tone(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /test-tone: play a 5-second 1 kHz sine through the music
    chain so the user can adjust their amp's volume by watching the
    live mic level meter. Pauses renderers + voice loop for the tone
    duration via the same measurement_window the sweep uses.

    Synchronous-feeling from the browser's POV (it returns once the
    tone has finished playing) so the polling state machine doesn't
    have to track a "test tone in progress" sub-state.
    """
    from jasper.correction import coordinator, playback

    body = _read_json_body(handler)
    duration_s = max(1.0, min(15.0, float(body.get("duration_s", 5.0))))

    async def _run_test_tone() -> None:
        async with coordinator.measurement_window():
            await playback.play_test_tone(duration_s=duration_s)

    _run_async(_run_test_tone(), timeout=duration_s + 30.0)
    return {"played": True, "duration_s": duration_s}


def _handle_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /status: snapshot the current session."""
    sess = _get_or_create_session()
    return sess.snapshot()


def _handle_upload_capture(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /upload-capture: read the WAV body, write to disk, run
    the analysis pipeline. Routes to either the multi-position
    capture path (if state == AWAITING_CAPTURE) or the verify path
    (if state == AWAITING_VERIFY_CAPTURE)."""
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")

    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        raise ValueError("empty body")
    body = handler.rfile.read(length)

    sess.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
    captured_path = sess.cfg.capture_dir / (
        f"capture_{sess.session_id}_p{sess.current_position}_{int(time.time())}.wav"
    )
    captured_path.write_bytes(body)

    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        _run_async(
            sess.on_verify_capture_uploaded(captured_path), timeout=30.0,
        )
    else:
        _run_async(sess.on_capture_uploaded(captured_path), timeout=30.0)

    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
        "measured": (
            sess.measured_curve.__dict__ if sess.measured_curve else None
        ),
        "target": (
            sess.target_curve.__dict__ if sess.target_curve else None
        ),
        "predicted": (
            sess.predicted_curve.__dict__ if sess.predicted_curve else None
        ),
        "verify": (
            sess.verify_curve.__dict__ if sess.verify_curve else None
        ),
        "verify_metrics": sess.verify_metrics,
        "peqs": [p.__dict__ for p in sess.peqs],
    }


def _maybe_restore_main_volume(sess, cam) -> None:
    """If autolevel ran and locked a measurement-friendly level,
    restore main_volume to the pre-autolevel value after the
    measurement workflow completes (apply or reset). This keeps the
    user's listening level intact across what otherwise would be a
    surprising "music is quieter now" experience.

    Idempotent — skips silently if no autolevel ran in this session.
    """
    from jasper.correction.session import AutolevelStatus

    al = sess.autolevel
    if al.original_main_volume_db is None:
        return
    # Only restore when autolevel had a "ran and finished" outcome.
    # If still RAMPING or IDLE, don't interfere.
    if al.status not in {
        AutolevelStatus.LOCKED,
        AutolevelStatus.MAXED_OUT,
    }:
        return

    async def _restore() -> None:
        await cam.set_volume_db(al.original_main_volume_db, best_effort=True)

    _run_async(_restore(), timeout=5.0)
    logger.info(
        "restored main_volume to %.1f dB after autolevel workflow",
        al.original_main_volume_db,
    )


def _handle_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /apply: write YAML + reload CamillaDSP. Restores
    pre-autolevel main_volume if autolevel was used."""
    from jasper.camilla import CamillaController

    sess = _get_or_create_session()
    cam = CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    _run_async(sess.apply(_set), timeout=15.0)
    _maybe_restore_main_volume(sess, cam)
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "config_path": (
            str(sess.config_path) if sess.config_path else None
        ),
    }


def _handle_reset(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /reset: roll back to /etc/camilladsp/v1.yml. Restores
    pre-autolevel main_volume if autolevel was used."""
    from jasper.camilla import CamillaController

    sess = _get_or_create_session()
    cam = CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    _run_async(sess.reset(_set), timeout=15.0)
    _maybe_restore_main_volume(sess, cam)
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
                if path == "/next-position":
                    self._send_json(_handle_next_position(self))
                    return
                if path == "/verify":
                    self._send_json(_handle_verify(self))
                    return
                if path == "/test-tone":
                    self._send_json(_handle_test_tone(self))
                    return
                if path == "/autolevel/start":
                    self._send_json(_handle_autolevel_start(self))
                    return
                if path == "/autolevel/lock":
                    self._send_json(_handle_autolevel_lock(self))
                    return
                if path == "/autolevel/cancel":
                    self._send_json(_handle_autolevel_cancel(self))
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
