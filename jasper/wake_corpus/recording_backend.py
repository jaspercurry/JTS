# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Recording engine for the wake-corpus recorder.

The backend drives a background asyncio loop (in a daemon thread) from
sync HTTP handler threads via ``run_coroutine_threadsafe``; each clip's
UDP capture is a :class:`~jasper.wake_corpus.clip_capture.RecordingTask`
running on that loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jasper.aec_sweep import AEC3_SWEEP_SOURCE_XVF
from jasper.log_event import log_event
from jasper.mic_mute_persistence import (
    DEFAULT_PATH as MIC_MUTE_STATE_PATH,
    read_mic_muted,
)
from jasper.wake_ports import build_ports

from . import active_session, clip_recording, session_store
from .clip_capture import RecordingTask
from .clip_store import ClipStore
from .errors import (
    MIC_MUTED_MESSAGE,
    LifecycleBusyError,
    MicMutedError,
    StateError,
)
from .runtime_probe import PROFILE_STANDARD
from .session_store import ClipMetadata

logger = logging.getLogger("jasper-wake-corpus-web")


# ---------------------------------------------------------------------------
# Recorder-backend constants
# ---------------------------------------------------------------------------

DEFAULT_METADATA_SUBDIR = "metadata"

# Hard cap so a forgotten "stop" doesn't fill memory with a 1-hour
# buffer. The server auto-stops at this duration with a flag in the
# metadata so the operator notices.
MAX_RECORDING_DURATION_SEC = 30.0

STOP_SHUTDOWN_JOIN_SEC = 5.0


# ---------------------------------------------------------------------------
# Backend — the state the parts share, and its lifecycle
# ---------------------------------------------------------------------------


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
        # Serialize complete session/recording lifecycle transactions whose
        # slow I/O happens after _lock is released: begin/load/unload/delete,
        # UDP capture start, clip stop/save, and clip deletion. A start
        # reservation alone is not enough: without this guard a session
        # switch can pass its state check while RecordingTask.start() is
        # binding ports, or while stop_recording() is publishing WAVs and
        # metadata for the prior session.
        self._lifecycle_lock = threading.Lock()
        self._session_id: str | None = None
        self._member: str | None = None
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
        self._enabled_legs: tuple[str, ...] = active_session.default_enabled_legs(
            self._ports,
        )
        self._capture_plan: dict[str, Any] | None = None
        self._audio_context: dict[str, Any] | None = None
        self._clips = ClipStore(self._lock, output_dir)
        self._current: RecordingTask | None = None
        self._current_clip_id: str | None = None
        self._current_meta: dict[str, str] | None = None  # condition, distance, start_ts
        self._current_plan_conformance: dict[str, Any] | None = None
        # Sentinel: set inside _lock when a start_recording call has
        # passed validation but the (slow) RecordingTask.start() hasn't
        # finished yet, so is_recording() counts the clip while its UDP
        # ports bind. A concurrent Start is refused by _lifecycle_lock
        # before it reaches this.
        self._starting_clip_id: str | None = None
        self._auto_stop_handle: Any | None = None  # asyncio.TimerHandle
        self._mute_poll_handle: Any | None = None  # asyncio.TimerHandle
        self._pending_stop: tuple[bool, bool] | None = None  # auto, mute
        # A deferred stop belongs to one exact in-memory clip generation.
        # Both identities are required so stale Timer callbacks can never
        # retarget a later clip or clear its retry state.
        self._pending_stop_generation: clip_recording.StopGeneration | None = None
        self._stop_retry_handle: threading.Timer | None = None
        self._stop_retry_attempts = 0
        self._safety_workers: set[threading.Thread] = set()
        self._shutdown_started = False
        self._shutdown_owner: threading.Thread | None = None
        self._shutdown_complete = False

        # Background asyncio loop running in a daemon thread. Lazily
        # created in start() so tests can construct a backend without
        # immediately spawning the thread.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

    # ----- lifecycle -------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._shutdown_started:
                raise StateError("backend is shutting down")
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
        active_session.maybe_load_recent_session(self)
        # Self-heal jasper-voice if a previous run entered corpus test
        # mode (which stops voice) and never exited. Runs after session
        # recovery so a resumed session keeps voice stopped.
        active_session.maybe_recover_stale_test_mode(self)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def shutdown(self) -> None:
        current_thread = threading.current_thread()
        with self._lock:
            if self._shutdown_complete or self._shutdown_owner is not None:
                return
            self._shutdown_owner = current_thread
            self._shutdown_started = True
            retry_handle = self._stop_retry_handle
            safety_workers = set(self._safety_workers)
        finished = False
        try:
            if retry_handle is not None:
                retry_handle.cancel()
            # Initial safety workers and Timer retries may be inside _submit().
            # One shared deadline bounds every join; never self-join.
            owners = safety_workers | ({retry_handle} if retry_handle else set())
            deadline = time.monotonic() + STOP_SHUTDOWN_JOIN_SEC
            for owner in owners:
                if owner is not current_thread:
                    owner.join(timeout=max(0.0, deadline - time.monotonic()))
            if any(
                owner is not current_thread and owner.is_alive()
                for owner in owners
            ):
                logger.warning(
                    "wake-corpus shutdown left its loop alive while a stop "
                    "worker remained active",
                )
                return
            clip_recording.clear_pending_stop(self)
            loop = self._loop
            loop_thread = self._loop_thread
            if (
                loop_thread is None
                or not loop_thread.is_alive()
                or (loop is not None and loop.is_closed())
            ):
                finished = True
                return
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    # The loop can close between the state check and signal.
                    # That is success only when teardown actually won.
                    if loop.is_closed() or not loop_thread.is_alive():
                        finished = True
                        return
                    raise
            loop_thread.join(
                timeout=max(0.0, deadline - time.monotonic()),
            )
            if loop_thread.is_alive():
                logger.warning(
                    "wake-corpus shutdown timed out waiting for its loop",
                )
                return
            finished = True
        finally:
            with self._lock:
                if self._shutdown_owner is current_thread:
                    self._shutdown_owner = None
                    self._shutdown_complete = finished

    def _submit(self, coro: Any) -> Any:
        """Run a coroutine on the backend loop, block for the result."""
        if self._loop is None:
            raise RuntimeError("backend not started; call .start() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    @contextmanager
    def _lifecycle_transaction(
        self,
        busy_message: str,
    ) -> Iterator[None]:
        """Own one complete state-plus-I/O lifecycle transaction.

        Every caller fails fast when another transition owns the backend.
        Safety-triggered stops use the separate capped-backoff recovery path;
        no handler or worker blocks indefinitely on a stalled owner.
        """
        acquired = self._lifecycle_lock.acquire(blocking=False)
        if not acquired:
            raise LifecycleBusyError(busy_message)
        try:
            yield
        finally:
            self._lifecycle_lock.release()

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
            return (
                self._current is not None
                or self._starting_clip_id is not None
            )

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

    def enabled_legs(self) -> tuple[str, ...]:
        """The active session's leg set, in recording/playback order."""
        with self._lock:
            return self._enabled_legs

    def audio_context(self) -> dict[str, Any] | None:
        """Production-profile/corpus-context snapshot for the active session."""
        with self._lock:
            return dict(self._audio_context) if self._audio_context else None

    def status_snapshot(self) -> dict[str, Any]:
        """Every `/api/status` field, under one lock acquisition."""
        return active_session.status_snapshot(self)

    def start_recording(self, condition: str, distance: str) -> dict[str, str]:
        """Begin recording on the backend loop. Returns {clip_id, start_ts}."""
        with self._lifecycle_transaction("recording already in progress"):
            return clip_recording.start_recording(self, condition, distance)

    def stop_recording(
        self,
        auto: bool = False,
        mute_stopped: bool = False,
        *,
        _expected_generation: clip_recording.StopGeneration | None = None,
    ) -> ClipMetadata:
        """Stop the current recording, save WAVs, return metadata."""
        with self._lifecycle_transaction(
            "can't stop recording: lifecycle transition in progress",
        ):
            return clip_recording.stop_recording(
                self,
                auto=auto,
                mute_stopped=mute_stopped,
                expected_generation=_expected_generation,
            )

    def delete_clip(self, clip_id: str) -> bool:
        """Hard-delete a clip's WAVs + mark it deleted in metadata.

        Returns True if the clip existed and was deleted, False if
        not found (or already deleted)."""
        with self._lifecycle_transaction(
            "can't delete clip: lifecycle transition in progress",
        ):
            if not self._clips.delete(clip_id):
                return False
            active_session.save_metadata(self)
            logger.info("clip deleted: %s", clip_id)
            return True

    def list_clips(self, include_deleted: bool = False) -> list[ClipMetadata]:
        return self._clips.list_clips(include_deleted)

    def clip(self, clip_id: str) -> ClipMetadata | None:
        return self._clips.clip(clip_id)

    # ----- sessions (see active_session) -----------------------------

    def note_test_mode_entered(self) -> None:
        active_session.note_test_mode_entered(self)

    def note_test_mode_exited(self) -> None:
        active_session.note_test_mode_exited(self)

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
        capture_plan: dict[str, Any] | None = None,
    ) -> str:
        """Open one session transaction, refusing concurrent initializers."""
        with self._lifecycle_transaction(
            "can't begin session: initialization in progress",
        ):
            return active_session.begin_session(
                self,
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

    def list_sessions(self) -> list[dict[str, Any]]:
        """Saved-session summaries; see session_store.list_session_summaries."""
        return session_store.list_session_summaries(
            self._metadata_dir, self._ports, self._session_id,
        )

    def load_session(self, session_id: str) -> dict[str, Any]:
        """Switch the in-memory active session to an existing one on
        disk. Returns the loaded session's metadata.

        Refuses if a recording is in progress (would orphan the clip).
        Refuses if the target session doesn't exist.
        """
        with self._lifecycle_transaction(
            "can't load session: recording or session transition in progress",
        ):
            return active_session.load_session(self, session_id)

    def unload_session(self) -> str | None:
        """Clear the in-memory append target without deleting WAVs.

        This is the graceful end-of-session path for the web UI. The
        session remains in the Sessions list and can be explicitly
        loaded later, but a page refresh or server restart starts from
        a blank new-session form.
        """
        with self._lifecycle_transaction(
            "can't unload session: recording or session transition in progress",
        ):
            return active_session.unload_session(self)

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
        with self._lifecycle_transaction(
            "can't delete session: recording or session transition in progress",
        ):
            return active_session.delete_session(self, session_id)

