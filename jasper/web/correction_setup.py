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
      GET  /sessions        recent measurement bundle summaries
      GET  /session-report  read-only evidence packet for one bundle
      POST /start           reset DSP, create session, request noise capture
      POST /upload-noise    body = pre-sweep noise WAV, then play sweep
      POST /upload-capture  body = WAV bytes, runs analysis pipeline
      POST /repeat-position optional same-seat repeat sweep
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
import concurrent.futures
import hashlib
import html
import json
import logging
import math
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ._common import (
    NAV_BACK_CSS,
    PAGE_STYLE,
    begin_request,
    csrf_fetch_helpers_js,
    csrf_meta_html,
    reject_csrf,
    send_html_response,
    verify_csrf,
)

logger = logging.getLogger(__name__)


# 48 kHz, EC=NS=AGC=false — pinned by the iOS verify step. The Phase 1
# sweep math assumes the captured signal is at this rate; on mismatch
# we refuse the upload rather than silently resampling (silent
# resampling would produce a working but wrong correction).
REQUIRED_SAMPLE_RATE = 48000
MAX_JSON_BODY_BYTES = 64 * 1024
MAX_CALIBRATION_UPLOAD_JSON_BYTES = 1024 * 1024
# Browser captures are mono 16-bit PCM at 48 kHz. A normal 10 s sweep
# upload is ~1 MB; 32 MB leaves generous room for measurement-window
# setup latency while still avoiding unbounded reads in the Pi web
# process.
MAX_WAV_BODY_BYTES = 32 * 1024 * 1024
MAX_DEVICE_FIELD_CHARS = 160


class BadRequest(ValueError):
    """Client supplied an invalid request body."""


class RequestConflict(RuntimeError):
    """Client request conflicts with the current correction session state."""


# Module-level session + bridge to the async loop. Lazy-init on
# first use so importing this module is cheap (lets `python -m
# jasper.web.correction_setup --help` work without spinning up a
# loop).
_session_lock = threading.Lock()
_session = None  # type: ignore[var-annotated]
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_start_in_progress = False

_ACTIVE_SESSION_STATES = frozenset({
    "needs_noise_capture",
    "preparing",
    "sweeping",
    "awaiting_capture",
    "needs_repeat_capture",
    "awaiting_repeat_capture",
    "needs_next_position",
    "analyzing",
    "verifying",
    "awaiting_verify_capture",
})


def _active_state_for_session(sess: Any | None) -> str | None:
    if sess is None:
        return None
    state = getattr(getattr(sess, "state", None), "value", None)
    return state if state in _ACTIVE_SESSION_STATES else None


def _reserve_start_slot() -> str | None:
    """Atomically reserve /start or return the state blocking it.

    The session state only becomes active once the background sweep task
    starts. This small reservation closes the gap between accepting
    `/start` and the new session visibly leaving IDLE.
    """
    global _start_in_progress
    with _session_lock:
        if _start_in_progress:
            return "starting"
        active_state = _active_state_for_session(_session)
        if active_state is not None:
            return active_state
        _start_in_progress = True
        return None


def _clear_start_slot() -> None:
    global _start_in_progress
    with _session_lock:
        _start_in_progress = False


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
    with _session_lock:
        if _session is None:
            _session = MeasurementSession()
        return _session


def _replace_session(
    *,
    total_positions: int = 1,
    target_choice: str = "flat",
    strategy_choice: str | None = None,
    mic_calibration=None,
    input_device: dict[str, Any] | None = None,
    repeat_main_position: bool = False,
):
    """Replace the global session with a fresh one. Called by /start
    so the user can re-run measurements without restarting the
    daemon. Phase 2 takes total_positions + target_choice from the
    body so the new session is configured before its first sweep."""
    from jasper.correction.session import MeasurementSession
    global _session
    with _session_lock:
        _session = MeasurementSession(
            total_positions=total_positions,
            target_choice=target_choice,
            strategy_choice=strategy_choice,
            mic_calibration=mic_calibration,
            input_device=input_device,
            repeat_main_position=repeat_main_position,
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
  .quality-banner { border-radius: 6px; padding: 0.7em 0.9em;
                    margin: 0.5em 0; font-size: 0.94em; }
  .quality-banner.warn { background: #fff8e1; border: 1px solid #d6b656;
                         color: #5f4500; }
  .quality-banner.fail { background: #ffe8e8; border: 1px solid #d99;
                         color: #800; }
  .quality-banner ul { margin: 0.4em 0 0; padding-left: 1.2em; }
  .quality-banner.hidden { display: none; }

  .browser-audio-card { border-radius: 6px; padding: 0.7em 0.9em;
                        margin: 0.5em 0; font-size: 0.94em;
                        background: #f7f7f7; border: 1px solid #ddd; }
  .browser-audio-card.ok { background: #e6f4ea; border-color: #1db954;
                           color: #176f36; }
  .browser-audio-card.warn { background: #fff8e1; border-color: #d6b656;
                             color: #5f4500; }
  .browser-audio-card.fail { background: #ffe8e8; border-color: #d99;
                             color: #800; }
  .browser-audio-card.hidden { display: none; }
  .browser-audio-card ul { margin: 0.4em 0 0; padding-left: 1.2em; }

  .confidence-card { border:1px solid #ddd; border-left:5px solid #999;
                     border-radius:6px; padding:0.75em 0.9em; margin:0.8em 0;
                     background:#fafafa; }
  .confidence-card.high { border-left-color:#1db954; }
  .confidence-card.medium { border-left-color:#d6b656; }
  .confidence-card.low { border-left-color:#d44; }
  .confidence-card.hidden { display:none; }
  .confidence-card h3 { margin-top:0; }
  .confidence-card .confidence-score { font-variant-numeric: tabular-nums;
                                      font-weight:600; }
  .confidence-card .gate-list { display:flex; gap:0.4em; flex-wrap:wrap;
                                margin:0.5em 0; }
  .confidence-card .gate { display:inline-block; border-radius:4px;
                           padding:0.18em 0.45em; font-size:0.86em;
                           background:#eee; color:#555; }
  .confidence-card .gate.allowed { background:#e4f5e9; color:#176f36; }
  .confidence-card .gate.blocked { background:#f7e4e4; color:#8a1f1f; }
  .confidence-card ul { margin:0.4em 0 0; padding-left:1.2em; }

  .runtime-card { border:1px solid #ddd; border-left:5px solid #999;
                  border-radius:6px; padding:0.75em 0.9em; margin:0.8em 0;
                  background:#fafafa; }
  .runtime-card.ok { border-left-color:#1db954; }
  .runtime-card.warn { border-left-color:#d6b656; }
  .runtime-card.fail { border-left-color:#d44; }
  .runtime-card.hidden { display:none; }
  .runtime-card h3 { margin-top:0; }
  .runtime-card ul { margin:0.4em 0 0; padding-left:1.2em; }

  .results-summary { border:1px solid #ddd; border-radius:6px;
                     background:#fafafa; padding:0.75em 0.9em;
                     margin:0.8em 0; }
  .results-summary.hidden { display:none; }
  .results-summary h3 { margin:0 0 0.45em; }
  .metric-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(120px, 1fr));
                 gap:0.5em; margin:0.6em 0; }
  .metric { background:white; border:1px solid #e2e2e2; border-radius:6px;
            padding:0.55em 0.65em; }
  .metric .label { display:block; color:#666; font-size:0.82em; }
  .metric .value { display:block; font-size:1.08em; font-weight:700;
                   font-variant-numeric:tabular-nums; margin-top:0.1em; }
  .band-table { width:100%; border-collapse:collapse; margin-top:0.55em;
                font-size:0.9em; }
  .band-table th, .band-table td { padding:0.35em 0.45em;
                                   border-bottom:1px solid #eee;
                                   text-align:right; }
  .band-table th:first-child, .band-table td:first-child { text-align:left; }
  @media (max-width: 520px) {
    .band-table, .band-table thead, .band-table tbody,
    .band-table tr, .band-table th, .band-table td { display:block; }
    .band-table thead { display:none; }
    .band-table tr { border:1px solid #e2e2e2; border-radius:6px;
                     padding:0.4em 0.55em; margin:0.55em 0;
                     background:#fff; }
    .band-table td { display:flex; justify-content:space-between;
                     gap:1em; border-bottom:1px solid #f0f0f0;
                     text-align:right; padding:0.3em 0; }
    .band-table td:first-child { font-weight:700; }
    .band-table td:last-child { border-bottom:0; }
    .band-table td::before { content:attr(data-label); color:#666;
                             font-weight:600; text-align:left; }
    .band-table td:first-child::before { content:''; }
  }
  .band-pill { display:inline-block; border-radius:4px; padding:0.1em 0.35em;
               font-size:0.82em; background:#eee; color:#555; }
  .band-pill.high { background:#e4f5e9; color:#176f36; }
  .band-pill.medium { background:#fff8e1; color:#5f4500; }
  .band-pill.low { background:#f7e4e4; color:#8a1f1f; }
  .chart-controls { display:flex; gap:0.7em; flex-wrap:wrap;
                    align-items:end; margin:0.6em 0; }
  .chart-controls label { display:flex; gap:0.35em; align-items:center;
                          font-size:0.9em; }
  .chart-controls label.stacked { display:block; }
  .chart-controls select { min-width:135px; }

  .mic-panel { background:#f7f7f7; border:1px solid #ddd;
               border-radius:6px; padding:0.8em 0.9em; margin:1em 0; }
  .mic-grid { display:grid; grid-template-columns: minmax(0, 1fr);
              gap:0.6em; }
  .mic-row { display:flex; gap:0.5em; flex-wrap:wrap; align-items:end; }
  .mic-row label { flex:1 1 180px; }
  .mic-row button { flex:0 0 auto; }
  .mic-status { margin:0.6em 0 0; color:#555; }
  .mic-status.ok { color:#176f36; font-weight:600; }
  .mic-status.bad { color:#a00000; font-weight:600; }
  .cal-preview { font-variant-numeric: tabular-nums; color:#555; }

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
  .state-badge.needs_noise_capture { background: #fff3cd; color: #806000; }
  .state-badge.preparing      { background: #fff3cd; color: #806000; }
  .state-badge.sweeping       { background: #cfe5ff; color: #003580; }
  .state-badge.awaiting_capture { background: #cfe5ff; color: #003580; }
  .state-badge.needs_repeat_capture { background: #fff3cd; color: #806000; }
  .state-badge.awaiting_repeat_capture { background: #cfe5ff; color: #003580; }
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

  #current-correction { border-radius: 6px; padding: 0.7em 0.9em;
                        margin: 0.5em 0 1em; display: flex;
                        align-items: center; justify-content: space-between;
                        flex-wrap: wrap; gap: 0.6em; }
  #current-correction.applied { background: #e6f4ea; border: 1px solid #1db954; }
  #current-correction.flat    { background: #f4f4f4; border: 1px solid #ddd; color: #555; }
  #current-correction.custom  { background: #fff8e1; border: 1px solid #d6b656; color: #5f4500; }
  #current-correction .label { font-weight: 600; }
  #current-correction button.danger { background: #d44; }

  .report-panel { border-top: 1px solid #e5e5e5; margin-top: 1.2em;
                  padding-top: 1em; }
  .session-list { display: grid; gap: 0.6em; margin-top: 0.7em; }
  .session-item { border: 1px solid #ddd; border-radius: 6px;
                  padding: 0.7em 0.8em; background: #fafafa; }
  .session-item p { margin: 0.25em 0 0.55em; }
  .session-report { border: 1px solid #ddd; border-left: 5px solid #777;
                    border-radius: 6px; padding: 0.8em 0.9em;
                    margin-top: 0.9em; background: #fff; }
  .session-report.ready { border-left-color: #1db954; }
  .session-report.caution { border-left-color: #d6b656; }
  .session-report.blocked { border-left-color: #d44; }
  .session-report h3 { margin: 0 0 0.4em; }
  .session-report h4 { margin: 0.75em 0 0.3em; }
  .session-report ul { margin: 0.25em 0 0; padding-left: 1.2em; }
"""


_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
__CSRF_META__
<title>Room correction — JTS speaker</title>
<style>__STYLE__</style>
</head>
<body>
__NAV_BACK__
<h1>Room correction</h1>
<p class="sub">Measure your room from this iPhone, design correction filters, and apply them to the speaker.</p>

<div id="current-correction" class="flat" aria-live="polite">
  <span class="label" id="current-correction-label">Checking current correction…</span>
  <button id="current-correction-reset" type="button" class="danger hidden">Reset to flat</button>
</div>

<details class="advice" open>
  <summary>Where to put the phone</summary>
  <ol>
    <li><strong>Hold or prop the phone where your head will be when listening</strong> — sitting on the couch / chair, at ear height. <em>Not</em> on the cushion below your head; the cushion absorbs sound your ears would receive.</li>
    <li>Phone <strong>flat, screen up</strong>, with the <strong>bottom edge</strong> (speaker / mic end) pointing toward the speakers.</li>
    <li>Take it out of any case if it has one.</li>
    <li>Keep the room quiet during the sweep — close windows, mute other devices, no talking.</li>
  </ol>
  <p class="hint">If you are using an external USB measurement mic, pick it below after granting mic permission. Holding the mic at ear height means we're measuring what you actually hear.</p>
</details>

<div class="mic-panel">
  <h2 style="margin-top:0">Microphone</h2>
  <div class="mic-grid">
    <div class="mic-row">
      <label for="input-device-select">Input device
        <select id="input-device-select">
          <option value="">Default microphone</option>
        </select>
      </label>
      <button id="refresh-inputs" type="button" class="secondary">Refresh inputs</button>
    </div>
    <p class="hint" style="margin:0">Browser labels usually appear after you tap <strong>Start mic capture</strong> and grant permission.</p>

    <label for="mic-model-select">Calibration
      <select id="mic-model-select">
        <option value="">None / phone built-in</option>
        __MIC_MODEL_OPTIONS__
        <option value="other">Other calibrated mic</option>
      </select>
    </label>

    <div id="serial-row" class="mic-row hidden">
      <label for="mic-serial">Serial number
        <input id="mic-serial" type="text" inputmode="text" autocomplete="off"
               placeholder="e.g. 700-1234">
      </label>
      <button id="fetch-calibration" type="button" class="secondary">Fetch calibration</button>
    </div>

    <div id="upload-row" class="mic-row hidden">
      <label for="calibration-file">Calibration file
        <input id="calibration-file" type="file" accept=".txt,.cal,.frd,.csv,.omm,text/plain">
      </label>
      <label for="mic-orientation">Orientation
        <select id="mic-orientation">
          <option value="0deg">0° / pointed at speaker</option>
          <option value="90deg">90° / upright</option>
          <option value="unknown">Unknown</option>
        </select>
      </label>
      <label for="calibration-sign">File values are
        <select id="calibration-sign">
          <option value="correction">dB correction to add</option>
          <option value="response">mic response to invert</option>
        </select>
      </label>
      <button id="upload-calibration" type="button" class="secondary">Upload calibration</button>
    </div>
    <p id="calibration-status" class="mic-status">No calibration loaded. This is okay for a quick check, but a calibrated mic is recommended before trusting filter decisions.</p>
    <p id="calibration-preview" class="cal-preview hidden"></p>
  </div>
</div>

<button id="start" type="button">Start mic capture</button>

<div id="constraints" class="hidden" aria-live="polite">
  <h2>Capture settings</h2>
  <p class="hint">iOS Safari may silently ignore audio constraints (WebKit Bug 179411). The measurement refuses to start unless every row reads <span class="ok">✓ ok</span>.</p>
  <table class="constraint-table">
    <thead><tr><th>Setting</th><th>Requested</th><th>Actual</th><th>Status</th></tr></thead>
    <tbody id="constraint-rows"></tbody>
  </table>
  <div id="err-banner" class="err-banner hidden"></div>
  <div id="browser-audio-report" class="browser-audio-card hidden"></div>

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
      __TARGET_PROFILE_OPTIONS__
    </select>

    <label for="strategy-select" style="margin-top:0.6em">Correction strategy</label>
    <select id="strategy-select" form="dummy">
      __CORRECTION_STRATEGY_OPTIONS__
    </select>
    <p class="hint" style="margin-top:0.3em">Strategy controls the correction band, filter count, cut/boost policy, and safety bounds. Balanced is the default; Assertive is for calibrated, repeatable measurements.</p>
    <label style="margin-top:0.6em">
      <input id="repeat-main-position" type="checkbox" checked>
      Repeat the main seat once for a trust check
    </label>
    <p class="hint" style="margin-top:0.3em">This adds one extra sweep at the first position and helps JTS tell measurement noise from real room behavior.</p>
  </div>

  <p>Status: <span id="state-badge" class="state-badge idle">idle</span>
    <span id="state-detail" class="hint"></span></p>
  <div id="quality-banner" class="quality-banner hidden"></div>

  <div id="position-prompt" class="hidden" style="background:#fff3cd; border-radius:6px; padding:0.7em 0.9em; margin:0.5em 0;">
    <p style="margin:0; font-weight:600">Move phone to position <span id="position-current">2</span> of <span id="position-total">5</span>.</p>
    <p class="hint" style="margin-top:0.3em">Move ~30 cm from the previous position — left, right, forward, or back, head-height. Same orientation: phone flat, bottom edge pointing at the speakers. Tap Continue when ready.</p>
  </div>

  <p style="display:flex; gap:0.6em; flex-wrap:wrap">
    <button id="autolevel" type="button" class="secondary" disabled>Auto-level</button>
    <button id="autolevel-lock" type="button" class="primary hidden">Lock now</button>
    <button id="autolevel-cancel" type="button" class="danger hidden">Cancel</button>
    <button id="run-measurement" type="button" class="primary" disabled>Run measurement</button>
    <button id="repeat-position" type="button" class="primary hidden">Repeat main seat</button>
    <button id="continue-position" type="button" class="primary hidden">Continue to next position</button>
    <button id="apply-correction" type="button" class="primary hidden">Apply correction</button>
    <button id="verify-correction" type="button" class="primary hidden">Verify with re-measurement</button>
    <button id="reset-correction" type="button" class="danger hidden">Reset to flat</button>
  </p>
  <p class="hint" style="margin-top:0.4em">Before measuring, tap <strong>Auto-level</strong>. The speaker plays a 1 kHz tone while we gradually raise the volume from quiet to a measurement-friendly level (capped at −6 dB software volume — your amp's analog gain is still the final say). When the iPhone mic hears it in the target range, we lock automatically. If the volume sounds right to <em>you</em> first, tap <strong>Lock now</strong>. Takes ~6 seconds at most.</p>
  <p class="hint" style="margin-top:0.4em">Each measurement starts from flat — your current correction (if any) is reset first so the sweep captures the raw room. After you tap <strong>Apply</strong>, the new correction takes over.</p>
  <div id="autolevel-status" class="hidden" style="background:#fff3cd; border-radius:6px; padding:0.7em 0.9em; margin:0.5em 0;">
    <p style="margin:0; font-weight:600" id="autolevel-line">Auto-leveling…</p>
    <p class="hint" style="margin-top:0.3em" id="autolevel-detail"></p>
  </div>
  <div id="result-section" class="hidden">
    <div id="confidence-panel" class="confidence-card hidden"></div>
    <div id="runtime-integrity-panel" class="runtime-card hidden"></div>
    <div id="results-summary" class="results-summary hidden"></div>
    <h3>Frequency response</h3>
    <div class="chart-controls">
      <label class="stacked" for="chart-smoothing">Display smoothing<br>
        <select id="chart-smoothing">
          <option value="none">Saved 1/48-oct</option>
          <option value="1/12" selected>1/12-oct</option>
          <option value="1/6">1/6-oct</option>
          <option value="1/3">1/3-oct</option>
        </select>
      </label>
      <label><input id="chart-show-spread" type="checkbox" checked> spatial spread</label>
      <label><input id="chart-show-filter" type="checkbox" checked> filter effect</label>
      <label><input id="chart-show-band" type="checkbox" checked> correction band</label>
    </div>
    <div class="chart-wrap"><canvas id="chart"></canvas></div>
    <p class="hint">
      <span style="color:#d44">red</span> = measured (averaged across positions),
      <span style="color:#888">gray dashed</span> = target,
      <span style="color:#1db954">green</span> = predicted post-correction.
      <span style="color:#2b7bb9">blue dashed</span> = filter effect.
      After Verify: <span style="color:#a050d0">purple dashed</span> = post-correction measurement.
    </p>
    <p id="verify-summary" class="hint hidden"></p>
    <div id="design-report" class="hidden"></div>
    <h3>Filters designed</h3>
    <div class="peq-list" id="peq-list"></div>
  </div>
</div>

<section id="measurement-reports" class="report-panel">
  <h2>Measurement reports</h2>
  <p class="hint">Read-only evidence from previous sessions. Reports summarize what JTS measured, what looks trustworthy, and what evidence is missing without exposing raw recordings in the browser.</p>
  <button id="load-sessions" type="button" class="secondary">Load recent reports</button>
  <div id="session-history" class="session-list"></div>
  <div id="session-report" class="session-report hidden"></div>
</section>

<details class="disclosure">
  <summary>Optional: silence Safari's "Not Private" warning on future visits</summary>
  <div class="disclosure-body">
    <p>You're seeing this page because you tapped through Safari's "Not Private" warning — that's fine and the page works correctly. The warning appears on every visit unless you install this speaker's certificate as a trusted authority on this device.</p>
    <ol>
      <li>Tap <a href="http://__HOSTNAME__/jts-root-ca.crt">Download the JTS root CA</a> (plain HTTP — necessary because HTTPS isn't trusted yet). Safari prompts <em>"This website is trying to download a configuration profile."</em> Tap <strong>Allow</strong>.</li>
      <li>Open the <strong>Settings</strong> app. A new entry near the top says <em>"Profile Downloaded — JTS Speaker Local CA"</em>. Tap it → <strong>Install</strong> → enter passcode → <strong>Install</strong> → <strong>Done</strong>.</li>
      <li>Go to <strong>Settings → General → About → Certificate Trust Settings</strong>. Toggle <strong>JTS Speaker Local CA</strong> on. Tap <strong>Continue</strong> through the warning Apple shows for any non-public CA.</li>
    </ol>
    <p class="hint">To remove later: Settings → General → VPN &amp; Device Management → JTS Speaker Local CA → Remove Profile.</p>
  </div>
</details>

<script>
(function () {
  'use strict';

  var REQUIRED_SR = __REQUIRED_SR__;

  var startBtn = document.getElementById('start');
  var inputDeviceSelect = document.getElementById('input-device-select');
  var refreshInputsBtn = document.getElementById('refresh-inputs');
  var micModelSelect = document.getElementById('mic-model-select');
  var micSerialInput = document.getElementById('mic-serial');
  var micOrientationSelect = document.getElementById('mic-orientation');
  var serialRow = document.getElementById('serial-row');
  var uploadRow = document.getElementById('upload-row');
  var calibrationFileInput = document.getElementById('calibration-file');
  var calibrationSignSelect = document.getElementById('calibration-sign');
  var fetchCalibrationBtn = document.getElementById('fetch-calibration');
  var uploadCalibrationBtn = document.getElementById('upload-calibration');
  var calibrationStatus = document.getElementById('calibration-status');
  var calibrationPreview = document.getElementById('calibration-preview');
  var currentCorrectionBanner = document.getElementById('current-correction');
  var currentCorrectionLabel = document.getElementById('current-correction-label');
  var currentCorrectionResetBtn = document.getElementById('current-correction-reset');
  var constraintsBlock = document.getElementById('constraints');
  var rowsTbody = document.getElementById('constraint-rows');
  var errBanner = document.getElementById('err-banner');
  var browserAudioReport = document.getElementById('browser-audio-report');
  var levelBar = document.getElementById('level-bar-fill');
  var levelReadout = document.getElementById('level-db');
  var measureSection = document.getElementById('measure-section');
  var stateBadge = document.getElementById('state-badge');
  var stateDetail = document.getElementById('state-detail');
  var qualityBanner = document.getElementById('quality-banner');
  var autolevelBtn = document.getElementById('autolevel');
  var autolevelLockBtn = document.getElementById('autolevel-lock');
  var autolevelCancelBtn = document.getElementById('autolevel-cancel');
  var autolevelStatus = document.getElementById('autolevel-status');
  var autolevelLine = document.getElementById('autolevel-line');
  var autolevelDetail = document.getElementById('autolevel-detail');
  var runBtn = document.getElementById('run-measurement');
  var repeatBtn = document.getElementById('repeat-position');
  var continueBtn = document.getElementById('continue-position');
  var applyBtn = document.getElementById('apply-correction');
  var verifyBtn = document.getElementById('verify-correction');
  var resetBtn = document.getElementById('reset-correction');
  var positionsSelect = document.getElementById('positions-select');
  var repeatMainPosition = document.getElementById('repeat-main-position');
  var targetSelect = document.getElementById('target-select');
  var strategySelect = document.getElementById('strategy-select');
  var positionPrompt = document.getElementById('position-prompt');
  var positionCurrent = document.getElementById('position-current');
  var positionTotal = document.getElementById('position-total');
  var resultSection = document.getElementById('result-section');
  var resultsSummary = document.getElementById('results-summary');
  var chartSmoothing = document.getElementById('chart-smoothing');
  var chartShowSpread = document.getElementById('chart-show-spread');
  var chartShowFilter = document.getElementById('chart-show-filter');
  var chartShowBand = document.getElementById('chart-show-band');
  var canvas = document.getElementById('chart');
  var peqList = document.getElementById('peq-list');
  var verifySummary = document.getElementById('verify-summary');
  var designReport = document.getElementById('design-report');
  var confidencePanel = document.getElementById('confidence-panel');
  var runtimeIntegrityPanel = document.getElementById('runtime-integrity-panel');
  var loadSessionsBtn = document.getElementById('load-sessions');
  var sessionHistory = document.getElementById('session-history');
  var sessionReport = document.getElementById('session-report');

  var ctx = null;
  var micStream = null;
  var workletNode = null;
  var pollTimer = null;
  var sessionId = null;
  var currentState = null;
  var lastResult = null;
  var lastVerify = null;
  var inVerifyMode = false;
  var captureMode = 'measurement';
  // Latest mic RMS in dBFS, updated by the AudioWorklet at ~20 Hz.
  // The autolevel loop reads this to decide when the speaker level
  // has reached the target range.
  var latestMicRmsDb = -120;
  var lastNoiseFloorDb = null;
  var autolevelRmsBuffer = [];  // recent dB samples for smoothing
  var selectedCalibrationId = null;
  var selectedCalibrationMeta = null;
  var selectedInputDevice = null;

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

  function stopMicStream() {
    if (micStream) {
      micStream.getTracks().forEach(function (t) { t.stop(); });
      micStream = null;
    }
    if (ctx && ctx.state !== 'closed') {
      ctx.close().catch(function () {});
    }
    ctx = null;
    workletNode = null;
  }

  function escapeText(s) {
    return String(s || '').replace(/[&<>"']/g, function (ch) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch];
    });
  }

  async function populateInputDevices(selectedId) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      return;
    }
    try {
      var devices = await navigator.mediaDevices.enumerateDevices();
      var inputs = devices.filter(function (d) { return d.kind === 'audioinput'; });
      var prior = selectedId || inputDeviceSelect.value || '';
      inputDeviceSelect.innerHTML = '<option value="">Default microphone</option>';
      inputs.forEach(function (d, idx) {
        var opt = document.createElement('option');
        opt.value = d.deviceId;
        opt.textContent = d.label || ('Microphone ' + (idx + 1));
        inputDeviceSelect.appendChild(opt);
      });
      if (prior) inputDeviceSelect.value = prior;
    } catch (e) {
      console.warn('enumerateDevices failed', e);
    }
  }

  function updateMicCalibrationRows() {
    var model = micModelSelect.value;
    selectedCalibrationId = null;
    selectedCalibrationMeta = null;
    calibrationPreview.classList.add('hidden');
    calibrationPreview.textContent = '';
    calibrationStatus.className = 'mic-status';
    if (!model) {
      serialRow.classList.add('hidden');
      uploadRow.classList.add('hidden');
      calibrationStatus.textContent =
        'No calibration loaded. This is okay for a quick check, but a calibrated mic is recommended before trusting filter decisions.';
    } else if (model === 'other') {
      serialRow.classList.add('hidden');
      uploadRow.classList.remove('hidden');
      calibrationStatus.textContent =
        'Upload a calibration file for this microphone.';
    } else {
      serialRow.classList.remove('hidden');
      uploadRow.classList.remove('hidden');
      calibrationStatus.textContent =
        'Enter the mic serial so JTS can fetch the calibration file. Upload is available as a fallback.';
    }
  }

  function invalidateLoadedCalibration() {
    if (!selectedCalibrationId && !selectedCalibrationMeta) return;
    selectedCalibrationId = null;
    selectedCalibrationMeta = null;
    calibrationPreview.classList.add('hidden');
    calibrationPreview.textContent = '';
    calibrationStatus.className = 'mic-status bad';
    if (micModelSelect.value === 'other') {
      calibrationStatus.textContent =
        'Calibration settings changed. Upload the file again before measuring.';
    } else {
      calibrationStatus.textContent =
        'Calibration settings changed. Fetch or upload again before measuring.';
    }
  }

  function showCalibrationLoaded(payload) {
    selectedCalibrationMeta = payload.calibration || null;
    selectedCalibrationId = selectedCalibrationMeta ?
      selectedCalibrationMeta.calibration_id : null;
    if (!selectedCalibrationId) return;
    calibrationStatus.className = 'mic-status ok';
    calibrationStatus.textContent =
      'Loaded ' + selectedCalibrationMeta.label + ' calibration (' +
      selectedCalibrationMeta.point_count + ' points).';
    if (payload.preview && payload.preview.freqs_hz) {
      var n = payload.preview.freqs_hz.length;
      var f0 = payload.preview.freqs_hz[0];
      var f1 = payload.preview.freqs_hz[n - 1];
      calibrationPreview.textContent =
        'Preview range: ' + Math.round(f0) + '–' + Math.round(f1) +
        ' Hz · hash ' + selectedCalibrationMeta.file_sha256.slice(0, 12);
      calibrationPreview.classList.remove('hidden');
    }
  }

  async function fetchCalibration() {
    var model = micModelSelect.value;
    var serial = micSerialInput.value.trim();
    if (!model || model === 'other') return;
    if (!serial) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent = 'Enter the microphone serial number first.';
      return;
    }
    fetchCalibrationBtn.disabled = true;
    calibrationStatus.className = 'mic-status';
    calibrationStatus.textContent = 'Fetching calibration from vendor…';
    try {
      var payload = await postJson('calibration/fetch', {
        model: model,
        serial: serial,
        orientation: micOrientationSelect.value || 'unknown'
      });
      showCalibrationLoaded(payload);
    } catch (e) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent =
        'Lookup failed: ' + e.message + '. Use upload as a fallback.';
    } finally {
      fetchCalibrationBtn.disabled = false;
    }
  }

  async function uploadCalibration() {
    var file = calibrationFileInput.files && calibrationFileInput.files[0];
    if (!file) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent = 'Choose a calibration file first.';
      return;
    }
    uploadCalibrationBtn.disabled = true;
    calibrationStatus.className = 'mic-status';
    calibrationStatus.textContent = 'Reading calibration file…';
    try {
      var content = await file.text();
      var payload = await postJson('calibration/upload', {
        filename: file.name,
        content: content,
        model: micModelSelect.value || 'other',
        label: micModelSelect.options[micModelSelect.selectedIndex].text,
        orientation: micOrientationSelect.value || 'unknown',
        sign_convention: calibrationSignSelect.value || 'correction'
      });
      showCalibrationLoaded(payload);
    } catch (e) {
      calibrationStatus.className = 'mic-status bad';
      calibrationStatus.textContent = 'Upload failed: ' + e.message;
    } finally {
      uploadCalibrationBtn.disabled = false;
    }
  }

  function selectedInputDeviceMetadata(actual) {
    var opt = inputDeviceSelect.options[inputDeviceSelect.selectedIndex];
    var requestedId = inputDeviceSelect.value || null;
    return {
      device_id: actual.deviceId || requestedId,
      requested_device_id: requestedId,
      actual_device_id: actual.deviceId || null,
      label: actual.label || (opt && opt.textContent) || null,
      browser_label: actual.label || null,
      sample_rate: actual.sampleRate,
      channel_count: actual.channelCount,
      echo_cancellation: actual.echoCancellation,
      noise_suppression: actual.noiseSuppression,
      auto_gain_control: actual.autoGainControl
    };
  }

  function renderConstraints(actual, problems) {
    var rows = [
      ['inputDevice', 'selected',
       actual.label || actual.deviceId || 'default microphone', true],
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
        '<td>' + escapeText(r[0]) + '</td><td>' + escapeText(r[1]) + '</td><td>' + escapeText(r[2]) + '</td>' +
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
    renderBrowserAudioLocal(actual, problems);
  }

  function renderBrowserAudioLocal(actual, problems) {
    var issues = problems.slice();
    if (actual.channelCount !== 1) {
      issues.push('channelCount is ' + actual.channelCount + ' (JTS will use the first channel)');
    }
    var level = problems.length ? 'fail' : (issues.length ? 'warn' : 'ok');
    browserAudioReport.className = 'browser-audio-card ' + level;
    browserAudioReport.innerHTML =
      '<strong>Browser audio path: ' +
      (level === 'ok' ? 'ready' : (level === 'fail' ? 'blocked' : 'usable with warnings')) +
      '</strong>' +
      (issues.length
        ? '<ul>' + issues.map(function (issue) {
            return '<li>' + escapeText(issue) + '</li>';
          }).join('') + '</ul>'
        : '<p class="hint">Input metadata looks ready for measurement. Capture quality is still checked after each sweep.</p>');
  }

  function renderBrowserAudioReport(report) {
    if (!report) return;
    var level = report.level || (report.failed ? 'fail' : 'warn');
    browserAudioReport.className = 'browser-audio-card ' + level;
    var issues = report.issues || [];
    browserAudioReport.innerHTML =
      '<strong>Browser audio path: ' +
      escapeText(level === 'ok' ? 'ready' : (level === 'fail' ? 'blocked' : 'usable with warnings')) +
      '</strong><p class="hint">' + escapeText(report.summary || '') + '</p>' +
      (issues.length
        ? '<ul>' + issues.map(function (issue) {
            return '<li>' + escapeText(issue.message || issue.code) + '</li>';
          }).join('') + '</ul>'
        : '');
  }

  async function startMicCapture() {
    startBtn.disabled = true;
    startBtn.textContent = 'Capturing…';
    stopMicStream();

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
    var audioConstraints = {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
      sampleRate: REQUIRED_SR,
      channelCount: 1
    };
    if (inputDeviceSelect.value) {
      audioConstraints.deviceId = {exact: inputDeviceSelect.value};
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: audioConstraints,
        video: false
      });
      micStream = stream;
    } catch (e) {
      stopMicStream();
      alert('Microphone permission denied or unavailable: ' + e.message);
      startBtn.disabled = false;
      startBtn.textContent = 'Start mic capture';
      return;
    }

    constraintsBlock.classList.remove('hidden');
    measureSection.classList.remove('hidden');

    var settings = stream.getAudioTracks()[0].getSettings();
    var trackLabel = stream.getAudioTracks()[0].label || '';
    var actual = {
      sampleRate: settings.sampleRate || ctx.sampleRate,
      echoCancellation: settings.echoCancellation,
      noiseSuppression: settings.noiseSuppression,
      autoGainControl: settings.autoGainControl,
      channelCount: settings.channelCount || 1,
      deviceId: settings.deviceId || inputDeviceSelect.value || '',
      label: trackLabel
    };
    await populateInputDevices(actual.deviceId);
    selectedInputDevice = selectedInputDeviceMetadata(actual);
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
        onCaptureReady(ev.data.buffer, captureMode);
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

  function qualityReports(payload) {
    var reports = [];
    if (payload && Array.isArray(payload.capture_quality)) {
      reports = reports.concat(payload.capture_quality);
    }
    if (payload && payload.verify_quality) {
      reports.push(payload.verify_quality);
    }
    return reports;
  }

  function renderQuality(payload) {
    var seen = {};
    var issues = [];
    qualityReports(payload).forEach(function (report) {
      (report && report.issues || []).forEach(function (issue) {
        var key = [issue.severity, issue.code, issue.message].join('|');
        if (!seen[key]) {
          seen[key] = true;
          issues.push(issue);
        }
      });
    });
    if (!issues.length) {
      qualityBanner.className = 'quality-banner hidden';
      qualityBanner.innerHTML = '';
      return;
    }
    var hasFail = issues.some(function (issue) {
      return issue.severity === 'fail';
    });
    qualityBanner.className = 'quality-banner ' + (hasFail ? 'fail' : 'warn');
    qualityBanner.innerHTML =
      '<strong>' + (hasFail ? 'Measurement blocked:' : 'Measurement quality warnings:') +
      '</strong><ul>' +
      issues.map(function (issue) {
        return '<li>' + escapeText(issue.message || issue.code) + '</li>';
      }).join('') +
      '</ul>';
  }

  function formatAppliedAt(epoch) {
    if (!epoch) return '';
    var d = new Date(epoch * 1000);
    if (isNaN(d.getTime())) return '';
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var pad = function (n) { return n < 10 ? '0' + n : String(n); };
    return days[d.getDay()] + ' ' + d.getFullYear() + '-' +
      pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
      pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function renderCurrentCorrection(cc, config) {
    // `cc` is the parsed JTS room-correction descriptor. `config`
    // distinguishes flat JTS baseline, preference EQ, and custom
    // CamillaDSP configs that JTS should not overclaim as flat.
    if (cc && cc.applied_at_epoch) {
      currentCorrectionBanner.className = 'applied';
      var when = formatAppliedAt(cc.applied_at_epoch);
      var count = cc.peq_count || 0;
      var noun = count === 1 ? 'filter' : 'filters';
      currentCorrectionLabel.textContent =
        'Current correction: ' + count + ' PEQ ' + noun +
        (when ? ' applied ' + when : '');
      currentCorrectionResetBtn.classList.remove('hidden');
    } else if (config && config.kind === 'custom') {
      currentCorrectionBanner.className = 'custom';
      currentCorrectionLabel.textContent =
        'Advanced DSP config active — JTS cannot safely preserve it during measurement.';
      currentCorrectionResetBtn.classList.remove('hidden');
    } else if (config && config.kind === 'sound_preference') {
      currentCorrectionBanner.className = 'flat';
      currentCorrectionLabel.textContent =
        'Preference EQ is active; no room correction is applied.';
      currentCorrectionResetBtn.classList.add('hidden');
    } else if (config && config.kind === 'unknown') {
      currentCorrectionBanner.className = 'custom';
      currentCorrectionLabel.textContent =
        config.message || 'Could not identify the active CamillaDSP config.';
      currentCorrectionResetBtn.classList.add('hidden');
    } else {
      currentCorrectionBanner.className = 'flat';
      currentCorrectionLabel.textContent =
        'No correction applied — speaker is flat.';
      currentCorrectionResetBtn.classList.add('hidden');
    }
  }

  async function refreshCurrentCorrection() {
    try {
      var s = await fetchStatus();
      renderCurrentCorrection(s.current_correction, s.current_config);
    } catch (e) {
      currentCorrectionBanner.className = 'flat';
      currentCorrectionLabel.textContent =
        'Could not read current correction: ' + e.message;
      currentCorrectionResetBtn.classList.add('hidden');
    }
  }

  async function resetFromBanner() {
    currentCorrectionResetBtn.disabled = true;
    currentCorrectionLabel.textContent = 'Resetting to flat…';
    try {
      await postJson('reset', {});
    } catch (e) {
      currentCorrectionLabel.textContent =
        'Reset failed: ' + e.message;
      currentCorrectionResetBtn.disabled = false;
      return;
    }
    await refreshCurrentCorrection();
    currentCorrectionResetBtn.disabled = false;
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

  function renderDesignReport(report) {
    if (!report || !report.correction_strategy) {
      designReport.classList.add('hidden');
      designReport.innerHTML = '';
      return;
    }
    var before = report.before || {};
    var after = report.after || {};
    var improvement = report.improvement || {};
    var warnings = report.warnings || [];
    var filterAudits = report.filters || [];
    var warningHtml = '';
    if (warnings.length) {
      warningHtml = '<ul>' + warnings.map(function (w) {
        return '<li>' + escapeText(w.message || w.code) + '</li>';
      }).join('') + '</ul>';
    }
    var filterHtml = '';
    if (filterAudits.length) {
      filterHtml = '<ul>' + filterAudits.map(function (f) {
        return '<li>' + escapeText(f.rationale || (
          'Filter near ' + Math.round(f.freq_hz) + ' Hz'
        )) + '</li>';
      }).join('') + '</ul>';
    }
    designReport.classList.remove('hidden');
    designReport.innerHTML =
      '<h3>Design audit</h3>' +
      '<p class="hint">' +
      'Strategy: <strong>' + escapeText(report.correction_strategy.label) +
      '</strong> · Target: <strong>' + escapeText(report.target_profile.label) +
      '</strong> · Band: ' + Math.round(report.band_hz[0]) + '-' +
      Math.round(report.band_hz[1]) + ' Hz.</p>' +
      '<p class="hint">Predicted modal-band RMS error: ' +
      (before.rms_db || 0).toFixed(1) + ' dB -> ' +
      (after.rms_db || 0).toFixed(1) + ' dB' +
      ' (' + (improvement.rms_db || 0).toFixed(1) +
      ' dB improvement).</p>' +
      warningHtml + filterHtml;
  }

  function renderConfidence(payload) {
    var report = payload && (
      payload.confidence_report ||
      (payload.design_report && payload.design_report.confidence_report)
    );
    if (!report) {
      confidencePanel.className = 'confidence-card hidden';
      confidencePanel.innerHTML = '';
      return;
    }

    var level = report.level || 'low';
    var score = typeof report.score === 'number' ? report.score : 0;
    var variance = report.position_variance || {};
    var gates = report.strategy_gates || {};
    var findings = (report.findings || []).slice(0, 5);
    var gateHtml = ['safe', 'balanced', 'assertive'].map(function (name) {
      var gate = gates[name] || {};
      var allowed = !!gate.allowed;
      return '<span class="gate ' + (allowed ? 'allowed' : 'blocked') + '">' +
        escapeText(name) + ': ' + (allowed ? 'allowed' : 'blocked') +
        '</span>';
    }).join('');

    var varianceHtml = '';
    if (variance.available) {
      varianceHtml =
        '<p class="hint">Position variance: ' +
        'p90 std ' + Number(variance.p90_std_db || 0).toFixed(1) + ' dB, ' +
        'max range ' + Number(variance.max_range_db || 0).toFixed(1) +
        ' dB across ' + Number(variance.position_count || 0) +
        ' positions.</p>';
    } else {
      varianceHtml =
        '<p class="hint">Position variance unavailable: ' +
        escapeText(variance.reason || 'need more completed positions') +
        '.</p>';
    }

    var findingsHtml = '';
    if (findings.length) {
      findingsHtml = '<ul>' + findings.map(function (finding) {
        return '<li>' + escapeText(finding.message || finding.code) + '</li>';
      }).join('') + '</ul>';
    }

    confidencePanel.className = 'confidence-card ' + level;
    confidencePanel.innerHTML =
      '<h3>Measurement confidence</h3>' +
      '<p><strong>' + escapeText(level.toUpperCase()) + '</strong> · ' +
      '<span class="confidence-score">' + score + '/100</span></p>' +
      '<p class="hint">' + escapeText(report.summary || '') + '</p>' +
      varianceHtml +
      '<div class="gate-list">' + gateHtml + '</div>' +
      findingsHtml;
  }

  function renderRuntimeIntegrity(payload) {
    var report = payload && (
      payload.runtime_integrity ||
      (payload.confidence_report && payload.confidence_report.runtime_integrity)
    );
    if (!report || (!report.snapshot_count && !report.capture_count)) {
      runtimeIntegrityPanel.className = 'runtime-card hidden';
      runtimeIntegrityPanel.innerHTML = '';
      return;
    }
    var level = report.level || 'ok';
    var latest = report.latest_snapshot || {};
    var memory = latest.memory || {};
    var load = latest.load_per_core;
    var issues = (report.issues || []).slice(0, 5);
    var issueHtml = issues.length
      ? '<ul>' + issues.map(function (issue) {
          return '<li>' + escapeText(issue.message || issue.code) + '</li>';
        }).join('') + '</ul>'
      : '<p class="hint">No runtime warnings were recorded around the sweep.</p>';
    var loadText = Number.isFinite(Number(load))
      ? Number(load).toFixed(2) + ' load/core'
      : 'load unavailable';
    var memText = Number.isFinite(Number(memory.available_mb))
      ? Number(memory.available_mb).toFixed(0) + ' MB free'
      : 'memory unavailable';
    runtimeIntegrityPanel.className = 'runtime-card ' + level;
    runtimeIntegrityPanel.innerHTML =
      '<h3>Runtime integrity</h3>' +
      '<p><strong>' + escapeText(level.toUpperCase()) + '</strong> · ' +
      Number(report.capture_count || 0) + ' capture artifact(s), ' +
      Number(report.snapshot_count || 0) + ' system snapshot(s).</p>' +
      '<p class="hint">' + escapeText(loadText) + ' · ' +
      escapeText(memText) + '</p>' +
      issueHtml;
  }

  function numberOrNull(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function formatHz(value) {
    var n = numberOrNull(value);
    if (n === null) return '—';
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + ' kHz';
    return n.toFixed(n >= 100 ? 0 : 1) + ' Hz';
  }

  function formatDb(value) {
    var n = numberOrNull(value);
    if (n === null) return '—';
    return (n > 0 ? '+' : '') + n.toFixed(1) + ' dB';
  }

  function formatMaybeDb(value) {
    var n = numberOrNull(value);
    return n === null ? '—' : n.toFixed(1) + ' dB';
  }

  function reportIssueList(items, fallback) {
    items = (items || []).filter(function (item) { return !!item; }).slice(0, 8);
    if (!items.length) return '<p class="hint">' + escapeText(fallback) + '</p>';
    return '<ul>' + items.map(function (item) {
      return '<li>' + escapeText(
        item.message || item.reason || item.code || item.kind || String(item)
      ) + '</li>';
    }).join('') + '</ul>';
  }

  async function loadSessionReports() {
    loadSessionsBtn.disabled = true;
    sessionHistory.textContent = 'Loading recent sessions…';
    try {
      var resp = await fetch('sessions', {cache: 'no-store'});
      if (!resp.ok) throw new Error('sessions ' + resp.status);
      var payload = await resp.json();
      renderSessionHistory(payload.sessions || []);
    } catch (e) {
      sessionHistory.textContent = 'Could not load measurement reports: ' + e.message;
    } finally {
      loadSessionsBtn.disabled = false;
    }
  }

  function renderSessionHistory(sessions) {
    sessionHistory.innerHTML = '';
    if (!sessions.length) {
      var empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = 'No completed measurement bundles found yet.';
      sessionHistory.appendChild(empty);
      return;
    }
    sessions.forEach(function (session) {
      var item = document.createElement('div');
      item.className = 'session-item';
      var title = document.createElement('strong');
      title.textContent = 'Session ' + (session.session_id || 'unknown');
      var meta = document.createElement('p');
      meta.className = 'hint';
      var state = session.state || 'unknown';
      var positions = Number(session.current_position || 0) + '/' +
        Number(session.total_positions || 0);
      var started = formatAppliedAt(session.started_at);
      meta.textContent = state + ' · positions ' + positions +
        (started ? ' · ' + started : '') +
        (session.has_result ? ' · result saved' : ' · no result yet');
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = 'View report';
      button.dataset.sessionId = session.session_id || '';
      item.appendChild(title);
      item.appendChild(meta);
      item.appendChild(button);
      sessionHistory.appendChild(item);
    });
  }

  async function loadSessionReport(sessionId) {
    if (!sessionId) return;
    sessionReport.className = 'session-report';
    sessionReport.textContent = 'Loading report…';
    try {
      var resp = await fetch(
        'session-report?id=' + encodeURIComponent(sessionId),
        {cache: 'no-store'}
      );
      var text = await resp.text();
      var payload;
      try {
        payload = JSON.parse(text);
      } catch (_e) {
        payload = {error: text};
      }
      if (!resp.ok) {
        throw new Error(payload.error || ('session-report ' + resp.status));
      }
      renderSessionReport(payload);
    } catch (e) {
      sessionReport.className = 'session-report blocked';
      sessionReport.textContent = 'Could not load report: ' + e.message;
    }
  }

  function renderSessionReport(payload) {
    var evidence = payload.evidence || {};
    var readiness = evidence.agent_readiness || {};
    var bundle = evidence.bundle || {};
    var measurement = evidence.measurement || {};
    var confidence = evidence.confidence || {};
    var acoustic = (evidence.acoustic_quality || {}).summary || {};
    var runtime = (evidence.runtime_integrity || {}).summary || {};
    var position = evidence.position_analysis || {};
    var repeatability = evidence.repeatability || {};
    var versions = payload.artifact_versions || {};
    var readinessLevel = readiness.level || 'caution';
    var suspicious = []
      .concat(bundle.issues || [])
      .concat(((evidence.runtime_integrity || {}).issues || []))
      .concat(((evidence.acoustic_quality || {}).issues || []))
      .concat(repeatability.issues || [])
      .concat(position.feature_flags || []);
    var trusted = [];
    if (bundle.has_result) trusted.push({message: 'Analysis result is present.'});
    if (bundle.has_artifact_manifest) trusted.push({message: 'Artifact manifest is present.'});
    if (acoustic.snr_level && acoustic.snr_level !== 'unavailable') {
      trusted.push({message: 'SNR evidence is ' + acoustic.snr_level + '.'});
    }
    if (runtime.level === 'ok') trusted.push({message: 'Runtime integrity is OK.'});
    if (repeatability.available) {
      trusted.push({message: 'Same-seat repeatability is ' + repeatability.level + '.'});
    }
    var gates = confidence.strategy_gates || {};
    var refused = ['safe', 'balanced', 'assertive'].filter(function (name) {
      return gates[name] && gates[name].allowed === false;
    }).map(function (name) {
      var reason = (gates[name].reasons || [])[0] || 'strategy gate blocked';
      return {message: name + ' correction blocked: ' + reason};
    });
    sessionReport.className = 'session-report ' + readinessLevel;
    sessionReport.innerHTML =
      '<h3>Measurement report · ' + escapeText(evidence.session_id || payload.session_id || 'unknown') + '</h3>' +
      '<p class="hint"><strong>Recommended next action:</strong> ' +
      escapeText(readiness.recommended_action || 'review evidence before applying stronger correction') + '</p>' +
      '<div class="metric-grid">' +
        '<div class="metric"><span class="label">Readiness</span><span class="value">' +
        escapeText(readinessLevel) + '</span></div>' +
        '<div class="metric"><span class="label">Confidence</span><span class="value">' +
        escapeText(confidence.level || '—') + ' · ' + Number(confidence.score || 0).toFixed(0) + '/100</span></div>' +
        '<div class="metric"><span class="label">SNR</span><span class="value">' +
        escapeText(acoustic.snr_level || '—') + ' · ' + formatMaybeDb(acoustic.min_estimated_snr_db) + '</span></div>' +
        '<div class="metric"><span class="label">Runtime</span><span class="value">' +
        escapeText(runtime.level || 'unknown') + '</span></div>' +
        '<div class="metric"><span class="label">Positions</span><span class="value">' +
        Number(position.position_count || measurement.positions_completed || 0) + '</span></div>' +
        '<div class="metric"><span class="label">Repeatability</span><span class="value">' +
        escapeText(repeatability.level || 'unavailable') + '</span></div>' +
      '</div>' +
      '<h4>What happened</h4>' +
      '<p class="hint">State ' + escapeText(bundle.state || 'unknown') +
      ' · target ' + escapeText(measurement.target_choice || 'unknown') +
      ' · strategy ' + escapeText(measurement.strategy_choice || 'unknown') +
      ' · bundle schema v' + escapeText(bundle.schema_version || 'unknown') + '.</p>' +
      '<h4>What looks trustworthy</h4>' +
      reportIssueList(trusted, 'No positive evidence was available yet.') +
      '<h4>What looks suspicious or missing</h4>' +
      reportIssueList(suspicious.concat((readiness.reasons || []).map(function (reason) {
        return {message: reason};
      })), 'No warnings were recorded in the read-only evidence packet.') +
      '<h4>What JTS refused to correct</h4>' +
      reportIssueList(refused, 'No strategy gate refusal was recorded.') +
      '<h4>Artifact versions</h4>' +
      '<p class="hint">bundle v' + escapeText(versions.bundle_schema_version || bundle.schema_version || 'unknown') +
      ' · manifest v' + escapeText(versions.artifact_manifest_schema_version || 'missing') +
      ' · result v' + escapeText(versions.result_json_schema_version || 'missing') +
      ' · runtime v' + escapeText(versions.runtime_integrity_schema_version || 'missing') +
      ' · acoustic v' + escapeText(versions.acoustic_quality_schema_version || 'missing') +
      ' · evidence packet v' + escapeText(versions.evidence_packet_schema_version || evidence.artifact_schema_version || 'unknown') +
      '.</p>';
  }

  function chartPayload(payload) {
    payload = payload || lastResult || {};
    return {
      confidence: payload.confidence_report ||
        (payload.design_report && payload.design_report.confidence_report) ||
        null,
      design: payload.design_report || null,
      position: payload.position_analysis ||
        (payload.design_report && payload.design_report.position_report) ||
        null,
      runtime: payload.runtime_integrity ||
        (payload.confidence_report && payload.confidence_report.runtime_integrity) ||
        null,
      peqs: payload.peqs || []
    };
  }

  function recommendedNextAction(payload) {
    var p = chartPayload(payload);
    var confidence = p.confidence || {};
    var level = confidence.level || 'low';
    var failed = (confidence.findings || []).some(function (finding) {
      return finding.severity === 'fail';
    });
    if (p.runtime && p.runtime.level === 'fail') {
      return 'Remeasure — runtime evidence says this sweep may be corrupted.';
    }
    if (p.runtime && p.runtime.level === 'warn' && level !== 'high') {
      return 'Review runtime warnings, then remeasure if the curve looks surprising.';
    }
    if (failed || level === 'low') {
      return 'Remeasure before trusting aggressive correction.';
    }
    if (!p.peqs.length) {
      return 'No correction is needed from this measurement.';
    }
    if (level === 'medium') {
      return 'Apply only a conservative strategy, then verify.';
    }
    return 'Apply the proposed correction, then verify from the main seat.';
  }

  function renderResultsSummary(payload) {
    if (!payload || !payload.measured) {
      resultsSummary.className = 'results-summary hidden';
      resultsSummary.innerHTML = '';
      return;
    }
    var p = chartPayload(payload);
    var confidence = p.confidence || {};
    var design = p.design || {};
    var improvement = design.improvement || {};
    var strategy = design.correction_strategy || {};
    var position = p.position || {};
    var bands = (position.bands || []).filter(function (band) {
      return band.available;
    }).slice(0, 5);
    var flags = position.feature_flags || [];
    var flagText = flags.length
      ? flags.slice(0, 3).map(function (flag) {
          return escapeText(flag.reason || flag.kind);
        }).join('<br>')
      : 'No rejected high-risk features were flagged.';
    var bandRows = bands.map(function (band) {
      var confidenceLevel = band.confidence_level || 'low';
      var residual = band.residual || {};
      return '<tr><td data-label="Band">' +
        escapeText(band.label || band.band_id) + '</td>' +
        '<td data-label="Range">' + formatHz((band.band_hz || [])[0]) + '-' +
        formatHz((band.band_hz || [])[1]) + '</td>' +
        '<td data-label="Confidence"><span class="band-pill ' + escapeText(confidenceLevel) + '">' +
        escapeText(confidenceLevel) + '</span></td>' +
        '<td data-label="Spread">' + formatDb(band.p90_std_db) + '</td>' +
        '<td data-label="RMS error">' + formatDb(residual.rms_db) + '</td></tr>';
    }).join('');

    resultsSummary.className = 'results-summary';
    resultsSummary.innerHTML =
      '<h3>Correction readout</h3>' +
      '<p class="hint"><strong>Recommended next action:</strong> ' +
      escapeText(recommendedNextAction(payload)) + '</p>' +
      '<div class="metric-grid">' +
        '<div class="metric"><span class="label">Confidence</span>' +
        '<span class="value">' + escapeText(confidence.level || '—') +
        ' · ' + Number(confidence.score || 0).toFixed(0) + '/100</span></div>' +
        '<div class="metric"><span class="label">Positions</span>' +
        '<span class="value">' + Number(position.position_count || 0) +
        '</span></div>' +
        '<div class="metric"><span class="label">Strategy</span>' +
        '<span class="value">' + escapeText(strategy.label || strategy.strategy_id || '—') +
        '</span></div>' +
        '<div class="metric"><span class="label">Filters</span>' +
        '<span class="value">' + Number(p.peqs.length || 0) + '</span></div>' +
        '<div class="metric"><span class="label">Runtime</span>' +
        '<span class="value">' + escapeText((p.runtime && p.runtime.level) || '—') +
        '</span></div>' +
        '<div class="metric"><span class="label">Predicted RMS change</span>' +
        '<span class="value">' + formatDb(improvement.rms_db) + '</span></div>' +
      '</div>' +
      (bandRows
        ? '<table class="band-table"><thead><tr><th>Band</th><th>Range</th>' +
          '<th>Confidence</th><th>Spread</th><th>RMS error</th></tr></thead>' +
          '<tbody>' + bandRows + '</tbody></table>'
        : '<p class="hint">Band confidence is unavailable for this run.</p>') +
      '<p class="hint"><strong>Rejected / caution areas:</strong><br>' +
      flagText + '</p>';
  }

  function smoothingWidthOctaves() {
    var mode = chartSmoothing ? chartSmoothing.value : 'none';
    if (mode === '1/3') return 1 / 3;
    if (mode === '1/6') return 1 / 6;
    if (mode === '1/12') return 1 / 12;
    return 0;
  }

  function smoothValues(freqs, values) {
    var width = smoothingWidthOctaves();
    if (!width || !freqs || !values || freqs.length !== values.length) {
      return values ? values.slice() : [];
    }
    var half = width / 2;
    return values.map(function (_value, i) {
      var f0 = Number(freqs[i]);
      if (!Number.isFinite(f0) || f0 <= 0) return Number(values[i] || 0);
      var sum = 0;
      var count = 0;
      for (var j = 0; j < values.length; j++) {
        var f = Number(freqs[j]);
        var v = Number(values[j]);
        if (!Number.isFinite(f) || f <= 0 || !Number.isFinite(v)) continue;
        if (Math.abs(Math.log2(f / f0)) <= half) {
          sum += v;
          count += 1;
        }
      }
      return count ? sum / count : Number(values[i] || 0);
    });
  }

  function smoothCurve(curve) {
    if (!curve || !curve.freqs_hz || !curve.magnitude_db) return curve;
    return {
      freqs_hz: curve.freqs_hz,
      magnitude_db: smoothValues(curve.freqs_hz, curve.magnitude_db)
    };
  }

  function filterEffectCurve(measured, predicted) {
    if (
      !measured || !predicted ||
      !measured.freqs_hz || !measured.magnitude_db ||
      !predicted.magnitude_db ||
      measured.magnitude_db.length !== predicted.magnitude_db.length
    ) {
      return null;
    }
    return {
      freqs_hz: measured.freqs_hz,
      magnitude_db: measured.magnitude_db.map(function (value, idx) {
        return Number(predicted.magnitude_db[idx] || 0) - Number(value || 0);
      })
    };
  }

  function drawChart(measured, target, predicted, payload) {
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
    var displayMeasured = smoothCurve(measured);
    var displayTarget = smoothCurve(target);
    var displayPredicted = smoothCurve(predicted);
    var p = chartPayload(payload);
    var band = p.design && p.design.band_hz;

    if (chartShowBand && chartShowBand.checked && band && band.length === 2) {
      c.fillStyle = 'rgba(29, 185, 84, 0.08)';
      var x0 = fx(Math.max(fMin, Number(band[0])));
      var x1 = fx(Math.min(fMax, Number(band[1])));
      c.fillRect(x0, mt, Math.max(0, x1 - x0), H);
    }

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

    function drawSpread(chart) {
      if (
        !chart || !chart.freqs_hz || !chart.min_db || !chart.max_db ||
        chart.freqs_hz.length !== chart.min_db.length ||
        chart.freqs_hz.length !== chart.max_db.length
      ) return;
      var minDb = smoothValues(chart.freqs_hz, chart.min_db);
      var maxDb = smoothValues(chart.freqs_hz, chart.max_db);
      c.fillStyle = 'rgba(212, 68, 68, 0.14)';
      c.beginPath();
      var first = true;
      for (var i = 0; i < chart.freqs_hz.length; i++) {
        var x = fx(chart.freqs_hz[i]);
        var y = fy(maxDb[i]);
        if (first) { c.moveTo(x, y); first = false; }
        else c.lineTo(x, y);
      }
      for (var j = chart.freqs_hz.length - 1; j >= 0; j--) {
        c.lineTo(fx(chart.freqs_hz[j]), fy(minDb[j]));
      }
      c.closePath();
      c.fill();
    }

    function drawCurve(curve, color, dashed, width) {
      if (!curve || !curve.freqs_hz) return;
      c.strokeStyle = color;
      c.lineWidth = width || 2;
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

    if (
      chartShowSpread && chartShowSpread.checked &&
      p.position && p.position.chart
    ) {
      drawSpread(p.position.chart);
    }

    drawCurve(displayTarget, '#888', true, 2);
    drawCurve(displayMeasured, '#d44', false, 2);
    drawCurve(displayPredicted, '#1db954', false, 2);
    if (chartShowFilter && chartShowFilter.checked) {
      drawCurve(
        filterEffectCurve(displayMeasured, displayPredicted),
        '#2b7bb9',
        true,
        1.6,
      );
    }
    // Phase 2: post-correction verify pass overlay (purple dashed).
    if (lastVerify) {
      drawCurve(smoothCurve(lastVerify), '#a050d0', true, 2);
    }

    (p.peqs || []).forEach(function (peq, idx) {
      var freq = Number(peq.freq_hz);
      if (!Number.isFinite(freq) || freq < fMin || freq > fMax) return;
      var x = fx(freq);
      c.strokeStyle = 'rgba(43, 123, 185, 0.45)';
      c.lineWidth = 1;
      c.beginPath(); c.moveTo(x, mt); c.lineTo(x, mt + H); c.stroke();
      c.fillStyle = '#2b7bb9';
      c.fillText(String(idx + 1), x - 3, mt + 11);
    });

    var flags = (p.position && p.position.feature_flags) || [];
    flags.slice(0, 6).forEach(function (flag) {
      var freq = Number(flag.freq_hz || flag.worst_freq_hz);
      if (!Number.isFinite(freq) || freq < fMin || freq > fMax) return;
      var x = fx(freq);
      c.strokeStyle = 'rgba(214, 130, 0, 0.55)';
      c.setLineDash([2, 3]);
      c.beginPath(); c.moveTo(x, mt); c.lineTo(x, mt + H); c.stroke();
      c.setLineDash([]);
    });
  }

  function redrawLatestChart() {
    if (lastResult && lastResult.measured) {
      drawChart(
        lastResult.measured,
        lastResult.target,
        lastResult.predicted,
        lastResult,
      );
    }
  }

  // -- Network --

  __CSRF_FETCH_HELPERS__

  async function postJson(path, body) {
    var resp = await fetch(path, {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify(body || {})
    });
    if (!resp.ok) {
      var msg = await resp.text();
      try {
        var payload = JSON.parse(msg);
        if (payload && payload.error) msg = payload.error;
      } catch (_e) {}
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

  function capturePreSweepNoise() {
    if (!workletNode) return;
    captureMode = 'noise';
    setStateBadge('needs_noise_capture', 'recording room noise…');
    workletNode.port.postMessage('startCapture');
    setTimeout(function () {
      if (captureMode === 'noise' && workletNode) {
        workletNode.port.postMessage('stopCapture');
      }
    }, 700);
  }

  async function startMeasurement() {
    runBtn.disabled = true;
    continueBtn.classList.add('hidden');
    applyBtn.classList.add('hidden');
    verifyBtn.classList.add('hidden');
    resetBtn.classList.add('hidden');
    resultSection.classList.add('hidden');
    positionPrompt.classList.add('hidden');
    verifySummary.classList.add('hidden');
    resultsSummary.className = 'results-summary hidden';
    resultsSummary.innerHTML = '';
    designReport.classList.add('hidden');
    designReport.innerHTML = '';
    confidencePanel.className = 'confidence-card hidden';
    confidencePanel.innerHTML = '';
    runtimeIntegrityPanel.className = 'runtime-card hidden';
    runtimeIntegrityPanel.innerHTML = '';
    qualityBanner.className = 'quality-banner hidden';
    qualityBanner.innerHTML = '';
    lastVerify = null;
    inVerifyMode = false;
    setStateBadge('preparing', 'pausing music…');
    try {
      var totalPositions = parseInt(positionsSelect.value, 10) || 1;
      var targetChoice = targetSelect.value || 'flat';
      var strategyChoice = strategySelect.value || 'balanced';
      var resp = await postJson('start', {
        total_positions: totalPositions,
        target_choice: targetChoice,
        strategy_choice: strategyChoice,
        noise_floor_db: lastNoiseFloorDb,
        calibration_id: selectedCalibrationId,
        input_device: selectedInputDevice,
        repeat_main_position: !!(repeatMainPosition && repeatMainPosition.checked)
      });
      sessionId = resp.session_id;
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
      return;
    }
    capturePreSweepNoise();
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
    capturePreSweepNoise();
    pollState();
  }

  async function repeatMainSeat() {
    repeatBtn.classList.add('hidden');
    repeatBtn.disabled = true;
    setStateBadge('preparing', 'preparing repeat sweep…');
    captureMode = 'repeat';
    if (workletNode) workletNode.port.postMessage('startCapture');
    try {
      await postJson('repeat-position', {});
    } catch (e) {
      captureMode = 'discard';
      if (workletNode) workletNode.port.postMessage('stopCapture');
      setStateBadge('failed', e.message);
      repeatBtn.disabled = false;
      return;
    }
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
    lastNoiseFloorDb = noiseFloorDb;
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
        headers: jsonHeaders(),
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
    captureMode = 'verify';
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
    repeatBtn.classList.add('hidden');
    repeatBtn.disabled = false;
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
    } else if (state === 'needs_repeat_capture') {
      positionPrompt.classList.remove('hidden');
      positionCurrent.textContent = '1';
      positionTotal.textContent = '1';
      repeatBtn.classList.remove('hidden');
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
      renderQuality(s);
      renderBrowserAudioReport(s.browser_audio_report);
      renderConfidence(s);
      renderRuntimeIntegrity(s);
      applyButtonPolicy(s.state, s.autolevel ? s.autolevel.status : 'idle');

      if (s.state === 'needs_next_position') {
        positionCurrent.textContent = (s.current_position + 1);
        positionTotal.textContent = s.total_positions;
        return;
      }
      if (s.state === 'needs_repeat_capture') {
        return;
      }
      if (
        s.state === 'awaiting_capture' ||
        s.state === 'awaiting_verify_capture' ||
        s.state === 'awaiting_repeat_capture'
      ) {
        if (workletNode) workletNode.port.postMessage('stopCapture');
        return;  // upload-capture handler resumes polling
      }
      if (s.state === 'verified' && s.verify_metrics) {
        verifySummary.innerHTML =
          '<strong>Post-correction (50–350 Hz):</strong> RMS deviation ' +
          s.verify_metrics.rms_db.toFixed(1) + ' dB, max ' +
          s.verify_metrics.max_db.toFixed(1) + ' dB.<br>' +
          '<span class="hint">' +
          'Verify is a <em>single-position</em> measurement vs the ' +
          'multi-position averaged design — in a modal room (especially a ' +
          'cube), per-position swings of 10–15 dB at modal frequencies ' +
          'are normal. Some bands will look corrected, some over-corrected, ' +
          'some under-corrected. The audible test is what actually matters: ' +
          'play familiar bass-heavy music and listen for the bass tightening ' +
          'and modal "boom" reducing without the music sounding thinned-out.' +
          '</span>';
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
    }
  }

  async function onCaptureReady(arrayBuffer, kind) {
    kind = kind || 'measurement';
    if (kind === 'discard') {
      return;
    }
    var float32 = new Float32Array(arrayBuffer);
    var wav = float32ToWav(float32, REQUIRED_SR);
    if (kind === 'noise') {
      setStateBadge('needs_noise_capture', 'uploading room noise…');
      captureMode = 'measurement';
      if (workletNode) workletNode.port.postMessage('startCapture');
      try {
        var noiseResp = await fetch('upload-noise', {
          method: 'POST',
          headers: csrfHeaders({'Content-Type': 'audio/wav'}),
          body: wav
        });
        if (!noiseResp.ok) {
          var noiseMsg = await noiseResp.text();
          throw new Error('upload-noise → ' + noiseResp.status + ': ' + noiseMsg);
        }
        await noiseResp.json();
        pollState();
      } catch (e) {
        captureMode = 'discard';
        if (workletNode) workletNode.port.postMessage('stopCapture');
        setStateBadge('failed', e.message);
        runBtn.disabled = false;
      }
      return;
    }

    setStateBadge('analyzing', 'uploading capture (' +
      Math.round(float32.length / REQUIRED_SR * 10) / 10 + ' s of audio)…');
    try {
      var resp = await fetch('upload-capture', {
        method: 'POST',
        headers: csrfHeaders({'Content-Type': 'audio/wav'}),
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
      renderDesignReport(data.design_report);
      renderConfidence(data);
      renderRuntimeIntegrity(data);
      renderResultsSummary(data);
      renderQuality(data);
      renderBrowserAudioReport(data.browser_audio_report);
      var hasResultPayload = !!(
        data.measured || data.verify || data.design_report ||
        (data.peqs && data.peqs.length)
      );
      if (hasResultPayload) resultSection.classList.remove('hidden');
      if (hasResultPayload && data.measured) {
        // Force a layout flush so getBoundingClientRect returns
        // real dimensions on the first draw.
        void canvas.offsetWidth;
        drawChart(data.measured, data.target, data.predicted, data);
        // Safety redraw next frame.
        requestAnimationFrame(function () {
          drawChart(data.measured, data.target, data.predicted, data);
        });
      }
      pollState();
    } catch (e) {
      setStateBadge('failed', e.message);
      runBtn.disabled = false;
      try {
        renderQuality(await fetchStatus());
      } catch (ignored) {}
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
    refreshCurrentCorrection();
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
    refreshCurrentCorrection();
  }

  startBtn.addEventListener('click', function () { startMicCapture(); });
  refreshInputsBtn.addEventListener('click', function () { populateInputDevices(); });
  inputDeviceSelect.addEventListener('change', function () {
    if (micStream) {
      startMicCapture().catch(function (e) {
        setStateBadge('failed', e.message);
      });
    }
  });
  micModelSelect.addEventListener('change', function () { updateMicCalibrationRows(); });
  micSerialInput.addEventListener('input', function () { invalidateLoadedCalibration(); });
  micOrientationSelect.addEventListener('change', function () { invalidateLoadedCalibration(); });
  calibrationSignSelect.addEventListener('change', function () { invalidateLoadedCalibration(); });
  calibrationFileInput.addEventListener('change', function () { invalidateLoadedCalibration(); });
  fetchCalibrationBtn.addEventListener('click', function () { fetchCalibration(); });
  uploadCalibrationBtn.addEventListener('click', function () { uploadCalibration(); });
  runBtn.addEventListener('click', function () { startMeasurement(); });
  repeatBtn.addEventListener('click', function () { repeatMainSeat(); });
  continueBtn.addEventListener('click', function () { continueToNextPosition(); });
  applyBtn.addEventListener('click', function () { applyCorrection(); });
  verifyBtn.addEventListener('click', function () { startVerify(); });
  resetBtn.addEventListener('click', function () { resetCorrection(); });
  autolevelBtn.addEventListener('click', function () { startAutolevel(); });
  autolevelCancelBtn.addEventListener('click', function () { cancelAutolevel(); });
  currentCorrectionResetBtn.addEventListener('click', function () { resetFromBanner(); });
  loadSessionsBtn.addEventListener('click', function () { loadSessionReports(); });
  sessionHistory.addEventListener('click', function (ev) {
    var button = ev.target && ev.target.closest
      ? ev.target.closest('button[data-session-id]')
      : null;
    if (button) loadSessionReport(button.dataset.sessionId || '');
  });

  // Populate the banner on page load (and after apply / reset so the
  // user sees the new state without a refresh).
  populateInputDevices();
  updateMicCalibrationRows();
  refreshCurrentCorrection();

  // Redraw chart on resize / orientation change — without this, the
  // canvas's drawing surface stays at the dimensions it had on the
  // first draw and gets CSS-stretched (blurry) on rotation.
  var resizeTimer = null;
  function scheduleChartRedraw() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      redrawLatestChart();
    }, 150);
  }
  window.addEventListener('resize', scheduleChartRedraw);
  window.addEventListener('orientationchange', scheduleChartRedraw);
  chartSmoothing.addEventListener('change', redrawLatestChart);
  chartShowSpread.addEventListener('change', redrawLatestChart);
  chartShowFilter.addEventListener('change', redrawLatestChart);
  chartShowBand.addEventListener('change', redrawLatestChart);
})();
</script>
</body>
</html>
"""


def _render_page(hostname: str, csrf_token: str = "") -> bytes:
    from jasper.correction.calibration import SUPPORTED_MODELS
    from jasper.correction.strategy import (
        DEFAULT_CORRECTION_STRATEGY_ID,
        DEFAULT_TARGET_PROFILE_ID,
        correction_strategy_options,
        target_profile_options,
    )

    mic_model_options = "\n        ".join(
        '<option value="{key}">{label}</option>'.format(
            key=html.escape(key, quote=True),
            label=html.escape(spec["label"]),
        )
        for key, spec in SUPPORTED_MODELS.items()
    )
    target_profile_options_html = "\n      ".join(
        '<option value="{key}"{selected}>{label} — {description}</option>'.format(
            key=html.escape(str(spec["target_id"]), quote=True),
            selected=(
                " selected"
                if spec["target_id"] == DEFAULT_TARGET_PROFILE_ID
                else ""
            ),
            label=html.escape(str(spec["label"])),
            description=html.escape(str(spec["description"])),
        )
        for spec in target_profile_options()
    )
    correction_strategy_options_html = "\n      ".join(
        '<option value="{key}"{selected}>{label} — {description}</option>'.format(
            key=html.escape(str(spec["strategy_id"]), quote=True),
            selected=(
                " selected"
                if spec["strategy_id"] == DEFAULT_CORRECTION_STRATEGY_ID
                else ""
            ),
            label=html.escape(str(spec["label"])),
            description=html.escape(str(spec["description"])),
        )
        for spec in correction_strategy_options()
    )
    home_html = (
        '<a class="nav-back" href="http://{host}/">← Home</a>'
        .format(host=html.escape(hostname, quote=True))
    )
    return (
        _PAGE_HTML
        .replace("__STYLE__", _CORRECTION_PAGE_STYLE + NAV_BACK_CSS)
        .replace(
            "__CSRF_META__",
            csrf_meta_html(csrf_token) if csrf_token else "",
        )
        .replace("__CSRF_FETCH_HELPERS__", csrf_fetch_helpers_js())
        .replace("__NAV_BACK__", home_html)
        .replace("__HOSTNAME__", hostname)
        .replace("__REQUIRED_SR__", str(REQUIRED_SAMPLE_RATE))
        .replace("__MIC_MODEL_OPTIONS__", mic_model_options)
        .replace("__TARGET_PROFILE_OPTIONS__", target_profile_options_html)
        .replace("__CORRECTION_STRATEGY_OPTIONS__", correction_strategy_options_html)
    ).encode("utf-8")


# ----------------------------------------------------------------------
# HTTP route handlers — sync wrappers around async session methods.
# ----------------------------------------------------------------------


def _read_json_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int = MAX_JSON_BODY_BYTES,
) -> dict[str, Any]:
    """Parse JSON body. Empty body → {}."""
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError as e:
        raise BadRequest("invalid Content-Length") from e
    if length <= 0:
        return {}
    if length > max_bytes:
        raise BadRequest(f"JSON body too large ({length} bytes)")
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise BadRequest("JSON body must be UTF-8") from e
    except json.JSONDecodeError as e:
        raise BadRequest(f"invalid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise BadRequest("JSON body must be an object")
    return data


def _camilla() -> "Any":
    """Construct a CamillaController against the configured host/port.
    Factored so tests can monkeypatch a single seam — and so the
    /start reset path doesn't drift from the /apply + /reset paths.
    """
    from jasper.camilla import CamillaController
    return CamillaController(
        host=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
        port=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )


def _calibration_root() -> Path:
    return Path(
        os.environ.get(
            "JASPER_CORRECTION_CALIBRATION_DIR",
            "/var/lib/jasper/correction/calibration_mics",
        )
    )


def _short_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:MAX_DEVICE_FIELD_CHARS]


def _device_id_hash(value: Any) -> str | None:
    text = _short_text(value)
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _optional_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _runtime_integrity_summary(sess: Any) -> dict[str, Any] | None:
    report = getattr(sess, "runtime_integrity", None)
    if report is None or not hasattr(report, "summary"):
        return None
    try:
        return report.summary()
    except Exception:  # noqa: BLE001
        logger.debug("runtime_integrity summary unavailable", exc_info=True)
        return None


def _schedule_measurement_sweep(sess: Any, cam: Any, *, from_state: Any) -> None:
    """Start the next normal measurement sweep and wait for visible progress."""
    from jasper.correction import coordinator, playback

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with coordinator.measurement_window():
                await sess.prepare_and_play_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("measurement sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(_run_sweep(), _ensure_loop())
    _run_async(sess.state_changed_from(from_state), timeout=6.0)


def _schedule_repeat_sweep(sess: Any, cam: Any, *, from_state: Any) -> None:
    """Start the optional main-seat repeat sweep."""
    from jasper.correction import coordinator, playback

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with coordinator.measurement_window():
                await sess.prepare_and_play_repeat_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("repeat sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(_run_sweep(), _ensure_loop())
    _run_async(sess.state_changed_from(from_state), timeout=6.0)


def _sanitize_input_device(raw: Any) -> dict[str, Any] | None:
    """Normalize browser-reported input-device metadata before bundles.

    Browser `deviceId` values can be stable identifiers, so persist
    hashes rather than raw IDs. Labels are user-visible in the browser
    picker and useful for debugging, but still capped.
    """
    if not isinstance(raw, dict):
        return None
    sanitized = {
        "device_id_hash": _device_id_hash(raw.get("device_id")),
        "requested_device_id_hash": _device_id_hash(
            raw.get("requested_device_id"),
        ),
        "actual_device_id_hash": _device_id_hash(raw.get("actual_device_id")),
        "label": _short_text(raw.get("label")),
        "browser_label": _short_text(raw.get("browser_label")),
        "sample_rate": _optional_float(raw.get("sample_rate")),
        "channel_count": _optional_float(raw.get("channel_count")),
        "echo_cancellation": _optional_bool(raw.get("echo_cancellation")),
        "noise_suppression": _optional_bool(raw.get("noise_suppression")),
        "auto_gain_control": _optional_bool(raw.get("auto_gain_control")),
    }
    return {k: v for k, v in sanitized.items() if v is not None} or None


def _handle_start(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /start: snapshot any current correction, hard-reset
    CamillaDSP to the base config, replace the session, and ask the
    browser for pre-sweep room-noise capture. The sweep starts only
    after `POST /upload-noise` lands.

    Body fields:
      - total_positions: int = 1 (Phase 1 default; UI sends 5 for MMM)
      - target_choice:   str = 'flat' | 'neutral' | 'warm' | 'bright'
      - strategy_choice: str = 'safe' | 'balanced' | 'assertive'
      - noise_floor_db:  float | None — optional, client autolevel
        preflight measurement; only saved into the debug bundle.
      - repeat_main_position: bool = true — optional same-seat repeat
        for repeatability evidence.

    Why reset before sweeping: if a correction is already loaded, the
    sweep traverses the corrected pipeline and the resulting curve
    reflects the corrected room, not the raw room — designing new
    filters from that would compound the corrections. Resetting first
    guarantees every measurement starts from the same flat baseline.
    """
    from jasper.correction.session import SessionState, describe_current_config

    body = _read_json_body(handler)
    blocking_state = _reserve_start_slot()
    if blocking_state is not None:
        logger.warning(
            "event=correction_start_rejected reason=active_session state=%s",
            blocking_state,
        )
        raise RequestConflict(
            "measurement already in progress; wait for the current sweep "
            "or reset before starting again"
        )

    try:
        total_positions = max(1, min(10, int(body.get("total_positions", 1))))
        target_choice = str(body.get("target_choice", "flat"))
        strategy_choice = str(body.get("strategy_choice", "balanced"))
        noise_floor_db_raw = body.get("noise_floor_db")
        calibration_id = str(body.get("calibration_id") or "").strip()
        input_device = _sanitize_input_device(body.get("input_device"))
        repeat_main_position = bool(body.get("repeat_main_position", True))
        noise_floor_db: float | None
        try:
            noise_floor_db = (
                float(noise_floor_db_raw)
                if noise_floor_db_raw is not None
                else None
            )
        except (TypeError, ValueError):
            noise_floor_db = None

        mic_calibration = None
        if calibration_id:
            from jasper.correction.calibration import load_calibration_record
            mic_calibration = load_calibration_record(
                calibration_id,
                root=_calibration_root(),
            )

        sess = _replace_session(
            total_positions=total_positions,
            target_choice=target_choice,
            strategy_choice=strategy_choice,
            mic_calibration=mic_calibration,
            input_device=input_device,
            repeat_main_position=repeat_main_position,
        )
        sess.noise_floor_db = noise_floor_db

        cam = _camilla()

        # Snapshot what was loaded BEFORE we reset, so the bundle records
        # the prior state. Best-effort: a snapshot failure should not stop
        # measurement, but the reset below is load-bearing and must succeed.
        async def _snapshot() -> dict[str, Any] | None:
            path = await cam.get_config_file_path(best_effort=True)
            return describe_current_config(
                path,
                config_dir=sess.cfg.config_dir,
                base_config_path=sess.cfg.base_config_path,
            )

        try:
            sess.current_correction_at_start = _run_async(_snapshot(), timeout=3.0)
        except Exception:  # noqa: BLE001
            logger.exception("/start: snapshot current_correction failed")
            sess.current_correction_at_start = None

        async def _reset_to_base() -> bool:
            return await cam.set_config_file_path(
                str(sess.cfg.base_config_path), best_effort=False,
            )

        try:
            reset_ok = _run_async(_reset_to_base(), timeout=5.0)
        except Exception:  # noqa: BLE001
            logger.exception("/start: reset to base config failed")
            raise RuntimeError(
                "could not reset speaker to flat before measuring"
            ) from None
        if not reset_ok:
            raise RuntimeError("could not reset speaker to flat before measuring")

        reservation_transferred = False
        try:
            _run_async(sess.begin_noise_capture(), timeout=3.0)
            state_started = sess.state == SessionState.NEEDS_NOISE_CAPTURE
        except concurrent.futures.TimeoutError:
            state_started = False

        if state_started:
            _clear_start_slot()
        else:
            _clear_start_slot()
            logger.warning(
                "event=correction_start_state_wait_timeout session=%s",
                sess.session_id,
            )

        snapshot = sess.snapshot()
        return {
            "session_id": sess.session_id,
            "state": sess.state.value,
            "total_positions": sess.total_positions,
            "target_choice": sess.target_choice,
            "strategy_choice": sess.strategy_choice,
            "target_profile": snapshot.get("target_profile"),
            "correction_strategy": snapshot.get("correction_strategy"),
            "input_device": sess.input_device,
            "browser_audio_report": sess.browser_audio_report,
            "mic_calibration": (
                sess.mic_calibration.public_metadata()
                if sess.mic_calibration
                else None
            ),
            "current_correction_at_start": sess.current_correction_at_start,
        }
    except Exception:
        if not locals().get("reservation_transferred", False):
            _clear_start_slot()
        raise


def _handle_next_position(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /next-position: request pre-sweep noise for the next
    multi-position measurement. Only valid in NEEDS_NEXT_POSITION
    state.

    The sweep itself starts after the browser uploads
    `noise/p<N>_pre.wav` to `/upload-noise`.
    """
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess.state != SessionState.NEEDS_NEXT_POSITION:
        raise RuntimeError(
            f"cannot advance to next position from state {sess.state.value}"
        )

    _run_async(sess.begin_noise_capture(), timeout=3.0)

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
    cam = _camilla()

    async def _run_verify_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with coordinator.measurement_window():
                await sess.start_verify_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
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
                # Tone source amplitude = -12 dBFS, matching the sweep
                # amplitude. Earlier this was -6 dBFS — 6 dB louder
                # than the actual sweep, which made the autolevel
                # phase startlingly loud AND inflated the user's
                # expectation of how loud the measurement sweep would
                # be. With -12 dBFS, the tone and sweep are the same
                # loudness so leveling-to-tone calibrates leveling-to-
                # sweep directly.
                tone_wav = playback._ensure_tone_wav(
                    freq_hz=1000.0,
                    duration_s=15.0,  # safety > max ramp duration
                    dbfs=-12.0,
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


def _calibration_payload(record) -> dict[str, Any]:
    from jasper.correction import calibration
    return {
        "calibration": record.public_metadata(),
        "preview": calibration.preview_curve(record.curve),
    }


def _handle_calibration_models(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    from jasper.correction.calibration import SUPPORTED_MODELS
    return {
        "models": [
            {"key": key, **value}
            for key, value in SUPPORTED_MODELS.items()
        ]
    }


def _handle_calibration_fetch(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.correction.calibration import fetch_vendor_calibration

    body = _read_json_body(handler)
    model = str(body.get("model") or "").strip()
    serial = str(body.get("serial") or "").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    record = fetch_vendor_calibration(
        model_key=model,
        serial=serial,
        orientation=orientation,
        root=_calibration_root(),
    )
    return _calibration_payload(record)


def _handle_calibration_upload(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.correction.calibration import store_calibration

    body = _read_json_body(
        handler,
        max_bytes=MAX_CALIBRATION_UPLOAD_JSON_BYTES,
    )
    text = str(body.get("content") or "")
    filename = str(body.get("filename") or "uploaded-calibration.txt")
    model = str(body.get("model") or "other").strip() or "other"
    label = str(body.get("label") or "Other calibrated mic").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    sign_convention = (
        str(body.get("sign_convention") or "correction").strip()
        or "correction"
    )
    record = store_calibration(
        text=text,
        provider="manual_upload",
        model=model,
        label=label,
        source=f"uploaded:{filename}",
        orientation=orientation,
        sign_convention=sign_convention,
        root=_calibration_root(),
    )
    return _calibration_payload(record)


def _handle_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /status: snapshot the current session + currently-loaded
    CamillaDSP config descriptor. `current_correction` is best-effort
    (returns None if CamillaDSP is unreachable) so the page still
    renders something useful when the daemon is restarting."""
    from jasper.correction.session import describe_current_config
    from jasper.dsp_apply import last_dsp_apply_state

    sess = _get_or_create_session()
    snap = sess.snapshot()
    cam = _camilla()
    try:
        path = _run_async(
            cam.get_config_file_path(best_effort=True), timeout=2.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("status: get_config_file_path failed")
        path = None
    current_config = describe_current_config(
        path,
        config_dir=sess.cfg.config_dir,
        base_config_path=sess.cfg.base_config_path,
    )
    snap["current_config"] = current_config
    snap["current_correction"] = current_config.get("current_correction")
    snap["last_dsp_apply"] = last_dsp_apply_state()
    return snap


def _handle_sessions(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /sessions: list recent session bundles for debugging /
    future UI history. Returns the parsed info.json for each entry,
    sorted by started_at desc; capped at 20. Bundles without a
    parseable info.json (in-progress writes, crashed mid-state) are
    skipped silently."""
    from jasper.correction.bundles import list_bundles

    sess = _get_or_create_session()
    return {"sessions": list_bundles(sess.cfg.sessions_dir, limit=20)}


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _bundle_report_versions(bundle_dir: Path) -> dict[str, Any]:
    from jasper.correction import (
        acoustic_quality,
        bundles,
        evidence,
        runtime_integrity,
    )

    info = _read_optional_json(bundle_dir / "info.json") or {}
    result = _read_optional_json(bundle_dir / "result.json") or {}
    runtime = _read_optional_json(bundle_dir / "runtime_integrity.json") or {}
    acoustic = _read_optional_json(bundle_dir / "acoustic_quality.json") or {}
    manifest = _read_optional_json(bundle_dir / bundles.ARTIFACT_MANIFEST_NAME) or {}
    return {
        "expected_bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "expected_artifact_manifest_schema_version": (
            bundles.CURRENT_ARTIFACT_MANIFEST_VERSION
        ),
        "expected_runtime_integrity_schema_version": runtime_integrity.SCHEMA_VERSION,
        "expected_acoustic_quality_schema_version": acoustic_quality.SCHEMA_VERSION,
        "expected_evidence_packet_schema_version": evidence.SCHEMA_VERSION,
        "bundle_schema_version": info.get("bundle_schema_version"),
        "artifact_manifest_schema_version": manifest.get("manifest_schema_version"),
        "artifact_manifest_bundle_schema_version": (
            manifest.get("bundle_schema_version")
        ),
        "result_json_schema_version": result.get("bundle_schema_version"),
        "runtime_integrity_schema_version": runtime.get("artifact_schema_version"),
        "acoustic_quality_schema_version": acoustic.get("artifact_schema_version"),
        "evidence_packet_schema_version": evidence.SCHEMA_VERSION,
    }


def _resolve_session_bundle_dir(sessions_dir: Path, session_id: str) -> Path:
    clean = session_id.strip()
    if not clean:
        raise BadRequest("missing session id")
    if len(clean) > 128:
        raise BadRequest("session id is too long")
    root = sessions_dir.resolve()
    candidate = sessions_dir / clean
    try:
        bundle_dir = candidate.resolve(strict=False)
        bundle_dir.relative_to(root)
    except (OSError, ValueError) as e:
        raise BadRequest("invalid session id") from e
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"session bundle not found: {clean}")
    return bundle_dir


def _handle_session_report(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /session-report?id=<session_id>: return a read-only,
    browser-safe measurement report built from one session bundle.

    This intentionally returns metadata and derived evidence only. Raw
    recordings stay in the private bundle for operator/CLI workflows.
    """
    from jasper.correction import bundles, evidence

    sess = _get_or_create_session()
    query = parse_qs(urlparse(handler.path).query)
    session_id = (query.get("id") or [""])[0]
    bundle_dir = _resolve_session_bundle_dir(sess.cfg.sessions_dir, session_id)
    packet = evidence.build_evidence_packet(bundle_dir)
    logger.info(
        "event=correction_session_report session=%s",
        packet.get("session_id") or bundle_dir.name,
    )
    return {
        "session_id": packet.get("session_id") or bundle_dir.name,
        "summary": bundles.summarize_bundle(bundle_dir),
        "artifact_versions": _bundle_report_versions(bundle_dir),
        "evidence": packet,
    }


def _read_wav_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int = MAX_WAV_BODY_BYTES,
) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError as e:
        raise BadRequest("invalid Content-Length") from e
    if length <= 0:
        raise BadRequest("empty body")
    if length > max_bytes:
        raise BadRequest(f"WAV body too large ({length} bytes)")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise BadRequest("incomplete WAV body")
    return raw


def _handle_upload_noise(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /upload-noise: persist pre-sweep silence, then play sweep."""
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")
    if sess.state != SessionState.NEEDS_NOISE_CAPTURE:
        raise RuntimeError(
            f"cannot accept noise capture from state {sess.state.value}"
        )

    body = _read_wav_body(handler)
    captured_path = sess.noise_capture_path_for_position(sess.current_position)
    captured_path.parent.mkdir(parents=True, exist_ok=True)
    captured_path.write_bytes(body)
    _run_async(sess.on_noise_capture_uploaded(captured_path), timeout=10.0)
    _schedule_measurement_sweep(
        sess,
        _camilla(),
        from_state=SessionState.NEEDS_NOISE_CAPTURE,
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
        "noise_reports": sess.noise_reports,
        "acoustic_quality": (
            (sess.acoustic_quality or {}).get("summary")
            if sess.acoustic_quality
            else None
        ),
    }


def _handle_repeat_position(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /repeat-position: play the optional same-seat repeat."""
    from jasper.correction.session import SessionState

    sess = _get_or_create_session()
    if sess.state != SessionState.NEEDS_REPEAT_CAPTURE:
        raise RuntimeError(
            f"cannot repeat main seat from state {sess.state.value}"
        )
    _schedule_repeat_sweep(
        sess,
        _camilla(),
        from_state=SessionState.NEEDS_REPEAT_CAPTURE,
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
    }


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

    body = _read_wav_body(handler)

    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        captured_path = sess.verify_capture_path()
    elif sess.state == SessionState.AWAITING_REPEAT_CAPTURE:
        captured_path = sess.repeat_capture_path_for_position(0)
    else:
        captured_path = sess.capture_path_for_position(sess.current_position)
    captured_path.parent.mkdir(parents=True, exist_ok=True)
    captured_path.write_bytes(body)

    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        _run_async(
            sess.on_verify_capture_uploaded(captured_path), timeout=30.0,
        )
    elif sess.state == SessionState.AWAITING_REPEAT_CAPTURE:
        _run_async(
            sess.on_repeat_capture_uploaded(captured_path), timeout=30.0,
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
        "capture_quality": sess.capture_quality,
        "noise_reports": sess.noise_reports,
        "repeat": (
            sess.repeat_curve.__dict__ if sess.repeat_curve else None
        ),
        "repeat_quality": sess.repeat_quality,
        "repeatability_report": sess.repeatability_report,
        "verify_quality": sess.verify_quality,
        "browser_audio_report": sess.browser_audio_report,
        "confidence_report": sess.confidence_report,
        "runtime_integrity": _runtime_integrity_summary(sess),
        "position_analysis": sess.position_analysis,
        "peqs": [p.__dict__ for p in sess.peqs],
        "design_report": sess.design_report,
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
    sess = _get_or_create_session()
    cam = _camilla()

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    async def _get() -> str | None:
        return await cam.get_config_file_path(best_effort=True)

    _run_async(sess.apply(_set, camilla_get_config=_get), timeout=15.0)
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
    sess = _get_or_create_session()
    cam = _camilla()

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
            send_html_response(self, body, status=status)

        def _send_text(self, text: str, *, status: int = 200) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_client_error(
            self, message: str, *, status: int = 400,
        ) -> None:
            self._send_json({"error": message}, status=status)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                self._send_html(_render_page(
                    cfg["hostname"], ctx["csrf_token"],
                ))
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
            if path == "/sessions":
                try:
                    self._send_json(_handle_sessions(self))
                except Exception as e:  # noqa: BLE001
                    logger.exception("/sessions failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/session-report":
                try:
                    self._send_json(_handle_session_report(self))
                except BadRequest as e:
                    self._send_client_error(str(e))
                except FileNotFoundError as e:
                    self._send_client_error(str(e), status=404)
                except Exception as e:  # noqa: BLE001
                    from jasper.correction.bundles import BundleError
                    if isinstance(e, BundleError):
                        self._send_client_error(str(e), status=422)
                        return
                    logger.exception("/session-report failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/calibration/models":
                try:
                    self._send_json(_handle_calibration_models(self))
                except Exception as e:  # noqa: BLE001
                    logger.exception("/calibration/models failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/start",
                "/next-position",
                "/repeat-position",
                "/verify",
                "/test-tone",
                "/autolevel/start",
                "/autolevel/lock",
                "/autolevel/cancel",
                "/upload-noise",
                "/upload-capture",
                "/calibration/fetch",
                "/calibration/upload",
                "/apply",
                "/reset",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not verify_csrf(self):
                reject_csrf(self)
                return
            try:
                if path == "/start":
                    try:
                        self._send_json(_handle_start(self))
                    except (FileNotFoundError, ValueError) as e:
                        self._send_client_error(str(e))
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/next-position":
                    self._send_json(_handle_next_position(self))
                    return
                if path == "/repeat-position":
                    self._send_json(_handle_repeat_position(self))
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
                    from jasper.correction import quality

                    try:
                        self._send_json(_handle_upload_capture(self))
                    except quality.CaptureQualityError as e:
                        sess = _get_or_create_session()
                        self._send_json({
                            "error": str(e),
                            "session_id": sess.session_id,
                            "state": sess.state.value,
                            "current_position": sess.current_position,
                            "total_positions": sess.total_positions,
                            "capture_quality": sess.capture_quality,
                            "verify_quality": sess.verify_quality,
                            "browser_audio_report": getattr(
                                sess, "browser_audio_report", None,
                            ),
                            "runtime_integrity": _runtime_integrity_summary(sess),
                        }, status=422)
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/upload-noise":
                    try:
                        self._send_json(_handle_upload_noise(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/calibration/fetch":
                    try:
                        self._send_json(_handle_calibration_fetch(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    except Exception as e:  # noqa: BLE001
                        from jasper.correction.calibration import (
                            CalibrationNotFoundError,
                            CalibrationUpstreamError,
                        )
                        if isinstance(e, CalibrationNotFoundError):
                            self._send_client_error(str(e), status=404)
                        elif isinstance(e, CalibrationUpstreamError):
                            self._send_client_error(str(e), status=502)
                        else:
                            raise
                    return
                if path == "/calibration/upload":
                    try:
                        self._send_json(_handle_calibration_upload(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/apply":
                    self._send_json(_handle_apply(self))
                    return
                if path == "/reset":
                    self._send_json(_handle_reset(self))
                    return
            except BadRequest as e:
                self._send_client_error(str(e))
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                self._send_json({"error": str(e)}, status=500)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def make_server(
    target, *, hostname: str = "jts.local",
) -> ThreadingHTTPServer:
    """Build the wizard server. `target` is socket/tuple/int per
    _systemd.make_http_server's contract."""
    from . import _systemd
    cfg = {"hostname": hostname}
    return _systemd.make_http_server(target, _make_handler(cfg))


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

    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target, hostname=args.hostname)

    handler_cls = server.RequestHandlerClass
    tracker = _systemd.IdleShutdownTracker()
    _systemd.install_request_idle_bump(handler_cls, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-correction-web adopting systemd fd (hostname=%s)",
            args.hostname,
        )
    else:
        logger.info(
            "jasper-correction-web listening on http://%s:%d (hostname=%s)",
            args.host, args.port, args.hostname,
        )

    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
