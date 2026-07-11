# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS correction measurement hub at /correction/.

The user opens the hub on a phone and chooses the measurement job:
room correction, active-crossover acoustic checks, or bass tuning. Room
correction captures pre-sweep room noise plus one or more measurement
positions, reviews confidence/visualization evidence, and optionally
applies a bounded room-correction profile through the shared CamillaDSP
apply path.

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
      GET  /                room correction page render
      GET  /room            room correction page render
      GET  /crossover       active-crossover measurement page render
      GET  /crossover/status
      GET  /bass            bass-management display page render
      GET  /bass/status     read-only bass-management state JSON
      GET  /healthz         liveness
      GET  /status          session snapshot JSON
      GET  /envelope        server-computed screen envelope (dumb-frontend)
      GET  /sessions        recent measurement bundle summaries
      GET  /session-report  read-only evidence packet for one bundle
      POST /start           reset DSP, create session, request noise capture
      POST /upload-noise    body = pre-sweep noise WAV, then play sweep
      POST /upload-capture  body = WAV bytes, runs analysis pipeline
      POST /repeat-position optional same-seat repeat sweep
      POST /apply           write YAML, reload CamillaDSP
      POST /reset           roll back to the topology-safe reset graph
      POST /session/delete  delete one historical measurement bundle
      POST /crossover/driver-test safe per-driver audible test
      POST /crossover/driver-confirm operator ACK for the active driver test
      POST /crossover/driver-abort stop/re-mute the active driver test
      POST /crossover/summed-test safe combined-driver audible test
      POST /crossover/driver-capture-sweep play the driver mic-capture sweep
      POST /crossover/summed-capture-sweep play the summed mic-capture sweep
      POST /crossover/driver-capture analyze + record one active-driver WAV
      POST /crossover/summed-capture analyze + record one summed-crossover WAV

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
import inspect
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from ..log_event import log_event

if TYPE_CHECKING:
    from jasper.capture_relay.client import RelayClient
    from jasper.capture_relay.correction_adapter import RelayCapture
    from jasper.capture_relay.session import PiCaptureSession
from ._common import (
    begin_request,
    bonded_follower_active,
    bonded_follower_leader_web_url,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    reject_csrf,
    send_html_response,
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
MAX_CROSSOVER_WAV_BODY_BYTES = 3 * 1024 * 1024
MAX_DEVICE_FIELD_CHARS = 160
# P6 tuning-LLM per-tap call budget. A frontier text model answering the
# interpret / propose packet is a few seconds; 90 s is a generous ceiling
# that still bounds a stalled provider connection on the Pi web process.
# Parse defensively: a jasper.env typo must degrade to the default, not
# crash the whole /correction/ wizard at import.
def _tuning_timeout_sec() -> float:
    try:
        value = float(os.environ.get("JASPER_TUNING_LLM_TIMEOUT_SEC", "90") or "90")
    except ValueError:
        return 90.0
    return value if value > 0 else 90.0


TUNING_LLM_TIMEOUT_SEC = _tuning_timeout_sec()
# The per-call output-token cap for the paid tuning calls lives at the
# model boundary — jasper.calibration_agent.model_client
# .TUNING_LLM_MAX_OUTPUT_TOKENS — shared with the live harness so the
# deployed cap and the live-validated cap cannot drift. The paid handlers
# reference it through their existing lazy model_client import.
# Minimum spacing between PAID tuning calls (interpret/propose), per
# process. Human taps are seconds apart; a stuck client retry loop must not
# silently burn spend. A second call inside the window gets an honest JSON
# error (409) the panel shows — never a silent drop.
TUNING_LLM_MIN_INTERVAL_SEC = 3.0
_FOLLOWER_DELEGATED_PAGE_PATHS = frozenset({"/", "/room", "/balance", "/sync"})
_RETURN_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$"
)


class BadRequest(ValueError):
    """Client supplied an invalid request body."""


class RequestConflict(RuntimeError):
    """Client request conflicts with the current correction session state."""


class SpendCapExceeded(RuntimeError):
    """The household daily spend cap is reached, so a PAID tuning call is
    refused. Distinct from RequestConflict (409, transient session/rate
    conflict) because this maps to HTTP 429 (Too Many Requests) with a
    rollover-worded message — the condition clears at the daily UTC rollover,
    not by retrying in a moment."""


# Module-level session + bridge to the async loop. Lazy-init on
# first use so importing this module is cheap (lets `python -m
# jasper.web.correction_setup --help` work without spinning up a
# loop).
_session_lock = threading.Lock()
_session = None  # type: ignore[var-annotated]
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None

# Active phone-mic-relay capture surfaced in /status: {tap_link, status} or None.
# Set by POST /relay/capture, updated by its background runner. Guarded by
# _session_lock (same single-session scope).
_relay_capture: dict[str, Any] | None = None
# Bound the foreground relay registration so a slow/unreachable relay fails fast
# rather than hanging the request thread for RelayClient's 15 s default.
_RELAY_REGISTER_TIMEOUT_S = 10.0
# Require a short rolling ambient window before the Pi starts the level tone.
# A single USB-mic startup block is too noisy to become the trust-floor SSOT;
# ten 200 ms samples gives a stable two-second median while keeping setup
# bounded and well inside the relay's rolling three-second sample window.
_RELAY_LEVEL_AMBIENT_MIN_SAMPLES = 10

# Mutating routes this handler accepts. Module-scoped so route membership is
# pinnable by a test (deleting a line would otherwise 404 a route silently).
_POST_ROUTES = frozenset({
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
    "/relay/level-match",
    "/relay/capture",
    "/relay/verify",
    "/calibration/fetch",
    "/calibration/upload",
    "/apply",
    "/reset",
    "/session/delete",
    "/interpret",
    "/propose",
    "/propose/apply",
    "/crossover/driver-test",
    "/crossover/driver-confirm",
    "/crossover/driver-abort",
    "/crossover/summed-test",
    "/crossover/driver-capture-sweep",
    "/crossover/summed-capture-sweep",
    "/crossover/driver-capture",
    "/crossover/summed-capture",
    "/crossover/level-match",
    "/crossover/relay-capture",
    "/crossover/apply",
    "/balance/start",
    "/balance/ramp",
    "/balance/meter",
    "/balance/lock",
    "/balance/stop",
    "/balance/apply",
    "/balance/reset",
    "/sync/start",
    "/sync/play",
    "/sync/analyze",
    "/sync/relay-capture",
    "/sync/apply",
    "/sync/stop",
    "/sync/reset",
})


def _set_relay_capture(value: dict[str, Any] | None) -> None:
    global _relay_capture
    with _session_lock:
        _relay_capture = value


def _get_relay_capture() -> dict[str, Any] | None:
    with _session_lock:
        return dict(_relay_capture) if _relay_capture else None


def _get_relay_capture_for(*kind_prefixes: str) -> dict[str, Any] | None:
    """Return relay state only to the flow that owns it.

    The process has one hardware-safe relay slot, but room, sync, and crossover
    pages must never render one another's phone link or waiting state.
    """
    relay = _get_relay_capture()
    if relay is None:
        return None
    kind = str(relay.get("kind") or "")
    return relay if any(kind.startswith(prefix) for prefix in kind_prefixes) else None


def _begin_relay_capture() -> bool:
    """Atomically claim the single relay-capture slot. Returns False if one is
    already in flight (so a double-tap can't spawn two relay sessions + a file
    race for one position — mirrors /autolevel's "already in progress" guard).
    The slot is released by `_set_relay_capture(None)` on a failed open, or by the
    background runner setting `complete`/`failed`."""
    global _relay_capture
    with _session_lock:
        if _relay_capture and _relay_capture.get("status") in (
            "starting",
            "awaiting_phone",
        ):
            return False
        _relay_capture = {"status": "starting"}
        return True


@dataclass(frozen=True)
class RelayCaptureKind:
    """Per-flow plug for the generic relay orchestrator (`_run_relay_capture`).

    Each measurement flow (room sweep, sync, crossover, …) injects only what is
    flow-specific — how to mint+register its relay capture, and how to run it +
    consume the verified WAV (play its stimulus on `armed`, then analyze). The
    orchestrator owns everything common: the single-slot re-entrancy guard,
    bounded registration, the `/status.relay` holder, and the background-task
    lifecycle. Adding a kind is a descriptor, not a fourth copy of the handler.

    `open(client, relay_base, capture_origin, return_url) -> RelayCapture`
    mints+registers the kind's `capture_spec`; `run_and_consume(client,
    pi_session)` awaits the phone capture (with the kind's stimulus as the
    `on_armed` callback) and feeds the verified WAV to the kind's existing
    analysis seam.
    """

    label: str
    open: Callable[[RelayClient, str, str, str], "RelayCapture"]
    run_and_consume: Callable[[RelayClient, PiCaptureSession], Awaitable[None]]


def _request_local_return_url(
    handler: BaseHTTPRequestHandler | None,
    path: str,
) -> str:
    """Build the local Pi URL the phone should return to after upload.

    The POST has already passed `guard_mutating_request`, but this helper still
    rejects host-shaped surprises before embedding the value in the public capture
    spec. Prefer the exact Host the user's browser reached (`jts5.local`,
    `jts5.local:port`, or a LAN IP); fall back to the configured hostname for
    tests/non-browser callers.
    """
    raw_host = ""
    if handler is not None:
        raw_host = str(handler.headers.get("Host") or "").strip().rstrip(".")
    fallback_host = str(os.environ.get("JASPER_HOSTNAME") or "jts.local").strip()
    fallback_host = re.sub(r"^https?://", "", fallback_host).strip("/").rstrip(".")
    host = raw_host if _RETURN_HOST_RE.match(raw_host) else fallback_host
    if not _RETURN_HOST_RE.match(host):
        host = "jts.local"
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"http://{host}{clean_path}"


def _run_relay_capture(
    kind: RelayCaptureKind,
    relay_base: str,
    *,
    return_url: str,
) -> dict[str, Any]:
    """Own the common relay-capture lifecycle for any kind. The caller has already
    gated on the relay being configured and run the kind's own state/calibration
    prechecks; this claims the slot, registers, spawns the background runner, and
    surfaces the tap-link. Mirrors the room handler's prior inline body so room
    behavior is unchanged — kinds just differ by their injected open/run."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.client import RelayClient
    from jasper.capture_relay.health import relay_registration_token_from_env

    if not _begin_relay_capture():
        raise ValueError("a phone-mic relay capture is already in progress")
    capture_origin = correction_adapter.capture_origin_from_env()
    spawned = False
    try:
        # Register in the foreground (the session must exist before the phone opens
        # the tap-link), bounded so a slow/unreachable relay fails fast.
        client = RelayClient(
            relay_base,
            timeout=_RELAY_REGISTER_TIMEOUT_S,
            registration_token=relay_registration_token_from_env(),
        )
        rc = kind.open(client, relay_base, capture_origin, return_url)

        async def _run() -> None:
            try:
                await kind.run_and_consume(client, rc.pi_session)
                _set_relay_capture(
                    {"tap_link": rc.tap_link, "status": "complete", "kind": kind.label}
                )
            except Exception as exc:  # noqa: BLE001 — surface loudly; never crash the loop
                # run_capture already logs event=capture_relay.failed with a
                # traceback; this outer net also flips /status.relay to failed and
                # carries the operator-facing reason (e.g. a device/calibration
                # mismatch) so the jts3/jts5 status page can show why.
                log_event(
                    logger,
                    "capture_relay.adapter_failed",
                    level=logging.WARNING,
                    exc_info=True,
                    kind=kind.label,
                    reason=type(exc).__name__,
                )
                _set_relay_capture({
                    "tap_link": rc.tap_link,
                    "status": "failed",
                    "kind": kind.label,
                    "error": str(exc),
                })

        _set_relay_capture(
            {"tap_link": rc.tap_link, "status": "awaiting_phone", "kind": kind.label}
        )
        asyncio.run_coroutine_threadsafe(_run(), _ensure_loop())
        spawned = True
        return {"tap_link": rc.tap_link, "status": "awaiting_phone"}
    finally:
        if not spawned:
            _set_relay_capture(None)  # release the slot on any early failure


def _require_relay_base() -> str:
    """Return the configured relay origin, or raise the gated-off ValueError.

    Called FIRST by every relay endpoint so an operator can still set
    JASPER_CAPTURE_RELAY_BASE=disabled/off/0/none and keep the on-Pi flow
    byte-identical. Fresh installs seed https://relay.jasper.tech because phone
    microphone access needs a publicly trusted HTTPS capture page. Also narrows
    the value from str|None to str for the register call."""
    from jasper.capture_relay.health import relay_base_from_env

    relay_base = relay_base_from_env()
    if relay_base is None:
        raise ValueError(
            "phone-mic relay capture is not configured — set "
            "JASPER_CAPTURE_RELAY_BASE (and deploy the relay + capture page), or "
            "use the on-Pi /correction/ capture flow"
        )
    return relay_base


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


def active_correction_phase() -> str | None:
    """Read-only: the active room-correction session state, or None.

    The counterpart to balance/sync ``active_phase()`` so another measurement
    flow (active-speaker commissioning) can exclude correction without the side
    effect of ``_reserve_start_slot`` (which reserves /start)."""
    with _session_lock:
        return _active_state_for_session(_session)


def _crossover_blocking_phase() -> str | None:
    """Return another active measurement phase that should block crossover."""

    from .active_speaker_flow import blocking_measurement_phase

    return blocking_measurement_phase()


def _reserve_start_slot() -> str | None:
    """Atomically reserve /start or return the state blocking it.

    The session state only becomes active once the background sweep task
    starts. This small reservation closes the gap between accepting
    `/start` and the new session visibly leaving IDLE.
    """
    global _start_in_progress
    # The pair-balance and pair-sync flows share this process precisely so the
    # measurement surfaces can exclude each other here (both open
    # measurement_window; concurrent windows would interleave the
    # renderer stop/start). Active-speaker commissioning excludes the same way
    # (it plays sweeps through the production graph) but participates
    # cooperatively rather than holding a window — see active_speaker_flow.
    # Lazy imports: these modules never import this module back at import time.
    from .active_speaker_flow import active_phase as _active_speaker_phase
    from .balance_flow import active_phase as _balance_phase
    from .sync_flow import active_phase as _sync_phase
    balance_active = _balance_phase()
    if balance_active is not None:
        return f"balance:{balance_active}"
    sync_active = _sync_phase()
    if sync_active is not None:
        return f"sync:{sync_active}"
    commissioning = _active_speaker_phase()
    if commissioning is not None:
        return f"active_speaker:{commissioning}"
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
<main class="page correction-stack" data-required-sr="__REQUIRED_SR__" data-capture-relay-enabled="__CAPTURE_RELAY_ENABLED__">
__TABS__
<p class="page-sub">Measure your room with a phone, design correction filters, and apply them to the speaker.</p>

<div id="current-correction" class="flat" aria-live="polite">
  <span class="label" id="current-correction-label">Checking current correction…</span>
  <button id="current-correction-reset" type="button" class="btn btn--danger hidden">Reset correction</button>
</div>

<!-- Stepped-wizard chrome (P3b). Server-computed screen envelope (GET
     /envelope) drives everything here: which step you're on, the one
     plain-language verdict, homeowner nudges (a sentence + severity, never
     a block), the single primary action (always live — nudges never
     disable it), and the step indicator. The workflow sections below stay;
     the router shows the ones the current step needs. -->
<section id="wizard-chrome" class="wizard-chrome hidden" aria-live="polite">
  <ol id="wizard-steps" class="wizard-steps" aria-label="Room correction steps"></ol>
  <p id="wizard-verdict" class="wizard-verdict"></p>
  <div id="wizard-nudges" class="wizard-nudges"></div>
  <button id="wizard-next" type="button" class="btn btn--primary hidden"></button>
</section>

<!-- P6 tuning assistant. Hidden until the envelope's tuning_llm block says
     it is offered (a measurement screen). When offered-but-unavailable
     (no OpenAI key) it renders the nudge; when available it shows the two
     per-tap actions. The paid call happens ONLY on a tap. -->
<section id="tuning-panel" class="tuning-panel hidden" aria-live="polite">
  <h2 class="tuning-title">Tuning assistant</h2>
  <p id="tuning-nudge" class="tuning-nudge hidden"></p>
  <div id="tuning-actions" class="tuning-actions hidden">
    <button id="tuning-interpret" type="button" class="btn">Explain my room</button>
    <button id="tuning-propose" type="button" class="btn">Suggest a tweak</button>
  </div>
  <p id="tuning-status" class="tuning-status hidden"></p>
  <div id="tuning-explanation" class="tuning-explanation hidden"></div>
  <p id="tuning-provenance" class="tuning-provenance hidden"></p>
  <div id="tuning-proposals" class="tuning-proposals"></div>
</section>

<section id="relay-panel" class="relay-panel hidden" aria-live="polite">
  <h2 style="margin-top:0">Room measurement</h2>
  <p class="hint">JTS will open a guided capture page on <code>capture.jasper.tech</code>. The phone records first; the speaker plays only after that page is ready.</p>
  <button id="relay-start-capture" type="button" class="btn btn--primary">Start</button>
  <div id="relay-link-row" class="relay-link-row hidden">
    <a id="relay-tap-link" class="btn btn--primary" href="#" target="_blank" rel="noopener">Open capture page</a>
  </div>
  <p id="relay-status" class="relay-status">Ready to create a phone capture link.</p>
</section>

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

<details id="advanced-correction-options" class="advice">
  <summary>Advanced</summary>
  <p class="hint">Advanced options are mostly for development, relay outages, or calibrated local-browser capture on a trusted HTTPS speaker page.</p>
  <button id="local-capture-fallback" type="button" class="btn btn--ghost">Use local browser capture</button>
</details>

<div id="mic-panel" class="mic-panel">
  <h2 style="margin-top:0">Microphone</h2>
  <div class="mic-grid">
    <div id="local-input-row" class="mic-row local-capture-only">
      <label for="input-device-select">Input device
        <select id="input-device-select">
          <option value="" disabled selected>Detecting microphones…</option>
        </select>
      </label>
      <button id="refresh-inputs" type="button" class="btn btn--ghost">Refresh microphones</button>
    </div>
    <p id="local-input-hint" class="hint local-capture-only" style="margin:0">Your USB measurement mic should appear automatically (grant mic permission if asked). Tap <strong>Refresh microphones</strong> if it doesn’t, then select it before <strong>Start mic capture</strong>.</p>

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

<button id="start" type="button" class="btn btn--primary local-capture-only">Start mic capture</button>

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

  <div id="measurement-options" class="info-card">
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
    <label id="repeat-main-position-row" style="margin-top:0.6em">
      <input id="repeat-main-position" type="checkbox" checked>
      Repeat the main seat once for a trust check
    </label>
    <p id="repeat-main-position-hint" class="hint" style="margin-top:0.3em">This adds one extra sweep at the first position and helps JTS tell measurement noise from real room behavior.</p>
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
    <button id="reset-correction" type="button" class="btn btn--danger hidden">Reset correction</button>
    <button id="cancel-measurement" type="button" class="btn btn--danger hidden">Cancel measurement</button>
  </p>
  <p id="autolevel-hint" class="hint" style="margin-top:0.4em">Before measuring, tap <strong>Auto-level</strong>. The speaker plays a 1 kHz tone while we gradually raise the volume from quiet to a measurement-friendly level (capped at −6 dB software volume — your amp's analog gain is still the final say). When the phone mic hears it in the target range, we lock automatically. If the volume sounds right to <em>you</em> first, tap <strong>Lock now</strong>. Takes ~6 seconds at most.</p>
  <p class="hint" style="margin-top:0.4em">Each measurement bypasses your current correction and preference EQ first so the sweep captures the raw room. After you tap <strong>Apply</strong>, the new correction takes over.</p>
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
      After Verify: <span style="color:#a050d0">purple dashed</span> = post-correction measurement,
      with the measured before→after gap shaded
      <span style="color:#1db954">green</span> where it moved toward target and
      <span style="color:#d68200">amber</span> where it moved away.
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


def _render_follower_page(hostname: str, csrf_token: str = "") -> bytes:
    leader_url = bonded_follower_leader_web_url("/correction/")
    leader_link = (
        '<a class="btn btn--primary" href="'
        + html.escape(leader_url)
        + '">Open leader correction</a>'
        if leader_url else ""
    )
    header = canonical_header(
        "Room correction",
        back_href="http://{host}/".format(host=hostname),
    )
    body = f"""
{header}
<main class="page">
  <section class="info-card info-card--accent" role="note">
    <h2 class="section__title">Room correction is controlled by the pair leader</h2>
    <p class="form-hint">This speaker is an active follower. Room correction,
    balance, and sync measurements are content calibration for the paired
    playback image, so run them from the leader while the pair is active.</p>
    <div class="actions">
      {leader_link}
      <a class="btn" href="/rooms/">Manage pair</a>
    </div>
  </section>
</main>
"""
    return canonical_page(
        "Room correction — JTS speaker",
        body,
        csrf_token=csrf_token,
    )


def _render_page(hostname: str, csrf_token: str = "", flash: str = "") -> bytes:
    if bonded_follower_active():
        return _render_follower_page(hostname, csrf_token)
    from jasper.audio_measurement.calibration import (
        SUPPORTED_MODELS,
        model_label_aliases,
    )
    from jasper.correction.strategy import (
        DEFAULT_CORRECTION_STRATEGY_ID,
        DEFAULT_TARGET_PROFILE_ID,
        correction_strategy_options,
        target_profile_options,
    )

    # data-aliases carries the registry's label tokens to the wizard so it can
    # infer the model from a device label without a hardcoded client-side map.
    mic_model_options = "\n        ".join(
        '<option value="{key}" data-aliases="{aliases}">{label}</option>'.format(
            key=html.escape(key, quote=True),
            aliases=html.escape(",".join(model_label_aliases(key)), quote=True),
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
    from jasper.capture_relay import correction_adapter
    capture_relay_enabled = correction_adapter.relay_enabled()
    # Absolute http:// back link: /correction/ is HTTPS but the dashboard at /
    # is plain HTTP, so a relative "/" would try HTTPS on the root and fail.
    header = canonical_header(
        "Correction",
        back_href="http://{host}/".format(host=hostname),
    )
    from .correction_hub import section_tabs

    body = (
        _PAGE_BODY
        .replace("__HEADER__", header)
        .replace("__TABS__", section_tabs("room"))
        .replace("__HOSTNAME__", html.escape(hostname, quote=True))
        .replace("__REQUIRED_SR__", str(REQUIRED_SAMPLE_RATE))
        .replace(
            "__CAPTURE_RELAY_ENABLED__",
            "1" if capture_relay_enabled else "0",
        )
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


def _run_relay_measurement_sweep(
    sess: Any,
    cam: Any,
    *,
    client: RelayClient,
    pi_session: PiCaptureSession,
) -> None:
    """Play one relay-triggered sweep and publish real progress to the phone.

    The old relay flow relied on a fixed phone-side recording window. The phone
    now records until it sees ``phase=sweep_complete`` from the Pi, then keeps
    the spec's post-roll. This function therefore blocks until the actual sweep
    path returns, while still using the same measurement_window and
    MeasurementSession transition code as the local browser flow.
    """
    from jasper.correction import coordinator, playback

    def _host_event(phase: str, **extra: Any) -> None:
        payload = {
            "phase": phase,
            "position": int(getattr(sess, "current_position", 0)) + 1,
            "total_positions": int(getattr(sess, "total_positions", 1)),
            **extra,
        }
        client.post_host_event(pi_session.session_id, pi_session.pull_token, payload)

    async def _run_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        async with coordinator.measurement_window():
            if not await sess.ensure_level_match_volume(
                lambda db: cam.set_volume_db(db, best_effort=False)
            ):
                raise RuntimeError(
                    "the saved measurement level is unavailable; run the level "
                    "check again"
                )
            try:
                await asyncio.to_thread(_host_event, "sweep_started")
                await sess.prepare_and_play_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
                await asyncio.to_thread(_host_event, "sweep_complete")
            finally:
                # The renderers resume when measurement_window exits. Restore
                # the household listening volume before that boundary, on every
                # success and failure path.
                await sess.restore_level_match_volume(
                    lambda db: cam.set_volume_db(db, best_effort=False)
                )

    try:
        _run_async(_run_sweep(), timeout=90.0)
    except (concurrent.futures.TimeoutError, RuntimeError, OSError, ValueError) as exc:
        try:
            _host_event("sweep_failed", error=str(exc))
        except (RuntimeError, OSError, ValueError):
            logger.debug("could not publish relay sweep failure", exc_info=True)
        raise


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


# UX-side mirror lives in deploy/assets/correction/js/main.js
# (looksLikeBuiltInMic); keep the two patterns in sync. This server gate is
# the one that actually blocks a wrong-mic measurement.
_BUILTIN_MIC_LABEL_RE = re.compile(
    r"iphone|ipad|ipod|macbook|built[- ]?in|^\s*default", re.IGNORECASE
)


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
    # Every entry in the calibration registry is an external USB measurement
    # mic that can never be the phone's own built-in mic. Derive the provider
    # set from the registry so a new vendor only has to be added in one place.
    # mic_calibration is non-None here, so calibration (numpy) is already
    # imported — this lazy import keeps the idle module import numpy-free.
    from jasper.audio_measurement.calibration import SUPPORTED_MODELS
    external_providers = {
        spec["provider"] for spec in SUPPORTED_MODELS.values()
    }
    provider = str(getattr(mic_calibration, "provider", "") or "")
    if provider not in external_providers:
        return None
    label = str(input_device.get("browser_label") or input_device.get("label") or "")
    if label and _BUILTIN_MIC_LABEL_RE.search(label):
        return (
            f'captured device "{label}" looks like the phone built-in mic, but '
            f"a {provider} measurement-mic calibration is loaded; select the USB "
            "measurement mic before measuring"
        )
    return None


def _relay_device_calibration_block(
    mic_calibration: Any, device: dict[str, Any] | None
) -> str | None:
    """Whether to REFUSE a phone-relay capture because a loaded mic calibration
    can't be trusted for the mic the phone actually used.

    A relay capture is recorded by whatever input the phone selected — its
    built-in mic, OR a USB-C measurement mic plugged into the phone. A loaded
    vendor calibration curve is valid only for that USB measurement mic, never the
    phone's built-in. We can't know which until the phone records, so this runs
    POST-capture against the phone-reported `device` (the same built-in-vs-USB
    decision the same-origin browser flow makes via `_calibration_device_mismatch`):

      - no calibration loaded            → allow (nothing to mis-apply);
      - calibration loaded, no device    → refuse (can't verify the mic — an older
                                            capture page, or a non-compliant client);
      - calibration loaded, device given → defer to `_calibration_device_mismatch`
                                            (refuse a built-in-mic label, allow the
                                            USB measurement mic the curve is for).

    Returns a refusal message, or None to allow. The calibration itself is applied
    Pi-side during analysis (`MeasurementSession._smooth_capture`); this only gates
    whether the capture is trustworthy to analyze.
    """
    if mic_calibration is None:
        return None
    label = (device or {}).get("label") or (device or {}).get("browser_label")
    if not label:
        return (
            "a measurement-mic calibration is loaded, but the phone didn't report "
            "which mic it used — update the capture page, or remove the calibration "
            "to measure with the phone's own mic"
        )
    return _calibration_device_mismatch(mic_calibration, device)


@dataclass(frozen=True)
class _RelayLevelIdentity:
    """Mic + calibration identity acquired by the automatic level check."""

    calibration_id: str
    device_key: str


@dataclass(frozen=True)
class _RelaySetupBinding:
    """Pi-validated identity for one guided microphone setup."""

    binding_id: str
    sha256: str


def _setup_digest(setup: dict[str, Any]) -> str:
    raw = json.dumps(
        setup,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _bind_relay_setup(
    owner: Any,
    setup: dict[str, Any],
    identity: dict[str, Any] | None,
    *,
    expected_binding_id: str,
) -> _RelaySetupBinding:
    """Validate the full one-time setup and freeze its compact identity."""
    if not isinstance(identity, dict):
        raise ValueError("the phone did not provide a setup identity")
    binding_id = str(identity.get("binding_id") or "")
    digest = str(identity.get("sha256") or "").lower()
    if identity.get("schema") != 1 or binding_id != expected_binding_id:
        raise ValueError("the phone setup belongs to a different measurement run")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("the phone setup identity is malformed")
    if not secrets.compare_digest(digest, _setup_digest(setup)):
        raise ValueError("the phone setup identity does not match its contents")
    binding = _RelaySetupBinding(binding_id=binding_id, sha256=digest)
    owner.relay_setup_binding = binding
    return binding


def _assert_relay_setup_binding(
    owner: Any,
    compact_setup: dict[str, Any] | None,
    *,
    expected_binding_id: str,
) -> None:
    """Refuse stale/mutated follow-up links without resending raw setup."""
    claim = (
        compact_setup.get("binding")
        if isinstance(compact_setup, dict)
        else None
    )
    bound = getattr(owner, "relay_setup_binding", None)
    if not isinstance(bound, _RelaySetupBinding):
        raise ValueError("the microphone setup is no longer active; run level check")
    if not isinstance(claim, dict) or claim.get("schema") != 1:
        raise ValueError("the phone did not provide the frozen microphone setup")
    if (
        str(claim.get("binding_id") or "") != expected_binding_id
        or str(claim.get("binding_id") or "") != bound.binding_id
        or not secrets.compare_digest(
            str(claim.get("sha256") or "").lower(), bound.sha256
        )
    ):
        raise ValueError("the microphone setup changed; run the level check again")


def _relay_level_identity(sess: Any) -> _RelayLevelIdentity:
    device = dict(getattr(sess, "input_device", None) or {})
    device_key = str(
        device.get("actual_device_id_hash")
        or device.get("device_id_hash")
        or device.get("label")
        or device.get("browser_label")
        or ""
    ).casefold()
    return _RelayLevelIdentity(
        calibration_id=str(
            getattr(
                getattr(sess, "mic_calibration", None), "calibration_id", ""
            )
            or ""
        ),
        device_key=device_key,
    )


def _assert_relay_level_identity(
    sess: Any,
    expected: _RelayLevelIdentity,
    *,
    device: dict[str, Any] | None = None,
) -> None:
    """Refuse a sweep if its mic/calibration differs from its level check."""
    current = _relay_level_identity(sess)
    if current.calibration_id != expected.calibration_id:
        raise ValueError(
            "the microphone calibration changed after level matching; run the "
            "level check again"
        )
    if device is None:
        return
    actual = _sanitize_input_device(device) or {}
    actual_key = str(
        actual.get("actual_device_id_hash")
        or actual.get("device_id_hash")
        or actual.get("label")
        or actual.get("browser_label")
        or ""
    ).casefold()
    if expected.device_key and not actual_key:
        raise ValueError(
            "the phone did not identify the microphone used for the sweep; "
            "run the level check again"
        )
    if expected.device_key and actual_key != expected.device_key:
        raise ValueError(
            "the microphone changed after level matching; select the same "
            "microphone or run the level check again"
        )


def _room_correction_readiness() -> dict[str, Any]:
    """Read the speaker-owned prerequisite; never derive a second rule here."""
    from jasper.active_speaker.setup_status import read_active_speaker_setup_status

    return read_active_speaker_setup_status()


def _handle_start(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /start: snapshot the current DSP graph, load a measurement
    baseline with room/preference layers stripped, replace the session, and
    ask the browser for pre-sweep room-noise capture. The sweep starts only
    after `POST /upload-noise` lands.

    Body fields:
      - total_positions: int = 1 (Phase 1 default; UI sends 5 for MMM)
      - target_choice:   str = 'flat' | 'neutral' | 'warm' | 'bright'
      - strategy_choice: str = 'safe' | 'balanced' | 'assertive'
      - noise_floor_db:  float | None — optional, client autolevel
        preflight measurement; only saved into the debug bundle.
      - repeat_main_position: bool = true — optional same-seat repeat
        for repeatability evidence.

    Why strip layers before sweeping: if a correction or preference EQ is
    loaded, the sweep traverses that layer and the resulting curve reflects
    the user's taste or the old correction, not the raw room. The carrier
    keeps the topology-owned speaker graph (crossovers, driver EQ, delays,
    gains, limiters) and strips only Layer B/C.
    """
    from jasper.correction.session import SessionState
    setup = _room_correction_readiness()
    if setup.get("room_correction_allowed") is not True:
        raw_acoustic = setup.get("acoustic_commissioning")
        acoustic = raw_acoustic if isinstance(raw_acoustic, dict) else {}
        reason = str(
            acoustic.get("reason") or "speaker_room_correction_not_ready"
        )
        detail = str(
            acoustic.get("detail")
            or "Speaker setup is not ready for room correction."
        )
        href = str(acoustic.get("setup_href") or "/sound/")
        log_event(
            logger,
            "correction_start_rejected",
            reason=reason,
            setup_href=href,
            level=logging.WARNING,
        )
        raise RequestConflict(f"{detail} Open {href}")

    body = _read_json_body(handler)
    blocking_state = _reserve_start_slot()
    if blocking_state is not None:
        log_event(
            logger,
            "correction_start_rejected",
            reason="active_session",
            state=blocking_state,
            level=logging.WARNING,
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
            from jasper.audio_measurement.calibration import load_calibration_record
            mic_calibration = load_calibration_record(
                calibration_id,
                root=_calibration_root(),
            )

        mismatch = _calibration_device_mismatch(mic_calibration, input_device)
        if mismatch is not None:
            log_event(
                logger,
                "correction_start_rejected",
                reason="calibration_device_mismatch",
                provider=getattr(mic_calibration, "provider", ""),
                level=logging.WARNING,
            )
            raise ValueError(mismatch)

        from jasper.correction import browser_audio

        browser_report = browser_audio.assess_browser_audio_path(
            input_device=input_device,
            expected_sample_rate=REQUIRED_SAMPLE_RATE,
            has_mic_calibration=mic_calibration is not None,
        ).to_dict()
        if browser_report.get("failed") is True:
            issue_codes = [
                issue.get("code")
                for issue in browser_report.get("issues", [])
                if isinstance(issue, dict) and issue.get("severity") == "fail"
            ]
            log_event(
                logger,
                "correction_start_rejected",
                reason="browser_audio_path_failed",
                issue_codes=",".join(
                    str(code) for code in issue_codes if code
                ),
                level=logging.WARNING,
            )
            raise ValueError(
                browser_report.get("summary")
                or "browser audio path is not safe for measurement"
            )

        cam = _camilla()
        prior_session = _get_or_create_session()
        _run_async(
            prior_session.restore_level_match_volume(
                lambda db: cam.set_volume_db(db, best_effort=False)
            ),
            timeout=5.0,
        )
        sess = _replace_session(
            total_positions=total_positions,
            target_choice=target_choice,
            strategy_choice=strategy_choice,
            mic_calibration=mic_calibration,
            input_device=input_device,
            repeat_main_position=repeat_main_position,
        )
        requested_transport = str(body.get("capture_transport") or "local")
        sess.capture_transport = (
            "relay" if requested_transport == "relay" else "local"
        )
        sess.noise_floor_db = noise_floor_db

        if sess.browser_audio_report.get("failed") is True:
            issue_codes = [
                issue.get("code")
                for issue in sess.browser_audio_report.get("issues", [])
                if isinstance(issue, dict) and issue.get("severity") == "fail"
            ]
            log_event(
                logger,
                "correction_start_rejected",
                reason="browser_audio_path_failed",
                issue_codes=",".join(str(code) for code in issue_codes if code),
                level=logging.WARNING,
            )
            raise ValueError(
                sess.browser_audio_report.get("summary")
                or "browser audio path is not safe for measurement"
            )

        from jasper.sound.graph_carrier import CarrierCannotHostEq

        try:
            baseline_payload = _run_async(
                _load_measurement_baseline(sess, cam),
                timeout=10.0,
            )
        except CarrierCannotHostEq:
            logger.warning("/start: measurement baseline rejected by graph carrier")
            raise
        except RuntimeError as exc:
            logger.exception("/start: measurement baseline load rejected")
            raise RuntimeError(str(exc)) from None
        except Exception:  # noqa: BLE001
            logger.exception("/start: measurement baseline load failed")
            raise RuntimeError(
                "could not load speaker measurement baseline before measuring"
            ) from None
        sess.current_correction_at_start = baseline_payload.get(
            "current_correction_at_start"
        )

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
            log_event(
                logger,
                "correction_start_state_wait_timeout",
                session=sess.session_id,
                level=logging.WARNING,
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
            "measurement_config_path": baseline_payload.get(
                "measurement_config_path"
            ),
        }
    except Exception:  # noqa: BLE001
        if not locals().get("reservation_transferred", False):
            _clear_start_slot()
        raise


async def _load_measurement_baseline(sess: Any, cam: Any) -> dict[str, Any]:
    """Load a topology-preserving measurement graph for this correction run.

    The graph carrier is the single bridge between "whatever CamillaDSP is
    running" and "emit the same speaker topology with different program-domain
    layers." Passing ``room_peqs=[]`` and ``SoundProfile(enabled=False)`` strips
    old room correction and preference EQ while keeping crossovers/protection.
    """

    from jasper.correction.runtime_safety import (
        CorrectionRuntimeSafetyError,
        assert_correction_graph_safe,
    )
    from jasper.dsp_apply import DspApplyError, apply_dsp_config
    from jasper.correction.status import describe_current_config
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import (
        CarrierCannotHostEq,
        carrier_for_loaded_config,
    )
    from jasper.sound.profile import SoundProfile

    current_path = await cam.get_config_file_path(best_effort=False)
    if not current_path:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    sess.pre_measurement_config_path = Path(current_path)
    sess.cfg.config_dir.mkdir(parents=True, exist_ok=True)
    out_path = sess.cfg.config_dir / (
        f"correction_measurement_{sess.session_id}_{int(sess.started_at)}.yml"
    )
    # The measurement graph must capture the SAME program tap fan-in is feeding,
    # else under transport_pipe it would measure a dead loopback. Thread the coupling.
    coupling_capture_kwargs = coupling_capture_kwargs_from_env()

    async def _prepare_config() -> dict[str, Any]:
        anchor = await cam.get_config_file_path(best_effort=False)
        if not anchor:
            raise RuntimeError("CamillaDSP did not report a loaded config path")
        carrier = carrier_for_loaded_config(anchor, config_dir=sess.cfg.config_dir)
        result = carrier.reemit(
            SoundProfile(enabled=False),
            room_peqs=[],
            out_path=out_path,
            profile_id=f"measurement-{sess.session_id}",
            fanin_coupling_capture_kwargs=coupling_capture_kwargs,
        )
        assert_correction_graph_safe(result.yaml)
        sess.pre_measurement_config_path = Path(anchor)
        return {
            "prior_config_path": anchor,
            "room_peq_count": result.room_peq_count,
            "sound_filter_count": 0,
        }

    try:
        state = await apply_dsp_config(
            source="correction_measurement",
            candidate_path=out_path,
            load_config=lambda path: cam.set_config_file_path(
                path,
                best_effort=False,
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=True,
            ),
            prepare=_prepare_config,
            room_peq_count=0,
            sound_filter_count=0,
        )
    except DspApplyError as exc:
        if isinstance(
            exc.__cause__,
            (CarrierCannotHostEq, CorrectionRuntimeSafetyError),
        ):
            raise exc.__cause__ from exc
        raise
    sess.measurement_config_path = out_path
    if state.prior_config_path:
        sess.pre_measurement_config_path = Path(state.prior_config_path)
    descriptor = describe_current_config(
        sess.pre_measurement_config_path,
        config_dir=sess.cfg.config_dir,
        base_config_path=sess.cfg.base_config_path,
    )
    log_event(
        logger,
        "correction.measurement_baseline_loaded",
        session=sess.session_id,
        prior=str(sess.pre_measurement_config_path),
        candidate=str(out_path),
        op_id=state.op_id,
    )
    return {
        "current_correction_at_start": descriptor,
        "measurement_config_path": str(out_path),
        "prior_config_path": str(sess.pre_measurement_config_path),
        "last_dsp_apply": state.to_dict(),
    }


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
         (computed by the browser from the pre-sweep noise floor),
         POST /autolevel/lock.
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
    from jasper.audio_measurement import calibration
    return {
        "calibration": record.public_metadata(),
        "preview": calibration.preview_curve(record.curve),
    }


def _handle_calibration_models(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import SUPPORTED_MODELS
    return {
        "models": [
            {"key": key, **value}
            for key, value in SUPPORTED_MODELS.items()
        ]
    }


def _handle_calibration_fetch(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import fetch_vendor_calibration

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
    from jasper.audio_measurement.calibration import store_calibration

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


def _relay_calibration_from_setup(setup: dict[str, Any] | None) -> Any | None:
    """Materialize the phone wizard's calibration choice on the Pi.

    The phone cannot call the Pi directly, so serial/upload choices ride the
    relay event that arms the sweep. This mirrors the local `/calibration/*`
    handlers and returns the stored calibration record, or None for phone/no
    calibration.
    """
    calibration = setup.get("calibration") if isinstance(setup, dict) else None
    if not isinstance(calibration, dict):
        return None
    mode = str(calibration.get("mode") or "none").strip()
    if mode in ("", "none"):
        return None
    if mode == "serial":
        from jasper.audio_measurement.calibration import fetch_vendor_calibration

        return fetch_vendor_calibration(
            model_key=str(calibration.get("model") or "").strip(),
            serial=str(calibration.get("serial") or "").strip(),
            orientation=str(calibration.get("orientation") or "unknown").strip()
            or "unknown",
            root=_calibration_root(),
        )
    if mode == "upload":
        from jasper.audio_measurement.calibration import store_calibration

        filename = str(calibration.get("filename") or "uploaded-calibration.txt")
        return store_calibration(
            text=str(calibration.get("content") or ""),
            provider="manual_upload",
            model=str(calibration.get("model") or "other").strip() or "other",
            label=str(calibration.get("label") or filename).strip()
            or "Uploaded calibration",
            source=f"uploaded:{filename}",
            orientation=str(calibration.get("orientation") or "unknown").strip()
            or "unknown",
            sign_convention=(
                str(calibration.get("sign_convention") or "correction").strip()
                or "correction"
            ),
            root=_calibration_root(),
        )
    raise ValueError(f"unknown calibration mode: {mode}")


def _apply_relay_setup_to_session(sess: Any, setup: dict[str, Any] | None) -> None:
    """Apply phone-side setup before the relay-triggered sweep starts."""
    if not isinstance(setup, dict):
        return
    if "total_positions" in setup:
        try:
            total_raw = setup.get("total_positions")
            if total_raw is None:
                raise ValueError
            requested_total = int(total_raw)
        except (TypeError, ValueError):
            requested_total = int(getattr(sess, "total_positions", 1))
        min_total = int(getattr(sess, "current_position", 0)) + 1
        sess.total_positions = max(min_total, min(10, requested_total))

    if isinstance(setup.get("calibration"), dict):
        sess.mic_calibration = _relay_calibration_from_setup(setup)


def _handle_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /status: snapshot the current session + currently-loaded
    CamillaDSP config descriptor. `current_correction` is best-effort
    (returns None if CamillaDSP is unreachable) so the page still
    renders something useful when the daemon is restarting."""
    from jasper.correction.status import describe_current_config
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
    # Active phone-mic-relay capture, when one is in flight (tap-link + status).
    # None on the default on-Pi flow, so the page only shows the relay UI when the
    # operator has enabled it.
    snap["relay"] = _get_relay_capture_for("room_", "level_ramp:room")
    return snap


def _handle_envelope(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /envelope: the server-computed screen envelope for the current
    session (revision plan §3.2). Additive alongside /status — a pure
    read that the dumb-frontend wizard renders each step from. The legacy
    single-page UI keeps using /status untouched; the page migration is a
    later PR."""
    from jasper.correction.envelope import build_envelope_logged

    sess = _get_or_create_session()
    return build_envelope_logged(sess)


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
    log_event(
        logger,
        "correction_session_report",
        session=payload.get("session_id") or session_id,
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
    log_event(
        logger,
        "correction_session_bundle_deleted",
        session=session_id,
        bundle=bundle_dir,
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

    auto_reverted = False
    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        _run_async(
            sess.on_verify_capture_uploaded(captured_path), timeout=30.0,
        )
        # P4: a CONFIRMED-regression verdict auto-reverts. The verdict was
        # computed inside on_verify_capture_uploaded (pure, no CamillaDSP); the
        # rollback happens here where the CamillaDSP callbacks live, riding the
        # SAME reset target the /reset button uses (Layer B removed, speaker
        # DSP + preference preserved). Every other verdict is a no-op.
        auto_reverted = _maybe_auto_revert(sess)
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
        "verify_before_after": sess.verify_before_after,
        "acceptance": getattr(sess, "acceptance", None),
        "auto_reverted": auto_reverted,
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


def _handle_relay_capture(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /relay/capture: capture the current position via the cloud relay (the
    phone runs the capture page on jasper.tech) instead of a same-origin browser
    upload.

    GATED + DEFAULT-OFF. Inert unless an operator sets JASPER_CAPTURE_RELAY_BASE,
    so the standard on-Pi /correction/ flow is byte-identical without it. When
    enabled it mints a relay session, returns the phone tap-link, and runs the
    capture in the background: when the phone is recording (it drops `armed`), the
    Pi plays the sweep through the SAME measurement_window()/prepare_and_play_sweep
    path the browser flow uses (loud-output safety + renderer/voice pause
    preserved), then pulls + decrypts + verifies and feeds the WAV into
    on_capture_uploaded — the identical 48 kHz / mono / 32 MB seam as a
    same-origin upload.

    ON-DEVICE: the background sweep playback and the real measurement cannot be
    exercised hardware-free — only the config gate, the state guard, and the seam
    wiring are unit-tested. The relay Worker + capture page must be deployed and
    the phone must reach jasper.tech. Audible failure cues await a
    jasper-web -> jasper-voice cue bridge; until then failures surface on the
    capture page, on the jts.local status page (`relay.status`), and in
    `event=capture_relay.*` logs. This is the integration point the
    docs/phone-mic-relay-plan.md adapter step describes, shipped gated so the
    default flow is unaffected while it is validated on hardware.
    """
    from jasper.capture_relay import correction_adapter
    from jasper.correction.session import SessionState

    relay_base = _require_relay_base()  # gated off until configured; inert otherwise

    sess = _get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")
    # A relay capture owns the sweep for one position (it plays on `armed`), so it
    # starts from the pre-sweep state, not the post-sweep AWAITING_CAPTURE.
    if sess.state != SessionState.NEEDS_NOISE_CAPTURE:
        raise ValueError(
            "relay capture starts a measurement position; expected state "
            f"needs_noise_capture, got {sess.state.value}"
        )
    level_identity = _relay_level_identity(sess)
    setup_binding_id = str(sess.session_id)
    # The mic-calibration / device check runs POST-capture (in _run_and_consume),
    # not here: the phone's mic — its built-in, or a USB-C measurement mic plugged
    # into it — isn't known until it records and reports its device.

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_room_sweep_capture(
            client,
            position=sess.current_position + 1,
            total_positions=sess.total_positions,
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
            guided_setup=False,
            setup_binding_id=setup_binding_id,
        )

    async def _run_and_consume(
        client: RelayClient, pi_session: PiCaptureSession
    ) -> None:
        # On `armed` (phone recording), play the sweep through the SAME
        # measurement_window()/prepare_and_play_sweep path the browser flow uses
        # (loud-output safety + renderer/voice pause preserved). run_capture's
        # default 120 s timeout is intentionally ~ the AWAITING_CAPTURE watchdog;
        # keep them aligned if either constant changes.
        capture_path = sess.capture_path_for_position(sess.current_position)

        def _on_armed(state: Any) -> None:
            try:
                _assert_relay_setup_binding(
                    sess,
                    state.setup,
                    expected_binding_id=setup_binding_id,
                )
                _assert_relay_level_identity(sess, level_identity)
                if state.noise_floor:
                    try:
                        sess.noise_floor_db = float(
                            state.noise_floor.get("rms_dbfs")
                        )
                    except (TypeError, ValueError):
                        logger.debug(
                            "relay noise_floor ignored: %r",
                            state.noise_floor,
                        )
            except (RuntimeError, ValueError) as exc:
                try:
                    client.post_host_event(
                        pi_session.session_id,
                        pi_session.pull_token,
                        {"phase": "sweep_failed", "error": str(exc)},
                    )
                except (RuntimeError, OSError, ValueError):
                    logger.debug("relay setup failure event failed", exc_info=True)
                raise
            _run_relay_measurement_sweep(
                sess,
                _camilla(),
                client=client,
                pi_session=pi_session,
            )

        try:
            result = await asyncio.to_thread(
                correction_adapter.run_and_store,
                client,
                pi_session,
                capture_path,
                on_armed=_on_armed,
            )
            # Device-aware calibration gate (the phone's mic is known only now):
            # refuse a loaded vendor curve on the phone's built-in mic, allow it
            # for the matching USB measurement mic.
            block = _relay_device_calibration_block(
                sess.mic_calibration, result.device
            )
            if block is not None:
                raise ValueError(block)
            _assert_relay_level_identity(
                sess, level_identity, device=result.device
            )
            if result.noise_floor:
                try:
                    rms_raw = result.noise_floor.get("rms_dbfs")
                    if rms_raw is None:
                        raise ValueError
                    sess.noise_floor_db = float(rms_raw)
                except (TypeError, ValueError):
                    logger.debug(
                        "relay noise_floor ignored: %r",
                        result.noise_floor,
                    )
            await sess.on_capture_uploaded(capture_path)
        finally:
            # Idempotent backstop for failures before the armed/sweep window.
            await sess.restore_level_match_volume(
                lambda db: _camilla().set_volume_db(db, best_effort=False)
            )

    kind = RelayCaptureKind(
        label="room_sweep", open=_open, run_and_consume=_run_and_consume
    )
    relay = _run_relay_capture(
        kind,
        relay_base,
        return_url=_request_local_return_url(handler, "/correction/"),
    )
    return {"session_id": sess.session_id, "state": sess.state.value, "relay": relay}


def _handle_relay_verify(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /relay/verify: record and analyze the real post-apply response."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_room_sweep_spec
    from jasper.correction.session import SessionState

    relay_base = _require_relay_base()
    sess = _get_or_create_session()
    if sess.state not in {SessionState.APPLIED, SessionState.VERIFIED}:
        raise ValueError("verification requires an applied room correction")
    last = sess.level_match_snapshot().get("last")
    ramp = last.get("ramp") if isinstance(last, dict) else None
    if not (
        isinstance(ramp, dict)
        and ramp.get("state") == "locked"
    ):
        raise ValueError("check the listening-position level before verification")
    level_identity = _relay_level_identity(sess)
    setup_binding_id = str(sess.session_id)

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_room_sweep_spec(
                position=1,
                total_positions=1,
                guided_setup=False,
                setup_binding_id=setup_binding_id,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    async def _run_and_consume(
        client: RelayClient, pi_session: PiCaptureSession
    ) -> None:
        from jasper.capture_relay.session import purge, run_capture
        from jasper.correction import coordinator, playback

        cam = _camilla()
        capture_path = sess.verify_capture_path()

        async def _play_verify() -> None:
            async def _runtime_probe() -> dict[str, Any] | None:
                return await cam.get_runtime_status(best_effort=True)

            async with coordinator.measurement_window():
                if not await sess.ensure_level_match_volume(
                    lambda db: cam.set_volume_db(db, best_effort=False)
                ):
                    raise RuntimeError(
                        "the verification level is no longer active; run the "
                        "level check again"
                    )
                try:
                    await asyncio.to_thread(
                        client.post_host_event,
                        pi_session.session_id,
                        pi_session.pull_token,
                        {"phase": "sweep_started", "position": 1, "total_positions": 1},
                    )
                    await sess.start_verify_sweep(
                        playback.play_sweep,
                        runtime_probe_async=_runtime_probe,
                    )
                    await asyncio.to_thread(
                        client.post_host_event,
                        pi_session.session_id,
                        pi_session.pull_token,
                        {"phase": "sweep_complete", "position": 1, "total_positions": 1},
                    )
                finally:
                    await sess.restore_level_match_volume(
                        lambda db: cam.set_volume_db(db, best_effort=False)
                    )

        def _on_armed(state: Any = None) -> None:
            setup = getattr(state, "setup", None)
            _assert_relay_setup_binding(
                sess,
                setup if isinstance(setup, dict) else None,
                expected_binding_id=setup_binding_id,
            )
            _assert_relay_level_identity(sess, level_identity)
            _run_async(_play_verify(), timeout=90.0)

        try:
            result = await asyncio.to_thread(
                run_capture,
                client,
                pi_session,
                on_armed=_on_armed,
            )
            block = _relay_device_calibration_block(
                sess.mic_calibration, result.device
            )
            if block is not None:
                raise ValueError(block)
            _assert_relay_level_identity(
                sess, level_identity, device=result.device
            )
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_bytes(result.wav)
            await sess.on_verify_capture_uploaded(capture_path)
            await asyncio.to_thread(_maybe_auto_revert, sess)
        finally:
            try:
                await asyncio.to_thread(purge, client, pi_session)
            except (OSError, RuntimeError, ValueError):
                logger.debug("verify relay purge failed", exc_info=True)
            await sess.restore_level_match_volume(
                lambda db: cam.set_volume_db(db, best_effort=False)
            )

    relay = _run_relay_capture(
        RelayCaptureKind(
            label="room_verify",
            open=_open,
            run_and_consume=_run_and_consume,
        ),
        relay_base,
        return_url=_request_local_return_url(handler, "/correction/"),
    )
    return {"session_id": sess.session_id, "state": sess.state.value, "relay": relay}


async def _run_relay_level_match(
    sess: Any,
    client: Any,
    pi_session: Any,
    *,
    geometry: str,
    run_token: str,
    setup_binding_id: str = "",
) -> None:
    """Run one relay-fed level match without blocking the correction loop.

    ``MeasurementSession`` owns the level state and volume lease.  This adapter
    owns only transport: one cached status snapshot plus a serialized outbound
    host-event queue.  The ramp never performs a blocking relay request from its
    control loop and never gains a direct reference to the relay client.
    """
    from jasper.capture_relay.session import purge
    from jasper.correction import coordinator, playback

    cached_status: dict[str, Any] = {}
    outbound: list[dict[str, Any]] = []
    stop_pump = asyncio.Event()

    def _read_status() -> dict[str, Any]:
        return dict(cached_status)

    def _queue_host_event(payload: dict[str, Any]) -> None:
        outbound.append(dict(payload))

    async def _pump() -> None:
        unhealthy = False
        while not stop_pump.is_set():
            try:
                while outbound:
                    payload = outbound.pop(0)
                    await asyncio.to_thread(
                        client.post_host_event,
                        pi_session.session_id,
                        pi_session.pull_token,
                        payload,
                    )
                fresh = await asyncio.to_thread(
                    client.status,
                    pi_session.session_id,
                    pi_session.pull_token,
                )
                if isinstance(fresh, dict):
                    cached_status.clear()
                    cached_status.update(fresh)
                if unhealthy:
                    unhealthy = False
                    logger.info("relay status pump recovered during level match")
            except (OSError, RuntimeError, ValueError):
                if not unhealthy:
                    unhealthy = True
                    logger.warning(
                        "relay status pump failed during level match "
                        "(latched until recovery)",
                        exc_info=True,
                    )
            try:
                await asyncio.wait_for(stop_pump.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    pump_task = asyncio.create_task(_pump())
    cam = _camilla()
    player = None
    setup_tokens_seen: set[str] = set()
    page_compatible = False
    try:
        async with coordinator.measurement_window():
            from jasper.correction.level_match import parse_level_batch

            # The phone starts its meter before the Pi starts the tone. Build a
            # deduplicated token-scoped ambient window so ordinary room noise
            # cannot satisfy the tone target and repeated relay polls cannot
            # manufacture sample count.
            initial_noise_floor = getattr(sess, "noise_floor_db", None)
            ambient_samples: dict[tuple[int, int], float] = {}
            # The relay runner starts when the link is minted, not when the
            # household opens it. Give the sequential setup + Start tap a
            # human-scale bounded window; the ramp itself owns its much shorter
            # acoustic safety timeout once samples begin.
            deadline = asyncio.get_running_loop().time() + 480.0
            while True:
                event = cached_status.get("event")
                if isinstance(event, dict) and not page_compatible and any(
                    key in event
                    for key in ("setup_validate", "level_refused", "level_batch")
                ):
                    from jasper.capture_relay.session import (
                        CapturePageIncompatible,
                        validate_capture_page,
                    )

                    try:
                        validate_capture_page(
                            event.get("capture_page"), pi_session.spec
                        )
                    except CapturePageIncompatible:
                        log_event(
                            logger,
                            "capture_relay.page_incompatible",
                            level=logging.WARNING,
                            session_id=pi_session.session_id,
                            expected_protocol=pi_session.spec.capture_protocol_version,
                            observed_protocol=(event.get("capture_page") or {}).get(
                                "capture_protocol_version"
                            ),
                            observed_build=(event.get("capture_page") or {}).get(
                                "capture_page_build"
                            ),
                        )
                        raise
                    page_compatible = True
                    log_event(
                        logger,
                        "capture_relay.page_compatible",
                        session_id=pi_session.session_id,
                        protocol=pi_session.spec.capture_protocol_version,
                        page_build=(event.get("capture_page") or {}).get(
                            "capture_page_build"
                        ),
                    )
                if isinstance(event, dict) and event.get("setup_validate"):
                    setup_token = str(event.get("setup_token") or "")
                    if setup_token and setup_token not in setup_tokens_seen:
                        setup_tokens_seen.add(setup_token)
                        setup = event.get("setup")
                        try:
                            if not isinstance(setup, dict):
                                raise ValueError("the phone setup is missing")
                            if setup_binding_id:
                                _bind_relay_setup(
                                    sess,
                                    setup,
                                    event.get("setup_identity"),
                                    expected_binding_id=setup_binding_id,
                                )
                            _apply_relay_setup_to_session(sess, setup)
                        except (RuntimeError, ValueError) as exc:
                            sess.relay_setup_binding = None
                            response = {
                                "phase": "setup_validation_failed",
                                "setup_token": setup_token,
                                "error": str(exc),
                            }
                        else:
                            response = {
                                "phase": "setup_validated",
                                "setup_token": setup_token,
                            }
                        await asyncio.to_thread(
                            client.post_host_event,
                            pi_session.session_id,
                            pi_session.pull_token,
                            response,
                        )
                        if response["phase"] == "setup_validation_failed":
                            raise ValueError(str(response["error"]))
                refusal = (
                    event.get("level_refused")
                    if isinstance(event, dict)
                    else None
                )
                if (
                    isinstance(refusal, dict)
                    and str(refusal.get("run_token") or "") == run_token
                ):
                    reason = str(refusal.get("reason") or "unsupported_microphone")
                    if reason == "agc_not_proven_off":
                        raise RuntimeError(
                            "this browser cannot prove automatic microphone gain "
                            "is disabled; use a supported browser or USB measurement "
                            "microphone"
                        )
                    raise RuntimeError(f"the phone refused the level check: {reason}")
                samples = parse_level_batch(
                    event if isinstance(event, dict) else {},
                    run_token=run_token,
                )
                if samples:
                    batch = (
                        event.get("level_batch") if isinstance(event, dict) else None
                    )
                    context = (
                        batch.get("context") if isinstance(batch, dict) else None
                    )
                    if isinstance(context, dict):
                        setup = context.get("setup")
                        if setup_binding_id:
                            _assert_relay_setup_binding(
                                sess,
                                setup if isinstance(setup, dict) else None,
                                expected_binding_id=setup_binding_id,
                            )
                        elif isinstance(setup, dict):
                            _apply_relay_setup_to_session(sess, setup)
                        device = context.get("device")
                        if isinstance(device, dict):
                            sess.input_device = _sanitize_input_device(device)
                    mismatch = _relay_device_calibration_block(
                        getattr(sess, "mic_calibration", None),
                        getattr(sess, "input_device", None),
                    )
                    if mismatch is not None:
                        raise ValueError(mismatch)
                    if initial_noise_floor is None:
                        for sample in samples:
                            value = float(sample.rms_dbfs)
                            if math.isfinite(value):
                                ambient_samples.setdefault(
                                    (int(sample.seq), int(sample.t_client_ms)),
                                    value,
                                )
                        if (
                            len(ambient_samples)
                            < _RELAY_LEVEL_AMBIENT_MIN_SAMPLES
                        ):
                            await asyncio.sleep(0.1)
                            continue
                        ordered = sorted(ambient_samples.values())
                        initial_noise_floor = ordered[len(ordered) // 2]
                        sess.noise_floor_db = initial_noise_floor
                        log_event(
                            logger,
                            "correction.level_match_ambient_baseline",
                            session_id=getattr(sess, "session_id", None),
                            geometry=geometry,
                            sample_count=len(ordered),
                            rms_dbfs=f"{initial_noise_floor:.1f}",
                            spread_db=f"{ordered[-1] - ordered[0]:.1f}",
                        )
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        "the phone did not provide an ambient level baseline"
                    )
                await asyncio.sleep(0.1)

            from jasper.audio_measurement.excitation import (
                AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
            )

            tone_wav = playback._ensure_tone_wav(
                freq_hz=1000.0,
                duration_s=90.0,
                dbfs=AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
                sample_rate=48000,
            )
            player = playback.TonePlayer(tone_wav)

            async def _get_volume() -> float:
                value = await cam.get_volume_db(best_effort=False)
                return float(value) if value is not None else 0.0

            async def _set_volume(db: float) -> None:
                applied = await cam.set_volume_db(db, best_effort=False)
                if applied is False:
                    raise RuntimeError("CamillaDSP rejected the measurement volume")

            outcome = await sess.run_level_match(
                geometry,
                get_main_volume_db=_get_volume,
                set_main_volume_db=_set_volume,
                play_continuous_tone=player.play,
                cancel_tone=player.cancel,
                read_status=_read_status,
                post_host_event=_queue_host_event,
                noise_floor_dbfs=initial_noise_floor,
                run_token=run_token,
            )

        if not outcome.locked:
            detail = outcome.ramp.error or "safe measurement level was not reached"
            raise ValueError(detail)
        mismatch = _relay_device_calibration_block(
            getattr(sess, "mic_calibration", None),
            getattr(sess, "input_device", None),
        )
        if mismatch is not None:
            await sess.restore_level_match_volume(
                lambda db: cam.set_volume_db(db, best_effort=False)
            )
            raise ValueError(mismatch)
    except (
        OSError,
        RuntimeError,
        ValueError,
        asyncio.TimeoutError,
        concurrent.futures.TimeoutError,
    ) as exc:
        # Failures before the tone/ramp starts used to disappear when the relay
        # session was purged, leaving the phone waiting on an empty slot. Publish
        # the same terminal shape as the ramp and leave a short observation
        # window before cleanup.
        try:
            await asyncio.to_thread(
                client.post_host_event,
                pi_session.session_id,
                pi_session.pull_token,
                {
                    "ramp": {
                        "state": "error",
                        "terminal": True,
                        "run_token": run_token,
                        "error": str(exc),
                    }
                },
            )
            await asyncio.sleep(0.75)
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "could not publish terminal level-match failure",
                exc_info=True,
            )
        raise
    finally:
        if player is not None:
            player.cancel()
        # Let the serialized pump flush the ramp's final queued state before
        # stopping it. This is bounded; relay TTL remains the cleanup backstop.
        flush_deadline = asyncio.get_running_loop().time() + 1.0
        while outbound and asyncio.get_running_loop().time() < flush_deadline:
            await asyncio.sleep(0.05)
        stop_pump.set()
        await pump_task
        try:
            await asyncio.to_thread(purge, client, pi_session)
        except (OSError, RuntimeError, ValueError):
            logger.debug("level-match relay purge failed", exc_info=True)


def _handle_relay_level_match(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /relay/level-match: lock the listening-position measurement level.

    The room session must already have loaded its topology-preserving
    measurement baseline via ``/start``.  The returned tap-link opens the
    trusted phone page, where mic/calibration setup precedes the meter-only
    ramp.  No WAV is uploaded by this relay kind.
    """
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_level_ramp_spec
    from jasper.correction.level_match import MicGeometry
    from jasper.correction.session import SessionState

    relay_base = _require_relay_base()
    sess = _get_or_create_session()
    allowed_states = {
        SessionState.NEEDS_NOISE_CAPTURE,
        SessionState.APPLIED,
        SessionState.VERIFIED,
    }
    if sess.state not in allowed_states:
        raise ValueError(
            "level matching must run immediately before a room or verification sweep"
        )
    run_token = secrets.token_urlsafe(18)
    setup_binding_id = str(sess.session_id)

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_level_ramp_spec(
                geometry_label=(
                    "main listening position for verification"
                    if sess.state in {SessionState.APPLIED, SessionState.VERIFIED}
                    else "main listening position"
                ),
                run_token=run_token,
                setup_binding_id=setup_binding_id,
                setup_collect_positions=True,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    async def _run(client: RelayClient, pi_session: PiCaptureSession) -> None:
        await _run_relay_level_match(
            sess,
            client,
            pi_session,
            geometry=MicGeometry.LISTENING_POSITION.value,
            run_token=run_token,
            setup_binding_id=setup_binding_id,
        )

    relay = _run_relay_capture(
        RelayCaptureKind(label="level_ramp:room", open=_open, run_and_consume=_run),
        relay_base,
        return_url=_request_local_return_url(handler, "/correction/"),
    )
    return {"session_id": sess.session_id, "state": sess.state.value, "relay": relay}


def _handle_crossover_relay_level_match(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/level-match: acquire the near-field Layer-A gain lease."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_level_ramp_spec
    from jasper.correction.level_match import MicGeometry

    from . import correction_crossover_backend as backend
    from . import correction_crossover_flow

    relay_base = _require_relay_base()
    status = backend.status_payload()
    if not status.get("active"):
        raise ValueError("this speaker has no active crossover to measure")
    raw_setup = status.get("setup")
    setup = raw_setup if isinstance(raw_setup, dict) else {}
    if setup.get("status") != "ready":
        raise ValueError(
            "finish and apply the protected active-speaker setup before measuring it"
        )
    blocking = _crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(f"another measurement is in progress ({blocking})")
    status = correction_crossover_flow.ensure_automatic_measurement_profile(
        status,
        _run_async,
        _camilla,
        status_loader=backend.status_payload,
    )
    raw_setup = status.get("setup")
    setup = raw_setup if isinstance(raw_setup, dict) else {}
    raw_setup_profile = setup.get("protected_profile")
    setup_profile = raw_setup_profile if isinstance(raw_setup_profile, dict) else {}
    context_id = str(setup_profile.get("source_fingerprint") or "") or None
    if context_id is None:
        raise ValueError(
            "protected speaker setup has no stable profile identity; reapply it "
            "before level matching"
        )

    lease = backend.level_lease()
    run_token = secrets.token_urlsafe(18)
    setup_binding_id = context_id

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_level_ramp_spec(
                geometry_label="speaker baffle measurement position",
                run_token=run_token,
                setup_binding_id=setup_binding_id,
                setup_collect_positions=False,
            ),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    async def _run(client: RelayClient, pi_session: PiCaptureSession) -> None:
        await _run_relay_level_match(
            lease,
            client,
            pi_session,
            geometry=MicGeometry.NEAR_FIELD_DRIVER.value,
            run_token=run_token,
            setup_binding_id=setup_binding_id,
        )
        lease.context_id = context_id

    relay = _run_relay_capture(
        RelayCaptureKind(
            label="level_ramp:crossover",
            open=_open,
            run_and_consume=_run,
        ),
        relay_base,
        return_url=_request_local_return_url(handler, "/correction/crossover/"),
    )
    return {"relay": relay, "level_match": lease.level_match_snapshot()}


def _handle_sync_relay_capture(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /sync/relay-capture: capture the sync markers via the cloud relay (the
    phone runs the capture page on jasper.tech) instead of a same-origin upload.

    GATED + DEFAULT-OFF, like /relay/capture. The sync session window must already
    be open (the /sync/ Start button → handle_start), exactly as the browser flow
    requires before playing the marker. sync_flow owns the stimulus + analysis;
    this just bridges the relay transport through the shared orchestrator. The
    second real caller of the RelayCaptureKind seam — a new kind is a descriptor,
    not a new handler. ON-DEVICE: the acoustic marker capture is not exercised
    hardware-free (same status as the room relay)."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_sync_marker_spec

    from . import sync_flow

    relay_base = _require_relay_base()  # gated off until configured; inert otherwise
    err = sync_flow.relay_precheck()
    if err is not None:
        raise ValueError(err)

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_sync_marker_spec(),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    kind = RelayCaptureKind(
        label="sync_marker",
        open=_open,
        run_and_consume=sync_flow.relay_run_and_consume,
    )
    return {
        "relay": _run_relay_capture(
            kind,
            relay_base,
            return_url=_request_local_return_url(handler, "/correction/sync"),
        )
    }


def _handle_crossover_relay_capture(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/relay-capture: capture one active-crossover driver/summed
    sweep via the cloud relay (the phone runs the capture page on jasper.tech)
    instead of a same-origin upload.

    The third
    real caller of the RelayCaptureKind seam — a new kind is a descriptor, not a
    new orchestrator. The `crossover_sweep` spec + `correction_crossover_flow`'s
    relay run-and-consume own the stimulus + analysis; this bridges the relay
    transport through the shared orchestrator. Body: `{kind: "driver"|"summed",
    speaker_group_id, role (driver only)}`. ON-DEVICE: the acoustic capture is
    not exercised hardware-free (same status as the room/sync relay — H2).

    Measurement mutual-exclusion is SERVER-computed twice, never client-supplied:
    refused here at POST time while room correction / balance / sync is active
    (mirrors sync's `relay_precheck`), and re-checked at armed time inside the
    run-and-consume (the phone can arm minutes later — a sweep played over
    another measurement silently corrupts both captures)."""
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    from . import correction_crossover_flow
    from . import correction_crossover_backend

    relay_base = _require_relay_base()  # gated off until configured; inert otherwise
    blocking = _crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(
            f"another measurement is in progress ({blocking}) — finish it "
            "before starting a crossover relay capture"
        )
    raw = _read_json_body(handler)
    lease = correction_crossover_backend.level_lease()
    status = correction_crossover_backend.status_payload()
    setup = status.get("setup") if isinstance(status, dict) else None
    if (
        not status.get("active")
        or not isinstance(setup, dict)
        or setup.get("status") != "ready"
    ):
        _run_async(
            lease.restore_level_match_volume(
                lambda db: _camilla().set_volume_db(db, best_effort=False)
            ),
            timeout=5.0,
        )
        raise ValueError(
            "protected speaker setup is no longer ready; finish it before "
            "capturing the crossover"
        )
    level = status.get("level_match") if isinstance(status, dict) else None
    if isinstance(level, dict) and level.get("valid") is False:
        _run_async(
            lease.restore_level_match_volume(
                lambda db: _camilla().set_volume_db(db, best_effort=False)
            ),
            timeout=5.0,
        )
        raise ValueError(
            "speaker setup changed after level matching; run the crossover "
            "level check again"
        )
    level = lease.level_match_snapshot()
    last = level.get("last") if isinstance(level, dict) else None
    ramp = last.get("ramp") if isinstance(last, dict) else None
    if not (
        isinstance(ramp, dict)
        and ramp.get("state") == "locked"
    ):
        raise ValueError("run the near-field automatic level check before capturing")
    calibration = getattr(lease, "mic_calibration", None)
    level_identity = _relay_level_identity(lease)
    if calibration is not None:
        raw["calibration_id"] = calibration.calibration_id
        raw["measurement_mode"] = "phase_aware"
    else:
        raw["measurement_mode"] = "magnitude_only"
    kind_id = correction_crossover_flow.relay_kind_from_raw(raw)
    driver_label = correction_crossover_flow.relay_driver_label(raw)

    def _open(
        client: RelayClient,
        base: str,
        capture_origin: str,
        return_url: str,
    ) -> RelayCapture:
        return correction_adapter.open_capture(
            client,
            build_crossover_sweep_spec(driver_label=driver_label),
            relay_base=base,
            capture_origin=capture_origin,
            return_url=return_url,
        )

    def _post_host_event(session_id: str, pull_token: str, payload: dict[str, Any]):
        # Bind the relay client fresh per post so the closure needs no client
        # capture; the orchestrator's client is the register client, reused here.
        from jasper.capture_relay.client import RelayClient
        from jasper.capture_relay.health import relay_registration_token_from_env

        client = RelayClient(
            relay_base,
            timeout=_RELAY_REGISTER_TIMEOUT_S,
            registration_token=relay_registration_token_from_env(),
        )
        return client.post_host_event(session_id, pull_token, payload)

    def _validate_capture(result: Any) -> None:
        block = _relay_device_calibration_block(calibration, result.device)
        if block is not None:
            raise ValueError(block)
        _assert_relay_level_identity(
            lease, level_identity, device=result.device
        )

    base_run_and_consume = correction_crossover_flow.build_crossover_relay_run_and_consume(
        raw,
        _run_async,
        _camilla,
        post_host_event=_post_host_event,
        # Server-side probe, re-evaluated fresh when the phone actually arms.
        blocking_phase=_crossover_blocking_phase,
        validate_capture=_validate_capture,
        prepare_play=lambda: lease.ensure_level_match_volume(
            lambda db: _camilla().set_volume_db(db, best_effort=False)
        ),
        restore_play=lambda: lease.restore_level_match_volume(
            lambda db: _camilla().set_volume_db(db, best_effort=False)
        ),
    )

    async def run_and_consume(client: RelayClient, pi_session: PiCaptureSession) -> None:
        try:
            await base_run_and_consume(client, pi_session)
        finally:
            # Idempotent backstop for failures before the armed/play window.
            await lease.restore_level_match_volume(
                lambda db: _camilla().set_volume_db(db, best_effort=False)
            )
    kind = RelayCaptureKind(
        label=f"crossover_sweep:{kind_id}",
        open=_open,
        run_and_consume=run_and_consume,
    )
    return {
        "relay": _run_relay_capture(
            kind,
            relay_base,
            return_url=_request_local_return_url(handler, "/correction/crossover/"),
        )
    }


def _maybe_restore_main_volume(sess, cam) -> None:
    """If autolevel ran and locked a measurement-friendly level,
    restore main_volume to the pre-autolevel value after the
    measurement workflow completes (apply or reset). This keeps the
    user's listening level intact across what otherwise would be a
    surprising "music is quieter now" experience.

    Idempotent — skips silently if no autolevel ran in this session.
    """
    # Runs inside the apply/reset `finally`, so the ENTIRE body is
    # best-effort — nothing here may raise, or it would mask the original
    # apply/reset error. The single guard covers the lazy import and the
    # autolevel-state reads too, not just the restore call. A failed restore
    # can strand the volume at the measurement level, but that is logged
    # loudly and is better than swallowing the real error.
    try:
        from jasper.correction.session import AutolevelStatus, SessionState

        restore_level_match = getattr(sess, "restore_level_match_volume", None)
        if callable(restore_level_match):
            async def _restore_level_match() -> bool:
                return await restore_level_match(
                    lambda db: cam.set_volume_db(db, best_effort=False)
                )

            if _run_async(_restore_level_match(), timeout=5.0):
                logger.info(
                    "restored main_volume after relay level-match workflow"
                )
                return

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
        # Don't restore mid-measurement. We run in apply()/reset()'s finally,
        # so this also fires when one was REJECTED from a transient state — a
        # stale /reset during a sweep, which the server refuses. The sweep
        # still needs the ramped level; dropping it underneath an active
        # measurement would corrupt the capture. Restore only once the
        # workflow has settled (idle / applied / verified / failed).
        if sess.state in {
            SessionState.PREPARING,
            SessionState.SWEEPING,
            SessionState.ANALYZING,
            SessionState.VERIFYING,
        }:
            return

        async def _restore() -> None:
            await cam.set_volume_db(
                al.original_main_volume_db, best_effort=True
            )

        _run_async(_restore(), timeout=5.0)
        logger.info(
            "restored main_volume to %.1f dB after autolevel workflow",
            al.original_main_volume_db,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "main_volume restore after autolevel workflow failed "
            "(volume may be left at the measurement level)",
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

    try:
        _run_async(sess.apply(_set, camilla_get_config=_get), timeout=15.0)
    finally:
        # Audio-safety: autolevel may have ramped main_volume well above the
        # listening level for measurement SNR. Restore it even if apply()
        # raised, so a failed apply never strands the speaker loud.
        _maybe_restore_main_volume(sess, cam)
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "config_path": (
            str(sess.config_path) if sess.config_path else None
        ),
    }


# --- P6: the tuning LLM surfaced in the flow (per-tap, confirm-gated) ---
#
# Each of these makes at most one PAID call, only on an explicit user tap
# (no polling — the envelope's `tuning_llm` block gates the button, but
# the paid call happens only here). The surface is hidden with a nudge
# when no OpenAI key is configured; if a request still arrives without a
# key, the advisor raises AdvisorModelError which surfaces as a 400.

def _require_tuning_key() -> None:
    from jasper.calibration_agent.key_provisioning import tuning_llm_available

    if not tuning_llm_available():
        raise RequestConflict(
            "the tuning assistant needs an OpenAI key — add one at /voice"
        )


# Monotonic timestamp of the last PAID tuning call attempt, shared across
# the two paid handlers. A mutable single-slot list so tests can reset it;
# guarded by a lock because the wizard is a ThreadingHTTPServer.
_tuning_paid_call_lock = threading.Lock()
_tuning_last_paid_call: list[float] = [0.0]


def _tuning_paid_call_gate() -> None:
    """Refuse a second PAID call within TUNING_LLM_MIN_INTERVAL_SEC.

    Stamped at ATTEMPT time (before the provider call), so concurrent or
    rapid-fire requests — a stuck client retry loop, a double-tap — cannot
    silently burn spend while one call is already in flight. The refusal
    is an honest 409 the panel shows, never a silent drop.
    """
    now = time.monotonic()
    with _tuning_paid_call_lock:
        since = now - _tuning_last_paid_call[0]
        if since < TUNING_LLM_MIN_INTERVAL_SEC:
            raise RequestConflict(
                "the tuning assistant just made a paid call — wait a "
                "moment and tap again"
            )
        _tuning_last_paid_call[0] = now


# Writes to the tuning-surface spend ledger — jasper-correction-web is its
# SOLE writer (never usage.db, which jasper-voice owns; a root-created file
# there wedges the voice ledger — the 2026-06-19 outage class). Serialised
# under this module lock, and each record opens a FRESH UsageStore
# (open → record → close). A process-global store would silently die on every
# handler thread except its creator's: this server is a ThreadingHTTPServer
# (one thread per TCP connection) and sqlite3 connections default to
# check_same_thread=True, so a store created on thread A raises
# ProgrammingError from thread B — which open_session's fail-soft catch
# swallows while the "recorded" event still logs (the review-proven
# one-row-of-three shape). Per-call open also makes the 0644 perms
# self-healing and removes any partial-init state.
_tuning_usage_lock = threading.Lock()


# Models already warned about as unpriced — once per process per model
# (mirrors the voice surface's once-per-daemon-start pricing.unpriced posture;
# this process is socket-activated with a 10-min idle exit, so the warning
# re-arms regularly without spamming per tap). Mutated under
# _tuning_usage_lock like the rest of the ledger state.
_tuning_unpriced_warned: set[str] = set()


def _warn_if_tuning_model_unpriced(model: str, overrides: dict[str, dict]) -> None:
    """WARN (once per process per model) when the tuning model has no rate.

    An operator ``JASPER_TUNING_LLM_MODEL`` override pointing at a model with
    no row in the bundled pricing (nor the /voice override file) records $0 —
    silently re-opening the exact hole this ledger closes. Mirror the voice
    surface's ``pricing.unpriced`` event with ``surface="tuning"`` so the
    journal carries the same signal. Caller MUST hold ``_tuning_usage_lock``
    and pass the same ``overrides`` the record itself prices with, so the
    warning and the recorded cost can never disagree."""
    from jasper.usage import pricing_for_model

    if not pricing_for_model(model, overrides=overrides).label.startswith(
        "unpriced:"
    ):
        return
    if model in _tuning_unpriced_warned:
        return
    _tuning_unpriced_warned.add(model)
    log_event(
        logger,
        "pricing.unpriced",
        model=model,
        surface="tuning",
        note=(
            "no rate available for the tuning model; its paid calls record "
            "$0 and the daily spend cap cannot bound tuning spend until a "
            "rate is set at /voice"
        ),
        level=logging.WARNING,
    )


def _heal_tuning_ledger_mode(tuning_db: str) -> None:
    """Ensure the ledger file is 0644 so the jasper-group readers
    (jasper-voice, jasper-web, doctor) can open it read-only.

    Self-healing on EVERY record, not just first create: a crash between
    sqlite-create (0600 under the root unit's UMask=0077) and the chmod, or a
    single failed chmod, must not permanently — and invisibly, since readers
    count an unopenable member as zero at DEBUG — break household-spend
    aggregation for the non-root surfaces. Root chmod'ing its own file is
    trivially cheap. Failure logs at WARNING: a wrong mode means other
    surfaces are under-counting household spend."""
    try:
        mode = os.stat(tuning_db).st_mode & 0o777
        if mode != 0o644:
            os.chmod(tuning_db, 0o644)
    except OSError:
        logger.warning(
            "tuning ledger mode heal failed for %s", tuning_db, exc_info=True,
        )


def _record_tuning_spend(out: dict[str, Any], usage_db: str) -> None:
    """Record one paid tuning call into the tuning ledger. FAIL-SOFT: any
    OSError / sqlite error logs ``tuning_spend.record_failed`` and returns —
    never raises, so a ledger problem cannot block the response the user is
    waiting on.

    The advisor's ``out["usage"]`` carries only aggregate token counts
    (``input_tokens`` / ``output_tokens``); gpt-5.4 has ONLY text rates, so
    pricing those aggregates as-is would charge the (absent) audio rate and
    record $0. Synthesize text-modality details (the research idiom) so the
    cost prices at the text rate — all-text with no cached refinement is the
    decided, conservative (slight over-estimate) simplification.
    """
    from jasper.calibration_agent.key_provisioning import resolve_tuning_model

    usage_in = out.get("usage") or {}
    try:
        input_tokens = int(usage_in.get("input_tokens") or 0)
        output_tokens = int(usage_in.get("output_tokens") or 0)
    except (TypeError, ValueError):
        input_tokens = output_tokens = 0
    # Text-modality details (note the SINGULAR "token" in the detail keys —
    # that is what usage.py's breakdown reads).
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_token_details": {"text_tokens": input_tokens},
        "output_token_details": {"text_tokens": output_tokens},
    }
    model = resolve_tuning_model()
    from jasper.usage import (
        UsageStore,
        load_pricing_overrides,
        tuning_usage_db_path,
    )

    tuning_db = tuning_usage_db_path(usage_db)
    # The /voice pricing editor's override file applies to tuning records too
    # (an operator who priced a custom tuning model there must get that rate),
    # and the unpriced warning below checks with the SAME overrides so the
    # warning and the recorded cost can never disagree.
    overrides = load_pricing_overrides()
    try:
        with _tuning_usage_lock:
            _warn_if_tuning_model_unpriced(model, overrides)
            # Fresh store per record — sqlite connections are thread-bound
            # (see the lock's comment block) and each handler request runs on
            # its own server thread. Open → record → close, all under the lock.
            store = UsageStore(tuning_db, pricing_overrides=overrides)
            try:
                _heal_tuning_ledger_mode(tuning_db)
                cost = store.record_background_usage(
                    provider="openai",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    usage=usage,
                )
                write_degraded = store.write_degraded
            finally:
                store._conn.close()
    except (OSError, sqlite3.Error) as e:
        log_event(
            logger,
            "tuning_spend.record_failed",
            level=logging.WARNING,
            error=type(e).__name__,
        )
        return
    if write_degraded:
        # The store's own fail-soft (open_session/close_session) swallowed a
        # write error, so NO row was persisted — the store already WARN-logged
        # the detail. Emit record_failed, never a "recorded" event for a row
        # that does not exist (the observability-lies half of the review's
        # Blocker 1).
        log_event(
            logger,
            "tuning_spend.record_failed",
            level=logging.WARNING,
            error="write_degraded",
        )
        return
    log_event(
        logger,
        "tuning_spend.recorded",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 6),
    )


def _spend_settings() -> tuple[str, float, float]:
    """The three spend knobs (usage_db, daily cap, safety multiplier), read
    FRESH per call: the wizard-owned SSOT file wins over ``os.environ``.

    The /voice spend-cap form writes ``JASPER_DAILY_SPEND_CAP_USD`` /
    ``_SAFETY_MULTIPLIER`` into ``/var/lib/jasper/voice_provider.env``.
    jasper-voice sources that file LAST in its EnvironmentFile chain, but this
    unit does not source it at all — and this socket-activated process is not
    restarted on a save (it can outlive one by its 10-min idle window), so a
    start-time env would go stale anyway. Overlaying the file's values over
    ``os.environ`` per call is the :mod:`jasper.voice.provider_state` idiom
    for exactly this stale-``os.environ`` trap, and matches what the running
    jasper-voice sees (file wins).

    Deliberately NOT ``Config.from_env()``: that hard-raises
    ``VoiceProviderNotConfigured`` when no voice provider is set, which would
    crash the tuning spend gate on an otherwise-valid box (OpenAI key + spend
    cap configured, voice provider not yet picked). Defaults are shared with
    ``Config`` via the ``jasper.usage`` constants (no mirrored literals);
    malformed values fall back to those defaults — deliberately SOFTER than
    ``Config.from_env``'s hard-raise, because a jasper.env typo must not 500
    the tuning surface."""
    from jasper.env_load import read_env_file_state
    from jasper.usage import (
        DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
        DEFAULT_DAILY_SPEND_CAP_USD,
        DEFAULT_USAGE_DB,
    )
    from jasper.voice.provider_state import PROVIDER_FILE

    # Path resolution mirrors provider_state._resolve_path: the path override
    # is a static deploy constant, so reading it from os.environ once per call
    # is fine — only the file's CONTENTS need the fresh read.
    provider_file = os.environ.get("JASPER_VOICE_PROVIDER_FILE", PROVIDER_FILE)
    file_state = read_env_file_state(provider_file)
    file_values = file_state.values if file_state.loaded else {}

    def _value(name: str) -> str:
        return (file_values.get(name) or os.environ.get(name) or "").strip()

    def _float(name: str, default: float) -> float:
        raw = _value(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    usage_db = _value("JASPER_USAGE_DB") or DEFAULT_USAGE_DB
    cap_usd = _float("JASPER_DAILY_SPEND_CAP_USD", DEFAULT_DAILY_SPEND_CAP_USD)
    multiplier = _float(
        "JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER",
        DEFAULT_DAILY_SPEND_CAP_SAFETY_MULTIPLIER,
    )
    return usage_db, cap_usd, multiplier


def _spend_usage_db() -> str:
    """The voice usage DB path (JASPER_USAGE_DB), so the tuning ledger sibling
    lands in the same directory the voice daemon uses."""
    return _spend_settings()[0]


def _tuning_spend_cap_gate() -> None:
    """Refuse a PAID tuning call when the household daily spend cap is reached.

    Built fresh per call over ``household_usage_reader(usage_db)`` — so tuning
    spend AND voice spend share one ceiling. When blocked, raise
    ``SpendCapExceeded`` (→ 429) with a rollover-worded message.

    Fail-OPEN on a read error, matching the rest of the spend accounting: if
    the cap can't read (SpendCap's store returns zero on an unreadable DB),
    ``allowed()`` is True — a ledger problem never blocks the user. We do NOT
    invent fail-closed here."""
    from jasper.usage import SpendCap, household_usage_reader

    usage_db, cap_usd, multiplier = _spend_settings()
    reader = household_usage_reader(usage_db)
    cap = SpendCap(reader, cap_usd, multiplier)
    if cap.allowed():
        return
    log_event(
        logger,
        "tuning_spend.cap_blocked",
        level=logging.WARNING,
        # Public surface only (no SpendCap._padded_spend): the raw summed
        # spend plus the multiplier lets the journal reader recompute the
        # padded comparison. One extra ledger read, blocked path only.
        spend_last_24h_usd=round(reader.spend_last_24h_usd(), 6),
        safety_multiplier=multiplier,
        cap_usd=cap_usd,
    )
    raise SpendCapExceeded(
        "daily spend cap reached — the tuning assistant will be "
        "available again after the daily rollover"
    )


def _handle_interpret(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /interpret: one paid call. Read-only "explain my room"."""
    from jasper.calibration_agent import correction_advisor, model_client

    _require_tuning_key()
    body = _read_json_body(handler)
    user_message = body.get("message")
    if user_message is not None and not isinstance(user_message, str):
        raise BadRequest("message must be a string")
    sess = _get_or_create_session()
    _tuning_paid_call_gate()
    _tuning_spend_cap_gate()
    try:
        out = correction_advisor.interpret(
            sess,
            user_message=user_message,
            timeout_sec=float(TUNING_LLM_TIMEOUT_SEC),
            max_output_tokens=model_client.TUNING_LLM_MAX_OUTPUT_TOKENS,
        )
    except model_client.AdvisorModelError as e:
        raise BadRequest(str(e)) from e
    _record_tuning_spend(out, _spend_usage_db())
    return out


def _handle_propose(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /propose: one paid call. The confirm-gated proposer.

    Nothing is applied here — proposals are validated + deterministically
    simulated, and returned with their sim verdict for the UI to surface
    for user confirmation. Applying happens only via /propose/apply.
    """
    from jasper.calibration_agent import correction_advisor, model_client

    _require_tuning_key()
    body = _read_json_body(handler)
    user_message = body.get("message")
    if user_message is not None and not isinstance(user_message, str):
        raise BadRequest("message must be a string")
    sess = _get_or_create_session()
    _tuning_paid_call_gate()
    _tuning_spend_cap_gate()
    try:
        out = correction_advisor.propose(
            sess,
            user_message=user_message,
            timeout_sec=float(TUNING_LLM_TIMEOUT_SEC),
            max_output_tokens=model_client.TUNING_LLM_MAX_OUTPUT_TOKENS,
        )
    except model_client.AdvisorModelError as e:
        raise BadRequest(str(e)) from e
    _record_tuning_spend(out, _spend_usage_db())
    return out


def _handle_propose_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /propose/apply: apply a confirmed correction proposal.

    NO paid call. The body carries the proposed ``correction_peqs`` (from
    a prior /propose response) and an explicit ``confirm: true``. The
    server RE-VALIDATES the set against the active strategy caps and
    RE-SIMULATES it (never trusting the client that it was accepted); only
    if the deterministic gate accepts AND the user confirmed does it
    populate ``session.peqs`` and route through the EXISTING apply path
    (the same simulate/headroom/re-clip apply any correction gets).
    """
    from jasper.calibration_agent import proposal_sim, response as advisor_response
    from jasper.correction.session import PEQJSON, SessionState

    body = _read_json_body(handler)
    if body.get("confirm") is not True:
        raise BadRequest("apply requires explicit confirm: true")
    raw_peqs = body.get("correction_peqs")
    if not isinstance(raw_peqs, list) or not raw_peqs:
        raise BadRequest("correction_peqs must be a non-empty list")

    sess = _get_or_create_session()
    if sess.state != SessionState.READY:
        raise RequestConflict(
            f"cannot apply a proposal from state {sess.state.value}; "
            "the correction must be in the review (READY) state"
        )

    # Re-validate schema + bounds against the ACTIVE strategy caps.
    from jasper.correction import strategy as _strategy
    strat = _strategy.resolve_correction_strategy(
        getattr(sess, "strategy_choice", None)
        or _strategy.DEFAULT_CORRECTION_STRATEGY_ID
    )
    bounds = strat.to_dict()
    packet = {
        "advisor_policy": {"allowed_actions": [
            {"id": "propose_correction_peq_adjustment", "allowed": True, "reasons": []},
        ]},
        "correction": {"strategy_bounds": bounds},
    }
    validation = advisor_response.validate_advisor_response(
        {
            "artifact_schema_version": advisor_response.RESPONSE_SCHEMA_VERSION,
            "kind": "jts_advisor_response",
            "action_plan": [{
                "type": advisor_response.ACTION_PROPOSE_CORRECTION_PEQ,
                "correction_peqs": raw_peqs,
                "rationale": "user-confirmed proposal re-check",
            }],
        },
        advisor_context=packet,
    )
    if not validation["accepted"]:
        return {
            "applied": False,
            "reason": "proposal failed re-validation against strategy caps",
            "issues": validation["issues"],
            "session_id": sess.session_id,
            "state": sess.state.value,
        }
    validated_peqs = validation["validated_action_plan"][0]["correction_peqs"]

    # Re-SIMULATE server-side; a client cannot assert acceptance for us.
    sim = proposal_sim.simulate_correction_proposal(
        validated_peqs,
        measured=getattr(sess, "measured_curve", None),
        baseline=getattr(sess, "position1_curve", None)
        or getattr(sess, "measured_curve", None),
        target=getattr(sess, "target_curve", None),
        max_total_boost_db=float(bounds.get("max_total_boost_db", 0.0)),
        f_high_hz=float(bounds.get("f_high_hz", 350.0)),
    )
    if not sim.accepted:
        return {
            "applied": False,
            "reason": "proposal rejected by the deterministic simulation gate",
            "simulation": sim.to_dict(),
            "session_id": sess.session_id,
            "state": sess.state.value,
        }
    if sim.acceptance is None:
        # Fail-closed at the apply seam: the P4 acceptance judge could not
        # run (baseline/target curves were absent), so the promise "every
        # applied proposal is judged by the same acceptance evaluator" can't
        # hold. The propose PREVIEW stays lenient by design (a ring+headroom
        # only preview is honest there); applying without the judge is not.
        return {
            "applied": False,
            "code": "missing_acceptance_basis",
            "reason": (
                "proposal could not be judged against the room baseline "
                "(no baseline/target curves for the acceptance evaluator); "
                "not applying"
            ),
            "simulation": sim.to_dict(),
            "session_id": sess.session_id,
            "state": sess.state.value,
        }

    # Deterministic gate + explicit confirm both passed: swap in the
    # proposed filters and route through the SAME apply path any
    # correction uses (which re-clips headroom at emit).
    log_event(
        logger,
        "correction.tuning_apply",
        session_id=sess.session_id,
        filter_count=len(validated_peqs),
        sim_verdict=(sim.acceptance or {}).get("verdict"),
    )
    sess.peqs = [
        PEQJSON(freq_hz=p["freq_hz"], q=p["q"], gain_db=p["gain_db"])
        for p in validated_peqs
    ]
    result = _handle_apply(handler)
    # Derive success from the actual outcome, never stamp it: session.apply
    # deliberately swallows the CamillaDSP-rejected-reload failure (state ->
    # FAILED, no exception raised), and claiming "applied" while the speaker
    # kept its previous sound would be a dishonest success message.
    result["applied"] = result.get("state") == "applied"
    if not result["applied"]:
        result["reason"] = "couldn't apply — the speaker kept its previous sound"
    result["simulation"] = sim.to_dict()
    return result


def _reset_accepts_target_config_path(reset_fn: Any) -> bool:
    try:
        params = inspect.signature(reset_fn).parameters
    except (TypeError, ValueError):
        return True
    if "target_config_path" in params:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _handle_reset(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /reset: cancel a measurement or strip active room correction.

    If a measurement is in progress (or failed before apply), restore the graph
    that was active before `/start`. Once a correction is applied, reset means
    "remove Layer B" — re-emit the current graph with room PEQs cleared while
    preserving topology-owned speaker DSP and current preference EQ.
    """
    sess = _get_or_create_session()
    cam = _camilla()

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    try:
        target = _resolve_reset_target(sess, cam)
        reset_kwargs = (
            {"target_config_path": target}
            if _reset_accepts_target_config_path(sess.reset)
            else {}
        )
        _run_async(sess.reset(_set, **reset_kwargs), timeout=15.0)
    finally:
        # Audio-safety: restore the pre-autolevel listening level even if
        # reset() raised (see _handle_apply).
        _maybe_restore_main_volume(sess, cam)
    return {"session_id": sess.session_id, "state": sess.state.value}


def _pre_measurement_restore_target(sess: Any) -> Path | None:
    """Prior graph to restore when reset is cancelling this measurement."""
    state_value = getattr(getattr(sess, "state", None), "value", None)
    if state_value in {"idle", "applied", "verified"}:
        return None
    prior = getattr(sess, "pre_measurement_config_path", None)
    return Path(prior) if prior else None


def _resolve_reset_target(sess: Any, cam: Any) -> Path:
    """Resolve the graph to restore for a reset / auto-revert.

    The single source of truth for "what should the speaker load when we undo
    room correction," shared by ``POST /reset`` (user-driven) and the P4
    confirmed-regression auto-revert (deterministic). If a measurement is
    mid-flight, restore the pre-``/start`` graph; once a correction is applied
    or verified, re-emit the current topology with room PEQs cleared (Layer B
    removed, speaker DSP + preference EQ preserved). A re-emit failure falls
    back to the safe base graph so an undo never strands the speaker.
    """
    from jasper.correction.runtime_safety import reset_config_path

    cfg = getattr(sess, "cfg", None)
    base_config_path = getattr(
        cfg,
        "base_config_path",
        Path("/etc/camilladsp/outputd-cutover.yml"),
    )
    target = _pre_measurement_restore_target(sess)
    if target is None:
        try:
            target = _run_async(
                _write_no_room_correction_config(sess, cam),
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "reset/auto-revert: no-room re-emit failed; falling back "
                "to safe graph",
            )
            target = reset_config_path(base_config_path)
    return target


def _maybe_auto_revert(sess: Any) -> bool:
    """Perform the P4 auto-revert when the verdict is a confirmed regression.

    Reads ``sess.acceptance_verdict``; only ``revert`` acts. Resolves the same
    reset target ``/reset`` uses and drives the session's ``auto_revert`` (which
    rides the existing ``reset()`` reversal). Returns True when a rollback ran.
    Best-effort: an auto-revert failure is logged and leaves the correction
    applied with the ``revert`` verdict still visible — the household can undo
    manually — rather than 500-ing the verify upload response. reset() itself
    fails the session loudly on a CamillaDSP rejection, so a failed revert is
    never silent.

    Failure honesty: when the attempt dies BEFORE the session could record an
    outcome (target-resolution raise, the 15 s response timeout), a "failed"
    outcome is stamped here so the result screen says the correction is STILL
    APPLIED. The stamp never overwrites a recorded outcome, and on a response
    timeout the still-running auto_revert coroutine records the real result
    when reset() finishes — a later "ok" overwrites this "failed", so the
    envelope converges on the truth.
    """
    if getattr(sess, "acceptance_verdict", None) != "revert":
        return False
    cam = _camilla()

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    try:
        target = _resolve_reset_target(sess, cam)
        revert_kwargs = (
            {"target_config_path": target}
            if _auto_revert_accepts_target(sess.auto_revert)
            else {}
        )
        return bool(
            _run_async(sess.auto_revert(_set, **revert_kwargs), timeout=15.0)
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "P4 auto-revert failed; correction left applied for manual undo",
        )
        if getattr(sess, "auto_revert_outcome", None) is None:
            sess.auto_revert_outcome = {"result": "failed", "at": time.time()}
        return False


def _auto_revert_accepts_target(revert_fn: Any) -> bool:
    """True when ``auto_revert`` accepts a ``target_config_path`` kwarg.

    Mirrors ``_reset_accepts_target_config_path`` so a duck-typed session
    stand-in without the kwarg still works (the session falls back to its
    captured pre-apply config).
    """
    try:
        params = inspect.signature(revert_fn).parameters
    except (TypeError, ValueError):
        return True
    if "target_config_path" in params:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


async def _write_no_room_correction_config(sess: Any, cam: Any) -> Path:
    """Emit the current graph with room correction cleared.

    For passive/full-range graphs this is the ordinary sound config. For active
    baselines it is still an active graph; content-based status/carrier checks
    keep that safe even though the durable filename is `sound_current.yml`.
    """

    from jasper.correction.runtime_safety import assert_correction_graph_safe
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.camilla_yaml import sound_config_path
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.profile import load_profile

    cfg = getattr(sess, "cfg", None)
    config_dir = Path(
        getattr(cfg, "config_dir", Path("/var/lib/camilladsp/configs"))
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    current_path = await cam.get_config_file_path(best_effort=False)
    if not current_path:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    out_path = sound_config_path(config_dir)
    carrier = carrier_for_loaded_config(current_path, config_dir=config_dir)
    profile = load_profile()
    result = carrier.reemit(
        profile,
        room_peqs=[],
        out_path=out_path,
        profile_id=f"correction-reset-{time.time_ns()}",
        fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
    )
    assert_correction_graph_safe(result.yaml)
    log_event(
        logger,
        "correction.reset_no_room_config",
        current=str(current_path),
        candidate=str(out_path),
        room_peqs=result.room_peq_count,
    )
    return out_path


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

        def _serve_json_route(
            self, label: str, handler_fn: Callable[[BaseHTTPRequestHandler], dict[str, Any]],
        ) -> None:
            """Shared JSON GET-route wrapper: any handler failure surfaces
            as a 500 JSON error instead of a stack-trace page or a dead
            request thread — the poll posture /status, /envelope, and
            /sessions share (one wrapper so the blanket net isn't
            re-declared per route)."""
            try:
                self._send_json(handler_fn(self))
            except Exception as e:  # noqa: BLE001 — route-level 500 net
                logger.exception("%s failed", label)
                self._send_json({"error": str(e)}, status=500)

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

        def _dispatch_balance(self, path: str) -> None:
            """POST /balance/* — the pair-balance walkthrough
            (balance_flow). /start additionally requires the
            correction session to be idle: both flows open
            measurement_window, and this is where the correction side
            of the mutual exclusion lives (the balance side lives in
            _reserve_start_slot)."""
            from . import balance_flow

            def _schedule(coro):
                return asyncio.run_coroutine_threadsafe(
                    coro, _ensure_loop())

            try:
                if path == "/balance/start":
                    with _session_lock:
                        blocked = (
                            "starting" if _start_in_progress
                            else _active_state_for_session(_session)
                        )
                    if blocked is not None:
                        self._send_json(
                            {"ok": False, "error": (
                                "a room-correction session is active "
                                f"({blocked})"
                            )},
                            status=HTTPStatus.CONFLICT)
                        return
                    payload, status = balance_flow.handle_start(
                        cfg["hostname"], _schedule)
                elif path == "/balance/ramp":
                    payload, status = balance_flow.handle_ramp(
                        self, _run_async, _schedule)
                elif path == "/balance/meter":
                    payload, status = balance_flow.handle_meter(self)
                elif path == "/balance/lock":
                    payload, status = balance_flow.handle_lock(self)
                elif path == "/balance/stop":
                    payload, status = balance_flow.handle_stop()
                elif path == "/balance/apply":
                    payload, status = balance_flow.handle_apply(self)
                else:  # /balance/reset
                    payload, status = balance_flow.handle_stop()
                self._send_json(payload, status=int(status))
            except Exception as e:  # noqa: BLE001
                logger.exception("%s failed", path)
                self._send_json({"ok": False, "error": str(e)},
                                status=500)

        def _dispatch_sync(self, path: str) -> None:
            """POST /sync/* — stereo-pair acoustic timing walkthrough."""
            from . import sync_flow

            def _schedule(coro):
                return asyncio.run_coroutine_threadsafe(
                    coro, _ensure_loop())

            # The relay capture has different response semantics (a dict +
            # ValueError → client error), and MUST be handled here inside the
            # /sync/ prefix dispatch — the main do_POST ladder never sees /sync/*.
            if path == "/sync/relay-capture":
                try:
                    self._send_json(_handle_sync_relay_capture(self))
                except ValueError as e:
                    self._send_client_error(str(e))
                return

            try:
                if path == "/sync/start":
                    with _session_lock:
                        blocked = (
                            "starting" if _start_in_progress
                            else _active_state_for_session(_session)
                        )
                    if blocked is not None:
                        self._send_json(
                            {"ok": False, "error": (
                                "a room-correction session is active "
                                f"({blocked})"
                            )},
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                    payload, status = sync_flow.handle_start(
                        cfg["hostname"], _schedule)
                elif path == "/sync/play":
                    payload, status = sync_flow.handle_play(
                        _run_async, _schedule)
                elif path == "/sync/analyze":
                    try:
                        body = _read_wav_body(self, max_bytes=2 * 1024 * 1024)
                    except BadRequest as e:
                        self._send_json(
                            {"ok": False, "error": str(e)},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    payload, status = sync_flow.handle_analyze(body)
                elif path == "/sync/apply":
                    payload, status = sync_flow.handle_apply(self)
                else:
                    payload, status = sync_flow.handle_stop()
                self._send_json(payload, status=int(status))
            except Exception as e:  # noqa: BLE001
                logger.exception("%s failed", path)
                self._send_json({"ok": False, "error": str(e)}, status=500)

        def _dispatch_crossover(self, path: str) -> None:
            """POST /crossover/* — secure active-crossover measurement."""
            from . import correction_crossover_flow

            try:
                if path in {
                    "/crossover/driver-capture",
                    "/crossover/summed-capture",
                }:
                    try:
                        body = _read_wav_body(
                            self,
                            max_bytes=MAX_CROSSOVER_WAV_BODY_BYTES,
                        )
                    except BadRequest as e:
                        self._send_json(
                            {"ok": False, "error": str(e)},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    if path == "/crossover/driver-capture":
                        payload, status = correction_crossover_flow.handle_driver_capture(
                            self,
                            body,
                        )
                    else:
                        payload, status = correction_crossover_flow.handle_summed_capture(
                            self,
                            body,
                        )
                    self._send_json(payload, status=int(status))
                    return

                if path == "/crossover/relay-capture":
                    # The relay handler reads its own JSON body (mirrors the room
                    # /relay/capture and /sync/relay-capture handlers).
                    self._send_json(_handle_crossover_relay_capture(self))
                    return

                if path == "/crossover/level-match":
                    self._send_json(_handle_crossover_relay_level_match(self))
                    return

                if path == "/crossover/apply":
                    raw = _read_json_body(self)
                    payload, status = correction_crossover_flow.handle_apply(
                        raw,
                        _run_async,
                        _camilla,
                    )
                    self._send_json(payload, status=int(status))
                    return

                raw = _read_json_body(self)
                if path == "/crossover/driver-test":
                    payload, status = correction_crossover_flow.handle_driver_test(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_crossover_blocking_phase(),
                    )
                elif path == "/crossover/driver-confirm":
                    payload, status = correction_crossover_flow.handle_driver_confirm(
                        raw,
                        _run_async,
                        _camilla,
                    )
                elif path == "/crossover/driver-abort":
                    payload, status = correction_crossover_flow.handle_driver_abort(
                        _run_async,
                        _camilla,
                    )
                elif path == "/crossover/summed-test":
                    payload, status = correction_crossover_flow.handle_summed_test(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_crossover_blocking_phase(),
                    )
                elif path == "/crossover/driver-capture-sweep":
                    payload, status = correction_crossover_flow.handle_driver_capture_sweep(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_crossover_blocking_phase(),
                    )
                else:
                    payload, status = correction_crossover_flow.handle_summed_capture_sweep(
                        raw,
                        _run_async,
                        _camilla,
                        blocking_phase=_crossover_blocking_phase(),
                    )
                self._send_json(payload, status=int(status))
            except BadRequest as e:
                self._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except ValueError as e:
                self._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except (OSError, RuntimeError, TypeError) as e:
                logger.exception("%s failed", path)
                self._send_json({"ok": False, "error": str(e)}, status=500)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path not in {
                "/",
                "/room",
                "/healthz",
                "/status",
                "/envelope",
                "/sessions",
                "/session-report",
                "/calibration/models",
                "/crossover",
                "/crossover/status",
                "/crossover/envelope",
                "/bass",
                "/bass/status",
                "/balance",
                "/balance/status",
                "/sync",
                "/sync/status",
            }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_read_request(self):
                return
            if bonded_follower_active() and path in _FOLLOWER_DELEGATED_PAGE_PATHS:
                ctx = begin_request(self)
                self._send_html(_render_follower_page(
                    cfg["hostname"], ctx["csrf_token"],
                ))
                return
            if path in {"/", "/room"}:
                ctx = begin_request(self)
                self._send_html(_render_page(
                    cfg["hostname"], ctx["csrf_token"], ctx["flash"],
                ))
                return
            if path == "/crossover":
                from . import correction_crossover_flow
                ctx = begin_request(self)
                self._send_html(
                    correction_crossover_flow.render_page(
                        cfg["hostname"], ctx["csrf_token"],
                    )
                )
                return
            if path == "/crossover/status":
                from . import correction_crossover_flow
                try:
                    payload, status = correction_crossover_flow.handle_status(
                        relay=_get_relay_capture_for(
                            "crossover_sweep:", "level_ramp:crossover"
                        ),
                    )
                    self._send_json(payload, status=int(status))
                except (OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.exception("/crossover/status failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/crossover/envelope":
                from . import correction_crossover_flow
                try:
                    payload, status = correction_crossover_flow.handle_envelope(
                        relay=_get_relay_capture_for(
                            "crossover_sweep:", "level_ramp:crossover"
                        ),
                    )
                    self._send_json(payload, status=int(status))
                except (OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.exception("/crossover/envelope failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/bass":
                from . import correction_bass_flow
                ctx = begin_request(self)
                self._send_html(
                    correction_bass_flow.render_page(
                        cfg["hostname"], ctx["csrf_token"],
                    )
                )
                return
            if path == "/bass/status":
                from . import correction_bass_flow
                try:
                    payload, status = correction_bass_flow.handle_status()
                    self._send_json(payload, status=int(status))
                except (OSError, RuntimeError, TypeError, ValueError) as e:
                    logger.exception("/bass/status failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/balance":
                from . import balance_flow
                ctx = begin_request(self)
                self._send_html(
                    balance_flow.render_page(ctx["csrf_token"]))
                return
            if path == "/balance/status":
                from . import balance_flow
                try:
                    self._send_json(balance_flow.handle_status())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/balance/status failed")
                    self._send_json({"error": str(e)}, status=500)
                return
            if path == "/sync":
                from . import sync_flow
                ctx = begin_request(self)
                self._send_html(sync_flow.render_page(ctx["csrf_token"]))
                return
            if path == "/sync/status":
                from . import sync_flow
                try:
                    self._send_json(sync_flow.handle_status())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/sync/status failed")
                    self._send_json({"error": str(e)}, status=500)
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
                self._serve_json_route("/status", _handle_status)
                return
            if path == "/envelope":
                self._serve_json_route("/envelope", _handle_envelope)
                return
            if path == "/sessions":
                self._serve_json_route("/sessions", _handle_sessions)
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
            if path not in _POST_ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if bonded_follower_active() and not path.startswith("/crossover/"):
                log_event(
                    logger,
                    "correction.follower_content_dsp_blocked",
                    path=path,
                )
                self._send_json(
                    {
                        "error": (
                            "room correction is controlled on the pair "
                            "leader while this speaker is a follower"
                        ),
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return
            if path.startswith("/balance/"):
                self._dispatch_balance(path)
                return
            if path.startswith("/sync/"):
                self._dispatch_sync(path)
                return
            if path.startswith("/crossover/"):
                self._dispatch_crossover(path)
                return
            try:
                if path == "/start":
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.sound.graph_carrier import CarrierCannotHostEq
                    try:
                        self._send_json(_handle_start(self))
                    except (CorrectionRuntimeSafetyError, CarrierCannotHostEq) as e:
                        self._send_client_error(
                            str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
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
                    from jasper.audio_measurement import quality

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
                if path == "/relay/capture":
                    try:
                        self._send_json(_handle_relay_capture(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/relay/level-match":
                    try:
                        self._send_json(_handle_relay_level_match(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/relay/verify":
                    try:
                        self._send_json(_handle_relay_verify(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    return
                if path == "/calibration/fetch":
                    try:
                        self._send_json(_handle_calibration_fetch(self))
                    except ValueError as e:
                        self._send_client_error(str(e))
                    except Exception as e:  # noqa: BLE001
                        from jasper.audio_measurement.calibration import (
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
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.sound.graph_carrier import CarrierCannotHostEq
                    try:
                        self._send_json(_handle_apply(self))
                    except (CarrierCannotHostEq, CorrectionRuntimeSafetyError) as e:
                        self._send_client_error(
                            str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    return
                if path == "/reset":
                    # Local import keeps session/numpy off the socket-activated
                    # process's import path (mirrors the other handlers).
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.correction.session import SessionBusyError
                    try:
                        self._send_json(_handle_reset(self))
                    except CorrectionRuntimeSafetyError as e:
                        self._send_client_error(
                            str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
                    except SessionBusyError as e:
                        # Rejected because a sweep/analysis is mid-flight — a
                        # state conflict (409), not a server error (500).
                        self._send_client_error(str(e), status=409)
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
                if path == "/interpret":
                    try:
                        self._send_json(_handle_interpret(self))
                    except BadRequest as e:
                        self._send_client_error(str(e))
                    except SpendCapExceeded as e:
                        self._send_client_error(
                            str(e), status=HTTPStatus.TOO_MANY_REQUESTS,
                        )
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/propose":
                    try:
                        self._send_json(_handle_propose(self))
                    except BadRequest as e:
                        self._send_client_error(str(e))
                    except SpendCapExceeded as e:
                        self._send_client_error(
                            str(e), status=HTTPStatus.TOO_MANY_REQUESTS,
                        )
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    return
                if path == "/propose/apply":
                    from jasper.correction.runtime_safety import (
                        CorrectionRuntimeSafetyError,
                    )
                    from jasper.sound.graph_carrier import CarrierCannotHostEq
                    try:
                        self._send_json(_handle_propose_apply(self))
                    except BadRequest as e:
                        self._send_client_error(str(e))
                    except RequestConflict as e:
                        self._send_client_error(str(e), status=409)
                    except (CarrierCannotHostEq, CorrectionRuntimeSafetyError) as e:
                        self._send_client_error(
                            str(e),
                            status=HTTPStatus.UNPROCESSABLE_ENTITY,
                        )
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
        description="HTTPS correction measurement hub at /correction/ for the JTS speaker",
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
