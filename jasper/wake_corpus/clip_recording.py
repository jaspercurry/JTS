# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Start, stop and publish one wake-corpus clip, with its safety stops.

Functions over ``RecordingBackend``'s recording state, which they read and
write under its state lock; the backend runs ``start_recording`` and
``stop_recording`` inside its lifecycle transaction. The duration cap and
the mid-recording mute poll fire on the backend loop and hand the stop to a
worker thread; a stop that finds the lifecycle busy retries on one
capped-backoff Timer.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

from jasper.log_event import log_event
from jasper.wake_conditions import CONDITIONS, DISTANCES

from . import active_session
from .capture_plan import validate_active_capture_plan
from .clip_capture import RecordingTask
from .errors import LifecycleBusyError, NoRecordingError, StateError
from .session_store import ClipMetadata

if TYPE_CHECKING:
    from .recording_backend import RecordingBackend

logger = logging.getLogger("jasper-wake-corpus-web")

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

StopGeneration = tuple[str, RecordingTask]


def start_recording(
    backend: RecordingBackend, condition: str, distance: str,
) -> dict[str, str]:
    """Start one clip's capture; the caller holds the lifecycle transaction."""
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
    backend._refuse_if_muted("start_recording")

    clip_id = str(uuid.uuid4())
    with backend._lock:
        if backend._shutdown_started:
            raise StateError("backend is shutting down")
        if backend._session_id is None or backend._member is None:
            raise StateError("call begin_session() first")
        if backend._current is not None or backend._starting_clip_id is not None:
            raise StateError("recording already in progress")
        capture_plan = dict(backend._capture_plan or {})
        backend._starting_clip_id = clip_id
        # Per-session leg selection. Built under the lock so the
        # session's clips all share one leg set.
        active_legs = list(backend._enabled_legs)
        aec3_sweep_source = backend._aec3_sweep_source

    conformance = validate_active_capture_plan(capture_plan)
    if not conformance.ok:
        with backend._lock:
            if backend._starting_clip_id == clip_id:
                backend._starting_clip_id = None
        detail = "; ".join(conformance.errors) or conformance.status
        raise StateError(
            "capture plan no longer matches the active bridge/runtime; "
            f"{detail}. Rebuild or re-enter corpus test mode.",
        )

    ports_for_task = {
        leg: backend._ports[leg]
        for leg in active_legs if leg in backend._ports
    }
    task = RecordingTask(
        ports_for_task,
        aec3_sweep_source=aec3_sweep_source,
    )
    # Start on the backend loop. If the UDP bind fails (jasper-voice
    # is still up, port already in use), this raises and we never
    # transition into the recording state.
    try:
        backend._submit(task.start())
    except Exception as e:  # noqa: BLE001
        with backend._lock:
            backend._starting_clip_id = None
        raise StateError(
            f"failed to start recording (is jasper-voice down?): {e}",
        ) from e

    start_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with backend._lock:
        backend._current = task
        backend._current_clip_id = clip_id
        backend._current_meta = {
            "condition": condition,
            "distance": distance,
            "start_ts": start_ts,
        }
        backend._current_plan_conformance = conformance.to_json()
        backend._starting_clip_id = None  # transitioned: starting → current
        # _submit() above succeeded, so the loop exists.
        loop = cast(asyncio.AbstractEventLoop, backend._loop)
        # Auto-stop timer — guards against a forgotten Stop click.
        backend._auto_stop_handle = loop.call_later(
            backend._max_duration_sec,
            _auto_stop_threadsafe,
            backend,
            (clip_id, task),
        )
        # Mid-recording mute watch — if the household flips the mic
        # mute while a clip is rolling, stop within one poll.
        backend._mute_poll_handle = loop.call_later(
            MUTE_POLL_INTERVAL_SEC,
            _mute_poll,
            backend,
            (clip_id, task),
        )
    return {"clip_id": clip_id, "start_ts": start_ts}


def _mute_poll(backend: RecordingBackend, generation: StopGeneration) -> None:
    """Runs on the backend loop every MUTE_POLL_INTERVAL_SEC while a
    recording is in flight. Stops the recording (keeping the partial
    clip, flagged `mute_stopped`) the first poll after the household
    mutes the mic. The retained audio was captured while unmuted —
    modulo at most one poll interval — so keeping it is consistent
    with the privacy promise while telling the operator why the clip
    ended early. Fail-soft: a poll error logs and rearms rather than
    leaving the recording unwatched."""
    with backend._lock:
        if not _can_admit_current_locked(backend, generation):
            return
    try:
        muted = backend.mic_muted()
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
        _spawn_safety_worker(
            backend, generation, auto=False, mute_stopped=True,
        )
        return
    with backend._lock:
        if (
            _can_admit_current_locked(backend, generation)
            and backend._loop is not None
        ):
            backend._mute_poll_handle = backend._loop.call_later(
                MUTE_POLL_INTERVAL_SEC,
                _mute_poll,
                backend,
                generation,
            )


def _auto_stop_threadsafe(
    backend: RecordingBackend, generation: StopGeneration,
) -> None:
    """Fires on the backend loop when the clip reaches the backend's
    max duration. Triggers stop_recording on a worker thread so the
    loop thread doesn't block on its own sync method."""
    _spawn_safety_worker(
        backend, generation, auto=True, mute_stopped=False,
    )


def _spawn_safety_worker(
    backend: RecordingBackend,
    generation: StopGeneration,
    *,
    auto: bool,
    mute_stopped: bool,
) -> None:
    """Atomically admit one initial worker so shutdown can join it."""
    worker = threading.Thread(
        target=_run_safety_worker,
        args=(backend, generation, auto, mute_stopped),
        daemon=True,
    )
    with backend._lock:
        if not _can_admit_current_locked(backend, generation):
            return
        backend._safety_workers.add(worker)
        worker.start()


def _run_safety_worker(
    backend: RecordingBackend,
    generation: StopGeneration,
    auto: bool,
    mute_stopped: bool,
) -> None:
    try:
        _safety_stop(
            backend, generation, auto=auto, mute_stopped=mute_stopped,
        )
    finally:
        with backend._lock:
            backend._safety_workers.discard(threading.current_thread())


def _safety_stop(
    backend: RecordingBackend,
    generation: StopGeneration,
    *,
    auto: bool,
    mute_stopped: bool,
) -> None:
    if not _stop_with_recovery(
        backend, generation, auto=auto, mute_stopped=mute_stopped,
    ):
        _schedule_stop_retry(
            backend,
            generation,
            auto=auto,
            mute_stopped=mute_stopped,
        )


def _matches_current_locked(
    backend: RecordingBackend, generation: StopGeneration,
) -> bool:
    clip_id, task = generation
    return (
        backend._current_clip_id == clip_id
        and backend._current is task
    )


def _can_admit_current_locked(
    backend: RecordingBackend, generation: StopGeneration,
) -> bool:
    return (
        not backend._shutdown_started
        and _matches_current_locked(backend, generation)
    )


def _owns_pending_locked(
    backend: RecordingBackend, generation: StopGeneration,
) -> bool:
    return backend._pending_stop_generation == generation


def _quiesce_current_capture(
    backend: RecordingBackend,
    generation: StopGeneration,
) -> None:
    """Stop one exact generation retaining frames; publish may retry."""
    _clip_id, task = generation
    with backend._lock:
        if not _matches_current_locked(backend, generation):
            return
        loop = backend._loop
    if loop is not None:
        try:
            loop.call_soon_threadsafe(task.request_stop)
        except RuntimeError:
            # Shutdown has made this generation terminal.
            pass


def clear_pending_stop(
    backend: RecordingBackend,
    generation: StopGeneration | None = None,
) -> None:
    with backend._lock:
        if (
            generation is not None
            and not _owns_pending_locked(backend, generation)
        ):
            return
        handle = backend._stop_retry_handle
        backend._pending_stop = None
        backend._pending_stop_generation = None
        backend._stop_retry_handle = None
        backend._stop_retry_attempts = 0
    if handle is not None:
        handle.cancel()


def _schedule_stop_retry(
    backend: RecordingBackend,
    generation: StopGeneration,
    *,
    auto: bool,
    mute_stopped: bool,
) -> None:
    abandoned_attempts = 0
    with backend._lock:
        if (
            not _can_admit_current_locked(backend, generation)
            or backend._loop is None
        ):
            return
        if backend._pending_stop is None:
            backend._pending_stop_generation = generation
        elif not _owns_pending_locked(backend, generation):
            return
        previous_auto, previous_mute = backend._pending_stop or (False, False)
        merged_mute = previous_mute or mute_stopped
        # Privacy is the stronger explanation if mute and duration race.
        merged_auto = (previous_auto or auto) and not merged_mute
        backend._pending_stop = (merged_auto, merged_mute)
        if backend._stop_retry_handle is not None:
            return
        backend._stop_retry_attempts += 1
        attempt = backend._stop_retry_attempts
        if attempt > STOP_RETRY_MAX_ATTEMPTS:
            abandoned_attempts = attempt - 1
            abandoned_clip_id = backend._current_clip_id
            backend._pending_stop = None
            backend._pending_stop_generation = None
            backend._stop_retry_handle = None
            backend._stop_retry_attempts = 0
            # The lifecycle owner never released; the clip is lost, but
            # the recorder must stay usable for the next one — clear the
            # in-progress slot `start_recording()` checks.
            backend._current = None
            backend._current_clip_id = None
            backend._current_meta = None
            backend._current_plan_conformance = None
        else:
            exponent = min(attempt - 1, 8)
            delay = min(
                STOP_RETRY_INITIAL_SEC * (2 ** exponent),
                STOP_RETRY_MAX_SEC,
            )
            retry_timer = threading.Timer(
                delay,
                _retry_pending_stop,
                args=(backend, generation),
            )
            retry_timer.daemon = True
            backend._stop_retry_handle = retry_timer
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


def _retry_pending_stop(
    backend: RecordingBackend, generation: StopGeneration,
) -> None:
    """One Timer-owned publication attempt; rearm only after it exits."""
    current_timer = threading.current_thread()
    with backend._lock:
        active_timer = backend._stop_retry_handle
        pending = backend._pending_stop
        owned = (
            _owns_pending_locked(backend, generation)
            and _matches_current_locked(backend, generation)
            and active_timer is current_timer
        )
    if pending is None or not owned:
        return
    auto, mute_stopped = pending
    if _stop_with_recovery(
        backend,
        generation,
        auto=auto,
        mute_stopped=mute_stopped,
    ):
        return
    # Keep the fired Timer as the in-flight sentinel through the complete
    # attempt. Repeated safety triggers merge into _pending_stop while it
    # is present; only this callback may replace it with the next timer.
    with backend._lock:
        if (
            _owns_pending_locked(backend, generation)
            and backend._stop_retry_handle is active_timer
        ):
            backend._stop_retry_handle = None
        pending = backend._pending_stop
        still_owned = (
            _owns_pending_locked(backend, generation)
            and _can_admit_current_locked(backend, generation)
        )
    if pending is not None and still_owned:
        auto, mute_stopped = pending
        _schedule_stop_retry(
            backend,
            generation,
            auto=auto,
            mute_stopped=mute_stopped,
        )


def _stop_with_recovery(
    backend: RecordingBackend,
    generation: StopGeneration,
    *,
    auto: bool,
    mute_stopped: bool,
) -> bool:
    """Quiesce immediately; return whether publication is terminal."""
    with backend._lock:
        live_generation = _matches_current_locked(backend, generation)
    if not live_generation:
        clear_pending_stop(backend, generation)
        return True
    _quiesce_current_capture(backend, generation)
    try:
        # Through the backend, not this module's stop_recording: the
        # retry depends on the lifecycle transaction refusing while busy.
        backend.stop_recording(
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
    clear_pending_stop(backend, generation)
    return True


def stop_recording(
    backend: RecordingBackend,
    *,
    auto: bool,
    mute_stopped: bool,
    expected_generation: StopGeneration | None,
) -> ClipMetadata:
    """Publish the current clip; the caller holds the lifecycle transaction."""
    with backend._lock:
        clip_id = backend._current_clip_id
        task = backend._current
        if (
            expected_generation is not None
            and (clip_id, task) != expected_generation
        ):
            raise NoRecordingError("no recording in progress")
    try:
        clip = _stop_recording(
            backend,
            auto=auto,
            mute_stopped=mute_stopped,
        )
    finally:
        if clip_id is not None and task is not None:
            # Cleanup stays inside lifecycle ownership so a later
            # Start cannot install state that this generation erases.
            clear_pending_stop(backend, (clip_id, task))
    return clip


def _stop_recording(
    backend: RecordingBackend,
    *,
    auto: bool,
    mute_stopped: bool,
) -> ClipMetadata:
    with backend._lock:
        if backend._current is None:
            raise NoRecordingError("no recording in progress")
        task = backend._current
        # Set with _current by start_recording; no session transition
        # runs while a clip is current.
        clip_id = cast(str, backend._current_clip_id)
        generation = (clip_id, task)
        if _owns_pending_locked(backend, generation):
            pending_auto, pending_mute = backend._pending_stop or (False, False)
            mute_stopped = mute_stopped or pending_mute
            auto = (auto or pending_auto) and not mute_stopped
        meta = cast("dict[str, str]", backend._current_meta)
        session_id = cast(str, backend._session_id)
        member = cast(str, backend._member)
        selected_legs = list(backend._enabled_legs)
        capture_plan = dict(backend._capture_plan or {})
        capture_plan_id = str(capture_plan.get("plan_id") or "")
        capture_plan_conformance = dict(backend._current_plan_conformance or {})
        audio_context = dict(backend._audio_context or {})
        # Cancel the auto-stop timer if it hasn't fired yet.
        if backend._auto_stop_handle is not None and not auto:
            backend._auto_stop_handle.cancel()
        backend._auto_stop_handle = None
        # The mute watch dies with the recording (cancelling an
        # already-fired handle is a harmless no-op).
        if backend._mute_poll_handle is not None:
            backend._mute_poll_handle.cancel()
        backend._mute_poll_handle = None
        # Clear state up-front: while the clip saves, is_recording(),
        # /api/status and the safety stops' generation checks already
        # treat it as stopped.
        backend._current = None
        backend._current_clip_id = None
        backend._current_meta = None
        backend._current_plan_conformance = None

    # Long operations (await stop, write WAVs) happen OUTSIDE the
    # lock — other API calls can read state concurrently.
    pcm_per_leg = backend._submit(task.stop())
    stop_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    duration_sec = task.elapsed_sec()
    capture_health = task.capture_health(duration_sec)

    seq = backend._clips.next_seq()
    files = backend._clips.write_wavs(
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
    backend._clips.append(clip)
    active_session.save_metadata(backend)
    logger.info(
        "clip saved: %s seq=%d condition=%s distance=%s dur=%.2fs%s%s",
        clip_id, seq, meta["condition"], meta["distance"],
        duration_sec, " (auto-stopped)" if auto else "",
        " (mute-stopped)" if mute_stopped else "",
    )
    return clip
