# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-wake-corpus-web — Browser-based corpus recording UI.

Open-ended recording (click to start, click to stop) for building the
gold corpus described in `docs/HANDOFF-wake-training-experiment.md`
Phase 0b. Much better operator UX than running `jasper-wake-enroll`
30 times across 6 conditions with terminal countdowns.

Mechanics:
  - Single-file HTML+JS frontend (no external assets)
  - stdlib `http.server` backend on a configurable port (default 8782)
  - Recording happens on the server via UdpMicCapture — same UDP
    streams (`:9876` AEC ON + `:9877` raw + `:9878` DTLN if present)
    that `jasper-wake-enroll` uses
  - Sync HTTP handlers bridge to an asyncio loop running in a
    background daemon thread via `run_coroutine_threadsafe`
  - Click-start opens captures, streams frames into per-leg buffers;
    click-stop cancels the streaming, writes WAVs to disk in the
    same layout `jasper-wake-enroll` uses (so downstream tools work
    without modification), records metadata in a per-session JSON
    sidecar

What this preserves vs `jasper-wake-enroll`:
  - File naming convention (`enroll_<member>_<session>_<seq>.aec-<leg>.wav`)
  - Quadrant directory layout (`aec_{on,off,dtln}_{nomusic,music}/`)
  - The need for jasper-voice to be stopped (UDP ports must be free)

What this adds:
  - Per-clip start/stop timestamps + duration in JSON sidecar
  - Per-clip distance tag (near/mid/far) — stored in JSON only, NOT
    in filenames, to keep the directory layout compatible with the
    extract/score/review pipeline. Training tools that want distance-
    aware splits can JOIN via the JSON.
  - In-browser playback (HTML5 audio) for instant verification
  - One-click delete (hard-removes WAVs + marks deleted in metadata)
  - Corpus test-mode control that stops jasper-voice, applies selected
    optional bridge outputs, and restores the production-light state on
    exit

Usage:
  sudo /opt/jasper/.venv/bin/jasper-wake-corpus-web
  # binds loopback (127.0.0.1:8782) by default; reach it via nginx at
  # http://jts.local/wake-corpus/ from any browser on the LAN

  # the bind is overridable (it already defaults to loopback):
  sudo jasper-wake-corpus-web --host 127.0.0.1 --port 8782

Module layout: this file is a thin HTTP adapter. The recording engine
(``RecordingBackend`` + capture task + clip/metadata writing + test-mode
marker recovery) lives in ``jasper.wake_corpus.recording_backend``; the
bridge env / leg-plan / systemctl + enter/exit corpus-test-mode layer
lives in ``jasper.wake_corpus.bridge_session``. Both are re-exported below
so every ``wake_corpus_setup.NAME`` keeps resolving for existing callers.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import secrets
import subprocess  # used directly by the adapter; tests patch wake_corpus_setup.subprocess
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from jasper.control.restart_broker import manage_units
from jasper.log_event import log_event

# CONDITIONS / DISTANCES come from the shared single source of truth
# (jasper.wake_conditions); test_wake_conditions asserts the recorder
# re-exports the SAME singleton objects (no local redefinition).
from jasper.wake_conditions import CONDITIONS, DISTANCES  # noqa: F401
from jasper.aec_sweep import (  # noqa: F401 - re-exported for tests/consumers
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    AEC3_SWEEP_VARIANTS,
    Aec3SweepConfigError,
    USB_AEC3_CORPUS_LABEL,
    USB_AEC3_SWEEP_BASELINE_LABEL,
    variant_metadata,
)
# Reuse audio I/O + systemctl helpers from the CLI. Single source of
# truth for the WAV format + the "stop jasper-voice to free UDP" dance.
from jasper.cli.wake_enroll import (  # noqa: F401 - re-exported for tests/consumers
    CHANNELS,
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
    VOICE_UNIT,
    require_root,
    write_wav,
)
from jasper.wake_ports import (  # noqa: F401 - re-exported for tests/consumers
    DEFAULT_AEC_CHIP_AEC_150_PORT,
    DEFAULT_AEC_CHIP_AEC_210_PORT,
    DEFAULT_AEC_DTLN_PORT,
    DEFAULT_AEC_OFF_PORT,
    DEFAULT_AEC_ON_PORT,
    DEFAULT_AEC_REF_PORT,
    DEFAULT_AEC_RAW0_PORT,
    DEFAULT_AEC_XVF_RAW0_DTLN_PORT,
    DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT,
    DEFAULT_AEC3_SWEEP_PORTS,
    DEFAULT_AEC_USB_DTLN_PORT,
    DEFAULT_AEC_USB_RAW_PORT,
    DEFAULT_AEC_USB_WEBRTC_PORT,
    build_ports,
)
from jasper.audio_profile_state import MicProbe  # noqa: F401 - re-exported

# Recording engine + bridge orchestration now live in the package.
# `bridge_session` is imported as a module so the HTTP handler can call
# the test-patchable systemctl/voice/bridge seams through it — a single
# `monkeypatch.setattr(bridge_session, NAME, ...)` then covers both the
# handler's call and any inter-function call inside the bridge layer.
from jasper.wake_corpus import bridge_session
from jasper.wake_corpus.bridge_session import (  # noqa: F401 - re-exported
    AEC3_SWEEP_LEGS,
    AEC_INIT_UNIT,
    AEC_MODE_PATH,
    AUDIO_CONTEXT_SCHEMA_VERSION,
    AUDIO_VALIDATION_ARTIFACT_PATH,
    BASE_LEGS,
    BRIDGE_CORPUS_ENV_PATH,
    BRIDGE_CORPUS_OUTPUT_VARS,
    BRIDGE_OUTPUT_LABELS,
    BRIDGE_RESTART_TIMEOUT_SEC,
    BRIDGE_STATS_PATH,
    BRIDGE_UNIT,
    CAPTURE_PLAN_SCHEMA_VERSION,
    CHIP_AEC_LEGS,
    CHIP_AEC_PROFILE_BASE_LEGS,
    CORPUS_PROFILES,
    DEFAULT_CHIP_REF_BUFFER_FRAMES,
    DEFAULT_CHIP_REF_PCM,
    DEFAULT_CHIP_REF_PERIOD_FRAMES,
    DEFAULT_CHIP_REF_SAMPLE_RATE,
    DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
    DEFAULT_USB_MIC_DEVICE,
    DEFAULT_USB_MIXER_CARD,
    DTLN_LEG,
    LEG_LABELS,
    LEGACY_AEC3_SWEEP_LEGS,
    LEGS,
    OUTPUTD_REF_UDP_PORT,
    OUTPUTD_REF_UDP_TARGET,
    OUTPUTD_UNIT,
    PROFILE_CHIP_AEC_COMPARISON,
    PROFILE_STANDARD,
    RAW0_LEG,
    SYSTEM_ENV_PATH,
    USB_AGC_CONTROL,
    USB_CORPUS_LEGS,
    USB_DTLN_LEG,
    XVF_RAW0_DTLN_LEG,
    XVF_RAW0_WEBRTC_AEC3_LEG,
    _enabled_legs_from_metadata,
    _parse_amixer_bool,
    _session_aec3_sweep_source,
    _validation_artifact_summary,
    aec_bridge_active,
    bridge_output_status,
    build_capture_health,
    build_capture_plan,
    build_session_audio_context,
    chip_aec_config_metadata,
    disable_bridge_corpus_outputs,
    enter_corpus_test_mode,
    exit_corpus_test_mode,
    missing_bridge_outputs_for_session,
    read_bridge_stats_snapshot,
    restart_aec_bridge,
    restart_unit,
    set_bridge_outputs_for_plan,
    set_bridge_outputs_for_session,
    set_voice_daemon_state,
    usb_mic_status,
    validate_active_capture_plan,
    voice_daemon_active,
)
from jasper.wake_corpus.recording_backend import (  # noqa: F401 - re-exported
    ACTIVE_SESSION_MARKER,
    DEFAULT_METADATA_SUBDIR,
    DEFAULT_OUTPUT_DIR,
    MAX_RECORDING_DURATION_SEC,
    METADATA_SCHEMA_VERSION,
    RESUME_WINDOW_SEC,
    MIC_MUTED_MESSAGE,
    TEST_MODE_MARKER,
    TEST_MODE_STALE_SEC,
    ClipMetadata,
    MicMutedError,
    RecordingBackend,
    RecordingTask,
    StateError,
    compute_rms_dbfs,
)

# Canonical design system. The recorder renders its document shell through
# `canonical_page()` (the shared /assets/app.css look) with `canonical_header()`
# for the top bar — the same seam every migrated wizard reuses. Page behaviour
# lives in the static ES module at /assets/wake-corpus/js/main.js (loaded as
# `type="module"`), and the bespoke recorder visuals live in the page
# stylesheet at /assets/wake-corpus/wake-corpus.css; the "← Home" link and the
# confirm dialog are provided by the canonical header and the shared
# dialog.js module respectively, so this page no longer embeds the old
# one-page chrome or dialog snippets.
from jasper.web._common import (
    canonical_header,
    canonical_page,
    guard_read_request,
    json_island,
    send_json_response,
    toggle_html,
)

logger = logging.getLogger("jasper-wake-corpus-web")


@dataclass(frozen=True)
class CaptureOption:
    control_id: str
    title: str
    hint: str
    checked: bool = False
    hidden: bool = False


CAPTURE_OPTIONS: tuple[CaptureOption, ...] = (
    CaptureOption(
        "include-chip-aec-profile",
        "Chip AEC comparison",
        "150/210 beams, raw0, AEC3, reference.",
        checked=True,
    ),
    CaptureOption(
        "include-raw-mic-0",
        "Raw mic 0",
        "Unprocessed chip channel.",
        hidden=True,
    ),
    CaptureOption(
        "include-xvf-raw0-dtln",
        "XVF raw0 DTLN",
        "Neural cleanup on raw0.",
    ),
    CaptureOption(
        "include-dtln",
        "XVF DTLN",
        "Neural cleanup on ASR beam.",
        hidden=True,
    ),
    CaptureOption(
        "include-aec3-sweep",
        "USB AEC3 sweep",
        "Delay variants for USB mic.",
        hidden=True,
    ),
    CaptureOption(
        "include-usb-mic",
        "USB mic + reference",
        "Raw USB, AEC3, reference.",
    ),
    CaptureOption(
        "include-usb-dtln",
        "USB DTLN",
        "Neural cleanup on USB mic.",
    ),
)


# Default bind. Loopback by default for safety; CLI flag opens to LAN.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8782

# CSRF header name. Matches common JS framework conventions; the
# embedded JS reads `<meta name="csrf-token">` and sends this header
# on every mutating request.
CSRF_HEADER = "X-CSRF-Token"


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    backend: RecordingBackend
    csrf_token: str

    # ----- helpers --------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, body: Any, status: int = 200) -> None:
        send_json_response(self, body, status=status)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON body: {e}") from e

    def _check_csrf(self) -> bool:
        """Verify the X-CSRF-Token header matches the server token.

        Returns True if valid, False (and sends 403) otherwise. The
        token is embedded in the served HTML page via a meta tag; the
        page's JS reads it and sends it on every mutating request.
        Defense against a malicious cross-origin site triggering
        recordings or daemon toggles from the operator's browser.

        Uses `secrets.compare_digest` for timing-safe comparison
        (defense-in-depth — the attacker probably can't observe
        latency in practice, but it's a one-line free win).
        """
        header_token = self.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(header_token, self.csrf_token):
            self._send_error_json(
                403,
                f"missing or invalid {CSRF_HEADER} header — reload "
                "the page to refresh the token",
            )
            return False
        return True

    # ----- routing --------------------------------------------------
    #
    # do_GET / do_POST dispatch via the _GET_ROUTES / _POST_ROUTES tables
    # (exact path -> handler-method name) defined at the bottom of this
    # class; do_DELETE and the few <id>-prefix routes (e.g. GET
    # /api/clip/<id>/wav) are matched inline since they aren't exact
    # paths. Each table entry's handler holds the exact body the inlined
    # `if path == ...` branch had — moved into a named method, logic
    # unchanged. Mirrors the route-table pattern in
    # jasper/control/server.py.
    #
    # ORDERING IS LOAD-BEARING and preserved from the inline form:
    #   - GET is read-guarded but NOT CSRF-protected (read-only). POST +
    #     DELETE check CSRF FIRST (before any body read or table lookup).
    #   - do_POST reads + parses the JSON body AFTER the CSRF check, then
    #     dispatches; each POST handler takes the parsed `body`.
    #   - Prefix routes that don't fit an exact-match table
    #     (`/api/clip/<id>/wav` GET, `/api/clip|session/<id>` DELETE) are
    #     handled explicitly, in the same position as before.
    #   - Unknown paths 404 LAST (table miss / prefix miss).

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        clip_wav_route = path.startswith("/api/clip/") and path.endswith("/wav")
        level_route = path == "/api/recording/level"

        if not (
            path == "/"
            or path in self._GET_ROUTES
            or clip_wav_route
            or level_route
        ):
            self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")
            return
        if not guard_read_request(self):
            return

        if path == "/":
            html_text = _render_index_html(self.csrf_token)
            data = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        handler_name = self._GET_ROUTES.get(path)
        if handler_name is not None:
            getattr(self, handler_name)()
            return

        if clip_wav_route:
            self._serve_wav(path, url)
            return

        if level_route:
            self._serve_level_sse()
            return

        self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")

    def _get_status(self) -> None:
        self._send_json({
            "voice_daemon_active": bridge_session.voice_daemon_active(),
            "session_id": self.backend.session_id(),
            "member": self.backend.member(),
            "include_raw_mic_0": self.backend.include_raw_mic_0(),
            "include_dtln": self.backend.include_dtln(),
            "include_usb_mic": self.backend.include_usb_mic(),
            "include_usb_dtln": self.backend.include_usb_dtln(),
            "include_xvf_raw0_dtln": self.backend.include_xvf_raw0_dtln(),
            "include_aec3_sweep": self.backend.include_aec3_sweep(),
            "corpus_profile": self.backend.corpus_profile(),
            "chip_aec_config": self.backend.chip_aec_config(),
            "aec3_sweep_source": self.backend.aec3_sweep_source(),
            "aec3_sweep_variants": self.backend.aec3_sweep_variants(),
            "aec3_sweep_config": self.backend.aec3_sweep_config(),
            "enabled_legs": list(self.backend.enabled_legs()),
            "capture_plan": self.backend.capture_plan(),
            "capture_plan_conformance": self.backend.capture_plan_conformance(),
            "audio_context": self.backend.audio_context(),
            "bridge_outputs": bridge_session.bridge_output_status(),
            "is_recording": self.backend.is_recording(),
            "elapsed_sec": self.backend.elapsed_recording_sec(),
            "clip_count": len(self.backend.list_clips()),
        })

    def _get_clips(self) -> None:
        self._send_json({
            "clips": [c.to_json() for c in self.backend.list_clips()],
        })

    def _get_sessions(self) -> None:
        self._send_json({"sessions": self.backend.list_sessions()})

    def _get_usb_mic_status(self) -> None:
        self._send_json(bridge_session.usb_mic_status())

    def _serve_level_sse(self) -> None:
        """Server-Sent Events stream of the live AEC-ON RMS in dBFS.

        Connects from the JS on page load, stays open for the lifetime
        of the tab. When recording is active, pushes {"recording": true,
        "rms_dbfs": <float>} every ~80 ms. When idle, pushes {"recording":
        false, "rms_dbfs": null} less frequently so the connection
        stays warm without burning CPU.

        Exit paths:
          - Client closes tab → wfile.write raises BrokenPipeError /
            ConnectionResetError → we exit cleanly.
          - Server shuts down → same.

        NOT CSRF-protected: this is a read-only GET with no side
        effects, like /api/status. The token requirement applies to
        mutating endpoints only.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
        self.end_headers()

        idle_period_sec = 0.5  # slow when not recording
        active_period_sec = 0.08  # ~12.5 Hz, matches frame rate
        try:
            while True:
                rms = self.backend.get_current_rms_dbfs()
                if rms is None:
                    payload = json.dumps({
                        "recording": False, "rms_dbfs": None,
                    })
                    sleep = idle_period_sec
                else:
                    payload = json.dumps({
                        "recording": True, "rms_dbfs": rms,
                    })
                    sleep = active_period_sec
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(sleep)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client gone

    def _serve_wav(self, path: str, url: Any) -> None:
        # /api/clip/<id>/wav?leg=<on|off|dtln>
        parts = path.split("/")
        if len(parts) != 5 or parts[1] != "api" or parts[2] != "clip" or parts[4] != "wav":
            self.send_error(HTTPStatus.NOT_FOUND, "bad clip URL")
            return
        clip_id = parts[3]
        qs = parse_qs(url.query)
        leg = qs.get("leg", ["on"])[0]
        if leg not in LEGS:
            self._send_error_json(400, f"bad leg: {leg}")
            return
        clip = self.backend.clip(clip_id)
        if clip is None or clip.deleted:
            self.send_error(HTTPStatus.NOT_FOUND, "clip not found")
            return
        wav_path = clip.files.get(leg)
        if wav_path is None:
            self.send_error(
                HTTPStatus.NOT_FOUND, f"no {leg} leg for this clip",
            )
            return
        p = Path(wav_path)
        if not p.is_file():
            self.send_error(
                HTTPStatus.NOT_FOUND, f"WAV missing on disk: {wav_path}",
            )
            return
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as f:
            self.wfile.write(f.read())

    # ----- POST -------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"

        # Route-check before CSRF-check (the _common.py wizard
        # convention): bogus paths return 404 without revealing
        # CSRF state.
        if path not in self._POST_ROUTES:
            self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")
            return

        # All POSTs are mutating — require CSRF token. _check_csrf
        # sends the 403 itself; we just return on failure.
        if not self._check_csrf():
            return

        try:
            body = self._read_json()
        except ValueError as e:
            self._send_error_json(400, str(e))
            return

        getattr(self, self._POST_ROUTES[path])(body)

    def _post_session(self, body: dict[str, Any]) -> None:
        member = (body.get("member") or "").strip()
        corpus_profile = str(body.get("corpus_profile") or PROFILE_STANDARD)
        if corpus_profile not in CORPUS_PROFILES:
            self._send_error_json(400, f"unknown corpus_profile: {corpus_profile}")
            return
        include_raw_mic_0 = bool(body.get("include_raw_mic_0", False))
        include_dtln = bool(body.get("include_dtln", True))
        include_usb_mic = bool(body.get("include_usb_mic", False))
        include_usb_dtln = bool(body.get("include_usb_dtln", False))
        include_xvf_raw0_dtln = bool(body.get("include_xvf_raw0_dtln", False))
        include_aec3_sweep = bool(body.get("include_aec3_sweep", False))
        try:
            aec3_sweep_source = (
                _session_aec3_sweep_source(body.get("aec3_sweep_source"))
                if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
            )
        except Aec3SweepConfigError as e:
            self._send_error_json(400, str(e))
            return
        if include_aec3_sweep and aec3_sweep_source == AEC3_SWEEP_SOURCE_USB:
            include_usb_mic = True
        if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
            include_raw_mic_0 = True
            include_aec3_sweep = False
        enable_bridge_outputs = bool(
            body.get("enable_bridge_outputs", False),
        )
        if not member:
            self._send_error_json(400, "member is required")
            return
        if self.backend.is_recording():
            self._send_error_json(
                409,
                "can't begin session: recording in progress",
            )
            return
        # Mic mute is a privacy promise. Refuse BEFORE the bridge-output
        # side effects below (env writes + bridge restart) so a muted
        # household never has its bridge reconfigured for a session that
        # the backend would refuse anyway. The backend re-checks at
        # begin_session/start_recording (the authoritative gate); this is
        # the wizard-side fast path with the same user-facing message.
        if self.backend.mic_muted():
            log_event(
                logger,
                "wake_corpus.mute_refused",
                op="post_session",
                level=logging.WARNING,
            )
            self._send_error_json(409, MIC_MUTED_MESSAGE)
            return
        try:
            capture_plan = bridge_session.build_capture_plan(
                self.backend.ports(),
                corpus_profile=corpus_profile,
                include_dtln=include_dtln,
                include_raw_mic_0=include_raw_mic_0,
                include_usb_mic=include_usb_mic,
                include_usb_dtln=include_usb_dtln,
                include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=aec3_sweep_source,
                include_bridge_readiness=True,
                include_runtime_profile=True,
                plan_state=bridge_session.CAPTURE_PLAN_STATE_SESSION,
            )
        except (ValueError, Aec3SweepConfigError) as e:
            self._send_error_json(400, str(e))
            return
        bridge = capture_plan.get("bridge")
        missing_outputs = (
            bridge.get("missing_outputs", [])
            if isinstance(bridge, dict) else []
        )
        if missing_outputs and not enable_bridge_outputs:
            labels = [
                BRIDGE_OUTPUT_LABELS.get(key, key)
                for key in missing_outputs
            ]
            self._send_json({
                "error": (
                    "bridge outputs are disabled for requested "
                    f"legs: {', '.join(labels)}"
                ),
                "can_enable_bridge_outputs": True,
                "missing_bridge_outputs": missing_outputs,
                "missing_bridge_output_labels": labels,
            }, status=409)
            return
        try:
            bridge_session.set_bridge_outputs_for_plan(capture_plan)
            capture_plan = bridge_session.build_capture_plan(
                self.backend.ports(),
                corpus_profile=corpus_profile,
                include_dtln=include_dtln,
                include_raw_mic_0=include_raw_mic_0,
                include_usb_mic=include_usb_mic,
                include_usb_dtln=include_usb_dtln,
                include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=aec3_sweep_source,
                include_bridge_readiness=True,
                include_runtime_profile=True,
                plan_state=bridge_session.CAPTURE_PLAN_STATE_SESSION,
            )
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or str(e)).strip()
            msg = (
                f"could not enable bridge outputs; {BRIDGE_UNIT} "
                "restart failed and the env was rolled back"
            )
            if detail:
                msg = f"{msg}: {detail[-500:]}"
            self._send_error_json(500, msg)
            return
        except subprocess.TimeoutExpired:
            self._send_error_json(
                500,
                f"could not enable bridge outputs; {BRIDGE_UNIT} "
                "restart timed out and the env was rolled back",
            )
            return
        except OSError as e:
            self._send_error_json(
                500,
                f"failed to enable bridge outputs: {e}",
            )
            return
        try:
            session_id = self.backend.begin_session(
                member,
                corpus_profile=corpus_profile,
                include_raw_mic_0=include_raw_mic_0,
                include_dtln=include_dtln,
                include_usb_mic=include_usb_mic,
                include_usb_dtln=include_usb_dtln,
                include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=aec3_sweep_source,
                capture_plan=capture_plan,
            )
        except (ValueError, StateError) as e:
            self._send_error_json(400, str(e))
            return
        self._send_json({
            "session_id": session_id, "member": member,
            "include_raw_mic_0": include_raw_mic_0,
            "include_dtln": include_dtln,
            "include_usb_mic": include_usb_mic,
            "include_usb_dtln": include_usb_dtln,
            "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
            "include_aec3_sweep": include_aec3_sweep,
            "corpus_profile": corpus_profile,
            "chip_aec_config": self.backend.chip_aec_config(),
            "aec3_sweep_source": aec3_sweep_source,
            "aec3_sweep_variants": self.backend.aec3_sweep_variants(),
            "aec3_sweep_config": self.backend.aec3_sweep_config(),
            "enabled_legs": list(self.backend.enabled_legs()),
            "capture_plan": self.backend.capture_plan(),
            "audio_context": self.backend.audio_context(),
            "bridge_outputs": bridge_session.bridge_output_status(),
        })

    def _post_capture_plan(self, body: dict[str, Any]) -> None:
        corpus_profile = str(body.get("corpus_profile") or PROFILE_STANDARD)
        try:
            plan = bridge_session.build_capture_plan(
                self.backend.ports(),
                corpus_profile=corpus_profile,
                include_dtln=bool(body.get("include_dtln", True)),
                include_raw_mic_0=bool(body.get("include_raw_mic_0", False)),
                include_usb_mic=bool(body.get("include_usb_mic", False)),
                include_usb_dtln=bool(body.get("include_usb_dtln", False)),
                include_xvf_raw0_dtln=bool(
                    body.get("include_xvf_raw0_dtln", False),
                ),
                include_aec3_sweep=bool(
                    body.get("include_aec3_sweep", False),
                ),
                aec3_sweep_source=body.get("aec3_sweep_source"),
                include_runtime_profile=True,
            )
        except (ValueError, Aec3SweepConfigError) as e:
            self._send_error_json(400, str(e))
            return
        self._send_json({"capture_plan": plan})

    def _post_session_load(self, body: dict[str, Any]) -> None:
        sid = (body.get("session_id") or "").strip()
        if not sid:
            self._send_error_json(400, "session_id is required")
            return
        try:
            result = self.backend.load_session(sid)
        except ValueError as e:
            self._send_error_json(404, str(e))
            return
        except StateError as e:
            self._send_error_json(409, str(e))
            return
        self._send_json(result)

    def _post_session_unload(self, body: dict[str, Any]) -> None:
        try:
            unloaded = self.backend.unload_session()
        except StateError as e:
            self._send_error_json(409, str(e))
            return
        self._send_json({"unloaded_session": unloaded})

    def _post_clip_start(self, body: dict[str, Any]) -> None:
        condition = (body.get("condition") or "").strip()
        distance = (body.get("distance") or "").strip()
        try:
            result = self.backend.start_recording(condition, distance)
        except (ValueError, StateError) as e:
            self._send_error_json(409, str(e))
            return
        self._send_json(result)

    def _post_clip_stop(self, body: dict[str, Any]) -> None:
        try:
            clip = self.backend.stop_recording()
        except StateError as e:
            self._send_error_json(409, str(e))
            return
        self._send_json(clip.to_json())

    def _post_bridge_outputs(self, body: dict[str, Any]) -> None:
        action = (body.get("action") or "").strip()
        if action != "disable":
            self._send_error_json(400, "action must be disable")
            return
        if self.backend.is_recording():
            self._send_error_json(
                409,
                "stop the current recording before disabling bridge outputs",
            )
            return
        try:
            bridge_session.disable_bridge_corpus_outputs()
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or str(e)).strip()
            msg = (
                f"could not disable bridge outputs; {BRIDGE_UNIT} "
                "restart failed and the env was rolled back"
            )
            if detail:
                msg = f"{msg}: {detail[-500:]}"
            self._send_error_json(500, msg)
            return
        except subprocess.TimeoutExpired:
            self._send_error_json(
                500,
                f"could not disable bridge outputs; {BRIDGE_UNIT} "
                "restart timed out and the env was rolled back",
            )
            return
        except OSError as e:
            self._send_error_json(
                500,
                f"failed to disable bridge outputs: {e}",
            )
            return
        self._send_json({"bridge_outputs": bridge_session.bridge_output_status()})

    def _post_corpus_test_mode(self, body: dict[str, Any]) -> None:
        action = (body.get("action") or "").strip()
        if action not in ("enter", "exit"):
            self._send_error_json(400, "action must be enter or exit")
            return
        if self.backend.is_recording():
            self._send_error_json(
                409,
                "stop the current recording before changing corpus test mode",
            )
            return
        try:
            if action == "enter":
                bridge_session.enter_corpus_test_mode(
                    corpus_profile=str(
                        body.get("corpus_profile") or PROFILE_STANDARD,
                    ),
                    include_dtln=bool(body.get("include_dtln", True)),
                    include_usb_mic=bool(body.get("include_usb_mic", False)),
                    include_usb_dtln=bool(body.get("include_usb_dtln", False)),
                    include_xvf_raw0_dtln=bool(
                        body.get("include_xvf_raw0_dtln", False),
                    ),
                    include_aec3_sweep=bool(
                        body.get("include_aec3_sweep", False),
                    ),
                    aec3_sweep_source=body.get("aec3_sweep_source"),
                )
                # Mark only after voice was actually stopped, so a
                # later startup can self-heal an abandoned session.
                self.backend.note_test_mode_entered()
            else:
                bridge_session.exit_corpus_test_mode()
                self.backend.note_test_mode_exited()
                self.backend.unload_session()
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or str(e)).strip()
            msg = f"corpus test mode {action} failed"
            if detail:
                msg = f"{msg}: {detail[-500:]}"
            self._send_error_json(500, msg)
            return
        except subprocess.TimeoutExpired:
            self._send_error_json(
                500,
                f"corpus test mode {action} timed out while restarting "
                f"{BRIDGE_UNIT}",
            )
            return
        except StateError as e:
            self._send_error_json(409, str(e))
            return
        except ValueError as e:
            self._send_error_json(400, str(e))
            return
        except OSError as e:
            self._send_error_json(
                500,
                f"corpus test mode {action} failed: {e}",
            )
            return
        self._send_json({
            "action": action,
            "voice_daemon_active": bridge_session.voice_daemon_active(),
            "bridge_outputs": bridge_session.bridge_output_status(),
        })

    def _post_voice_daemon(self, body: dict[str, Any]) -> None:
        action = (body.get("action") or "").strip()
        if action not in ("start", "stop"):
            self._send_error_json(400, "action must be start or stop")
            return
        disable_outputs = bool(body.get("disable_bridge_outputs", False))
        # Refuse to start jasper-voice while a recording is in
        # progress: starting it would try to bind UDP ports the
        # recording owns, sending the daemon into a restart loop
        # while the operator wonders why their speaker is dead.
        # Caller sees a clear error and knows to stop the
        # recording first.
        if action == "start" and self.backend.is_recording():
            self._send_error_json(
                409,
                "stop the current recording first; jasper-voice "
                "can't bind UDP ports the recording is using",
            )
            return
        if action == "start" and disable_outputs:
            try:
                bridge_session.disable_bridge_corpus_outputs()
            except subprocess.CalledProcessError as e:
                detail = (e.stderr or e.stdout or str(e)).strip()
                msg = (
                    f"could not disable bridge outputs; {BRIDGE_UNIT} "
                    "restart failed and the env was rolled back"
                )
                if detail:
                    msg = f"{msg}: {detail[-500:]}"
                self._send_error_json(500, msg)
                return
            except subprocess.TimeoutExpired:
                self._send_error_json(
                    500,
                    f"could not disable bridge outputs; {BRIDGE_UNIT} "
                    "restart timed out and the env was rolled back",
                )
                return
            except OSError as e:
                self._send_error_json(
                    500,
                    f"failed to disable bridge outputs: {e}",
                )
                return
        # WS1 Phase 3: start/stop voice via the restart broker (blocking so
        # the corpus session sees the daemon settle) — surfaces a 500 on
        # failure, same as the previous check=True systemctl.
        resp = manage_units(
            VOICE_UNIT, verb=action, reason="wake-corpus session",
            no_block=False, timeout=30.0,
        )
        if not resp.get("ok"):
            detail = resp.get("error") or f"rc={resp.get('rc')}"
            self._send_error_json(500, f"systemctl {action} failed: {detail}")
            return
        self._send_json({
            "action": action,
            "voice_daemon_active": bridge_session.voice_daemon_active(),
            "bridge_outputs": bridge_session.bridge_output_status(),
        })

    # ----- DELETE -----------------------------------------------------

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = path.split("/")
        # Route-check before CSRF-check (the _common.py wizard
        # convention): only /api/clip/<id> and /api/session/<id>
        # exist; bogus paths 404 without revealing CSRF state.
        if not (
            len(parts) == 4
            and parts[1] == "api"
            and parts[2] in ("clip", "session")
        ):
            self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")
            return
        if not self._check_csrf():
            return
        # /api/clip/<id>
        if parts[2] == "clip":
            clip_id = parts[3]
            ok = self.backend.delete_clip(clip_id)
            if not ok:
                self._send_error_json(404, "clip not found")
                return
            self._send_json({"deleted": clip_id})
            return
        # /api/session/<id> — hard-delete a whole session (WAVs + JSON)
        session_id = parts[3]
        try:
            result = self.backend.delete_session(session_id)
        except ValueError as e:
            self._send_error_json(404, str(e))
            return
        except StateError as e:
            self._send_error_json(409, str(e))
            return
        self._send_json({"deleted_session": session_id, **result})

    # ----- route tables (exact path -> handler-method name) ----------
    # Keyed by exact path. do_GET / do_POST disambiguate by method.
    # The string keys keep the route literals greppable; prefix routes
    # (/api/clip/<id>/wav, the DELETE /api/clip|session/<id> forms) are
    # handled explicitly in the do_* methods because they don't fit an
    # exact-match table. test_get_routes_resolve_via_render_and_module
    # asserts the ES module's relative api paths stay in sync.

    _GET_ROUTES = {
        "/api/status": "_get_status",
        "/api/clips": "_get_clips",
        "/api/sessions": "_get_sessions",
        "/api/usb-mic/status": "_get_usb_mic_status",
    }
    _POST_ROUTES = {
        "/api/session": "_post_session",
        "/api/capture-plan": "_post_capture_plan",
        "/api/session/load": "_post_session_load",
        "/api/session/unload": "_post_session_unload",
        "/api/clip/start": "_post_clip_start",
        "/api/clip/stop": "_post_clip_stop",
        "/api/bridge-outputs": "_post_bridge_outputs",
        "/api/corpus-test-mode": "_post_corpus_test_mode",
        "/api/voice-daemon": "_post_voice_daemon",
    }


def _make_handler_class(
    backend: RecordingBackend, csrf_token: str,
) -> type[_Handler]:
    class _BoundHandler(_Handler):
        pass
    _BoundHandler.backend = backend
    _BoundHandler.csrf_token = csrf_token
    return _BoundHandler


def make_server(
    target,
    *,
    csrf_token: str,
    backend: RecordingBackend,
) -> ThreadingHTTPServer:
    """Construct the recorder's HTTP server bound to `target`.

    `target` is either:
      - an `(host, port)` tuple for direct binding (CLI use)
      - a `socket.socket` already bound by systemd (socket-activation
        path via `jasper.web.__main__`)
      - an `int` port (legacy direct-bind shortcut)

    Pairs with `jasper.web._systemd.make_http_server` to handle the
    socket-vs-bind branching. The backend must already be `start()`ed
    by the caller (the asyncio loop thread + crash-recovery state both
    depend on it).
    """
    from . import _systemd
    handler_cls = _make_handler_class(backend, csrf_token)
    return _systemd.make_http_server(target, handler_cls)


# ---------------------------------------------------------------------------
# Frontend — body fragment for canonical_page(); CSS + JS are static assets
# ---------------------------------------------------------------------------
#
# The page renders through `canonical_page()` (shared /assets/app.css look).
# This template is just the <body> fragment: the canonical sticky header, the
# <main class="page"> with the recorder's cards (identical element IDs so the
# behaviour module binds the same way), the JSON config island carrying the
# Python-built leg labels/order, and the ES module entry point. Page-specific
# CSS lives in /assets/wake-corpus/wake-corpus.css (linked via page_css_href);
# the behaviour lives in /assets/wake-corpus/js/main.js.


def _capture_option_row(option: CaptureOption) -> str:
    hidden_attr = " hidden" if option.hidden else ""
    return (
        f'    <div class="capture-option"{hidden_attr}>\n'
        f'      <label class="capture-option__text" '
        f'for="{html.escape(option.control_id, quote=True)}">\n'
        f"        <strong>{html.escape(option.title)}</strong>\n"
        f'        <span class="hint">{html.escape(option.hint)}</span>\n'
        "      </label>\n"
        f"      {toggle_html(option.control_id, checked=option.checked)}\n"
        "    </div>"
    )


def _capture_options_html() -> str:
    return "\n".join(_capture_option_row(option) for option in CAPTURE_OPTIONS)


_INDEX_BODY_TEMPLATE = """{header}
<main class="page">
  <div class="card" id="status-card">
    <div class="row">
      <label>Mode:</label>
      <span id="corpus-mode-status" class="pill gray">checking…</span>
      <button id="corpus-mode-exit" style="margin-left:auto">Exit corpus test mode</button>
    </div>
    <div class="row">
      <label>jasper-voice:</label>
      <span id="voice-status" class="pill gray">checking…</span>
    </div>
    <div class="row">
      <label>Extra corpus outputs:</label>
      <span id="bridge-output-status" class="pill gray">checking…</span>
    </div>
    <div class="row">
      <label>Session:</label>
      <span id="session-id">(no session)</span>
    </div>
  </div>

  <div class="card" id="session-card">
    <h2 id="session-card-title" style="margin-top:0">Begin a new session</h2>
    <div class="row">
      <label for="member">Name:</label>
      <input type="text" id="member" value="jasper" maxlength="20">
    </div>
{capture_options}
    <div id="capture-plan-preview" class="capture-plan-preview">
      <span class="hint">Planning capture graph…</span>
    </div>
    <p class="hint">
      Raw clips are saved in the configured wake-corpus directory
      (<code>/var/lib/jasper/enrollment_positives/</code> on an installed
      speaker) with the member name and persist until you delete the session;
      recording refuses to start while the household mic mute is on.
    </p>
    <div class="session-primary-actions">
      <button id="session-begin" class="primary">
        Enter corpus test mode &amp; begin session
      </button>
      <button id="session-unload" style="display:none">Unload session</button>
    </div>
  </div>

  <details class="card" id="sessions-card">
    <summary style="cursor:pointer">
      <strong>Sessions</strong>
      <span style="color:#888; font-size:0.86em; margin-left:0.4em">
        load or delete previous recordings
      </span>
    </summary>
    <div id="sessions-list" style="margin-top:0.8em">(loading…)</div>
    <p style="margin:0.6em 0 0; color:#888; font-size:0.86em">
      Tap <strong>Load</strong> to resume an existing session, or
      <strong>Delete</strong> to remove its WAVs + metadata permanently.
    </p>
  </details>

  <div class="card" id="record-card" style="display:none">
    <h2 style="margin-top:0">Record a clip</h2>
    <div class="row">
      <label>Condition:</label>
      <div class="conditions">
        <label><input type="radio" name="condition" value="quiet" checked><span>quiet</span></label>
        <label><input type="radio" name="condition" value="ambient"><span>ambient (AC/fridge)</span></label>
        <label><input type="radio" name="condition" value="music"><span>music</span></label>
      </div>
    </div>
    <div class="row">
      <label>Distance:</label>
      <div class="distances">
        <label><input type="radio" name="distance" value="near" checked><span>near ~1m</span></label>
        <label><input type="radio" name="distance" value="mid"><span>mid ~2m</span></label>
        <label><input type="radio" name="distance" value="far"><span>far ~3-4m</span></label>
      </div>
    </div>
    <p style="margin:0.8em 0; color:#666; font-size:0.92em">
      Click the button (or press <kbd>Space</kbd>) to start. Say
      <strong>"Jarvis"</strong>. Click again to stop.
    </p>
    <div class="mic-level" id="mic-level">
      <span class="mic-level-label">Mic level:</span>
      <div class="mic-level-track"><div id="mic-level-fill" class="mic-level-fill"></div></div>
      <span id="mic-level-readout" class="mic-level-readout">—</span>
    </div>
    <button id="record-btn" class="primary recordBtn" disabled>● RECORD</button>
    <div id="recording-info" style="display:none; margin-top:0.6em">
      <span class="pill red">RECORDING</span>
      <span id="elapsed" style="margin-left:0.6em">0.0s</span>
    </div>
    <div id="err" class="err"></div>
  </div>

  <div class="card" id="counts-card" style="display:none">
    <h2 style="margin-top:0">Per-cell counts</h2>
    <div id="counts-matrix" class="matrix"></div>
    <p style="margin:0.6em 0 0; color:#888; font-size:0.86em">
      Session A: ~7-9 per cell. Session B: ~2-3 per cell.
    </p>
  </div>

  <div class="card" id="clips-card" style="display:none">
    <h2 style="margin-top:0">Recorded clips (this session)</h2>
    <div class="clip" style="font-weight:600; border-bottom:2px solid #333">
      <span>#</span><span>condition</span><span>distance</span>
      <span>duration</span><span>audio</span><span></span>
    </div>
    <div id="clips-list"></div>
  </div>
</main>
{config_island}
<script type="module" src="/assets/wake-corpus/js/main.js"></script>
"""


def _render_index_html(csrf_token: str = "") -> str:
    """Render the recorder page on the canonical design system.

    Returns the full HTML document (str). The document shell comes from
    ``canonical_page()`` (shared /assets/app.css); the body is the
    ``_INDEX_BODY_TEMPLATE`` fragment with the canonical header injected.

    The Python-owned leg labels + playback order (which depend on the
    AEC3 sweep registry and so can't live in the cached ES module) are
    serialized into a JSON data island (``json_island()``) the
    behaviour module reads at load time; the helper owns the
    serialization + escaping that keeps a label from closing the inline
    ``<script>`` element early.
    """
    config = {
        "leg_labels": LEG_LABELS,
        "aec3_sweep_order": list(AEC3_SWEEP_LEGS + LEGACY_AEC3_SWEEP_LEGS),
        "usb_aec3_sweep_baseline_label": USB_AEC3_SWEEP_BASELINE_LABEL,
    }
    header = canonical_header("Wake-word corpus")
    body = _INDEX_BODY_TEMPLATE.replace("{header}", header).replace(
        "{config_island}", json_island("wake-corpus-config", config),
    ).replace(
        "{capture_options}", _capture_options_html(),
    )
    return canonical_page(
        "Wake-word corpus",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/wake-corpus/wake-corpus.css",
    ).decode("utf-8")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wake-corpus-web",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"Bind host (default {DEFAULT_HOST}; use 0.0.0.0 for LAN access).",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Bind port (default {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(os.environ.get("JASPER_WAKE_TRAIN_DATA", "data"))
        / "enrollment_positives",
        help="Output root for WAVs + metadata. Layout matches "
             "jasper-wake-enroll (default ./data/enrollment_positives).",
    )
    parser.add_argument(
        "--aec-on-port", type=int, default=DEFAULT_AEC_ON_PORT,
        help=f"UDP port for AEC ON leg (default {DEFAULT_AEC_ON_PORT}).",
    )
    parser.add_argument(
        "--aec-off-port", type=int, default=DEFAULT_AEC_OFF_PORT,
        help=f"UDP port for AEC OFF (raw chip-direct) leg (default {DEFAULT_AEC_OFF_PORT}).",
    )
    parser.add_argument(
        "--aec-dtln-port", type=int, default=DEFAULT_AEC_DTLN_PORT,
        help=f"UDP port for DTLN leg (default {DEFAULT_AEC_DTLN_PORT}).",
    )
    parser.add_argument(
        "--aec-raw0-port", type=int, default=DEFAULT_AEC_RAW0_PORT,
        help=f"UDP port for raw mic 0 leg (default {DEFAULT_AEC_RAW0_PORT}).",
    )
    parser.add_argument(
        "--aec-ref-port", type=int, default=DEFAULT_AEC_REF_PORT,
        help=f"UDP port for corpus reference leg (default {DEFAULT_AEC_REF_PORT}).",
    )
    parser.add_argument(
        "--aec-usb-raw-port", type=int, default=DEFAULT_AEC_USB_RAW_PORT,
        help=f"UDP port for cheap USB raw leg (default {DEFAULT_AEC_USB_RAW_PORT}).",
    )
    parser.add_argument(
        "--aec-usb-webrtc-port", type=int, default=DEFAULT_AEC_USB_WEBRTC_PORT,
        help="UDP port for cheap USB WebRTC leg "
             f"(default {DEFAULT_AEC_USB_WEBRTC_PORT}).",
    )
    parser.add_argument(
        "--aec-usb-dtln-port", type=int, default=DEFAULT_AEC_USB_DTLN_PORT,
        help="UDP port for cheap USB DTLN leg "
             f"(default {DEFAULT_AEC_USB_DTLN_PORT}).",
    )
    parser.add_argument(
        "--aec-chip-aec-150-port", type=int,
        default=DEFAULT_AEC_CHIP_AEC_150_PORT,
        help="UDP port for chip AEC fixed-beam 150-degree leg "
             f"(default {DEFAULT_AEC_CHIP_AEC_150_PORT}).",
    )
    parser.add_argument(
        "--aec-chip-aec-210-port", type=int,
        default=DEFAULT_AEC_CHIP_AEC_210_PORT,
        help="UDP port for chip AEC fixed-beam 210-degree leg "
             f"(default {DEFAULT_AEC_CHIP_AEC_210_PORT}).",
    )
    parser.add_argument(
        "--aec-xvf-raw0-webrtc-aec3-port", type=int,
        default=DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT,
        help="UDP port for XVF raw0 WebRTC AEC3 comparison leg "
             f"(default {DEFAULT_AEC_XVF_RAW0_WEBRTC_AEC3_PORT}).",
    )
    parser.add_argument(
        "--aec-xvf-raw0-dtln-port", type=int,
        default=DEFAULT_AEC_XVF_RAW0_DTLN_PORT,
        help="UDP port for XVF raw0 DTLN comparison leg "
             f"(default {DEFAULT_AEC_XVF_RAW0_DTLN_PORT}).",
    )
    for variant in AEC3_SWEEP_VARIANTS:
        parser.add_argument(
            f"--{variant.leg.replace('_', '-')}-port",
            type=int,
            default=DEFAULT_AEC3_SWEEP_PORTS[variant.leg],
            dest=f"aec3_sweep_port_{variant.leg}",
            help=(
                f"UDP port for {variant.label} sweep leg "
                f"(default {DEFAULT_AEC3_SWEEP_PORTS[variant.leg]})."
            ),
        )
    parser.add_argument(
        "--no-dtln", action="store_true",
        help="Skip the DTLN leg entirely (for 2-stream Pis or "
             "JASPER_WAKE_LEG_DTLN=0).",
    )
    parser.add_argument(
        "--no-usb-corpus", action="store_true",
        help="Hide corpus-only ref/USB leg ports from this recorder process.",
    )
    parser.add_argument(
        "--no-require-root", action="store_true",
        help="Skip the root check. Useful for dev — but voice-daemon "
             "start/stop won't work without sudo, and UDP bind may "
             "fail if other processes hold the ports.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.no_require_root:
        require_root()

    ports = build_ports(
        aec_on_port=args.aec_on_port,
        aec_off_port=args.aec_off_port,
        aec_dtln_port=args.aec_dtln_port,
        aec_raw0_port=args.aec_raw0_port,
        aec_ref_port=args.aec_ref_port,
        aec_usb_raw_port=args.aec_usb_raw_port,
        aec_usb_webrtc_port=args.aec_usb_webrtc_port,
        aec_usb_dtln_port=args.aec_usb_dtln_port,
        aec_chip_aec_150_port=args.aec_chip_aec_150_port,
        aec_chip_aec_210_port=args.aec_chip_aec_210_port,
        aec_xvf_raw0_webrtc_aec3_port=args.aec_xvf_raw0_webrtc_aec3_port,
        aec_xvf_raw0_dtln_port=args.aec_xvf_raw0_dtln_port,
        aec3_sweep_ports={
            variant.leg: getattr(args, f"aec3_sweep_port_{variant.leg}")
            for variant in AEC3_SWEEP_VARIANTS
        },
        include_dtln=not args.no_dtln,
        include_usb=not args.no_usb_corpus,
    )

    backend = RecordingBackend(args.output, ports=ports)
    backend.start()
    # CSRF token regenerated each process startup. If you reload the
    # tab the page picks up the current token; old tabs keep their
    # stale token and get 403s until reload — acceptable UX for an
    # operator tool that runs for a single session.
    csrf_token = secrets.token_hex(16)
    try:
        server = make_server(
            (args.host, args.port),
            csrf_token=csrf_token,
            backend=backend,
        )
        logger.info(
            "jasper-wake-corpus-web on http://%s:%d  output=%s  legs=%s",
            args.host, args.port, args.output,
            ",".join(ports.keys()),
        )
        logger.info(
            "Open http://<this-host>:%d in a browser. Ctrl-C to stop.",
            args.port,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("shutting down on Ctrl-C")
    finally:
        backend.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
