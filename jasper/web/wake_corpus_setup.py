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
  - Visual indicator of jasper-voice state + one-click start/stop

Usage:
  sudo /opt/jasper/.venv/bin/jasper-wake-corpus-web
  # then open http://jts.local:8782/ in any browser on the LAN

  # or override the bind for security:
  sudo jasper-wake-corpus-web --host 127.0.0.1 --port 8782
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

# Reuse audio I/O + systemctl helpers from the CLI. Single source of
# truth for the WAV format + the "stop jasper-voice to free UDP" dance.
from jasper.cli.wake_enroll import (
    CHANNELS,
    DEFAULT_AEC_DTLN_PORT,
    DEFAULT_AEC_OFF_PORT,
    DEFAULT_AEC_ON_PORT,
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
    VOICE_UNIT,
    require_root,
    systemctl,
    write_wav,
)

logger = logging.getLogger("jasper-wake-corpus-web")


# Default bind. Loopback by default for safety; CLI flag opens to LAN.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8782

DEFAULT_OUTPUT_DIR = Path("data/enrollment_positives")
DEFAULT_METADATA_SUBDIR = "metadata"

# Validated input domains. Match the upstream extract/score/review
# pipeline's expectations exactly so files land where downstream tools
# look for them.
CONDITIONS = ("quiet", "music")
DISTANCES = ("near", "mid", "far")
LEGS = ("on", "off", "dtln")

# Hard cap so a forgotten "stop" doesn't fill memory with a 1-hour
# buffer. The server auto-stops at this duration with a flag in the
# metadata so the operator notices.
MAX_RECORDING_DURATION_SEC = 30.0


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ClipMetadata:
    """One recorded clip's complete metadata, written to the per-session
    JSON sidecar. All fields are JSON-serializable.
    """

    clip_id: str
    member: str
    condition: str
    distance: str
    session_id: str
    seq: int
    start_ts: str  # ISO8601 UTC
    stop_ts: str
    duration_sec: float
    files: dict[str, str]  # leg → absolute WAV path
    deleted: bool = False
    auto_stopped: bool = False
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Recording — the actual audio I/O
# ---------------------------------------------------------------------------


class RecordingTask:
    """Open-ended audio recording from multiple UDP captures.

    Constructed on each Start click; cancelled on Stop click. Background
    asyncio task streams frames into per-leg buffers. `stop()` cancels
    cleanly + returns the captured PCM bytes per leg.

    Memory bound: at 16 kHz mono int16 ≈ 32 KB/s per leg × 3 legs ≈
    96 KB/s. Capped to MAX_RECORDING_DURATION_SEC by the backend, so
    worst-case footprint is bounded.
    """

    def __init__(self, ports: dict[str, int]) -> None:
        self._ports = ports
        self._buffers: dict[str, list[np.ndarray]] = {leg: [] for leg in ports}
        self._captures: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._stack: AsyncExitStack | None = None
        self._start_monotonic: float = 0.0

    async def start(self) -> None:
        # Lazy import — keeps this module importable on dev machines
        # that don't have sounddevice / portaudio (UdpMicCapture is
        # pure-asyncio but lives in audio_io which imports sounddevice
        # at the top).
        from jasper.audio_io import UdpMicCapture

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for leg, port in self._ports.items():
                cap = await self._stack.enter_async_context(
                    UdpMicCapture(port=port),
                )
                self._captures[leg] = cap
        except Exception:
            # If any leg fails to bind, clean up the ones that succeeded
            # so the user can retry without a "port already in use"
            # cascade on the next start.
            await self._stack.__aexit__(None, None, None)
            raise

        self._start_monotonic = time.monotonic()
        self._task = asyncio.create_task(self._collect_all())

    async def _collect_all(self) -> None:
        async def _per_leg(leg: str, cap: Any) -> None:
            async for frame in cap.frames():
                self._buffers[leg].append(frame)

        await asyncio.gather(*[
            _per_leg(leg, cap) for leg, cap in self._captures.items()
        ])

    def elapsed_sec(self) -> float:
        if self._start_monotonic == 0:
            return 0.0
        return time.monotonic() - self._start_monotonic

    async def stop(self) -> dict[str, bytes]:
        """Cancel the collection task, return PCM bytes per leg."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("recording task raised on cancel: %s", e)

        result: dict[str, bytes] = {}
        for leg, frames in self._buffers.items():
            if frames:
                pcm = np.concatenate(frames).astype(np.int16).tobytes()
            else:
                pcm = b""
            result[leg] = pcm

        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("cleanup raised: %s", e)
        return result


# ---------------------------------------------------------------------------
# Backend — single-recording state + persistence, thread-safe
# ---------------------------------------------------------------------------


class StateError(RuntimeError):
    """Raised when an operation isn't valid in the current state
    (e.g. starting a recording while one is in progress)."""


class RecordingBackend:
    """Single-recording-at-a-time backend, controllable from sync HTTP
    handlers via a background asyncio event loop.

    Lifecycle:
        backend = RecordingBackend(...)
        backend.start()                     # spins up the loop thread
        backend.begin_session("jasper")
        clip_id = backend.start_recording("quiet", "near")
        ...
        clip_meta = backend.stop_recording()
        backend.delete_clip(clip_id)
        backend.shutdown()                  # joins the loop thread
    """

    def __init__(
        self,
        output_dir: Path,
        ports: dict[str, int] | None = None,
        max_duration_sec: float = MAX_RECORDING_DURATION_SEC,
    ) -> None:
        self._output_dir = output_dir
        self._metadata_dir = output_dir / DEFAULT_METADATA_SUBDIR
        self._ports = ports or {
            "on": DEFAULT_AEC_ON_PORT,
            "off": DEFAULT_AEC_OFF_PORT,
            "dtln": DEFAULT_AEC_DTLN_PORT,
        }
        self._max_duration_sec = max_duration_sec

        # State guarded by _lock. Touched from HTTP handler threads
        # AND from the loop thread (auto-stop timer); the lock makes
        # all observers see consistent state.
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._member: str | None = None
        self._clips: list[ClipMetadata] = []
        self._current: RecordingTask | None = None
        self._current_clip_id: str | None = None
        self._current_meta: dict[str, str] | None = None  # condition, distance, start_ts
        self._auto_stop_handle: Any | None = None  # asyncio.TimerHandle

        # Background asyncio loop running in a daemon thread. Lazily
        # created in start() so tests can construct a backend without
        # immediately spawning the thread.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

    # ----- lifecycle -------------------------------------------------

    def start(self) -> None:
        if self._loop_thread is not None:
            return  # idempotent
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="wake-corpus-loop", daemon=True,
        )
        self._loop_thread.start()
        self._loop_ready.wait()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def shutdown(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)

    def _submit(self, coro: Any) -> Any:
        """Run a coroutine on the backend loop, block for the result."""
        if self._loop is None:
            raise RuntimeError("backend not started; call .start() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # ----- session + clip state -------------------------------------

    def session_id(self) -> str | None:
        with self._lock:
            return self._session_id

    def member(self) -> str | None:
        with self._lock:
            return self._member

    def is_recording(self) -> bool:
        with self._lock:
            return self._current is not None

    def begin_session(self, member: str) -> str:
        """Open a fresh recording session. Resets the in-memory clip
        list (existing on-disk WAVs are untouched).

        Returns the new session_id (UTC timestamp).
        """
        safe_member = "".join(c for c in member.lower() if c.isalnum() or c == "_")
        if not safe_member:
            raise ValueError(f"member name has no usable chars: {member!r}")
        with self._lock:
            if self._current is not None:
                raise StateError(
                    "can't begin session: recording in progress",
                )
            self._session_id = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ",
            )
            self._member = safe_member
            self._clips = []
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        return self._session_id

    def start_recording(self, condition: str, distance: str) -> dict[str, str]:
        """Begin recording on the backend loop. Returns {clip_id, start_ts}."""
        if condition not in CONDITIONS:
            raise ValueError(
                f"unknown condition {condition!r}; expected {CONDITIONS}",
            )
        if distance not in DISTANCES:
            raise ValueError(
                f"unknown distance {distance!r}; expected {DISTANCES}",
            )

        with self._lock:
            if self._session_id is None or self._member is None:
                raise StateError("call begin_session() first")
            if self._current is not None:
                raise StateError("recording already in progress")

        task = RecordingTask(self._ports)
        # Start on the backend loop. If the UDP bind fails (jasper-voice
        # is still up, port already in use), this raises and we never
        # transition into the recording state.
        try:
            self._submit(task.start())
        except Exception as e:
            raise StateError(
                f"failed to start recording (is jasper-voice down?): {e}",
            ) from e

        clip_id = str(uuid.uuid4())
        start_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            self._current = task
            self._current_clip_id = clip_id
            self._current_meta = {
                "condition": condition,
                "distance": distance,
                "start_ts": start_ts,
            }
            # Auto-stop timer — guards against a forgotten Stop click.
            self._auto_stop_handle = self._loop.call_later(
                self._max_duration_sec, self._auto_stop_threadsafe,
            )
        return {"clip_id": clip_id, "start_ts": start_ts}

    def _auto_stop_threadsafe(self) -> None:
        """Fires on the backend loop when MAX_RECORDING_DURATION_SEC
        elapses. Triggers stop_recording on a worker thread so the
        loop thread doesn't block on its own sync method."""
        thread = threading.Thread(
            target=self._auto_stop_safe, daemon=True,
        )
        thread.start()

    def _auto_stop_safe(self) -> None:
        try:
            self.stop_recording(auto=True)
        except Exception as e:
            logger.warning("auto-stop failed: %s", e)

    def stop_recording(self, auto: bool = False) -> ClipMetadata:
        """Stop the current recording, save WAVs, return metadata."""
        with self._lock:
            if self._current is None:
                raise StateError("no recording in progress")
            task = self._current
            clip_id = self._current_clip_id
            meta = self._current_meta
            session_id = self._session_id
            member = self._member
            # Cancel the auto-stop timer if it hasn't fired yet.
            if self._auto_stop_handle is not None and not auto:
                self._auto_stop_handle.cancel()
            self._auto_stop_handle = None
            # Clear state up-front so a second Stop click during the
            # save isn't a confusing no-op.
            self._current = None
            self._current_clip_id = None
            self._current_meta = None

        # Long operations (await stop, write WAVs) happen OUTSIDE the
        # lock — other API calls can read state concurrently.
        pcm_per_leg = self._submit(task.stop())
        stop_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        duration_sec = task.elapsed_sec()

        # Pick the next sequence number — count of non-deleted clips
        # this session. Sequence is per-session, not per-condition,
        # so filenames stay unique across the whole session.
        with self._lock:
            seq = sum(1 for c in self._clips if not c.deleted) + 1

        files: dict[str, str] = {}
        condition_dir = "music" if meta["condition"] == "music" else "nomusic"
        for leg, pcm in pcm_per_leg.items():
            if not pcm:
                continue
            filename = f"enroll_{member}_{session_id}_{seq:03d}.aec-{leg}.wav"
            full_path = self._output_dir / f"aec_{leg}_{condition_dir}" / filename
            full_path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(full_path, pcm)
            files[leg] = str(full_path)

        clip = ClipMetadata(
            clip_id=clip_id,
            member=member,
            condition=meta["condition"],
            distance=meta["distance"],
            session_id=session_id,
            seq=seq,
            start_ts=meta["start_ts"],
            stop_ts=stop_ts,
            duration_sec=duration_sec,
            files=files,
            deleted=False,
            auto_stopped=auto,
        )
        with self._lock:
            self._clips.append(clip)
        self._save_metadata()
        logger.info(
            "clip saved: %s seq=%d condition=%s distance=%s dur=%.2fs%s",
            clip_id, seq, meta["condition"], meta["distance"],
            duration_sec, " (auto-stopped)" if auto else "",
        )
        return clip

    def delete_clip(self, clip_id: str) -> bool:
        """Hard-delete a clip's WAVs + mark it deleted in metadata.

        Returns True if the clip existed and was deleted, False if
        not found (or already deleted)."""
        with self._lock:
            clip = next(
                (c for c in self._clips
                 if c.clip_id == clip_id and not c.deleted),
                None,
            )
            if clip is None:
                return False
            for path_str in clip.files.values():
                p = Path(path_str)
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.warning("failed to delete %s: %s", p, e)
            clip.deleted = True
        self._save_metadata()
        logger.info("clip deleted: %s", clip_id)
        return True

    def list_clips(self, include_deleted: bool = False) -> list[ClipMetadata]:
        with self._lock:
            return [
                c for c in self._clips
                if include_deleted or not c.deleted
            ]

    def clip(self, clip_id: str) -> ClipMetadata | None:
        with self._lock:
            return next(
                (c for c in self._clips if c.clip_id == clip_id),
                None,
            )

    def elapsed_recording_sec(self) -> float:
        with self._lock:
            if self._current is None:
                return 0.0
            return self._current.elapsed_sec()

    # ----- metadata persistence -------------------------------------

    def _metadata_path(self) -> Path:
        return self._metadata_dir / f"enroll_{self._member}_{self._session_id}.json"

    def _save_metadata(self) -> None:
        """Atomic-rewrite the session JSON sidecar. Called after every
        clip write + delete so the file on disk always reflects the
        current state (resilient to a server crash mid-session)."""
        with self._lock:
            if self._session_id is None:
                return
            path = self._metadata_path()
            data = {
                "session_id": self._session_id,
                "member": self._member,
                "ports": self._ports,
                "clips": [c.to_json() for c in self._clips],
            }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Voice-daemon control — same systemctl helpers as wake-enroll
# ---------------------------------------------------------------------------


def voice_daemon_active() -> bool:
    """True if jasper-voice is currently running (systemd active)."""
    import subprocess
    rc = subprocess.run(
        ["systemctl", "is-active", VOICE_UNIT],
        capture_output=True, text=True,
    )
    return rc.returncode == 0 and rc.stdout.strip() == "active"


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    backend: RecordingBackend
    output_dir: Path

    # ----- helpers --------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, body: Any, status: int = 200) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

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

    # ----- GET --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        if path == "/":
            html_text = _render_index_html()
            data = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/status":
            self._send_json({
                "voice_daemon_active": voice_daemon_active(),
                "session_id": self.backend.session_id(),
                "member": self.backend.member(),
                "is_recording": self.backend.is_recording(),
                "elapsed_sec": self.backend.elapsed_recording_sec(),
                "clip_count": len(self.backend.list_clips()),
            })
            return

        if path == "/api/clips":
            self._send_json({
                "clips": [c.to_json() for c in self.backend.list_clips()],
            })
            return

        if path.startswith("/api/clip/") and path.endswith("/wav"):
            self._serve_wav(path, url)
            return

        self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")

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

        try:
            body = self._read_json()
        except ValueError as e:
            self._send_error_json(400, str(e))
            return

        if path == "/api/session":
            member = (body.get("member") or "").strip()
            if not member:
                self._send_error_json(400, "member is required")
                return
            try:
                session_id = self.backend.begin_session(member)
            except (ValueError, StateError) as e:
                self._send_error_json(400, str(e))
                return
            self._send_json({"session_id": session_id, "member": member})
            return

        if path == "/api/clip/start":
            condition = (body.get("condition") or "").strip()
            distance = (body.get("distance") or "").strip()
            try:
                result = self.backend.start_recording(condition, distance)
            except (ValueError, StateError) as e:
                self._send_error_json(409, str(e))
                return
            self._send_json(result)
            return

        if path == "/api/clip/stop":
            try:
                clip = self.backend.stop_recording()
            except StateError as e:
                self._send_error_json(409, str(e))
                return
            self._send_json(clip.to_json())
            return

        if path == "/api/voice-daemon":
            action = (body.get("action") or "").strip()
            if action not in ("start", "stop"):
                self._send_error_json(400, "action must be start or stop")
                return
            import subprocess
            try:
                subprocess.run(
                    ["systemctl", action, VOICE_UNIT], check=True,
                )
            except subprocess.CalledProcessError as e:
                self._send_error_json(500, f"systemctl {action} failed: {e}")
                return
            self._send_json({
                "action": action,
                "voice_daemon_active": voice_daemon_active(),
            })
            return

        self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")

    # ----- DELETE -----------------------------------------------------

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        # /api/clip/<id>
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "clip":
            clip_id = parts[3]
            ok = self.backend.delete_clip(clip_id)
            if not ok:
                self._send_error_json(404, "clip not found")
                return
            self._send_json({"deleted": clip_id})
            return
        self.send_error(HTTPStatus.NOT_FOUND, f"not found: {path}")


def _make_handler_class(backend: RecordingBackend) -> type[_Handler]:
    class _BoundHandler(_Handler):
        pass
    _BoundHandler.backend = backend
    return _BoundHandler


# ---------------------------------------------------------------------------
# Frontend — single-file HTML+CSS+JS, no external assets
# ---------------------------------------------------------------------------


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JTS Wake-Word Corpus Recorder</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                   Roboto, sans-serif;
      max-width: 900px;
      margin: 1.5em auto;
      padding: 0 1em;
      color: #222;
      background: #fafaf7;
    }
    h1 { border-bottom: 2px solid #333; padding-bottom: 0.3em; }
    .card {
      background: #fff;
      border: 1px solid #ddd;
      border-radius: 6px;
      padding: 1em 1.2em;
      margin: 1em 0;
    }
    .row { display: flex; align-items: center; gap: 1em; margin: 0.5em 0; }
    .row label { font-weight: 600; min-width: 80px; }
    .row input[type=text], .row select {
      padding: 0.4em 0.6em;
      border: 1px solid #ccc;
      border-radius: 4px;
      font-size: 1em;
    }
    .pill {
      display: inline-block;
      padding: 0.2em 0.7em;
      border-radius: 999px;
      font-size: 0.86em;
      font-weight: 600;
    }
    .pill.green { background: #d8f0d8; color: #1f7a1f; }
    .pill.red { background: #f7d8d8; color: #a31f1f; }
    .pill.gray { background: #eee; color: #555; }
    button {
      padding: 0.45em 0.9em;
      border: 1px solid #999;
      border-radius: 4px;
      background: #f4f4f4;
      cursor: pointer;
      font-size: 0.95em;
    }
    button.primary {
      background: #1f7a1f;
      color: white;
      border-color: #1f7a1f;
      font-weight: 600;
    }
    button.danger { background: #a31f1f; color: white; border-color: #a31f1f; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    button.recordBtn {
      width: 100%;
      padding: 1.2em;
      font-size: 1.4em;
      font-weight: 700;
      letter-spacing: 0.05em;
    }
    button.recording {
      background: #d32d2d;
      color: white;
      border-color: #d32d2d;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(211, 45, 45, 0.55); }
      70% { box-shadow: 0 0 0 14px rgba(211, 45, 45, 0); }
    }
    .conditions, .distances {
      display: flex;
      gap: 0.5em;
      flex-wrap: wrap;
    }
    .conditions label, .distances label {
      flex: 1;
      min-width: 70px;
      display: flex;
      gap: 0.4em;
      padding: 0.5em 0.7em;
      border: 1px solid #ccc;
      border-radius: 4px;
      cursor: pointer;
      background: #f6f6f6;
      font-weight: 500;
    }
    .conditions input[type=radio]:checked + span,
    .distances input[type=radio]:checked + span {
      font-weight: 700;
    }
    .conditions label:has(input:checked),
    .distances label:has(input:checked) {
      background: #d8f0d8;
      border-color: #1f7a1f;
    }
    .clip {
      display: grid;
      grid-template-columns: 50px 80px 70px 60px 220px auto;
      gap: 0.6em;
      align-items: center;
      padding: 0.5em 0;
      border-bottom: 1px solid #eee;
      font-size: 0.92em;
    }
    .clip .seq { font-variant-numeric: tabular-nums; color: #888; }
    .clip.deleted { opacity: 0.4; text-decoration: line-through; }
    .counter {
      display: inline-block;
      min-width: 30px;
      padding: 0.1em 0.4em;
      border-radius: 3px;
      background: #eef;
      color: #335;
      font-variant-numeric: tabular-nums;
      font-weight: 600;
    }
    .matrix {
      display: grid;
      grid-template-columns: 60px repeat(3, 1fr);
      gap: 0.3em;
      margin: 0.5em 0;
      font-size: 0.88em;
    }
    .matrix > div {
      padding: 0.3em 0.5em;
      background: #f0f0e8;
      border-radius: 3px;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }
    .matrix > div.header { background: #ccc; font-weight: 600; }
    audio { height: 28px; }
    .err {
      color: #a31f1f;
      font-weight: 600;
      padding: 0.4em 0;
    }
  </style>
</head>
<body>
  <h1>JTS Wake-Word Corpus Recorder</h1>

  <div class="card" id="status-card">
    <div class="row">
      <label>jasper-voice:</label>
      <span id="voice-status" class="pill gray">checking…</span>
      <button id="voice-toggle" style="margin-left:auto">…</button>
    </div>
    <div class="row">
      <label>Session:</label>
      <span id="session-id">(no session)</span>
    </div>
  </div>

  <div class="card" id="session-card">
    <div class="row">
      <label for="member">Member:</label>
      <input type="text" id="member" value="jasper" maxlength="20">
      <button id="session-begin">Begin session</button>
    </div>
  </div>

  <div class="card" id="record-card" style="display:none">
    <h2 style="margin-top:0">Record a clip</h2>
    <div class="row">
      <label>Condition:</label>
      <div class="conditions">
        <label><input type="radio" name="condition" value="quiet" checked><span>quiet</span></label>
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
      Recommended per Phase 0b: ~13-14 utterances per cell across two sessions.
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

  <script>
    const $ = id => document.getElementById(id);
    let elapsedTimer = null;

    async function api(method, path, body) {
      const opts = { method, headers: {'Content-Type': 'application/json'} };
      if (body !== undefined) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      if (!r.ok) {
        const e = await r.json().catch(() => ({error: 'request failed'}));
        throw new Error(e.error || `${r.status}`);
      }
      return r.json();
    }

    function showErr(msg) {
      $('err').textContent = msg || '';
      if (msg) console.error(msg);
    }

    async function refreshStatus() {
      try {
        const s = await api('GET', '/api/status');
        const voiceEl = $('voice-status');
        const toggleEl = $('voice-toggle');
        if (s.voice_daemon_active) {
          voiceEl.textContent = 'RUNNING';
          voiceEl.className = 'pill red';
          toggleEl.textContent = 'Stop jasper-voice';
          toggleEl.onclick = () => toggleVoice('stop');
          $('record-btn').disabled = true;
        } else {
          voiceEl.textContent = 'stopped';
          voiceEl.className = 'pill green';
          toggleEl.textContent = 'Start jasper-voice';
          toggleEl.onclick = () => toggleVoice('start');
          $('record-btn').disabled = !s.session_id;
        }
        $('session-id').textContent = s.session_id
          ? `${s.member} / ${s.session_id}` : '(no session)';
        if (s.session_id) {
          $('record-card').style.display = 'block';
          $('counts-card').style.display = 'block';
          $('clips-card').style.display = 'block';
        }
        if (s.is_recording) {
          $('recording-info').style.display = 'block';
          $('record-btn').textContent = '■ STOP';
          $('record-btn').classList.add('recording');
          $('record-btn').classList.remove('primary');
          $('record-btn').disabled = false;
        } else {
          $('recording-info').style.display = 'none';
          $('record-btn').textContent = '● RECORD';
          $('record-btn').classList.remove('recording');
          $('record-btn').classList.add('primary');
        }
      } catch (e) { showErr(`status: ${e.message}`); }
    }

    async function toggleVoice(action) {
      try {
        await api('POST', '/api/voice-daemon', {action});
        await refreshStatus();
      } catch (e) { showErr(`voice-daemon ${action}: ${e.message}`); }
    }

    async function beginSession() {
      const member = $('member').value.trim();
      if (!member) { showErr('member is required'); return; }
      try {
        await api('POST', '/api/session', {member});
        showErr('');
        await refreshStatus();
        await refreshClips();
      } catch (e) { showErr(`begin session: ${e.message}`); }
    }

    function selectedRadio(name) {
      const r = document.querySelector(`input[name="${name}"]:checked`);
      return r ? r.value : null;
    }

    async function toggleRecord() {
      const isRecording = $('record-btn').classList.contains('recording');
      if (isRecording) {
        try {
          await api('POST', '/api/clip/stop', {});
          if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
          await refreshStatus();
          await refreshClips();
        } catch (e) { showErr(`stop: ${e.message}`); }
      } else {
        const condition = selectedRadio('condition');
        const distance = selectedRadio('distance');
        try {
          const r = await api('POST', '/api/clip/start', {condition, distance});
          showErr('');
          const startMs = Date.now();
          if (elapsedTimer) clearInterval(elapsedTimer);
          elapsedTimer = setInterval(() => {
            const s = ((Date.now() - startMs) / 1000).toFixed(1);
            $('elapsed').textContent = `${s}s`;
          }, 100);
          await refreshStatus();
        } catch (e) { showErr(`start: ${e.message}`); }
      }
    }

    async function refreshClips() {
      try {
        const r = await api('GET', '/api/clips');
        const list = $('clips-list');
        list.innerHTML = '';
        const counts = {};
        for (const c of r.clips) {
          const key = `${c.distance}-${c.condition}`;
          counts[key] = (counts[key] || 0) + 1;
          const row = document.createElement('div');
          row.className = 'clip';
          row.innerHTML = `
            <span class="seq">${String(c.seq).padStart(3, '0')}</span>
            <span>${c.condition}</span>
            <span>${c.distance}</span>
            <span>${c.duration_sec.toFixed(2)}s</span>
            <audio controls preload="none" src="/api/clip/${c.clip_id}/wav?leg=on"></audio>
            <button class="danger" data-id="${c.clip_id}">delete</button>
          `;
          row.querySelector('button').onclick = async (ev) => {
            const id = ev.target.dataset.id;
            if (!confirm(`Delete clip ${c.seq}?`)) return;
            try {
              await api('DELETE', `/api/clip/${id}`);
              await refreshClips();
            } catch (e) { showErr(`delete: ${e.message}`); }
          };
          list.prepend(row);  // newest first
        }
        renderCounts(counts);
      } catch (e) { showErr(`clips: ${e.message}`); }
    }

    function renderCounts(counts) {
      const matrix = $('counts-matrix');
      matrix.innerHTML = '';
      // Header row
      matrix.innerHTML = `
        <div class="header"></div>
        <div class="header">quiet</div>
        <div class="header">music</div>
        <div class="header">total</div>
      `;
      let grand = 0;
      for (const d of ['near', 'mid', 'far']) {
        const q = counts[`${d}-quiet`] || 0;
        const m = counts[`${d}-music`] || 0;
        const t = q + m;
        grand += t;
        matrix.innerHTML += `
          <div class="header">${d}</div>
          <div>${q}</div>
          <div>${m}</div>
          <div>${t}</div>
        `;
      }
      matrix.innerHTML += `
        <div class="header">total</div>
        <div></div><div></div>
        <div>${grand}</div>
      `;
    }

    $('session-begin').onclick = beginSession;
    $('record-btn').onclick = toggleRecord;

    // Spacebar toggles recording when a session is active + we're not
    // typing in an input. Convenient for hands-on-room workflows.
    document.addEventListener('keydown', (e) => {
      if (e.code !== 'Space') return;
      if (document.activeElement.tagName === 'INPUT') return;
      if ($('record-btn').disabled) return;
      e.preventDefault();
      toggleRecord();
    });

    refreshStatus();
    refreshClips();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>
"""


def _render_index_html() -> str:
    return _INDEX_HTML


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
        "--no-dtln", action="store_true",
        help="Skip the DTLN leg entirely (for 2-stream Pis or "
             "JASPER_WAKE_LEG_DTLN=0).",
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

    ports: dict[str, int] = {
        "on": args.aec_on_port,
        "off": args.aec_off_port,
    }
    if not args.no_dtln:
        ports["dtln"] = args.aec_dtln_port

    backend = RecordingBackend(args.output, ports=ports)
    backend.start()
    try:
        server = ThreadingHTTPServer(
            (args.host, args.port), _make_handler_class(backend),
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
