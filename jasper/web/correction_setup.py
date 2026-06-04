"""Room correction wizard at /correction/.

The user opens the page on a phone, selects a calibrated input,
captures pre-sweep room noise plus one or more measurement positions,
reviews confidence/visualization evidence, and optionally applies a
bounded room-correction profile through the shared CamillaDSP apply
path.

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
      POST /reset           roll back to /etc/camilladsp/outputd-cutover.yml
      POST /session/delete  delete one historical measurement bundle

Why a separate service from jasper-web (Spotify + voice settings):
the correction flow eventually imports numpy/scipy through
`jasper.correction.*` while handling measurements. Keeping this
socket-activated service separate from lightweight setup pages keeps
the idle management UI cheap on a 1 GB Pi.
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
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ._common import (
    begin_request,
    canonical_header,
    canonical_page,
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
_BUNDLE_DELETE_BLOCKED_STATES = _ACTIVE_SESSION_STATES | {"ready"}


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
# Page body (canonical design system).
# ----------------------------------------------------------------------
#
# /correction/ is a restyle-in-place migration onto the canonical look:
# the document shell is canonical_page() (app.css + CSRF meta + icon
# sprite); the chrome is canonical_header() + the shared .btn / card
# vocabulary. The page's behaviour — getUserMedia mic capture, the
# AudioWorklet level meter, the measurement-sweep + autolevel + verify
# state machine driven by polling GET /status, the canvas chart, and the
# session-report reader — ships UNCHANGED as the static ES module
# /assets/correction/js/main.js. The measurement/DSP correctness can only
# be re-verified on real Pi hardware, so the JS was relocated verbatim
# (one module), not split or rewritten.
#
# getUserMedia requires a secure context; /correction/ is served over
# HTTPS (mkcert local CA). The back link is an absolute http://<host>/ so
# the Home affordance lands on the plain-HTTP dashboard rather than trying
# HTTPS on /. Page-specific styling lives in /assets/correction/correction.css.


_PAGE_BODY = """
__HEADER__
<main class="page correction-stack" data-required-sr="__REQUIRED_SR__">
<p class="page-sub">Measure your room from this iPhone, design correction filters, and apply them to the speaker.</p>

<div id="current-correction" class="flat" aria-live="polite">
  <span class="label" id="current-correction-label">Checking current correction…</span>
  <button id="current-correction-reset" type="button" class="btn btn--danger hidden">Reset to flat</button>
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
          <option value="" disabled selected>Tap “Detect microphones”…</option>
        </select>
      </label>
      <button id="refresh-inputs" type="button" class="btn btn--ghost">Detect microphones</button>
    </div>
    <p class="hint" style="margin:0">Tap <strong>Detect microphones</strong> and grant permission so your USB measurement mic appears, then select it before <strong>Start mic capture</strong>.</p>

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
      <button id="fetch-calibration" type="button" class="btn btn--ghost">Fetch calibration</button>
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
      <button id="upload-calibration" type="button" class="btn btn--ghost">Upload calibration</button>
    </div>
    <p id="calibration-status" class="mic-status">No calibration loaded. This is okay for a quick check, but a calibrated mic is recommended before trusting filter decisions.</p>
    <p id="calibration-preview" class="cal-preview hidden"></p>
  </div>
</div>

<button id="start" type="button" class="btn btn--primary">Start mic capture</button>

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

  <div class="info-card">
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

  <div id="position-prompt" class="note-box hidden">
    <p style="margin:0; font-weight:600">Move phone to position <span id="position-current">2</span> of <span id="position-total">5</span>.</p>
    <p class="hint" style="margin-top:0.3em">Move ~30 cm from the previous position — left, right, forward, or back, head-height. Same orientation: phone flat, bottom edge pointing at the speakers. Tap Continue when ready.</p>
  </div>

  <p style="display:flex; gap:0.6em; flex-wrap:wrap">
    <button id="autolevel" type="button" class="btn btn--ghost" disabled>Auto-level</button>
    <button id="autolevel-lock" type="button" class="btn btn--primary hidden">Lock now</button>
    <button id="autolevel-cancel" type="button" class="btn btn--danger hidden">Cancel</button>
    <button id="run-measurement" type="button" class="btn btn--primary" disabled>Run measurement</button>
    <button id="repeat-position" type="button" class="btn btn--primary hidden">Repeat main seat</button>
    <button id="continue-position" type="button" class="btn btn--primary hidden">Continue to next position</button>
    <button id="apply-correction" type="button" class="btn btn--primary hidden">Apply correction</button>
    <button id="verify-correction" type="button" class="btn btn--primary hidden">Verify with re-measurement</button>
    <button id="reset-correction" type="button" class="btn btn--danger hidden">Reset to flat</button>
    <button id="cancel-measurement" type="button" class="btn btn--danger hidden">Cancel measurement</button>
  </p>
  <p class="hint" style="margin-top:0.4em">Before measuring, tap <strong>Auto-level</strong>. The speaker plays a 1 kHz tone while we gradually raise the volume from quiet to a measurement-friendly level (capped at −6 dB software volume — your amp's analog gain is still the final say). When the iPhone mic hears it in the target range, we lock automatically. If the volume sounds right to <em>you</em> first, tap <strong>Lock now</strong>. Takes ~6 seconds at most.</p>
  <p class="hint" style="margin-top:0.4em">Each measurement starts from flat — your current correction (if any) is reset first so the sweep captures the raw room. After you tap <strong>Apply</strong>, the new correction takes over.</p>
  <div id="autolevel-status" class="note-box hidden">
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
  <p class="hint">Read-only evidence from previous sessions. Raw measurement recordings are private and stay on the speaker unless you delete the bundle.</p>
  <button id="load-sessions" type="button" class="btn btn--ghost">Load recent reports</button>
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
</main>
<script type="module" src="/assets/correction/js/main.js"></script>
"""


def _render_page(hostname: str, csrf_token: str = "", flash: str = "") -> bytes:
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
    # Absolute http:// back link: /correction/ is HTTPS but the dashboard at /
    # is plain HTTP, so a relative "/" would try HTTPS on the root and fail.
    header = canonical_header(
        "Room correction",
        back_href="http://{host}/".format(host=hostname),
    )
    body = (
        _PAGE_BODY
        .replace("__HEADER__", header)
        .replace("__HOSTNAME__", html.escape(hostname, quote=True))
        .replace("__REQUIRED_SR__", str(REQUIRED_SAMPLE_RATE))
        .replace("__MIC_MODEL_OPTIONS__", mic_model_options)
        .replace("__TARGET_PROFILE_OPTIONS__", target_profile_options_html)
        .replace("__CORRECTION_STRATEGY_OPTIONS__", correction_strategy_options_html)
    )
    return canonical_page(
        "Room correction — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/correction.css",
    )


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


_BUILTIN_MIC_LABEL_RE = re.compile(
    r"iphone|ipad|ipod|macbook|built[- ]?in|^\s*default", re.IGNORECASE
)
# Vendor providers whose mics are always external USB measurement mics —
# they can never legitimately be the phone's own built-in microphone.
_EXTERNAL_MIC_PROVIDERS = frozenset({"dayton_audio", "minidsp"})


def _calibration_device_mismatch(
    mic_calibration: Any, input_device: dict[str, Any] | None
) -> str | None:
    """Detect applying a vendor measurement-mic calibration curve to audio
    captured from the phone's built-in mic — a silent, measurement-
    invalidating mismatch. The browser blocks this too, but this is the
    reliable backstop a stale/bypassed client cannot evade.
    """
    if mic_calibration is None or not input_device:
        return None
    provider = str(getattr(mic_calibration, "provider", "") or "")
    if provider not in _EXTERNAL_MIC_PROVIDERS:
        return None
    label = str(input_device.get("browser_label") or input_device.get("label") or "")
    if label and _BUILTIN_MIC_LABEL_RE.search(label):
        return (
            f'captured device "{label}" looks like the phone built-in mic, but '
            f"a {provider} measurement-mic calibration is loaded; select the USB "
            "measurement mic before measuring"
        )
    return None


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

        mismatch = _calibration_device_mismatch(mic_calibration, input_device)
        if mismatch is not None:
            logger.warning(
                "event=correction_start_rejected reason=calibration_device_mismatch "
                "provider=%s",
                getattr(mic_calibration, "provider", ""),
            )
            raise ValueError(mismatch)

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


def _handle_session_report(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /session-report?id=<session_id>: return a read-only,
    browser-safe measurement report built from one session bundle.

    This intentionally returns metadata and derived evidence only. Raw
    recordings stay in the private bundle for operator/CLI workflows.
    """
    from . import correction_report

    sess = _get_or_create_session()
    query = parse_qs(urlparse(handler.path).query)
    session_id = (query.get("id") or [""])[0]
    try:
        payload = correction_report.build_session_report_payload(
            sessions_dir=sess.cfg.sessions_dir,
            session_id=session_id,
        )
    except correction_report.InvalidSessionId as e:
        raise BadRequest(str(e)) from e
    logger.info(
        "event=correction_session_report session=%s",
        payload.get("session_id") or session_id,
    )
    return payload


def _handle_session_delete(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /session/delete: delete one historical measurement bundle."""
    import shutil

    from . import correction_report

    sess = _get_or_create_session()
    body = _read_json_body(handler)
    session_id = str(body.get("id") or "")
    try:
        bundle_dir = correction_report.resolve_session_bundle_dir(
            sess.cfg.sessions_dir,
            session_id,
        )
    except correction_report.InvalidSessionId as e:
        raise BadRequest(str(e)) from e
    current_state = getattr(getattr(sess, "state", None), "value", None)
    if (
        session_id == getattr(sess, "session_id", None)
        and current_state in _BUNDLE_DELETE_BLOCKED_STATES
    ):
        raise RequestConflict(
            "cannot delete the measurement bundle for an active session"
        )
    shutil.rmtree(bundle_dir)
    logger.info(
        "event=correction_session_bundle_deleted session=%s bundle=%s",
        session_id,
        bundle_dir,
    )
    return {"deleted": True, "session_id": session_id}


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
    """POST /reset: roll back to /etc/camilladsp/outputd-cutover.yml. Restores
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
                    cfg["hostname"], ctx["csrf_token"], ctx["flash"],
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
                "/session/delete",
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
                if path == "/session/delete":
                    try:
                        self._send_json(_handle_session_delete(self))
                    except BadRequest as e:
                        self._send_client_error(str(e))
                    except FileNotFoundError as e:
                        self._send_client_error(str(e), status=404)
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
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
