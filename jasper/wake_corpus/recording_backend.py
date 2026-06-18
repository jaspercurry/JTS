"""Recording engine for the wake-corpus recorder.

``RecordingBackend`` + its capture task, the per-clip metadata shape, and
the test-mode marker crash-recovery — all extracted verbatim from
``jasper/web/wake_corpus_setup.py``.

The backend drives a background asyncio loop (in a daemon thread) from
sync HTTP handler threads via ``run_coroutine_threadsafe``. It is the
upper layer of the recorder: it imports the bridge env / leg-plan /
capture-health helpers + shared leg/profile constants from
:mod:`jasper.wake_corpus.bridge_session`. ``UdpMicCapture`` is imported
lazily inside ``RecordingTask.start`` so this module stays importable on
dev machines without sounddevice/portaudio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from jasper.aec_sweep import (
    AEC3_SWEEP_SOURCE_USB,
    AEC3_SWEEP_SOURCE_XVF,
    config_metadata,
    variant_metadata,
)
from jasper.wake_conditions import CONDITIONS, DISTANCES
from jasper.cli.wake_enroll import VOICE_UNIT, write_wav
from jasper.log_event import log_event
from jasper.mic_mute_persistence import (
    DEFAULT_PATH as MIC_MUTE_STATE_PATH,
    read_mic_muted,
)
from jasper.wake_ports import build_ports

from .bridge_session import (
    AEC3_SWEEP_LEGS,
    CORPUS_PROFILES,
    DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
    DTLN_LEG,
    PROFILE_CHIP_AEC_COMPARISON,
    PROFILE_STANDARD,
    RAW0_LEG,
    USB_CORPUS_LEGS,
    USB_DTLN_LEG,
    XVF_RAW0_DTLN_LEG,
    _default_enabled_legs,
    _enabled_legs_from_metadata,
    _legacy_aec3_sweep_source,
    _metadata_flag,
    _session_aec3_sweep_source,
    _session_legs,
    build_capture_health,
    build_capture_plan,
    build_session_audio_context,
    chip_aec_config_metadata,
    exit_corpus_test_mode,
    read_bridge_stats_snapshot,
)

logger = logging.getLogger("jasper-wake-corpus-web")


# ---------------------------------------------------------------------------
# Recorder-backend constants
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR = Path("data/enrollment_positives")
DEFAULT_METADATA_SUBDIR = "metadata"
ACTIVE_SESSION_MARKER = ".active_session.json"
# Crash-safety marker for corpus test mode. Entering test mode stops
# jasper-voice (the UDP ports must be free to record), so an operator
# who opens the recorder and just closes the tab would otherwise leave
# the speaker permanently deaf — the socket-activated web service idle-
# exits after 10 min and nothing restarts jasper-voice. This marker
# records "test mode stopped voice" on disk; backend startup (which the
# socket re-runs on the next /wake-corpus/ request after an idle exit)
# restores production audio if the marker is stale and no session is
# being resumed. Cleared on a clean test-mode exit.
TEST_MODE_MARKER = ".corpus_test_mode.json"

# Hard cap so a forgotten "stop" doesn't fill memory with a 1-hour
# buffer. The server auto-stops at this duration with a flag in the
# metadata so the operator notices.
MAX_RECORDING_DURATION_SEC = 30.0

# How long after the last clip's metadata-file mtime we'll still
# resume a session on backend startup. Set to 1 hour so a quick crash-
# and-restart picks up cleanly, but a session abandoned overnight
# doesn't surprise the operator the next day with "wait, why does the
# UI show clips from yesterday?"
RESUME_WINDOW_SEC = 3600.0

# How often a live recording re-checks the persisted mic-mute flag
# (/var/lib/jasper/mic_mute.env). The corpus recorder runs while
# jasper-voice is STOPPED (test mode frees the UDP ports), so the
# daemon's own mute gate is absent — this poll is the only mid-
# recording enforcement of the household's privacy switch. 1 s bounds
# the post-mute capture window to ~1 s of a ≤30 s clip; the read is a
# tiny local-file stat+parse, safe on the backend loop.
MUTE_POLL_INTERVAL_SEC = 1.0

# How long after entering corpus test mode we treat the marker as
# abandoned and self-heal jasper-voice back on. Kept well under the
# jasper-web 10-min idle-exit window so that whenever the socket re-
# spawns the service (the next /wake-corpus/ request after the operator
# walked away), the marker is reliably stale and recovery fires. While a
# tab is open it polls /api/status every ~2 s, so the service never idle-
# exits and this never runs against a live session.
TEST_MODE_STALE_SEC = 300.0

METADATA_SCHEMA_VERSION = 2


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
    # True when the recording was force-stopped because the household
    # muted the mic mid-clip (see MUTE_POLL_INTERVAL_SEC). The audio on
    # disk predates the mute flip (±1 poll interval); the flag tells
    # the operator why the clip ended early.
    mute_stopped: bool = False
    notes: str = ""
    selected_legs: list[str] = field(default_factory=list)
    capture_plan: dict[str, Any] = field(default_factory=dict)
    audio_context: dict[str, Any] = field(default_factory=dict)
    capture_health: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Recording — the actual audio I/O
# ---------------------------------------------------------------------------


def compute_rms_dbfs(frame: np.ndarray) -> float:
    """Return the RMS of an int16 PCM frame in dBFS.

    -100.0 dBFS for near-silent or empty frames (avoids -inf from
    log(0)). 0.0 dBFS = full-scale int16. Used by the SSE level-meter
    endpoint so the UI can show a live "is your voice reaching the
    mic?" bar while recording.
    """
    if len(frame) == 0:
        return -100.0
    mean_sq = float(np.mean(frame.astype(np.float64) ** 2))
    if mean_sq < 1.0:
        return -100.0
    rms = mean_sq ** 0.5
    return 20.0 * float(np.log10(rms / 32768.0))


class RecordingTask:
    """Open-ended audio recording from multiple UDP captures.

    Constructed on each Start click; cancelled on Stop click. Background
    asyncio task streams frames into per-leg buffers. `stop()` cancels
    cleanly + returns the captured PCM bytes per leg.

    Side effect: while recording, updates `current_rms_dbfs` on every
    AEC-ON frame so the SSE level meter can read it. Only the AEC ON
    leg is metered (it's the canonical wake-detection signal); cost
    is one numpy reduction per ~80 ms.

    Memory bound: at 16 kHz mono int16 ≈ 32 KB/s per leg × 3 legs ≈
    96 KB/s. Capped to MAX_RECORDING_DURATION_SEC by the backend, so
    worst-case footprint is bounded.
    """

    def __init__(
        self,
        ports: dict[str, int],
        *,
        aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF,
    ) -> None:
        self._ports = ports
        self._aec3_sweep_source = aec3_sweep_source
        self._buffers: dict[str, list[np.ndarray]] = {leg: [] for leg in ports}
        self._captures: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._stack: AsyncExitStack | None = None
        self._start_monotonic: float = 0.0
        self._bridge_stats_start: dict[str, Any] | None = None
        self._bridge_stats_stop: dict[str, Any] | None = None
        # Live RMS of the most recent AEC ON frame, read by the SSE
        # level-meter handler. Written from the asyncio loop thread,
        # read from HTTP handler threads — single-float reads/writes
        # are atomic in CPython so no lock needed.
        self.current_rms_dbfs: float = -100.0

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
        except Exception:  # noqa: BLE001
            # If any leg fails to bind, clean up the ones that succeeded
            # so the user can retry without a "port already in use"
            # cascade on the next start.
            await self._stack.__aexit__(None, None, None)
            raise

        self._start_monotonic = time.monotonic()
        self._bridge_stats_start = read_bridge_stats_snapshot()
        self._task = asyncio.create_task(self._collect_all())

    async def _collect_all(self) -> None:
        async def _per_leg(leg: str, cap: Any) -> None:
            is_aec_on = (leg == "on")
            async for frame in cap.frames():
                self._buffers[leg].append(frame)
                # Live-meter the AEC ON leg only — it's the canonical
                # wake-detection signal. Single-float atomic write; no
                # lock needed (CPython guarantee).
                if is_aec_on:
                    self.current_rms_dbfs = compute_rms_dbfs(frame)

        await asyncio.gather(*[
            _per_leg(leg, cap) for leg, cap in self._captures.items()
        ])

    def elapsed_sec(self) -> float:
        if self._start_monotonic == 0:
            return 0.0
        return time.monotonic() - self._start_monotonic

    async def stop(self) -> dict[str, bytes]:
        """Cancel the collection task, return PCM bytes per leg.

        Idempotent: calling twice is a no-op on the second call (the
        task + stack sentinels are cleared after first cleanup, so we
        skip both double-await and double-exit which AsyncExitStack
        would error on).
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("recording task raised on cancel: %s", e)
        self._task = None

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
            except Exception as e:  # noqa: BLE001
                logger.warning("cleanup raised: %s", e)
            self._stack = None
        self._bridge_stats_stop = read_bridge_stats_snapshot()
        return result

    def capture_health(self, wall_duration_sec: float) -> dict[str, Any]:
        return build_capture_health(
            wall_duration_sec=wall_duration_sec,
            buffers=self._buffers,
            bridge_start=self._bridge_stats_start,
            bridge_stop=self._bridge_stats_stop,
            aec3_sweep_source=self._aec3_sweep_source,
        )


# ---------------------------------------------------------------------------
# Backend — single-recording state + persistence, thread-safe
# ---------------------------------------------------------------------------


class StateError(RuntimeError):
    """Raised when an operation isn't valid in the current state
    (e.g. starting a recording while one is in progress)."""


# Single user-facing refusal copy, shared by the backend's
# MicMutedError and the wizard's pre-side-effect fast path so the
# household sees one consistent message wherever the gate fires.
MIC_MUTED_MESSAGE = (
    "mic is muted — the wake-corpus recorder will not capture audio "
    "while the household mic mute is on. Unmute from the /system/ "
    "dashboard, then retry."
)


class MicMutedError(StateError):
    """Raised when the household mic-mute privacy switch is on.

    Mic mute is a privacy promise (see jasper/mic_mute_persistence.py)
    and is normally enforced inside jasper-voice — but the corpus
    recorder records the bridge's UDP legs directly while jasper-voice
    is stopped, so it must honor the persisted flag itself. Subclasses
    StateError so the wizard's existing error plumbing surfaces the
    message as an HTTP error without new handler branches."""


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
        mic_mute_path: Path | str = MIC_MUTE_STATE_PATH,
    ) -> None:
        self._output_dir = output_dir
        self._metadata_dir = output_dir / DEFAULT_METADATA_SUBDIR
        # Persisted household mic-mute flag — checked before any
        # session/recording starts and polled mid-recording. See
        # MicMutedError for why the recorder enforces this itself.
        self._mic_mute_path = mic_mute_path
        # All known ports. The recorder subscribes to a per-session
        # subset: base production legs by default, raw0 / USB / ref
        # only when the session opted in.
        self._ports = ports or build_ports()
        self._max_duration_sec = max_duration_sec

        # State guarded by _lock. Touched from HTTP handler threads
        # AND from the loop thread (auto-stop timer); the lock makes
        # all observers see consistent state.
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._member: str | None = None
        # Whether THIS session includes the truly-raw mic 0 leg. Set
        # by begin_session(include_raw_mic_0=…); read by
        # start_recording to decide which UDP ports to subscribe to.
        # Per-session (not per-clip) so a session's clips all share
        # the same leg set and downstream training tools can rely on
        # "session contains raw0 → every clip has it."
        self._include_raw_mic_0: bool = False
        self._include_dtln: bool = False
        self._include_usb_mic: bool = False
        self._include_usb_dtln: bool = False
        self._include_xvf_raw0_dtln: bool = False
        self._include_aec3_sweep: bool = False
        self._corpus_profile: str = PROFILE_STANDARD
        self._chip_aec_config: dict[str, object] | None = None
        self._aec3_sweep_source: str = AEC3_SWEEP_SOURCE_XVF
        self._aec3_sweep_variants: list[dict[str, object]] = []
        self._aec3_sweep_config: dict[str, object] | None = None
        self._enabled_legs: tuple[str, ...] = _default_enabled_legs(self._ports)
        self._capture_plan: dict[str, Any] | None = None
        self._audio_context: dict[str, Any] | None = None
        self._clips: list[ClipMetadata] = []
        self._current: RecordingTask | None = None
        self._current_clip_id: str | None = None
        self._current_meta: dict[str, str] | None = None  # condition, distance, start_ts
        # Sentinel: set inside _lock when a start_recording call has
        # passed validation but the (slow) RecordingTask.start() hasn't
        # finished yet. Concurrent start attempts see this and refuse
        # with the correct "already in progress" error rather than
        # racing into a UDP-bind-failed error.
        self._starting_clip_id: str | None = None
        self._auto_stop_handle: Any | None = None  # asyncio.TimerHandle
        self._mute_poll_handle: Any | None = None  # asyncio.TimerHandle

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
        # Recover from a previous run only when the prior process left
        # an active-session marker behind. A plain recent metadata file
        # is not enough: after a graceful test-mode exit, reopening the
        # page should feel like a fresh start.
        self._maybe_load_recent_session()
        # Self-heal jasper-voice if a previous run entered corpus test
        # mode (which stops voice) and never exited. Runs after session
        # recovery so a resumed session keeps voice stopped.
        self._maybe_recover_stale_test_mode()

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

    def ports(self) -> dict[str, int]:
        """Configured UDP ports this recorder process can subscribe to."""
        return dict(self._ports)

    def member(self) -> str | None:
        with self._lock:
            return self._member

    def is_recording(self) -> bool:
        with self._lock:
            return self._current is not None

    def mic_muted(self) -> bool:
        """Fresh read of the persisted household mic-mute flag.

        Read from disk every call (never cached) — the flag is toggled
        by jasper-control / the /system/ dashboard in a different
        process, so in-memory state would go stale. Fail-safe direction
        matches the daemon's: an unreadable/missing file reads as
        unmuted (see jasper/mic_mute_persistence.py)."""
        return read_mic_muted(self._mic_mute_path)

    def _refuse_if_muted(self, op: str) -> None:
        if not self.mic_muted():
            return
        log_event(
            logger,
            "wake_corpus.mute_refused",
            op=op,
            path=self._mic_mute_path,
            note="household mic mute is on; refusing to record",
            level=logging.WARNING,
        )
        raise MicMutedError(MIC_MUTED_MESSAGE)

    def get_current_rms_dbfs(self) -> float | None:
        """Latest AEC-ON RMS in dBFS, or None if not recording.

        Read by the /api/recording/level SSE endpoint, called ~12 Hz
        (matches the frame rate). Returns None when no recording is
        in flight; the UI grays out the level bar in that state.
        """
        with self._lock:
            if self._current is None:
                return None
            return self._current.current_rms_dbfs

    # ----- crash recovery -------------------------------------------

    def _active_session_marker_path(self) -> Path:
        return self._metadata_dir / ACTIVE_SESSION_MARKER

    def _write_active_session_marker(self) -> None:
        """Persist the session currently open for appending.

        Metadata files are historical artifacts. This marker is the
        narrow crash-recovery signal: if the web process dies while a
        session is open, startup can reattach; if the operator unloads
        or exits test mode cleanly, the marker is removed.
        """
        with self._lock:
            session_id = self._session_id
            member = self._member
        if session_id is None:
            return
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        path = self._active_session_marker_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = {
            "session_id": session_id,
            "member": member,
            "updated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
        }
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    def _clear_active_session_marker(self) -> None:
        try:
            self._active_session_marker_path().unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("failed to clear active session marker: %s", e)

    def _test_mode_marker_path(self) -> Path:
        return self._metadata_dir / TEST_MODE_MARKER

    def note_test_mode_entered(self) -> None:
        """Record that corpus test mode just stopped jasper-voice.

        The marker lets a later backend startup self-heal the speaker if
        the operator entered test mode and then walked away without
        exiting (see TEST_MODE_MARKER). Best-effort: a write failure must
        not block the operator from recording.
        """
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        path = self._test_mode_marker_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = {
            "entered_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
        }
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            tmp.replace(path)
        except OSError as e:
            logger.warning("failed to write test-mode marker: %s", e)

    def note_test_mode_exited(self) -> None:
        try:
            self._test_mode_marker_path().unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("failed to clear test-mode marker: %s", e)

    def _clear_session_state_locked(self) -> None:
        self._session_id = None
        self._member = None
        self._clips = []
        self._include_raw_mic_0 = False
        self._include_dtln = False
        self._include_usb_mic = False
        self._include_usb_dtln = False
        self._include_xvf_raw0_dtln = False
        self._include_aec3_sweep = False
        self._corpus_profile = PROFILE_STANDARD
        self._chip_aec_config = None
        self._aec3_sweep_source = AEC3_SWEEP_SOURCE_XVF
        self._aec3_sweep_variants = []
        self._aec3_sweep_config = None
        self._enabled_legs = _default_enabled_legs(self._ports)
        self._capture_plan = None
        self._audio_context = None

    def _find_session_metadata(self, session_id: str) -> Path | None:
        for p in self._metadata_dir.glob("enroll_*.json"):
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("session_id") == session_id:
                return p
        return None

    def _load_session_data(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            session_id = data["session_id"]
            member = data["member"]
            clips = [
                ClipMetadata(**c) for c in data.get("clips", [])
            ]
        except (KeyError, TypeError) as e:
            raise ValueError(f"session schema mismatch: {e}") from e
        enabled_legs = _enabled_legs_from_metadata(data, self._ports)
        corpus_profile = str(data.get("corpus_profile") or PROFILE_STANDARD)
        if corpus_profile not in CORPUS_PROFILES:
            corpus_profile = PROFILE_STANDARD
        saved_config = data.get("aec3_sweep_config")
        saved_source = (
            saved_config.get("input_source")
            if isinstance(saved_config, dict) else None
        )
        aec3_sweep_source = _legacy_aec3_sweep_source(
            str(data.get("aec3_sweep_source") or saved_source or ""),
        )
        include_raw_mic_0 = RAW0_LEG in enabled_legs
        include_usb_mic = bool(
            data.get(
                "include_usb_mic",
                any(leg in USB_CORPUS_LEGS for leg in enabled_legs),
            )
            or (
                aec3_sweep_source == AEC3_SWEEP_SOURCE_USB
                and any(leg in AEC3_SWEEP_LEGS for leg in enabled_legs)
            )
        )
        include_aec3_sweep = (
            bool(data.get("include_aec3_sweep", False))
            or any(leg in enabled_legs for leg in AEC3_SWEEP_LEGS)
        )
        saved_variants = data.get("aec3_sweep_variants")
        if not isinstance(saved_variants, list):
            saved_variants = []
        if not isinstance(saved_config, dict):
            saved_config = None
        if include_aec3_sweep and not saved_variants:
            saved_variants = variant_metadata(input_source=aec3_sweep_source)
            saved_config = config_metadata(input_source=aec3_sweep_source)
        elif include_aec3_sweep and saved_config is not None:
            saved_config = dict(saved_config)
            saved_config.setdefault("input_source", aec3_sweep_source)
        include_dtln = _metadata_flag(data, "include_dtln", DTLN_LEG, enabled_legs)
        include_usb_dtln = _metadata_flag(
            data, "include_usb_dtln", USB_DTLN_LEG, enabled_legs,
        )
        include_xvf_raw0_dtln = _metadata_flag(
            data, "include_xvf_raw0_dtln", XVF_RAW0_DTLN_LEG, enabled_legs,
        )
        chip_config = data.get("chip_aec_config")
        if not isinstance(chip_config, dict):
            chip_config = (
                chip_aec_config_metadata()
                if corpus_profile == PROFILE_CHIP_AEC_COMPARISON else None
            )
        audio_context = data.get("audio_context")
        if not isinstance(audio_context, dict):
            audio_context = None
        capture_plan = data.get("capture_plan")
        if not isinstance(capture_plan, dict):
            capture_plan = None
        with self._lock:
            self._session_id = session_id
            self._member = member
            self._clips = clips
            self._include_raw_mic_0 = RAW0_LEG in enabled_legs
            self._include_dtln = include_dtln
            self._include_usb_mic = include_usb_mic
            self._include_usb_dtln = include_usb_dtln
            self._include_xvf_raw0_dtln = include_xvf_raw0_dtln
            self._include_aec3_sweep = include_aec3_sweep
            self._corpus_profile = corpus_profile
            self._chip_aec_config = chip_config
            self._aec3_sweep_source = aec3_sweep_source
            self._aec3_sweep_variants = saved_variants
            self._aec3_sweep_config = saved_config
            self._enabled_legs = enabled_legs
            self._capture_plan = capture_plan
            self._audio_context = audio_context
        return {
            "session_id": session_id,
            "member": member,
            "clip_count": sum(1 for c in clips if not c.deleted),
            "include_raw_mic_0": include_raw_mic_0,
            "include_dtln": include_dtln,
            "include_usb_mic": include_usb_mic,
            "include_usb_dtln": include_usb_dtln,
            "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
            "include_aec3_sweep": include_aec3_sweep,
            "corpus_profile": corpus_profile,
            "aec3_sweep_source": aec3_sweep_source,
            "enabled_legs": list(enabled_legs),
            "has_capture_plan": capture_plan is not None,
            "has_audio_context": audio_context is not None,
        }

    def _maybe_load_recent_session(
        self, now: float | None = None,
    ) -> None:
        """Recover the marked active session after a server crash.

        Called automatically from `start()`. Safe to call multiple
        times (only triggers if no session is currently set).
        """
        with self._lock:
            if self._session_id is not None:
                return  # already have a session, nothing to recover
        if not self._metadata_dir.is_dir():
            return

        now = now if now is not None else time.time()
        marker = self._active_session_marker_path()
        if not marker.is_file():
            return
        age = now - marker.stat().st_mtime
        if age > RESUME_WINDOW_SEC:
            logger.info(
                "skipping recovery: active session marker is %.0fs old "
                "(window=%.0fs)", age, RESUME_WINDOW_SEC,
            )
            self._clear_active_session_marker()
            return

        try:
            marker_data = json.loads(marker.read_text())
            session_id = str(marker_data["session_id"])
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "recovery skipped: failed to read %s: %s", marker, e,
            )
            return
        except KeyError:
            logger.warning(
                "recovery skipped: %s lacks session_id", marker,
            )
            return

        target = self._find_session_metadata(session_id)
        if target is None:
            logger.warning(
                "recovery skipped: active session metadata missing for %s",
                session_id,
            )
            self._clear_active_session_marker()
            return

        try:
            result = self._load_session_data(json.loads(target.read_text()))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "recovery skipped: failed to restore %s: %s", target, e,
            )
            return
        logger.info(
            "recovered active session %s for %s clips=%d legs=%s",
            result["session_id"], result["member"], result["clip_count"],
            ",".join(result["enabled_legs"]),
        )

    def _maybe_recover_stale_test_mode(
        self, now: float | None = None,
    ) -> None:
        """Restore production audio after an abandoned test-mode session.

        Entering corpus test mode stops jasper-voice; a clean exit clears
        the marker and restarts it. If the operator instead walks away,
        the marker is left behind and the speaker stays deaf. On a later
        backend startup (the socket re-spawns the service on the next
        request after its idle exit) this restores production audio when
        the marker is stale and nothing is actively recording.

        Conservative by design — it does NOT tear down when:
          - a recording is in progress, or
          - a session was just resumed (operator is mid-corpus-session;
            voice must stay stopped), or
          - the marker is still fresh (tab open, operator working).
        """
        marker = self._test_mode_marker_path()
        if not marker.is_file():
            return
        with self._lock:
            recording = self._current is not None
            session_active = self._session_id is not None
        if recording or session_active:
            return
        now = now if now is not None else time.time()
        age = now - marker.stat().st_mtime
        if age <= TEST_MODE_STALE_SEC:
            return
        log_event(
            logger,
            "wake_corpus.test_mode_recover",
            age=f"{age:.0f}s",
            note=(
                f"stale={TEST_MODE_STALE_SEC:.0f}s: "
                f"restoring production audio + restarting {VOICE_UNIT}"
            ),
            level=logging.WARNING,
        )
        try:
            exit_corpus_test_mode()
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as e:
            # Leave the marker so a later startup retries the recovery;
            # never crash the recorder over a failed restart.
            log_event(
                logger,
                "wake_corpus.test_mode_recover_failed",
                error=e,
                level=logging.WARNING,
            )
            return
        self.note_test_mode_exited()

    def begin_session(
        self,
        member: str,
        corpus_profile: str = PROFILE_STANDARD,
        include_raw_mic_0: bool = False,
        include_dtln: bool = True,
        include_usb_mic: bool = False,
        include_usb_dtln: bool = False,
        include_xvf_raw0_dtln: bool = False,
        include_aec3_sweep: bool = False,
        aec3_sweep_source: str | None = None,
    ) -> str:
        """Open a fresh recording session. Resets the in-memory clip
        list (existing on-disk WAVs are untouched).

        `include_raw_mic_0` (default False) — when True, clips in this
        session also capture the truly-raw mic 0 leg (chip channel 2)
        into `aec_raw0_<condition>/`. Per-session, not per-clip, so
        downstream tools can rely on session-wide consistency.

        `include_dtln` (default True) — when True and the recorder has
        a DTLN port configured, clips capture the XVF raw-through-DTLN
        comparison leg.

        `include_usb_mic` (default False) — when True, clips also
        capture the corpus-only reference + cheap USB mic legs. These
        require matching bridge env flags to be enabled, otherwise the
        UDP captures will simply have no audio to write.

        `include_usb_dtln` (default False) — when True, clips capture
        the cheap USB raw-through-DTLN leg. The bridge must be started
        with JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1 for packets to arrive.

        `include_aec3_sweep` (default False) — when True, clips also
        capture the bounded same-utterance AEC3 tuning variants emitted
        by jasper-aec-bridge. These are pilot/tuning legs, not
        production wake inputs.

        `aec3_sweep_source` selects which raw mic feeds those variants.
        New sessions default to the cheap USB mic so one utterance yields
        USB baseline + three USB AEC3 variants while retaining the XVF
        baseline leg for comparison.

        Returns the new session_id (UTC timestamp).
        """
        self._refuse_if_muted("begin_session")
        safe_member = "".join(c for c in member.lower() if c.isalnum() or c == "_")
        if not safe_member:
            raise ValueError(f"member name has no usable chars: {member!r}")
        if corpus_profile not in CORPUS_PROFILES:
            raise ValueError(f"unknown corpus profile: {corpus_profile!r}")
        if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
            include_raw_mic_0 = True
            include_aec3_sweep = False
            sweep_source = AEC3_SWEEP_SOURCE_XVF
        else:
            sweep_source = (
                _session_aec3_sweep_source(aec3_sweep_source)
                if include_aec3_sweep else AEC3_SWEEP_SOURCE_XVF
            )
        effective_include_usb_mic = include_usb_mic or (
            include_aec3_sweep and sweep_source == AEC3_SWEEP_SOURCE_USB
        )
        with self._lock:
            if self._current is not None:
                raise StateError(
                    "can't begin session: recording in progress",
                )
            # session_id = UTC second-resolution timestamp + a 4-hex
            # suffix. The suffix avoids a collision when an operator
            # (or a test) calls begin_session() twice within the same
            # second — without it, two sessions would share both the
            # in-memory id AND the on-disk metadata filename, and the
            # second would silently overwrite the first.
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            enabled_legs = _session_legs(
                self._ports,
                corpus_profile=corpus_profile,
                include_dtln=include_dtln,
                include_raw_mic_0=include_raw_mic_0,
                include_usb_mic=effective_include_usb_mic,
                include_usb_dtln=include_usb_dtln,
                include_xvf_raw0_dtln=include_xvf_raw0_dtln,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=sweep_source,
            )
            sweep_variants = (
                variant_metadata(input_source=sweep_source)
                if include_aec3_sweep else []
            )
            sweep_config = (
                config_metadata(input_source=sweep_source)
                if include_aec3_sweep else None
            )
            session_id = f"{ts}-{secrets.token_hex(2)}"
            chip_config = (
                chip_aec_config_metadata()
                if corpus_profile == PROFILE_CHIP_AEC_COMPARISON else None
            )
            capture_plan = build_capture_plan(
                self._ports,
                corpus_profile=corpus_profile,
                include_raw_mic_0=RAW0_LEG in enabled_legs,
                include_dtln=DTLN_LEG in enabled_legs,
                include_usb_mic=effective_include_usb_mic,
                include_usb_dtln=USB_DTLN_LEG in enabled_legs,
                include_xvf_raw0_dtln=XVF_RAW0_DTLN_LEG in enabled_legs,
                include_aec3_sweep=include_aec3_sweep,
                aec3_sweep_source=sweep_source,
                include_bridge_readiness=True,
                include_runtime_profile=True,
            )
            self._session_id = session_id
            self._member = safe_member
            self._clips = []
            self._include_raw_mic_0 = RAW0_LEG in enabled_legs
            self._include_dtln = DTLN_LEG in enabled_legs
            self._include_usb_mic = effective_include_usb_mic
            self._include_usb_dtln = USB_DTLN_LEG in enabled_legs
            self._include_xvf_raw0_dtln = XVF_RAW0_DTLN_LEG in enabled_legs
            self._include_aec3_sweep = include_aec3_sweep
            self._corpus_profile = corpus_profile
            self._chip_aec_config = chip_config
            self._aec3_sweep_source = sweep_source
            self._aec3_sweep_variants = sweep_variants
            self._aec3_sweep_config = sweep_config
            self._enabled_legs = enabled_legs
            self._capture_plan = capture_plan
            self._audio_context = None
        audio_context = build_session_audio_context(
            corpus_profile=corpus_profile,
            enabled_legs=enabled_legs,
            ports=self._ports,
            include_raw_mic_0=RAW0_LEG in enabled_legs,
            include_dtln=DTLN_LEG in enabled_legs,
            include_usb_mic=effective_include_usb_mic,
            include_usb_dtln=USB_DTLN_LEG in enabled_legs,
            include_xvf_raw0_dtln=XVF_RAW0_DTLN_LEG in enabled_legs,
            include_aec3_sweep=include_aec3_sweep,
            aec3_sweep_source=sweep_source,
            chip_aec_config=chip_config,
            capture_plan=capture_plan,
        )
        with self._lock:
            if self._session_id == session_id:
                self._audio_context = audio_context
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        self._save_metadata()  # write the per-session flag before clips arrive
        self._write_active_session_marker()
        return session_id

    def include_raw_mic_0(self) -> bool:
        """Whether the active session captures the raw-mic-0 leg."""
        with self._lock:
            return self._include_raw_mic_0

    def include_dtln(self) -> bool:
        """Whether the active session captures the XVF DTLN leg."""
        with self._lock:
            return self._include_dtln

    def include_usb_mic(self) -> bool:
        """Whether the active session captures corpus USB/ref legs."""
        with self._lock:
            return self._include_usb_mic

    def include_usb_dtln(self) -> bool:
        """Whether the active session captures the USB DTLN leg."""
        with self._lock:
            return self._include_usb_dtln

    def include_xvf_raw0_dtln(self) -> bool:
        """Whether the active session captures the XVF raw0 DTLN leg."""
        with self._lock:
            return self._include_xvf_raw0_dtln

    def include_aec3_sweep(self) -> bool:
        """Whether the active session captures same-utterance AEC3 variants."""
        with self._lock:
            return self._include_aec3_sweep

    def corpus_profile(self) -> str:
        with self._lock:
            return self._corpus_profile

    def chip_aec_config(self) -> dict[str, object] | None:
        with self._lock:
            return dict(self._chip_aec_config) if self._chip_aec_config else None

    def aec3_sweep_source(self) -> str:
        """Mic source that feeds the active session's AEC3 sweep variants."""
        with self._lock:
            return self._aec3_sweep_source

    def aec3_sweep_variants(self) -> list[dict[str, object]]:
        """Effective AEC3 sweep variants for the active session or UI status."""
        with self._lock:
            if self._include_aec3_sweep and self._aec3_sweep_variants:
                return list(self._aec3_sweep_variants)
        return variant_metadata(input_source=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE)

    def aec3_sweep_config(self) -> dict[str, object]:
        """Effective AEC3 sweep config provenance for the active session/status."""
        with self._lock:
            if self._include_aec3_sweep and self._aec3_sweep_config:
                return dict(self._aec3_sweep_config)
        return config_metadata(input_source=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE)

    def enabled_legs(self) -> tuple[str, ...]:
        """The active session's leg set, in recording/playback order."""
        with self._lock:
            return self._enabled_legs

    def capture_plan(self) -> dict[str, Any] | None:
        """Layered mic/channel/transform plan for the active session."""
        with self._lock:
            return dict(self._capture_plan) if self._capture_plan else None

    def audio_context(self) -> dict[str, Any] | None:
        """Production-profile/corpus-context snapshot for the active session."""
        with self._lock:
            return dict(self._audio_context) if self._audio_context else None

    def start_recording(self, condition: str, distance: str) -> dict[str, str]:
        """Begin recording on the backend loop. Returns {clip_id, start_ts}.

        Reserves the recording slot under the lock via
        `_starting_clip_id` before releasing for the slow async start;
        concurrent calls see the sentinel and refuse with the correct
        "already in progress" error instead of racing into a UDP-bind
        failure.
        """
        if condition not in CONDITIONS:
            raise ValueError(
                f"unknown condition {condition!r}; expected {CONDITIONS}",
            )
        if distance not in DISTANCES:
            raise ValueError(
                f"unknown distance {distance!r}; expected {DISTANCES}",
            )
        # Privacy gate: a session begun while unmuted can outlive a
        # later mute toggle, so re-check at every clip start too.
        self._refuse_if_muted("start_recording")

        clip_id = str(uuid.uuid4())
        with self._lock:
            if self._session_id is None or self._member is None:
                raise StateError("call begin_session() first")
            if self._current is not None or self._starting_clip_id is not None:
                raise StateError("recording already in progress")
            # Reserve the slot — concurrent calls now see this and
            # refuse cleanly.
            self._starting_clip_id = clip_id
            # Per-session leg selection. Built under the lock so the
            # session's clips all share one leg set.
            active_legs = list(self._enabled_legs)
            aec3_sweep_source = self._aec3_sweep_source

        ports_for_task = {
            leg: self._ports[leg]
            for leg in active_legs if leg in self._ports
        }
        task = RecordingTask(
            ports_for_task,
            aec3_sweep_source=aec3_sweep_source,
        )
        # Start on the backend loop. If the UDP bind fails (jasper-voice
        # is still up, port already in use), this raises and we never
        # transition into the recording state.
        try:
            self._submit(task.start())
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._starting_clip_id = None
            raise StateError(
                f"failed to start recording (is jasper-voice down?): {e}",
            ) from e

        start_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            self._current = task
            self._current_clip_id = clip_id
            self._current_meta = {
                "condition": condition,
                "distance": distance,
                "start_ts": start_ts,
            }
            self._starting_clip_id = None  # transitioned: starting → current
            # Auto-stop timer — guards against a forgotten Stop click.
            self._auto_stop_handle = self._loop.call_later(
                self._max_duration_sec, self._auto_stop_threadsafe,
            )
            # Mid-recording mute watch — if the household flips the mic
            # mute while a clip is rolling, stop within one poll.
            self._mute_poll_handle = self._loop.call_later(
                MUTE_POLL_INTERVAL_SEC, self._mute_poll,
            )
        return {"clip_id": clip_id, "start_ts": start_ts}

    def _mute_poll(self) -> None:
        """Runs on the backend loop every MUTE_POLL_INTERVAL_SEC while a
        recording is in flight. Stops the recording (keeping the partial
        clip, flagged `mute_stopped`) the first poll after the household
        mutes the mic. The retained audio was captured while unmuted —
        modulo at most one poll interval — so keeping it is consistent
        with the privacy promise while telling the operator why the clip
        ended early. Fail-soft: a poll error logs and rearms rather than
        leaving the recording unwatched."""
        with self._lock:
            if self._current is None:
                self._mute_poll_handle = None
                return
        try:
            muted = self.mic_muted()
        except Exception as e:  # noqa: BLE001 — never kill the watch
            log_event(
                logger,
                "wake_corpus.mute_poll_failed",
                error=e,
                level=logging.WARNING,
            )
            muted = False
        if muted:
            log_event(
                logger,
                "wake_corpus.mute_stop",
                note="mic muted mid-recording; stopping the clip",
                level=logging.WARNING,
            )
            # stop_recording is sync + blocks on the loop; hand it to a
            # worker thread (same shape as the auto-stop timer).
            threading.Thread(
                target=self._mute_stop_safe, daemon=True,
            ).start()
            return
        with self._lock:
            if self._current is not None and self._loop is not None:
                self._mute_poll_handle = self._loop.call_later(
                    MUTE_POLL_INTERVAL_SEC, self._mute_poll,
                )

    def _mute_stop_safe(self) -> None:
        try:
            # auto=False: this is not the duration-cap auto-stop — the
            # pending auto-stop timer must be cancelled, and the clip's
            # auto_stopped flag must stay False so downstream tools
            # don't misread a privacy stop as a forgotten Stop click.
            self.stop_recording(auto=False, mute_stopped=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("mute-stop failed: %s", e)

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
        except Exception as e:  # noqa: BLE001
            logger.warning("auto-stop failed: %s", e)

    def stop_recording(
        self, auto: bool = False, mute_stopped: bool = False,
    ) -> ClipMetadata:
        """Stop the current recording, save WAVs, return metadata."""
        with self._lock:
            if self._current is None:
                raise StateError("no recording in progress")
            task = self._current
            clip_id = self._current_clip_id
            meta = self._current_meta
            session_id = self._session_id
            member = self._member
            selected_legs = list(self._enabled_legs)
            capture_plan = dict(self._capture_plan or {})
            audio_context = dict(self._audio_context or {})
            # Cancel the auto-stop timer if it hasn't fired yet.
            if self._auto_stop_handle is not None and not auto:
                self._auto_stop_handle.cancel()
            self._auto_stop_handle = None
            # The mute watch dies with the recording (cancelling an
            # already-fired handle is a harmless no-op).
            if self._mute_poll_handle is not None:
                self._mute_poll_handle.cancel()
            self._mute_poll_handle = None
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
        capture_health = task.capture_health(duration_sec)

        # Pick the next sequence number. Sequence is per-session, not
        # per-condition, so filenames stay unique across the whole
        # session. Include deleted clips in the max() so a later clip
        # never reuses a previous filename after the operator deletes
        # one bad take.
        with self._lock:
            seq = max((c.seq for c in self._clips), default=0) + 1

        files: dict[str, str] = {}
        # Condition → directory mapping. "nomusic" preserved for
        # backward compat with existing recordings + downstream tools
        # (extract-wake-corpus.py emits the same name). "ambient" gets
        # its own dir so training can slice on it explicitly.
        condition_dir = {
            "quiet": "nomusic",
            "ambient": "ambient",
            "music": "music",
        }[meta["condition"]]
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
            mute_stopped=mute_stopped,
            selected_legs=selected_legs,
            capture_plan=capture_plan,
            audio_context=audio_context,
            capture_health=capture_health,
        )
        with self._lock:
            self._clips.append(clip)
        self._save_metadata()
        logger.info(
            "clip saved: %s seq=%d condition=%s distance=%s dur=%.2fs%s%s",
            clip_id, seq, meta["condition"], meta["distance"],
            duration_sec, " (auto-stopped)" if auto else "",
            " (mute-stopped)" if mute_stopped else "",
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
                "metadata_schema_version": METADATA_SCHEMA_VERSION,
                "session_id": self._session_id,
                "member": self._member,
                "ports": self._ports,
                "include_raw_mic_0": self._include_raw_mic_0,
                "include_dtln": self._include_dtln,
                "include_usb_mic": self._include_usb_mic,
                "include_usb_dtln": self._include_usb_dtln,
                "include_xvf_raw0_dtln": self._include_xvf_raw0_dtln,
                "include_aec3_sweep": self._include_aec3_sweep,
                "corpus_profile": self._corpus_profile,
                "chip_aec_config": self._chip_aec_config,
                "aec3_sweep_source": self._aec3_sweep_source,
                "aec3_sweep_variants": list(self._aec3_sweep_variants),
                "aec3_sweep_config": self._aec3_sweep_config,
                "enabled_legs": list(self._enabled_legs),
                "capture_plan": self._capture_plan,
                "audio_context": self._audio_context,
                "clips": [c.to_json() for c in self._clips],
            }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)

    # ----- sessions management --------------------------------------

    def list_sessions(self) -> list[dict[str, Any]]:
        """Scan the metadata dir, return one summary per session.

        Each summary: {session_id, member, mtime, clip_count,
        deleted_count, enabled_legs, conditions: {<cond>: n, ...}}.
        Sorted newest-first by mtime.

        Failure-soft: corrupt JSON files are skipped + logged, not
        raised — one bad file shouldn't black out the whole list.
        """
        if not self._metadata_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for p in sorted(
            self._metadata_dir.glob("enroll_*.json"),
            key=lambda f: f.stat().st_mtime, reverse=True,
        ):
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("skip corrupt session %s: %s", p.name, e)
                continue
            clips = data.get("clips", [])
            alive = [c for c in clips if not c.get("deleted")]
            conds: dict[str, int] = {}
            for c in alive:
                k = c.get("condition", "?")
                conds[k] = conds.get(k, 0) + 1
            enabled_legs = _enabled_legs_from_metadata(data, self._ports)
            saved_config = data.get("aec3_sweep_config")
            saved_source = (
                saved_config.get("input_source")
                if isinstance(saved_config, dict) else None
            )
            aec3_sweep_source = _legacy_aec3_sweep_source(
                str(data.get("aec3_sweep_source") or saved_source or ""),
            )
            audio_context = data.get("audio_context")
            if not isinstance(audio_context, dict):
                audio_context = {}
            capture_plan = data.get("capture_plan")
            if not isinstance(capture_plan, dict):
                capture_plan = {}
            resource = capture_plan.get("resource")
            if not isinstance(resource, dict):
                resource = {}
            audio_profile = audio_context.get("production_audio_profile")
            if not isinstance(audio_profile, dict):
                audio_profile = {}
            dac_reference = audio_context.get("dac_reference")
            if not isinstance(dac_reference, dict):
                dac_reference = {}
            validation = dac_reference.get("validation")
            if not isinstance(validation, dict):
                validation = {}
            out.append({
                "session_id": data.get("session_id", "?"),
                "member": data.get("member", "?"),
                "metadata_schema_version": data.get("metadata_schema_version"),
                "mtime": p.stat().st_mtime,
                "clip_count": len(alive),
                "deleted_count": len(clips) - len(alive),
                "include_raw_mic_0": bool(data.get("include_raw_mic_0", False)),
                "include_dtln": _metadata_flag(
                    data, "include_dtln", DTLN_LEG, enabled_legs,
                ),
                "include_usb_mic": bool(data.get("include_usb_mic", False)),
                "include_usb_dtln": _metadata_flag(
                    data, "include_usb_dtln", USB_DTLN_LEG, enabled_legs,
                ),
                "include_xvf_raw0_dtln": _metadata_flag(
                    data, "include_xvf_raw0_dtln", XVF_RAW0_DTLN_LEG, enabled_legs,
                ),
                "include_aec3_sweep": (
                    bool(data.get("include_aec3_sweep", False))
                    or any(leg in enabled_legs for leg in AEC3_SWEEP_LEGS)
                ),
                "corpus_profile": data.get("corpus_profile", PROFILE_STANDARD),
                "aec3_sweep_source": aec3_sweep_source,
                "enabled_legs": list(enabled_legs),
                "has_audio_context": bool(audio_context),
                "audio_profile_requested": audio_profile.get("requested"),
                "audio_profile_active": audio_profile.get("active"),
                "audio_profile_state": audio_profile.get("state"),
                "audio_validation_status": validation.get("status"),
                "capture_plan_recipe": capture_plan.get("recipe"),
                "capture_plan_resource_level": resource.get("level"),
                "conditions": conds,
                "is_active": (
                    self._session_id is not None
                    and data.get("session_id") == self._session_id
                ),
            })
        return out

    def load_session(self, session_id: str) -> dict[str, Any]:
        """Switch the in-memory active session to an existing one on
        disk. Returns the loaded session's metadata.

        Refuses if a recording is in progress (would orphan the clip).
        Refuses if the target session doesn't exist.
        """
        with self._lock:
            if self._current is not None:
                raise StateError(
                    "can't load session: recording in progress",
                )
        target = self._find_session_metadata(session_id)
        if target is None:
            raise ValueError(f"session not found: {session_id}")

        data = json.loads(target.read_text())
        try:
            result = self._load_session_data(data)
        except ValueError as e:
            raise ValueError(
                f"session {session_id} schema mismatch: {e}",
            ) from e
        self._write_active_session_marker()
        logger.info(
            "loaded session %s for %s with %d clip(s) include_raw_mic_0=%s "
            "include_dtln=%s include_usb_mic=%s include_usb_dtln=%s "
            "include_aec3_sweep=%s aec3_sweep_source=%s legs=%s",
            session_id, result["member"], result["clip_count"],
            result["include_raw_mic_0"], result["include_dtln"],
            result["include_usb_mic"], result["include_usb_dtln"],
            result["include_aec3_sweep"], result["aec3_sweep_source"],
            ",".join(result["enabled_legs"]),
        )
        return result

    def unload_session(self) -> str | None:
        """Clear the in-memory append target without deleting WAVs.

        This is the graceful end-of-session path for the web UI. The
        session remains in the Sessions list and can be explicitly
        loaded later, but a page refresh or server restart starts from
        a blank new-session form.
        """
        with self._lock:
            if self._current is not None or self._starting_clip_id is not None:
                raise StateError(
                    "can't unload session: recording in progress",
                )
            session_id = self._session_id
            self._clear_session_state_locked()
        self._clear_active_session_marker()
        if session_id is not None:
            logger.info("unloaded session %s", session_id)
        return session_id

    def delete_session(self, session_id: str) -> dict[str, int]:
        """Hard-delete every WAV referenced by a session + remove the
        JSON sidecar. Returns {wavs_deleted, wavs_missing}.

        Refuses if a recording is in progress (covers the case where
        the operator tries to delete the session they're recording
        into).

        If the deleted session was the active in-memory one, clears
        the in-memory state (operator now needs to begin a new
        session or load another).
        """
        with self._lock:
            if self._current is not None:
                raise StateError(
                    "can't delete session: recording in progress",
                )
        target = self._find_session_metadata(session_id)
        if target is None:
            raise ValueError(f"session not found: {session_id}")

        data = json.loads(target.read_text())
        wavs_deleted = 0
        wavs_missing = 0
        for c in data.get("clips", []):
            if c.get("deleted"):
                # Already-deleted clips have already had their WAVs
                # removed by delete_clip(); skip + don't count.
                continue
            for path_str in (c.get("files") or {}).values():
                p_wav = Path(path_str)
                try:
                    p_wav.unlink()
                    wavs_deleted += 1
                except FileNotFoundError:
                    wavs_missing += 1
                except OSError as e:
                    logger.warning("failed to delete %s: %s", p_wav, e)
                    wavs_missing += 1
        target.unlink()

        # If we just deleted the in-memory active session, clear state.
        with self._lock:
            if self._session_id == session_id:
                self._clear_session_state_locked()
                self._clear_active_session_marker()
        logger.info(
            "deleted session %s: %d wavs removed, %d missing",
            session_id, wavs_deleted, wavs_missing,
        )
        return {"wavs_deleted": wavs_deleted, "wavs_missing": wavs_missing}
