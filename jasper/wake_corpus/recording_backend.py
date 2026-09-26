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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jasper.aec_sweep import (
    AEC3_SWEEP_SOURCE_XVF,
    config_metadata,
    variant_metadata,
)
from jasper.log_event import log_event
from jasper.mic_mute_persistence import (
    DEFAULT_PATH as MIC_MUTE_STATE_PATH,
    read_mic_muted,
)
from jasper.wake_conditions import CONDITIONS, DISTANCES
from jasper.wake_ports import build_ports

from . import active_session, session_store
from .capture_plan import validate_active_capture_plan
from .clip_capture import RecordingTask
from .clip_store import ClipStore
from .errors import (
    MIC_MUTED_MESSAGE,
    LifecycleBusyError,
    MicMutedError,
    NoRecordingError,
    StateError,
)
from .runtime_probe import (
    DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
    PROFILE_STANDARD,
)
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

# How often a live recording re-checks the persisted mic-mute flag
# (/var/lib/jasper/mic_mute.env). The corpus recorder runs while
# jasper-voice is STOPPED (test mode frees the UDP ports), so the
# daemon's own mute gate is absent — this poll is the only mid-
# recording enforcement of the household's privacy switch. 1 s bounds
# the post-mute capture window to ~1 s of a ≤30 s clip; the read is a
# tiny local-file stat+parse, safe on the backend loop.
MUTE_POLL_INTERVAL_SEC = 1.0

# Safety-triggered stops never wait on the lifecycle mutex. They quiesce the
# capture immediately, then retry clip publication with one in-flight,
# capped-backoff threading.Timer until the current short-lived owner releases.
# This preserves eventual save without mutating the asyncio loop from a worker
# or stranding an HTTP/worker thread behind I/O of unknown duration.
STOP_RETRY_INITIAL_SEC = 0.05
STOP_RETRY_MAX_SEC = 1.0
# Ceiling on total retries before giving up on publishing the clip — a
# lifecycle owner that never releases would otherwise retry forever.
# Sum of the capped-exponential delays above, one per attempt: at the
# current STOP_RETRY_INITIAL_SEC/STOP_RETRY_MAX_SEC, 20 attempts is
# ~16.6 s of wall clock before abandonment.
STOP_RETRY_MAX_ATTEMPTS = 20
STOP_SHUTDOWN_JOIN_SEC = 5.0


_StopGeneration = tuple[str, RecordingTask]


# ---------------------------------------------------------------------------
# Backend — single-recording state + persistence, thread-safe
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
        # finished yet. Concurrent start attempts see this and refuse
        # with the correct "already in progress" error rather than
        # racing into a UDP-bind-failed error.
        self._starting_clip_id: str | None = None
        self._auto_stop_handle: Any | None = None  # asyncio.TimerHandle
        self._mute_poll_handle: Any | None = None  # asyncio.TimerHandle
        self._pending_stop: tuple[bool, bool] | None = None  # auto, mute
        # A deferred stop belongs to one exact in-memory clip generation.
        # Both identities are required so stale Timer callbacks can never
        # retarget a later clip or clear its retry state.
        self._pending_stop_generation: _StopGeneration | None = None
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
            self._clear_pending_stop()
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
        """Every `/api/status` field, read under one lock acquisition so a
        session switch (begin/load/unload) cannot mix fields from two
        sessions in one response.
        """
        with self._lock:
            include_aec3_sweep = self._include_aec3_sweep
            aec3_sweep_variants = (
                list(self._aec3_sweep_variants)
                if include_aec3_sweep and self._aec3_sweep_variants else None
            )
            aec3_sweep_config = (
                dict(self._aec3_sweep_config)
                if include_aec3_sweep and self._aec3_sweep_config else None
            )
            capture_plan = dict(self._capture_plan) if self._capture_plan else None
            capture_plan_conformance = (
                dict(self._current_plan_conformance)
                if self._current_plan_conformance else None
            )
            snapshot = {
                "session_id": self._session_id,
                "member": self._member,
                "include_raw_mic_0": self._include_raw_mic_0,
                "include_dtln": self._include_dtln,
                "include_usb_mic": self._include_usb_mic,
                "include_usb_dtln": self._include_usb_dtln,
                "include_xvf_raw0_dtln": self._include_xvf_raw0_dtln,
                "include_aec3_sweep": include_aec3_sweep,
                "corpus_profile": self._corpus_profile,
                "chip_aec_config": (
                    dict(self._chip_aec_config) if self._chip_aec_config else None
                ),
                "aec3_sweep_source": self._aec3_sweep_source,
                "enabled_legs": list(self._enabled_legs),
                "capture_plan": capture_plan,
                "audio_context": (
                    dict(self._audio_context) if self._audio_context else None
                ),
                "is_recording": (
                    self._current is not None
                    or self._starting_clip_id is not None
                ),
                "elapsed_sec": (
                    self._current.elapsed_sec() if self._current is not None else 0.0
                ),
                "clip_count": self._clips.live_count_locked(),
            }
        # Stateless fallbacks + the conformance re-check are pure functions
        # of the values snapshotted above, so they run outside the lock
        # without re-reading any `self._*` field.
        snapshot["aec3_sweep_variants"] = aec3_sweep_variants or variant_metadata(
            input_source=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
        )
        snapshot["aec3_sweep_config"] = aec3_sweep_config or config_metadata(
            input_source=DEFAULT_NEW_SESSION_AEC3_SWEEP_SOURCE,
        )
        if capture_plan and capture_plan_conformance is None:
            capture_plan_conformance = validate_active_capture_plan(capture_plan).to_json()
        snapshot["capture_plan_conformance"] = (
            capture_plan_conformance if capture_plan else None
        )
        return snapshot

    def start_recording(self, condition: str, distance: str) -> dict[str, str]:
        """Begin recording on the backend loop. Returns {clip_id, start_ts}.

        Reserves the recording slot under the lock via
        `_starting_clip_id` before releasing for the slow async start;
        concurrent calls see the sentinel and refuse with the correct
        "already in progress" error instead of racing into a UDP-bind
        failure.
        """
        with self._lifecycle_transaction("recording already in progress"):
            return self._start_recording(condition, distance)

    def _start_recording(self, condition: str, distance: str) -> dict[str, str]:
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
            if self._shutdown_started:
                raise StateError("backend is shutting down")
            if self._session_id is None or self._member is None:
                raise StateError("call begin_session() first")
            if self._current is not None or self._starting_clip_id is not None:
                raise StateError("recording already in progress")
            capture_plan = dict(self._capture_plan or {})
            # Reserve the slot — concurrent calls now see this and
            # refuse cleanly.
            self._starting_clip_id = clip_id
            # Per-session leg selection. Built under the lock so the
            # session's clips all share one leg set.
            active_legs = list(self._enabled_legs)
            aec3_sweep_source = self._aec3_sweep_source

        conformance = validate_active_capture_plan(capture_plan)
        if not conformance.ok:
            with self._lock:
                if self._starting_clip_id == clip_id:
                    self._starting_clip_id = None
            detail = "; ".join(conformance.errors) or conformance.status
            raise StateError(
                "capture plan no longer matches the active bridge/runtime; "
                f"{detail}. Rebuild or re-enter corpus test mode.",
            )

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
            self._current_plan_conformance = conformance.to_json()
            self._starting_clip_id = None  # transitioned: starting → current
            # Auto-stop timer — guards against a forgotten Stop click.
            self._auto_stop_handle = self._loop.call_later(
                self._max_duration_sec,
                self._auto_stop_threadsafe,
                (clip_id, task),
            )
            # Mid-recording mute watch — if the household flips the mic
            # mute while a clip is rolling, stop within one poll.
            self._mute_poll_handle = self._loop.call_later(
                MUTE_POLL_INTERVAL_SEC,
                self._mute_poll,
                (clip_id, task),
            )
        return {"clip_id": clip_id, "start_ts": start_ts}

    def _mute_poll(self, generation: _StopGeneration) -> None:
        """Runs on the backend loop every MUTE_POLL_INTERVAL_SEC while a
        recording is in flight. Stops the recording (keeping the partial
        clip, flagged `mute_stopped`) the first poll after the household
        mutes the mic. The retained audio was captured while unmuted —
        modulo at most one poll interval — so keeping it is consistent
        with the privacy promise while telling the operator why the clip
        ended early. Fail-soft: a poll error logs and rearms rather than
        leaving the recording unwatched."""
        with self._lock:
            if not self._can_admit_current_locked(generation):
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
            self._spawn_safety_worker(
                generation, auto=False, mute_stopped=True,
            )
            return
        with self._lock:
            if (
                self._can_admit_current_locked(generation)
                and self._loop is not None
            ):
                self._mute_poll_handle = self._loop.call_later(
                    MUTE_POLL_INTERVAL_SEC,
                    self._mute_poll,
                    generation,
                )

    def _auto_stop_threadsafe(self, generation: _StopGeneration) -> None:
        """Fires on the backend loop when MAX_RECORDING_DURATION_SEC
        elapses. Triggers stop_recording on a worker thread so the
        loop thread doesn't block on its own sync method."""
        self._spawn_safety_worker(
            generation, auto=True, mute_stopped=False,
        )

    def _spawn_safety_worker(
        self,
        generation: _StopGeneration,
        *,
        auto: bool,
        mute_stopped: bool,
    ) -> None:
        """Atomically admit one initial worker so shutdown can join it."""
        worker = threading.Thread(
            target=self._run_safety_worker,
            args=(generation, auto, mute_stopped),
            daemon=True,
        )
        with self._lock:
            if not self._can_admit_current_locked(generation):
                return
            self._safety_workers.add(worker)
            worker.start()

    def _run_safety_worker(
        self,
        generation: _StopGeneration,
        auto: bool,
        mute_stopped: bool,
    ) -> None:
        try:
            self._safety_stop(
                generation, auto=auto, mute_stopped=mute_stopped,
            )
        finally:
            with self._lock:
                self._safety_workers.discard(threading.current_thread())

    def _safety_stop(
        self,
        generation: _StopGeneration,
        *,
        auto: bool,
        mute_stopped: bool,
    ) -> None:
        if not self._stop_with_recovery(
            generation, auto=auto, mute_stopped=mute_stopped,
        ):
            self._schedule_stop_retry(
                generation,
                auto=auto,
                mute_stopped=mute_stopped,
            )

    def _matches_current_locked(self, generation: _StopGeneration) -> bool:
        clip_id, task = generation
        return (
            self._current_clip_id == clip_id
            and self._current is task
        )

    def _can_admit_current_locked(self, generation: _StopGeneration) -> bool:
        return not self._shutdown_started and self._matches_current_locked(generation)

    def _owns_pending_locked(self, generation: _StopGeneration) -> bool:
        return self._pending_stop_generation == generation

    def _quiesce_current_capture(
        self,
        generation: _StopGeneration,
    ) -> None:
        """Stop one exact generation retaining frames; publish may retry."""
        _clip_id, task = generation
        with self._lock:
            if not self._matches_current_locked(generation):
                return
            loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(task.request_stop)
            except RuntimeError:
                # Shutdown has made this generation terminal.
                pass

    def _clear_pending_stop(
        self,
        generation: _StopGeneration | None = None,
    ) -> None:
        with self._lock:
            if (
                generation is not None
                and not self._owns_pending_locked(generation)
            ):
                return
            handle = self._stop_retry_handle
            self._pending_stop = None
            self._pending_stop_generation = None
            self._stop_retry_handle = None
            self._stop_retry_attempts = 0
        if handle is not None:
            handle.cancel()

    def _schedule_stop_retry(
        self,
        generation: _StopGeneration,
        *,
        auto: bool,
        mute_stopped: bool,
    ) -> None:
        abandoned_attempts = 0
        with self._lock:
            if (
                not self._can_admit_current_locked(generation)
                or self._loop is None
            ):
                return
            if self._pending_stop is None:
                self._pending_stop_generation = generation
            elif not self._owns_pending_locked(generation):
                return
            previous_auto, previous_mute = self._pending_stop or (False, False)
            merged_mute = previous_mute or mute_stopped
            # Privacy is the stronger explanation if mute and duration race.
            merged_auto = (previous_auto or auto) and not merged_mute
            self._pending_stop = (merged_auto, merged_mute)
            if self._stop_retry_handle is not None:
                return
            self._stop_retry_attempts += 1
            attempt = self._stop_retry_attempts
            if attempt > STOP_RETRY_MAX_ATTEMPTS:
                abandoned_attempts = attempt - 1
                abandoned_clip_id = self._current_clip_id
                self._pending_stop = None
                self._pending_stop_generation = None
                self._stop_retry_handle = None
                self._stop_retry_attempts = 0
                # The lifecycle owner never released; the clip is lost, but
                # the recorder must stay usable for the next one — clear the
                # in-progress slot `start_recording()` checks.
                self._current = None
                self._current_clip_id = None
                self._current_meta = None
                self._current_plan_conformance = None
            else:
                exponent = min(attempt - 1, 8)
                delay = min(
                    STOP_RETRY_INITIAL_SEC * (2 ** exponent),
                    STOP_RETRY_MAX_SEC,
                )
                retry_timer = threading.Timer(
                    delay,
                    self._retry_pending_stop,
                    args=(generation,),
                )
                retry_timer.daemon = True
                self._stop_retry_handle = retry_timer
                # Start under the state lock so shutdown never observes an
                # unstarted Timer and then tries to join it.
                retry_timer.start()
        if abandoned_attempts:
            log_event(
                logger,
                "wake_corpus.stop_retry_abandoned",
                attempts=abandoned_attempts,
                clip_id=abandoned_clip_id,
                auto=merged_auto,
                mute_stopped=merged_mute,
                level=logging.ERROR,
            )
            return
        if attempt == 1 or attempt & (attempt - 1) == 0:
            log_event(
                logger,
                "wake_corpus.stop_retry_scheduled",
                attempt=attempt,
                delay_sec=f"{delay:.3f}",
                auto=merged_auto,
                mute_stopped=merged_mute,
                level=logging.WARNING,
            )

    def _retry_pending_stop(self, generation: _StopGeneration) -> None:
        """One Timer-owned publication attempt; rearm only after it exits."""
        current_timer = threading.current_thread()
        with self._lock:
            active_timer = self._stop_retry_handle
            pending = self._pending_stop
            owned = (
                self._owns_pending_locked(generation)
                and self._matches_current_locked(generation)
                and active_timer is current_timer
            )
        if pending is None or not owned:
            return
        auto, mute_stopped = pending
        if self._stop_with_recovery(
            generation,
            auto=auto,
            mute_stopped=mute_stopped,
        ):
            return
        # Keep the fired Timer as the in-flight sentinel through the complete
        # attempt. Repeated safety triggers merge into _pending_stop while it
        # is present; only this callback may replace it with the next timer.
        with self._lock:
            if (
                self._owns_pending_locked(generation)
                and self._stop_retry_handle is active_timer
            ):
                self._stop_retry_handle = None
            pending = self._pending_stop
            still_owned = (
                self._owns_pending_locked(generation)
                and self._can_admit_current_locked(generation)
            )
        if pending is not None and still_owned:
            auto, mute_stopped = pending
            self._schedule_stop_retry(
                generation,
                auto=auto,
                mute_stopped=mute_stopped,
            )

    def _stop_with_recovery(
        self,
        generation: _StopGeneration,
        *,
        auto: bool,
        mute_stopped: bool,
    ) -> bool:
        """Quiesce immediately; return whether publication is terminal."""
        with self._lock:
            live_generation = self._matches_current_locked(generation)
        if not live_generation:
            self._clear_pending_stop(generation)
            return True
        self._quiesce_current_capture(generation)
        try:
            self.stop_recording(
                auto=auto,
                mute_stopped=mute_stopped,
                _expected_generation=generation,
            )
        except LifecycleBusyError:
            return False
        except NoRecordingError:
            pass
        except StateError as e:
            logger.warning("deferred recording stop refused: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("deferred recording stop failed: %s", e)
        self._clear_pending_stop(generation)
        return True

    def stop_recording(
        self,
        auto: bool = False,
        mute_stopped: bool = False,
        *,
        _expected_generation: _StopGeneration | None = None,
    ) -> ClipMetadata:
        """Stop the current recording, save WAVs, return metadata."""
        with self._lifecycle_transaction(
            "can't stop recording: lifecycle transition in progress",
        ):
            with self._lock:
                clip_id = self._current_clip_id
                task = self._current
                if (
                    _expected_generation is not None
                    and (clip_id, task) != _expected_generation
                ):
                    raise NoRecordingError("no recording in progress")
            try:
                clip = self._stop_recording(
                    auto=auto,
                    mute_stopped=mute_stopped,
                )
            finally:
                if clip_id is not None and task is not None:
                    # Cleanup stays inside lifecycle ownership so a later
                    # Start cannot install state that this generation erases.
                    self._clear_pending_stop((clip_id, task))
        return clip

    def _stop_recording(
        self,
        auto: bool = False,
        mute_stopped: bool = False,
    ) -> ClipMetadata:
        with self._lock:
            if self._current is None:
                raise NoRecordingError("no recording in progress")
            task = self._current
            clip_id = self._current_clip_id
            generation = (clip_id, task)
            if self._owns_pending_locked(generation):
                pending_auto, pending_mute = self._pending_stop or (False, False)
                mute_stopped = mute_stopped or pending_mute
                auto = (auto or pending_auto) and not mute_stopped
            meta = self._current_meta
            session_id = self._session_id
            member = self._member
            selected_legs = list(self._enabled_legs)
            capture_plan = dict(self._capture_plan or {})
            capture_plan_id = str(capture_plan.get("plan_id") or "")
            capture_plan_conformance = dict(self._current_plan_conformance or {})
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
            self._current_plan_conformance = None

        # Long operations (await stop, write WAVs) happen OUTSIDE the
        # lock — other API calls can read state concurrently.
        pcm_per_leg = self._submit(task.stop())
        stop_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        duration_sec = task.elapsed_sec()
        capture_health = task.capture_health(duration_sec)

        seq = self._clips.next_seq()
        files = self._clips.write_wavs(
            member=member,
            session_id=session_id,
            seq=seq,
            condition=meta["condition"],
            pcm_per_leg=pcm_per_leg,
        )

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
            capture_plan_id=capture_plan_id,
            capture_plan_conformance=capture_plan_conformance,
            audio_context=audio_context,
            capture_health=capture_health,
        )
        self._clips.append(clip)
        active_session.save_metadata(self)
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

