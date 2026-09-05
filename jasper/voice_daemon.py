# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from datetime import datetime, timezone
from enum import Enum
from inspect import isawaitable
from types import SimpleNamespace

from jasper.aec_sweep import (
    AGC1_ENABLED_ENV,
    AGC1_MAX_GAIN_DB_ENV,
    AGC1_TARGET_DBFS_ENV,
    NS_ENABLED_ENV,
    NS_LEVEL_ENV,
)
from jasper.log_event import log_event

from .audio_buffer import (
    ACQUIRE_BUFFER_MAX_FRAMES,
    drain_acquire_buffer,
)
from .audio_io import (
    InputDeviceUnavailable,
    MicCapture,
    TtsPlayout,
    tts_wire_is_wide as _tts_wire_is_wide,
    wait_tts_drained_owned,
)
from .assistant_loudness import (
    active_voice_identity,
    tts_envelope_lufs_for_level,
)
from .tts_routing import (
    tts_socket_feeds_post_dsp_outputd,
    tts_socket_feeds_pre_dsp_fanin,
)
from .wake_events import (
    WakeEventStore,
    make_event_id,
    CAPTURE_PRE_SEC,
    CAPTURE_POST_SEC,
)
from .cues import AudioCueManager
from .vad import SpeechVAD
from .wake_legs import LegSpec, by_token, wake_input_legs
from .wake_condition_context import classify_condition
from .wake_conditions import DEFAULT_CONDITION
from .wake_fusion import WakeFuser
from .camilla import CamillaController, CueDuck, Ducker
from .config import Config
from .conversation_history import (
    ConversationSettings,
    ConversationStore,
    ConversationTurn,
    make_turn_id,
    prune_for_settings,
    read_settings as read_conversation_settings,
)
from .watchdog import Heartbeat
from .timers import Timer, announcement_text
from .research import DONE, FAILED, RESEARCH_EMPTY_RESULT_TEXT, ResearchJob, ResearchScheduler
from .usage import (
    SpendCap,
    UsageStore,
)
from .voice.session import AudioOutChunk, LiveConnection, LiveTurn  # noqa: F401
from .voice import earcons as _earcons
from .voice.earcons import (
    SYNTHETIC_AUDIO_PROFILE_PROVIDER,  # noqa: F401
    SYNTHETIC_AUDIO_PROFILE_UPDATED_AT,  # noqa: F401
    _generate_listening_chirp,
    _generate_mute_click,
    measure_pcm_24k_mono,
)
from .voice.prompt import (  # noqa: F401
    SYSTEM_INSTRUCTION,
    _build_system_instruction,
)
from .voice.catalog import InterruptReconcile, resolve_interrupt_reconcile
from .voice.provider_state import read_barge_in_enabled
from .voice.measurement_hold import MeasurementHold
from .voice.output_gate import (
    AssistantOutputEpisode,
    AssistantOutputGate,
)
from .voice.turn_playback import (  # noqa: F401
    _idle_watchdog,
    _play_responses,
)
from .volume_coordinator import VolumeCoordinator
from .mic_mute_persistence import read_mic_muted, write_mic_muted

logger = logging.getLogger(__name__)
EX_CONFIG_EXIT = 78
VOICE_PROVIDER_NOT_CONFIGURED_EXIT = EX_CONFIG_EXIT
VOICE_STARTUP_CONFIG_ERROR_EXIT = EX_CONFIG_EXIT
INTERNAL_ERROR_CUE_SLUG = "internal_error"
# Primary microphone could not be opened at startup (os.EX_NOINPUT). A
# DISTINCT code from EX_CONFIG (78) so the unit, doctor, and /state can
# tell "no usable mic" from "no provider configured". Listed in
# jasper-voice.service's SuccessExitStatus + RestartPreventExitStatus so
# the daemon parks cleanly (waiting for the AEC reconciler / udev to
# restart it on plug-in) instead of crash-looping toward
# StartLimitAction=reboot.
VOICE_MIC_UNAVAILABLE_EXIT = 66


def _synthetic_audio_profile(
    *,
    model: str,
    voice: str,
    pcm: bytes,
    wide: bool = False,
    fallback_source_lufs: float = -24.0,
    fallback_peak_dbfs: float = -12.0,
):
    _earcons.measure_pcm_24k_mono = measure_pcm_24k_mono
    return _earcons._synthetic_audio_profile(
        model=model,
        voice=voice,
        pcm=pcm,
        wide=wide,
        fallback_source_lufs=fallback_source_lufs,
        fallback_peak_dbfs=fallback_peak_dbfs,
    )


def _research_confirmation_instruction(job: ResearchJob) -> str:
    return (
        "For this turn only, the user is answering yes or no about whether "
        f"to read research result {job.id}. If the answer is yes or an "
        f"affirmative, call read_research_result(job_id='{job.id}', "
        "decision='yes'). If the answer is no or a negative, call "
        f"read_research_result(job_id='{job.id}', decision='no'). Speak "
        "only the tool's returned text field. Do not answer from memory, "
        "summarize, ask a follow-up, or start new research."
    )


def _track_task(
    task: asyncio.Task,
    task_set: set[asyncio.Task],
    *,
    label: str,
) -> asyncio.Task:
    task_set.add(task)

    def _discard(done: asyncio.Task) -> None:
        task_set.discard(done)
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.warning(
                "fire-and-forget task %s failed: %s",
                label,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_discard)
    return task


async def _cancel_tracked_tasks(task_set: set[asyncio.Task]) -> None:
    tasks = list(task_set)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    task_set.difference_update(tasks)


async def _capture_cleanup_error(
    operation: Callable[[], object],
) -> BaseException | None:
    """Run one sync/async cleanup step and return any raised outcome."""

    try:
        outcome = operation()
        if isawaitable(outcome):
            await outcome
    except BaseException as error:  # noqa: BLE001 - one cleanup capture boundary
        return error
    return None


class FanInDucker:
    """Voice-session duck transport for the pre-DSP TTS topology.

    The voice loop still owns the duck/restore lifecycle; fan-in owns
    where the attenuation happens. This keeps TTS out of the attenuated
    program lane while sending the final mixed signal through CamillaDSP
    crossover/protection.
    """

    def __init__(self, socket_path: str, duck_db: float) -> None:
        self._socket_path = socket_path
        self._duck_db = duck_db
        self._ducked = False

    @property
    def is_ducked(self) -> bool:
        return self._ducked

    @property
    def locks_camilla_volume(self) -> bool:
        """Fan-in ducking leaves Camilla available as the master volume."""
        return False

    async def duck(self) -> None:
        if self._ducked:
            return
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._send_command,
                b"PROGRAM_DUCK_ON\nCLOSE\n",
            ),
            name="fanin-program-duck-on",
        )
        deferred_cancel = False
        current = asyncio.current_task()
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                if current is None or current.cancelling() == 0:
                    break
                deferred_cancel = True
                current.uncancel()
        if worker.cancelled():
            raise asyncio.CancelledError
        error = worker.exception()
        ok = worker.result() if error is None else False
        # Once the bounded worker ran, False/OSError and unexpected failures
        # are ambiguous: connect/send may have delivered ON before CLOSE or
        # the reported error. Conservatively own one idempotent OFF so every
        # caller's cleanup restores the remote state before releasing output.
        self._ducked = True
        if error is not None:
            if deferred_cancel:
                raise asyncio.CancelledError from None
            raise error
        if not ok:
            if deferred_cancel:
                raise asyncio.CancelledError
            return
        log_event(
            logger,
            "duck",
            on="true",
            transport="fanin",
            socket=self._socket_path,
            duck_db=f"{self._duck_db:.1f}",
        )
        if deferred_cancel:
            raise asyncio.CancelledError

    async def restore(self) -> None:
        if not self._ducked:
            return
        try:
            ok = await asyncio.to_thread(
                self._send_command, b"PROGRAM_DUCK_OFF\nCLOSE\n"
            )
            if ok:
                log_event(
                    logger,
                    "duck",
                    on="false",
                    transport="fanin",
                    socket=self._socket_path,
                )
        finally:
            self._ducked = False

    def _send_command(self, payload: bytes) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(self._socket_path)
                sock.sendall(payload)
            return True
        except OSError as e:
            log_event(
                logger,
                "duck_failed",
                transport="fanin",
                socket=self._socket_path,
                detail=str(e),
                level=logging.WARNING,
            )
            return False


# Refractory after a turn ends before the wake detector is re-armed.
# Bounds the one transient that is a self-loop risk: TTS audio still in
# the ALSA dmix playout buffer when _end_turn runs. The dongle dmix is
# configured at 4096 frames @ 48 kHz ≈ 85 ms of buffering. TtsPlayout's
# drain primitive anchors turn-end on samples actually queued, so the
# refractory only needs to cover that dmix tail: 0.2 s is ~2.5x the
# 85 ms buffer — still a margin, but won't swallow conversational pacing.
WAKE_REFRACTORY_SEC = 0.2

# Head-room a push-to-talk turn must leave the model to start answering
# after the button closes the user's input.
#
# `_idle_watchdog`'s pre-response timer is anchored at TURN OPEN, not at
# end-of-input, so the hold and the model's first-chunk latency share one
# `JASPER_IDLE_TIMEOUT_SEC` envelope. Its own docstring puts that latency
# at "3-5 s, sometimes longer" for Live API providers; 6 s covers the
# documented range with margin. Whatever the hold cap ends up being, it
# must fire this far below `idle_timeout_sec` or the watchdog reaps the
# turn before the model can speak — and the teardown cancels
# `_play_responses` BEFORE calling `end_input`, so the user gets no
# answer at all rather than a short one.
PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC = 6.0

# Floor for the derived push-to-talk hold cap, so an operator who sets a
# very low JASPER_IDLE_TIMEOUT_SEC still gets a usable button rather than
# a turn that closes before they finish a sentence. Below this the
# watchdog may win; `_ptt_input_cap_sec` says so once, loudly.
PTT_MIN_INPUT_CAP_SEC = 5.0

# Liveness-tick cadence for a push-to-talk-only speaker, which has no
# primary mic stream to prove the async loop is iterating. Must stay well
# under `Heartbeat`'s stale threshold (jasper/watchdog.py) or the unit's
# WatchdogSec=30s would reap a healthy daemon; 2 s leaves 2.5x margin
# while costing one wakeup per interval. The relationship is pinned by
# test_ptt_keepalive_stays_inside_heartbeat_stale_threshold.
#
# A tick proves the loop is iterating, not that the accessory is still
# delivering audio: `UdpMicCapture.frames()` has no timeout, so a dead
# sender (remote battery, out of range, adapter stall) blocks its
# manual-mic task forever while the tick keeps patting the watchdog — a
# dead remote reads as a healthy speaker. Issue #2243. A frame timeout
# here is not the fix: silence is a push-to-talk device's steady state,
# so frame flow cannot tell idle from dead. Connection state can, and
# that is the accessory reconciler's to publish.
PTT_KEEPALIVE_INTERVAL_SEC = 2.0

# Cue for "you asked for a room-mic turn and this speaker has no room mic."
# Registered in jasper/cues/registry.py; named here so the failure handler and
# the guard test cannot drift from the registry entry.
NO_ROOM_MIC_CUE_SLUG = "no_room_microphone"

# How long a wake or a manual (button) session start waits out a paused
# connection before taking the turn anyway. Most pauses are a planned
# session rotation, whose gap is one teardown plus one connect (p50
# ~350 ms, p99 5.3 s); refusing instantly turns the common ones into
# a dead press and a false 'can't connect' cue. The bound keeps the
# SUCCESS path — which returns as soon as the turn opens — inside
# jasper.control.client.DEFAULT_TIMEOUT (2.0 s) with room for the
# round trip. A refusal outlives that either way: it cues first.
PAUSED_CONNECTION_WAIT_SEC = 1.2

# Per-leg score-freshness window. When a leg fires, another leg's most-
# recent score counts toward `fired_legs` (and the per-leg log line) only
# if it landed within this window — so a stream that stopped feeding (e.g.
# the bridge died) surfaces as "none" rather than lying with a stale
# score. 4x MicCapture's 80 ms frame period.
WAKE_STALE_SCORE_SEC = 0.32

# How often the WAKE loop recomputes the acoustic condition the fuser keys
# on. The fire gate reads a cached `_current_condition`; this bounds its
# staleness while keeping the ring-noise-floor cost off the per-frame path
# (recompute ~1x/s, not ~12x/s/leg). Conditions — music starting, the room
# going quiet — change on a human timescale, so ~1 s is ample.
CONDITION_REFRESH_SEC = 1.0

# Per-leg wake-telemetry capture-ring depth, in frames. Sized to the
# (pre + post) capture window plus a safety margin: a 4 + 2 = 6 s window
# with ~2 s slack for the post-fire collection window, so a snapshot
# never runs off the end of the ring. One ring per leg is allocated at
# the run() wiring site and handed to its _LegRuntime.
CAPTURE_RING_FRAMES = int(
    ((CAPTURE_PRE_SEC + CAPTURE_POST_SEC) * MicCapture.OUTPUT_RATE
     / MicCapture.OUTPUT_FRAME_SAMPLES) + 25
)


# End-of-utterance: fire activity_end once the user has been silent
# for this long AFTER they spoke. With manual VAD on the server
# side, this marker is what actually closes the user's turn so the
# model can respond. 0.8 s matches what mature open-source assistants
# (Mycroft, Silero defaults, OpenAI Realtime, Vapi) cluster around,
# and keeps perceived "I stopped talking → response starts" latency
# low.
END_OF_UTTERANCE_SILENCE_SEC = 0.8

# Hard cap on user audio length within a single turn. Once the user
# has been speaking continuously for this long without an
# end-of-utterance silence, force-close the turn. Defends against
# stuck-on TVs / loud monologues that could otherwise hold the
# turn open indefinitely. Generous (30 s) so verbose questions and
# dictation-style use cases aren't clipped.
HARD_RECORDING_CAP_SEC = 30.0

# Pre-roll: when wake fires, replay the most recent ~560 ms of mic
# audio into the turn so the first phoneme of the user's command
# isn't lost. openWakeWord fires when the END of "Hey Jarvis" passes
# its window — by that point the user is already 200-400 ms into
# their command. Without pre-roll we throw those frames away.
# 7 × 80 ms = 560 ms covers the wake-word tail + the start of the
# command for fast speakers.
PRE_ROLL_FRAMES = 7

# Research results are a "tell me later" promise, unlike timer chimes:
# completing during a voice session must defer, not disappear. Keep the
# in-memory hold queue small so a long session plus a burst of completions
# cannot grow without bound.
RESEARCH_PENDING_ANNOUNCE_CAP = 5
RESEARCH_FAILURE_COOLDOWN_SEC = 60.0 * 60.0
RESEARCH_FAILED_CUE_SLUG = "research_failed"
RESEARCH_READY_CONFIRMATION_TEXT = (
    "Your research is ready — want me to read it now?"
)
RESEARCH_CONFIRMATION_REFRACTORY_SEC = 0.35
RESEARCH_CONFIRMATION_OPEN_CANCEL_TIMEOUT_SEC = 20.0

# Silero speech-probability threshold for marking "the user has
# actually spoken" within a turn. Decoupled from
# JASPER_VAD_BARGE_IN_THRESHOLD (default 0.5) — that one is tuned
# strict to avoid TTS-bleed false-positives in the barge-in gate;
# this one is tuned LOOSE so soft / quiet speech still flips
# `_user_speech_seen` so the silence detector arms.
# Range: AirPlay music vocals scored 0.13 (0.10 was loose enough to let
# them flip the flag and feed a false wake to the model); real user
# speech in the same session bottomed out at 0.19. 0.15 sits between
# music transients and the softest real speech observed.
END_OF_UTTERANCE_SPEECH_THRESHOLD = 0.15

# If `_user_speech_seen` never flips within this window (user said
# the wake word and then nothing, or spoke too quietly for Silero
# to register), abort the turn cleanly and un-duck immediately.
# 5 s = 1.5 s grace + 3.5 s of "you can start now" — gives a slow
# speaker time to begin without making genuine false-wakes drag
# the duck out for too long.
NO_SPEECH_ABORT_SEC = 5.0

# End-of-turn timing — owned by TtsPlayout.expected_drain_at /
# wait_drained. Drain tail configured via JASPER_TTS_DRAIN_TAIL_SEC.

# Sustained-speech threshold for arming the end-of-utterance silence
# detector. After wake fires, Silero must report ≥ THRESHOLD
# speech-probability for at least this many seconds *continuously*
# before `_user_speech_seen` flips. Then — and only then — does
# trailing silence start counting toward end-of-utterance.
#
# 200 ms, not the 0.3 s default of OpenVoiceOS's dinkum-listener
# `speech_begin` parameter (ovos-dinkum-listener voice_loop.py): short
# single-word commands ("next", "pause") span only ~250 ms of audio, and
# 300 ms would miss them.
#
# Duration alone does NOT reject wake-word tail — wake-word phoneme tail
# plus room reverb routinely clears 3 consecutive 80 ms frames at Silero
# ≥ 0.15. SPEECH_RUN_PEAK_MIN is the other half of the gate; without it
# the tail arms the detector, 800 ms of silence fires end-of-utterance,
# and the model answers from pre-roll plus cached context while the user
# is still mid-pause.
SUSTAINED_SPEECH_TO_ARM_SEC = 0.20

# Minimum PEAK Silero score that the arming speech-run must reach.
# Real user speech peaks well above this within 2-3 frames while
# wake-tail residual maxes out in the 0.15-0.55 band. A sweep over an
# 83-event wake corpus found 0.60 rejects the tail (peak 0.52) while
# keeping every real-speech turn armed within 2 s. See
# scripts/probe-wake-gate.py for the harness used to derive this.
#
# Trade-off: a frame at >= 0.60 must appear within the arming run, so a
# mumbled or very quiet start may delay arming until a louder frame
# lands. NO_SPEECH_ABORT_SEC still applies, so the worst degradation is
# "turn aborts and user re-wakes" rather than a confabulated answer.
SPEECH_RUN_PEAK_MIN = 0.60

# In-session barge-in: how long the user must speak continuously (each
# frame >= JASPER_VAD_BARGE_IN_THRESHOLD) before we flush local TTS.
# Reuses the wake-tail arming duration so a real spoken interruption
# clears it within ~200 ms while a single bleed transient cannot. The
# per-frame bar is the (stricter) barge-in threshold, not the loose
# wake-tail 0.15 — bleed false-positives are the failure mode here.
BARGE_IN_SUSTAINED_SPEECH_SEC = SUSTAINED_SPEECH_TO_ARM_SEC


def _aec_reference_available(mic_device: str) -> bool:
    """True when the primary session mic leg is fed by the AEC bridge over
    UDP (``udp:<port>``), i.e. its signal has the speaker's own output
    (music AND TTS) cancelled against the final-output reference. That is
    the precondition for in-session barge-in detection: a direct ALSA
    device (the ``direct_mic`` profile, e.g. ``Array`` / ``hw:...``)
    carries un-cancelled TTS bleed, so VAD would self-trip the gate every
    turn — the self-interrupt loop the barge-in guard refuses to enter.

    This is leg/profile *selection*, not an AEC topology change: the "on"
    leg is the same stream the live session already consumes."""
    return mic_device.strip().lower().startswith("udp:")


class State(Enum):
    WAKE = "wake"
    SESSION = "session"


CONTENT_ACTIVITY_POLL_SEC = 1.0
CONTENT_ACTIVITY_THRESHOLD_DBFS = -55.0


class ContentActivityTracker:
    """Cheap observer for music/activity telemetry and server-VAD gating.

    It never sets TTS gain. Outputd owns the final assistant loudness
    decision; this tracker only keeps a recent best-effort playback RMS
    value for wake telemetry and the "music is playing, use server VAD"
    branch.
    """

    def __init__(
        self,
        camilla: CamillaController,
        *,
        threshold_dbfs: float = CONTENT_ACTIVITY_THRESHOLD_DBFS,
    ) -> None:
        self._camilla = camilla
        self._threshold_dbfs = float(threshold_dbfs)
        self._last_dbfs: float | None = None
        self._paused = False
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def music_dbfs(self) -> float | None:
        return self._last_dbfs

    def music_is_playing(self) -> bool:
        return self._last_dbfs is not None and self._last_dbfs > self._threshold_dbfs

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def refresh_now(self) -> float | None:
        if self._paused:
            return self._last_dbfs
        rms_pair = await self._camilla.get_playback_rms(best_effort=True)
        if rms_pair is None:
            return self._last_dbfs
        self._last_dbfs = max(rms_pair)
        return self._last_dbfs

    async def start(self) -> None:
        await self.refresh_now()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(CONTENT_ACTIVITY_POLL_SEC)
            except asyncio.CancelledError:
                return
            if self._paused:
                continue
            await self.refresh_now()

def _frame_rms_dbfs(frame) -> float | None:
    """Waveform RMS in dBFS for a single int16 mic frame.

    Cheap (≤80 µs per 1280-sample frame on Pi 5). Returns None on any
    error so callers fall through rather than crashing on a malformed
    frame.

    Reference: full-scale int16 is ±32768; RMS of full-scale sine
    ≈ 23170, so a -3 dBFS signal reads ~16384 RMS.
    """
    try:
        import numpy as _np  # local — keep module import cheap
        arr = _np.asarray(frame, dtype=_np.float32)
        if arr.size == 0:
            return None
        rms = float(_np.sqrt(_np.mean(arr * arr)))
        if rms <= 0.0:
            return -120.0  # digital silence floor
        return 20.0 * _np.log10(rms / 32768.0)
    except Exception:  # noqa: BLE001
        return None


def _ring_noise_floor_dbfs(ring, *, percentile: float = 25.0) -> float | None:
    """Ambient noise floor (dBFS) from a wake capture ring.

    A low percentile of the ring's per-frame RMS: the wake utterance is a
    minority of the ~6 s window, so the quieter frames approximate the room
    background. Computed once at fire time (never per frame), it splits
    "quiet" from "ambient" for the condition estimator. Returns None for an
    empty/absent ring or any error — telemetry must never break the wake
    fire path, and the caller treats None as "can't tell" (-> quiet).
    """
    if not ring:
        return None
    try:
        import numpy as _np  # local — keep module import cheap
        levels = [r for f in ring if (r := _frame_rms_dbfs(f)) is not None]
        if not levels:
            return None
        return float(_np.percentile(levels, percentile))
    except Exception:  # noqa: BLE001
        return None



async def _server_vad_response_trigger(turn, connection) -> None:
    """Wait for the server's VAD to signal end-of-utterance, then fire
    response.create. Only spawned when server_vad is active for the turn."""
    wait_eou = getattr(turn, "wait_for_server_eou", None)
    if wait_eou is None or not callable(wait_eou):
        return
    try:
        await asyncio.wait_for(wait_eou(), timeout=NO_SPEECH_ABORT_SEC + 5.0)
    except asyncio.TimeoutError:
        log_event(logger, "server_vad.eou_timeout", level=logging.WARNING)
        return
    except asyncio.CancelledError:
        raise
    if turn.turn_lost():
        return
    create = getattr(connection, "create_response_only", None)
    if create is not None and callable(create):
        try:
            await create()
            log_event(logger, "server_vad.response_create")
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                "server_vad.response_create_failed",
                error=f"{type(e).__name__}: {e}",
                level=logging.WARNING,
            )
    # Must not return: this task lives in WakeLoop._bg_tasks and the turn
    # completion watcher treats any completed member as "turn over", so
    # returning would tear the turn down before the model's response arrived.
    # Idle until _end_turn's cleanup loop cancels it.
    await asyncio.Event().wait()


class _LegRuntime:
    """Live state for one wake-detection leg.

    The set of legs is declared in `jasper.wake_legs`; adding a leg is a
    registry entry plus a config-driven construction in
    `WakeLoop.__init__`.
    """

    __slots__ = (
        "spec", "mic", "detector", "capture_ring",
        "shadow_vad", "recent_score", "recent_score_at",
    )

    def __init__(self, spec, mic, detector, capture_ring, shadow_vad=None):
        self.spec = spec
        self.mic = mic
        self.detector = detector
        self.capture_ring = capture_ring
        # Session-state shadow VAD — set only on the AEC-OFF leg. When
        # present, the leg loop scores it during SESSION for telemetry
        # (`_shadow_vad_score_raw`); other legs idle in SESSION.
        self.shadow_vad = shadow_vad
        # Most-recent raw wake score + the loop-clock time it was set.
        # Read at fire time so the wake event carries every leg's recent
        # peak, and to gate `fired_legs` on freshness.
        self.recent_score = 0.0
        self.recent_score_at = 0.0


class _ManualMicRuntime:
    """Live state for one push-to-talk mic source.

    Manual mic sources are opened by the daemon from
    ``Config.manual_mic_sources`` and selected per turn by source id. They are
    session-audio-only: no wake detector, no wake-event capture ring, and no
    pre-roll from the room mic.
    """

    __slots__ = ("source_id", "mic", "device")

    def __init__(self, source_id: str, mic: MicCapture, device: str):
        self.source_id = source_id
        self.mic = mic
        self.device = device


# Per-leg wake_events column mapping. The peak_score column is irregular for
# back-compat with the existing corpus (aec_on/aec_off vs dtln_aec), so the
# columns are listed explicitly rather than derived from the token. A new leg
# adds an entry here plus the matching additive columns in jasper.wake_events.
_LEG_DB: dict[str, dict[str, str]] = {
    "on": {
        "trigger_kind": "fire_aec_on", "peak_score": "peak_score_aec_on",
        "peak_offset": "peak_offset_ms_on", "mic_rms": "mic_rms_dbfs_on",
    },
    "off": {
        "trigger_kind": "fire_aec_off", "peak_score": "peak_score_aec_off",
        "peak_offset": "peak_offset_ms_off", "mic_rms": "mic_rms_dbfs_off",
    },
    "dtln": {
        "trigger_kind": "fire_dtln", "peak_score": "peak_score_dtln_aec",
        "peak_offset": "peak_offset_ms_dtln", "mic_rms": "mic_rms_dbfs_dtln",
    },
    "chip_aec_150": {
        "trigger_kind": "fire_chip_aec_150",
        "peak_score": "peak_score_chip_aec_150",
        "peak_offset": "peak_offset_ms_chip_aec_150",
        "mic_rms": "mic_rms_dbfs_chip_aec_150",
    },
    "chip_aec_210": {
        "trigger_kind": "fire_chip_aec_210",
        "peak_score": "peak_score_chip_aec_210",
        "peak_offset": "peak_offset_ms_chip_aec_210",
        "mic_rms": "mic_rms_dbfs_chip_aec_210",
    },
}

# Which Config field carries each wake leg's mic device string. Kept here, a
# voice-daemon construction concern, rather than on the jasper.wake_legs
# registry, which stays a pure cross-process identity table. The token and
# field name deliberately skew: the chip-direct leg's token is "off" but its
# device var is cfg.mic_device_raw, the operator-facing "raw" vocabulary
# (JASPER_MIC_DEVICE_RAW). The reconciler sets and clears these vars from the
# JASPER_WAKE_LEG_* booleans; an empty string means the leg is not configured.
_LEG_DEVICE_ATTR: dict[str, str] = {
    "on": "mic_device",
    "off": "mic_device_raw",
    "dtln": "mic_device_dtln",
    "chip_aec_150": "mic_device_chip_aec_150",
    "chip_aec_210": "mic_device_chip_aec_210",
}

# Sentinel for `WakeLoop.for_tests` constructor-time knobs, so a test can
# pass an explicit empty list (no legs) and have it mean "none" rather than
# "use the default".
_UNSET = object()


def _configured_wake_legs(
    cfg: Config,
    *,
    wake_detection_supported: bool = True,
) -> list[tuple[LegSpec, str]]:
    """Decide which wake legs to build and each one's device string.

    Pure (no I/O) so it is unit-testable; run() layers mic-open and
    AsyncExitStack lifecycle on top. The "on" (AEC3/primary) leg carries
    session audio and the Tier-1 heartbeat and is normally always built; the
    AEC reconciler owns making its device present, or parking voice. Optional
    "off"/"dtln" legs are built only when their device var is non-empty, so
    voice never opens a UDP listener nobody feeds.

    Two things produce an empty plan — a box with real voice input (a paired
    remote's button) but no always-listening stream to detect wake on:

    * the install profile does not grant ``Capability.WAKE_DETECTION``; the
      caller reads the marker and passes ``wake_detection_supported``, and
      ``Config`` stays env-only;
    * ``jasper-aec-reconcile`` published "no local mic" while an accessory
      offers a manual source (ADR-0217).

    Building the primary leg anyway would open a card that is not present,
    ``run()`` would re-raise ``InputDeviceUnavailable``, and the daemon would
    park before reaching the accessory sources (issue #2205).

    Both facts in the second case are read from their writers, never guessed:
    ``local_mic_present`` from ``jasper-aec-reconcile``, owner of the
    voice-input gate (``JASPER_LOCAL_MIC_PRESENT``) — ``Config`` defaults it
    to the literal ``"Array"`` and the reconciler writes a real candidate name
    on no-mic paths, so deriving it from ``cfg.mic_device`` misreads a real
    box; ``manual_mic_sources`` from ``jasper-accessory-reconcile``
    (``JASPER_MANUAL_MIC_SOURCES``).

    Only an explicit ``False`` drops the leg. ``None`` — the reconciler never
    ran, or did not resolve a custom device — keeps current behaviour, which
    keeps "this speaker has no room mic" distinguishable from "the room mic
    should be here and isn't": the second still raises and parks loudly rather
    than downgrading a mic-bearing speaker to push-to-talk.
    """
    if not wake_detection_supported:
        return []
    if cfg.local_mic_present is False and cfg.manual_mic_sources:
        return []
    legs: list[tuple[LegSpec, str]] = []
    for spec in wake_input_legs():
        device = getattr(cfg, _LEG_DEVICE_ATTR[spec.token])
        if spec.token == "on" or device:
            legs.append((spec, device))
    return legs


class WakeLoop:
    """Sole consumer of the primary mic. Dispatches each frame to either
    the wake-word detector (WAKE state) or the active live turn (SESSION
    state).

    `self._legs` holds one `_LegRuntime` per configured wake leg (keyed
    by jasper.wake_legs token), assembled by run() and passed in via
    `legs`. The primary "on" (AEC3) leg drives this main loop and carries
    session audio plus the watchdog heartbeat; optional "off"
    (chip-direct) and "dtln" legs run as parallel `_wake_leg_loop` tasks,
    each with its own `WakeWordDetector`. Any leg crossing threshold
    fires the wake event (OR-gate); a shared refractory + asyncio lock
    guarantees one user attempt = one wake event regardless of which
    leg(s) crossed first. Secondary legs are wake-detection-only: their
    frames don't populate pre-roll or flow into sessions — the primary
    "on" stream stays the canonical session audio source.
    """

    def __init__(
        self,
        cfg: Config,
        tts: TtsPlayout,
        connection: LiveConnection,
        ducker: Ducker | FanInDucker,
        content_activity: ContentActivityTracker,
        usage_store: UsageStore,
        spend_cap: SpendCap,
        stop_event: asyncio.Event,
        volume_coordinator: "VolumeCoordinator",
        *,
        legs: "list[_LegRuntime]",
        cues: AudioCueManager | None = None,
        camilla: CamillaController | None = None,
        heartbeat: "Heartbeat | None" = None,
        wake_event_store: WakeEventStore | None = None,
        tool_packs: list[dict] | None = None,
        conversation_store: ConversationStore | None = None,
        manual_mics: "list[_ManualMicRuntime] | None" = None,
        vad: SpeechVAD | None = None,
        initial_mic_muted: bool | None = None,
        barge_in_reconcile: InterruptReconcile | None = None,
    ) -> None:
        self._cfg = cfg
        self._tts = tts
        # Per-pack tool-registration outcomes, already serialized to the
        # /state.voice.tool_packs wire shape by outcomes_to_state. Opaque
        # here; held only so session_status can surface which tool
        # families registered / were gated off / failed to build.
        self._tool_packs: list[dict] = tool_packs or []
        # Wake-detection legs, keyed by jasper.wake_legs token. Assembled
        # by run(), which opens each leg's mic under the AsyncExitStack
        # and builds its detector, capture ring and — for "off" — a
        # session shadow VAD.
        self._legs: dict[str, _LegRuntime] = {
            leg.spec.token: leg for leg in legs
        }
        self._manual_mics: dict[str, _ManualMicRuntime] = {
            runtime.source_id: runtime for runtime in (manual_mics or [])
        }
        self._active_manual_source: str | None = None
        # Push-to-talk-only is a DERIVED runtime state, not a declared or
        # config-inferred one: this daemon resolved zero wake legs and holds
        # at least one manual mic source, so every turn it can ever open is a
        # button turn. Derived from what was actually opened rather than
        # inferred from config — `cfg.mic_device` defaults to the literal
        # "Array", so "empty mic_device" never fires on a real box — and it
        # composes with the mic-unplugged case without knowing anything about
        # install tiers.
        self._push_to_talk_only: bool = (
            not self._legs and bool(self._manual_mics)
        )
        # A configured leg without a _LEG_DB telemetry mapping would raise an
        # uncaught KeyError in the wake hot path, where telemetry must be
        # fail-soft; fail at startup instead of at fire time.
        _unmapped = [tok for tok in self._legs if tok not in _LEG_DB]
        if _unmapped:
            raise RuntimeError(
                f"wake legs missing a _LEG_DB telemetry mapping: "
                f"{sorted(_unmapped)} (add them to _LEG_DB in "
                "voice_daemon.py)"
            )
        # `_on` is absent on a push-to-talk-only speaker: no
        # always-listening microphone, so `_configured_wake_legs` planned no
        # legs. Every read site reachable in that mode is None-tolerant —
        # `run()` branches to a keepalive loop, and the capture-ring readers
        # sit behind a wake fire that cannot happen without a detector. The
        # ring still gets a real deque so no reader special-cases it.
        _on = self._legs.get("on")
        self._mic = _on.mic if _on is not None else None
        self._detector = _on.detector if _on is not None else None
        self._capture_ring_on = (
            _on.capture_ring if _on is not None
            else deque(maxlen=CAPTURE_RING_FRAMES)
        )
        self._capture_ring_off = (
            self._legs["off"].capture_ring if "off" in self._legs
            else deque(maxlen=CAPTURE_RING_FRAMES)
        )
        self._capture_ring_dtln = (
            self._legs["dtln"].capture_ring if "dtln" in self._legs
            else deque(maxlen=CAPTURE_RING_FRAMES)
        )
        # Shared OR-gate lock across the parallel leg loops. Held only for
        # the critical section that sets refractory_until + reads the
        # other legs' recent scores. Without this, two legs could race to
        # fire the same wake event simultaneously.
        self._wake_fire_lock: asyncio.Lock = asyncio.Lock()
        # The fire-decision seam: the single place a leg's fire threshold
        # is decided, so per-condition thresholds and any corroboration /
        # veto land here rather than in the parallel leg loops.
        # `_current_condition` is the acoustic condition the fuser keys
        # on.
        self._fuser: WakeFuser = WakeFuser()
        self._current_condition: str = DEFAULT_CONDITION
        # Loop-clock timestamp of the last condition recompute; 0.0 forces
        # a refresh on the first WAKE frame.
        self._condition_refreshed_at: float = 0.0
        self._connection = connection
        self._ducker = ducker
        self._output_gate = AssistantOutputGate()
        self._turn_output_episode: AssistantOutputEpisode | None = None
        # One admission authority for assistant audio, asked twice: the gate
        # refuses an episode that has not started yet, this hook refuses the
        # bytes of one that already had (issue #1913).
        tts.set_emission_admission(self._output_admission_refusal)
        # Direct camilla handle for `CueDuck` (snapshot-based duck
        # around dynamic-text cues). Optional for back-compat with
        # tests / out-of-tree callers; without it, dynamic-text cues
        # play unducked rather than crashing.
        self._camilla = camilla
        self._content_activity = content_activity
        self._usage_store = usage_store
        self._spend_cap = spend_cap
        self._conversation_store = conversation_store
        self._conversation_store_path = (
            conversation_store.db_path if conversation_store is not None else None
        )
        self._conversation_turn_seq = 0
        self._research_scheduler: ResearchScheduler | None = None
        self._research_provider_id: str | None = None
        self._research_model: str | None = None
        self._pending_research: list[ResearchJob] = []
        self._research_pending_cap = RESEARCH_PENDING_ANNOUNCE_CAP
        self._research_failure_cooldown_sec = RESEARCH_FAILURE_COOLDOWN_SEC
        self._last_research_failure_announce_at: float | None = None
        self._research_announce_lock = asyncio.Lock()
        self._research_window_active: bool = False
        self._research_window_job: ResearchJob | None = None
        self._research_window_decided: bool = False
        self._research_window_cancelled_by_wake: bool = False
        self._research_window_opening_done: asyncio.Event | None = None
        self._stop_event = stop_event
        self._volume_coordinator = volume_coordinator
        self._cues = cues
        # One-shot latch for the "cue requested but no cue manager"
        # WARN in _play_cue — see that method for why it must not be
        # silent, and why it logs once rather than per-cue.
        self._warned_cues_unconfigured = False
        # Bumped on every mic frame — proof that audio capture is alive
        # AND the async loop is iterating. If either dies (PortAudio
        # wedge, asyncio deadlock, mic device disappearance), the
        # heartbeat thread stops patting systemd and
        # `Restart=on-watchdog` revives us. See jasper/watchdog.py.
        self._heartbeat = heartbeat

        # Local Silero VAD for in-session barge-in gating. While the
        # model is producing TTS, mic frames are forwarded to Gemini
        # ONLY if the local VAD detects user speech — TTS bleed-through
        # is filtered out, real interrupts pass through.
        #
        # None on a push-to-talk-only daemon: every reader below is already
        # off on a button turn (barge-in refused, server VAD refused, the
        # endpointer bypassed), and `SpeechVAD()` is what pulls openwakeword
        # + onnxruntime into resident memory. See ADR-0217.
        self._vad: SpeechVAD | None = vad
        if self._vad is None and not self._push_to_talk_only:
            self._vad = SpeechVAD()
        # Session-state shadow VAD for the chip-direct ("off") leg, when
        # configured. Created in run() and carried on that leg's
        # _LegRuntime.
        self._vad_off: SpeechVAD | None = (
            self._legs["off"].shadow_vad if "off" in self._legs else None
        )

        self._state = State.WAKE
        self._turn: LiveTurn | None = None
        self._session_id: int | None = None
        # Re-entrancy guard for _end_turn (see its docstring). A bare
        # flag, deliberately NOT an early _state flip — _state must stay
        # SESSION through the teardown so output-stream gates hold.
        self._ending: bool = False
        self._bg_tasks: set[asyncio.Task] = set()
        self._bg_end_scheduled: bool = False
        self._fire_and_forget: set[asyncio.Task] = set()
        self._refractory_until: float = 0.0

        # Room-correction measurement window. When set, mic frames are
        # dropped (no wake-word feed, no session forward) and outputd is
        # asked to ignore content-meter samples so sweeps don't become
        # the next assistant-loudness baseline. `MeasurementHold` is the
        # only writer; every path here only reads the gate.
        self._measurement_active: asyncio.Event = asyncio.Event()
        self.measurement_hold = MeasurementHold(
            self, session_active=lambda: self._state is State.SESSION,
        )

        # User-driven mic mute, set via the MUTE / UNMUTE UDS commands.
        # When True the wake loop drains frames from the mic queue but
        # skips wake detection and session forwarding, and any active
        # session ends at the moment of mute ("stop NOW" semantics).
        # Persisted to mic_mute_state_path so it survives daemon restarts
        # (deploy, watchdog, AEC reconciler, web-wizard saves): mute is a
        # privacy promise, and a silent un-mute on restart breaks it.
        self._mic_muted = (
            read_mic_muted(cfg.mic_mute_state_path)
            if initial_mic_muted is None else initial_mic_muted
        )
        if self._mic_muted:
            logger.info(
                "mic mute: restored from %s (mic is muted at startup)",
                cfg.mic_mute_state_path,
            )

        # Pre-render generated earcons once; synthesis is pure, so caching
        # the PCM keeps the cost off hot paths. Same shape
        # `TtsPlayout.write()` accepts: 24 kHz mono at the box's wire
        # width. The bake width comes from the SAME resolution the playout
        # writes at, asked once here, so the recipe's float render is
        # quantized on the wire's grid rather than flattened onto the S16
        # grid and promoted afterwards.
        earcon_wide = _tts_wire_is_wide()
        self._earcon_wide = earcon_wide
        self._chirp_on_pcm: bytes = _generate_listening_chirp(
            going_on=True, wide=earcon_wide,
        )
        self._chirp_off_pcm: bytes = _generate_listening_chirp(
            going_on=False, wide=earcon_wide,
        )
        self._chirp_on_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="wake_start",
            pcm=self._chirp_on_pcm,
            wide=earcon_wide,
        )
        self._chirp_off_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="turn_end",
            pcm=self._chirp_off_pcm,
            wide=earcon_wide,
        )
        self._mute_click_on_pcm: bytes = _generate_mute_click(
            going_on=True, wide=earcon_wide,
        )
        self._mute_click_off_pcm: bytes = _generate_mute_click(
            going_on=False, wide=earcon_wide,
        )
        self._mute_click_on_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="unmute",
            pcm=self._mute_click_on_pcm,
            wide=earcon_wide,
        )
        self._mute_click_off_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="mute",
            pcm=self._mute_click_off_pcm,
            wide=earcon_wide,
        )

        # Monotonic wallclock at the moment wake fires. Used by
        # _begin_turn to break the wake→activity_start latency into
        # named segments (state reset, loudness prepare, duck,
        # acquire_turn) so a slow turn-acquire can be localized.
        # 0.0 means "no wake yet this session"; replaced on every fire.
        self._wake_event_at_monotonic: float = 0.0

        # End-of-utterance detection state (per-turn). `audio_stream_end`
        # MUST be sent the moment the user stops speaking, not at turn
        # cleanup: without it the server stays in "listening for end of
        # turn-1" and the next turn's audio is silently swallowed. Silero
        # gives per-frame speech probability; consecutive silence after
        # speech accumulates until it crosses the threshold, then
        # turn.end_input() sends the marker.
        self._user_speech_seen: bool = False
        self._silence_started_at: float = 0.0
        self._input_ended: bool = False
        self._turn_started_at_loop: float = 0.0
        self._max_silero_score_in_turn: float = 0.0
        # Anchor timestamp for the current run of continuous speech.
        # Resets to 0 on any sub-threshold frame; once `now -
        # _speech_run_started_at >= SUSTAINED_SPEECH_TO_ARM_SEC` AND
        # `_speech_run_max_silero >= SPEECH_RUN_PEAK_MIN`, arm the
        # silence detector.
        self._speech_run_started_at: float = 0.0
        # Max Silero score observed within the current speech run.
        # Resets to 0 on any sub-threshold frame (same lifetime as
        # `_speech_run_started_at`). Used to reject wake-tail audio
        # — see SPEECH_RUN_PEAK_MIN.
        self._speech_run_max_silero: float = 0.0
        self._server_vad_this_turn: bool = False
        # Decided once per turn in `_begin_turn_inner`: true when this turn's
        # session audio comes from a push-to-talk source, so the button owns
        # both boundaries and local VAD must not become a second writer of
        # end-of-input.
        self._manual_endpoint_this_turn: bool = False
        self._max_silero_raw_in_turn: float = 0.0
        self._silero_raw_armed_at_ms: int | None = None
        self._silero_aec_armed_at_ms: int | None = None

        # In-session barge-in (full-duplex). DEFAULT OFF: resolved fresh
        # per turn in _begin_turn from the per-provider SSOT flag, then
        # gated by AEC-reference availability.
        # `_barge_in_reference_available` is constant for the daemon
        # (mic_device is frozen Config); the no-reference WARN is one-shot
        # per daemon to avoid per-turn log spam on a misconfigured
        # direct_mic + barge-in-on install.
        self._barge_in_reference_available: bool = _aec_reference_available(
            cfg.mic_device,
        )
        self._barge_in_no_ref_warned: bool = False
        self._barge_in_ptt_warned: bool = False
        self._ptt_cap_warned: bool = False
        self._server_vad_ptt_warned: bool = False
        # One-shot per daemon like the latches above: the zero-chunks arm
        # observes that nothing came back, never why, so a per-turn repeat
        # buries the one-shot warnings that do name a cause. See #2228.
        self._silent_response_warned: bool = False
        self._barge_in_active: bool = False
        # Reconciliation kind for the active provider (resolved once — the
        # provider is fixed for the daemon's life; a switch restarts us).
        # Consumed by barge.detected + /state so a durable barge-in
        # (needs_client_truncate: OpenAI/Grok send response.cancel +
        # conversation.item.truncate) is distinguishable from a cosmetic one
        # (server_self_truncates: Gemini no-ops the reconcile, so a real-time
        # provider may resume).
        self._barge_in_reconcile = (
            resolve_interrupt_reconcile(cfg.voice_provider)
            if barge_in_reconcile is None else barge_in_reconcile
        )
        self._barge_in_run_started_at: float = 0.0
        self._barge_in_run_peak: float = 0.0
        self._barge_in_signalled_this_run: bool = False
        # Firing telemetry surfaced through session_status -> /state.voice.
        # `count` is a daemon-lifetime running total (NOT per-turn — a
        # per-turn counter reads 0 between turns, exactly when /state is
        # polled), so "is barge-in firing a lot?" is answerable from the
        # dashboard, complementing the per-fire event=barge.detected line.
        self._barge_in_count: int = 0
        self._barge_in_last_at: str | None = None
        self._barge_in_last_leg: str | None = None
        # Rolling ring of the most recent mic frames, appended in both
        # WAKE and SESSION state and drained into the new turn at
        # _begin_turn so the first phoneme of the command isn't clipped.
        self._pre_roll: deque = deque(maxlen=PRE_ROLL_FRAMES)

        # Wake-event telemetry. The store owns the SQLite writes, per-leg
        # audio capture and retention; the WakeLoop contributes the
        # per-leg capture rings and the in-flight event id. Those rings
        # stay separate from `_pre_roll`: they are sized for offline
        # review (~6 s windows around each wake event) while the pre-roll
        # is sized for first-phoneme preservation at turn-open (~560 ms).
        self._wake_event_store: WakeEventStore | None = wake_event_store
        # The wake event currently in flight, or None when in WAKE state
        # with no pending event. Set in `_handle_wake_frame` on fire;
        # cleared in `_end_turn` after the final outcome write. The
        # funnel-stage hooks consult this to know which row to UPDATE.
        self._current_event_id: str | None = None
        # Frames captured during the wake → turn-acquired window. While
        # `_acquiring` is set the mic loops route frames here instead of
        # through the wake or session handlers, and the background
        # acquire task drains them into the turn in order once
        # acquire_turn() resolves — so the user's full utterance survives
        # a context reset or network blip that stretches the acquire
        # window to several seconds.
        self._acquiring: bool = False
        self._acquire_buffer: deque = deque(maxlen=ACQUIRE_BUFFER_MAX_FRAMES)

        # Multi-device peering: epoch UUID assigned by the peering
        # daemon when this Pi wins arbitration. Used to correlate the
        # SESSION_STARTED / SESSION_ENDED notifications back to the
        # specific wake event. Empty string means "no peer-tracked
        # session" — either peering is disabled, or this is a
        # remote-driven session that didn't go through arbitration.
        self._peering_current_epoch: str = ""

    @classmethod
    def for_tests(
        cls, *, legs=_UNSET, manual_mics=None, tts=None, vad=_UNSET, **overrides,
    ):
        """Build a fully-shaped WakeLoop without opening hardware.

        The supported seam for unit tests that exercise individual
        methods, so production code needs no defensive probes for
        partially-initialised instances.

        ``**overrides`` are applied by ``setattr`` AFTER construction, so
        they cannot reach a decision ``__init__`` makes from its
        arguments. ``legs``, ``manual_mics``, ``tts`` and ``vad`` are
        constructor-time knobs, so a test can build the shape a
        push-to-talk-only speaker actually has — no wake legs plus a
        manual mic source — and exercise the real derivation rather than a
        value poked in afterwards. Pass ``legs=[]`` to mean "none";
        omitting it keeps the default primary leg. Pass ``vad=None`` to
        let ``__init__`` make its own VAD decision.
        """

        class _TestMic:
            async def frames(self):
                if False:
                    yield None

        class _TestDetector:
            threshold = 0.5

            def score_frame(self, _frame) -> float:
                return 0.0

            def reset(self) -> None:
                return None

        class _TestTts:
            def set_emission_admission(self, _admission) -> None:
                return None

            async def write_segment(self, *_args, **_kwargs) -> None:
                return None

            async def resume_content_meter(self) -> None:
                return None

            async def pause_content_meter(self) -> None:
                return None

            async def prepare_assistant_context(self, **_kwargs) -> None:
                return None

            async def end_segment(self) -> None:
                return None

            async def wait_drained(self) -> None:
                return None

            async def flush(self):
                return None

            def expected_drain_at(self) -> float:
                return 0.0

            def take_paced_sec(self) -> float:
                return 0.0

        class _TestConnection:
            def is_paused(self) -> bool:
                return False

            def last_failure_detail(self) -> str | None:
                return None

            def wake_cue(self) -> str:
                return "cant_connect"

            def request_reconnect_now(self) -> bool:
                return False

            def supports_server_vad(self) -> bool:
                return False

            async def acquire_turn(self):
                raise AssertionError("WakeLoop.for_tests acquire_turn stub used")

        class _TestDucker:
            is_ducked = False

            async def duck(self) -> None:
                return None

            async def restore(self) -> None:
                return None

        class _TestContentActivity:
            music_dbfs = None

            def music_is_playing(self) -> bool:
                return False

            def pause(self) -> None:
                return None

            def resume(self) -> None:
                return None

        class _TestUsageStore:
            write_degraded = False

            def open_session(self, *_args, **_kwargs) -> int:
                return 1

            def close_session(self, *_args, **_kwargs) -> float:
                return 0.0

        class _TestSpendCap:
            def allowed(self) -> bool:
                return True

        class _TestVolumeCoordinator:
            def get_listening_level(self) -> int:
                return 50

            def note_voice_session(self, *_args, **_kwargs) -> None:
                return None

            async def note_measurement_active(self, *_args, **_kwargs) -> None:
                return None

        class _TestVad:
            def predict(self, _frame) -> float:
                return 0.0

            def reset(self) -> None:
                return None

        cfg = SimpleNamespace(
            duck_db=0.0,
            idle_timeout_sec=10.0,
            mic_device="udp:9876",
            mic_mute_state_path="/tmp/jasper-voice-daemon-test-mute.env",
            peering_enabled=False,
            peering_uds_socket="/tmp/jasper-peering-test.sock",
            response_stall_timeout_sec=120.0,
            server_vad_enabled=False,
            server_vad_prefix_ms=300,
            server_vad_silence_ms=500,
            server_vad_threshold=0.5,
            vad_barge_in_threshold=0.5,
            voice_provider="test",
            wake_model="test_model",
        )
        mic = _TestMic()
        detector = _TestDetector()
        on_ring = deque(maxlen=CAPTURE_RING_FRAMES)
        self = cls(
            cfg=cfg,
            tts=_TestTts() if tts is None else tts,
            connection=_TestConnection(),
            ducker=_TestDucker(),
            content_activity=_TestContentActivity(),
            usage_store=_TestUsageStore(),
            spend_cap=_TestSpendCap(),
            stop_event=asyncio.Event(),
            volume_coordinator=_TestVolumeCoordinator(),
            legs=[
                _LegRuntime(
                    by_token("on"),
                    mic,
                    detector,
                    on_ring,
                ),
            ] if legs is _UNSET else legs,
            manual_mics=manual_mics,
            vad=_TestVad() if vad is _UNSET else vad,
            initial_mic_muted=False,
            barge_in_reconcile=InterruptReconcile.NEEDS_CLIENT_TRUNCATE,
        )
        for key, value in overrides.items():
            setattr(self, key if key.startswith("_") else f"_{key}", value)
        if "conversation_store" in overrides or "_conversation_store" in overrides:
            store = self._conversation_store
            self._conversation_store_path = (
                store.db_path if store is not None else None
            )
        return self

    def _create_fire_and_forget_task(
        self,
        coro: Coroutine[object, object, object],
        *,
        name: str,
    ) -> asyncio.Task:
        return _track_task(
            asyncio.create_task(coro, name=name),
            self._fire_and_forget,
            label=name,
        )

    def _arm_session_task_watcher(self) -> None:
        if not self._bg_tasks:
            return
        self._create_fire_and_forget_task(
            self._watch_session_tasks(tuple(self._bg_tasks)),
            name="session-task-watcher",
        )

    async def _watch_session_tasks(self, tasks: tuple[asyncio.Task, ...]) -> None:
        done, _pending = await asyncio.wait(
            set(tasks),
            return_when=asyncio.FIRST_COMPLETED,
        )
        # A normal mic frame also notices completed bg tasks. The watcher
        # exists for manual mic sources that can go quiet after button release,
        # so guard against stale completions from an already-ended turn.
        if not any(task in self._bg_tasks for task in done):
            return
        await self._end_turn()

    async def _cancel_fire_and_forget_tasks(self) -> None:
        await _cancel_tracked_tasks(self._fire_and_forget)

    def _arm_turn_background_end(self) -> None:
        """End the turn when a response/playback background task completes.

        The primary mic loop also checks ``_bg_tasks`` on each session
        frame, but a manual source stops producing frames after button
        release, so teardown must be anchored to response completion
        rather than to a later button press tickling the frame loop.
        """
        self._bg_end_scheduled = False
        for task in self._bg_tasks:
            task.add_done_callback(self._on_turn_background_done)

    def _on_turn_background_done(self, task: asyncio.Task) -> None:
        if task not in self._bg_tasks:
            return
        if self._ending or self._turn is None or self._bg_end_scheduled:
            return
        self._bg_end_scheduled = True
        self._create_fire_and_forget_task(
            self._end_turn(),
            name="voice-turn-background-end",
        )

    async def play_cue(self, slug: str) -> str:
        """Public wrapper for `_play_cue`, callable via the control
        socket so external clients (jasper-control HTTP, the
        `jasper-cues play` CLI) can play cues through the daemon's
        fan-in-backed TtsPlayout.

        Answers `measurement_active` while a room-correction measurement
        window is open. Refusal is structural either way — the episode
        below cannot be admitted — so this early read exists only to keep
        that distinct code on the wire, which a plain `busy` would hide."""
        if not slug:
            return "missing_slug"
        if self._cues is None:
            return "cues_not_configured"
        from .cues.registry import find as _find
        if _find(slug) is None:
            return "unknown_slug"
        refusal = self._output_admission_refusal()
        if refusal is not None:
            log_event(logger, "cue.skipped", reason=refusal, slug=slug)
            return refusal
        if self._output_gate.is_active:
            log_event(
                logger,
                "cue.play_busy",
                slug=slug,
                active_kind=self._output_gate.active_kind,
            )
            return "busy"
        episode = await self._output_gate.begin_if_idle("admin")
        if episode is None:
            log_event(
                logger,
                "cue.play_busy",
                slug=slug,
                active_kind=self._output_gate.active_kind,
            )
            return "busy"
        played = await self._play_cue_owned(slug, episode)
        return "ok" if played else "play_failed"

    async def play_supervisor_cue(self, slug: str) -> str:
        """Cue trigger reserved for proactive notifications from
        background supervisors (e.g. the GeminiLiveConnection's
        consecutive-failure escalation).

        Differs from `play_cue` by skipping if a user-driven turn is
        in flight: TtsPlayout has one active output stream (the
        outputd/fan-in TTS IPC connection), so layering an escalation
        cue on top of an active TTS turn would garble both. Suppressing
        the cue mid-session is the safe default — if the connection is
        wedged, the next wake event fires the same cue reactively anyway.

        A measurement window needs no check of its own here: the shared
        admission answer names it even when output happens to be busy too,
        and `play_cue` below refuses on it otherwise."""
        if self._state is State.SESSION:
            return "skipped_session_active"
        if self._output_gate.is_active:
            return self._output_admission_refusal() or "skipped_output_active"
        return await self.play_cue(slug)

    def set_research_scheduler(
        self,
        scheduler: ResearchScheduler | None,
        *,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Wire the research scheduler so announcements can mark jobs
        announced only after the wake loop has attempted the spoken path."""
        self._research_scheduler = scheduler
        self._research_provider_id = provider_id
        self._research_model = model

    async def announce_timer(self, timer: "Timer") -> None:
        """Public hook called by `TimerScheduler` when a timer fires.

        Speaks the announcement via dynamic-text TTS. Defers up to
        5 s if a voice session is currently active (don't cross-talk
        the LLM's TTS); after the grace window the announcement is
        skipped — the user is already engaged and a delayed timer
        chime would be more confusing than a missed one. The user
        can `list_timers` to recover state in either case.

        A room-correction measurement window drops the announcement
        wherever the loop below happens to be: output admission is closed
        for the whole window, so `_play_dynamic_text` refuses the episode
        and the emission seam refuses any byte that got past it.
        """
        text = announcement_text(timer)
        deadline = asyncio.get_event_loop().time() + 5.0
        while self._state is State.SESSION or self._output_gate.is_active:
            if asyncio.get_event_loop().time() >= deadline:
                logger.warning(
                    "timer announce: skipped (id=%s) — assistant output "
                    "still active after 5s grace window",
                    timer.id,
                )
                return
            await asyncio.sleep(0.5)
        logger.info(
            "timer announce: id=%s label=%r text=%r",
            timer.id, timer.label, text,
        )
        await self._play_dynamic_text(text)

    async def announce_research_ready(self, job: "ResearchJob") -> None:
        """Public hook called by `ResearchScheduler` when a job finishes.

        Research is a "tell me later" promise. Unlike timer chimes, a
        result that arrives mid-conversation is held until the wake loop
        returns to WAKE, then drained by _end_turn_inner.

        Held the same way while a room-correction measurement window is
        open (issue #1786): speaking would corrupt the sweep.

        The drain only runs on the household's next COMPLETED voice turn
        (_end_turn_inner → _drain_pending_research), not on
        `MeasurementHold.resume()` — a queued result can therefore sit for a
        while, bounded only by `_research_pending_cap`. Draining on resume
        would fire at the sweep's trailing edge, the in-flight-bleed
        window tracked as issue #1898.
        """
        async with self._research_announce_lock:
            if self._measurement_active.is_set():
                log_event(
                    logger,
                    "research.announce_suppressed",
                    job_id=job.id,
                    status=job.status,
                    reason="measurement_active",
                )
                self._queue_pending_research(job)
                return
            if self._state is State.SESSION or self._output_gate.is_active:
                self._queue_pending_research(job)
                return
            await self._speak_research_job(job)

    def _queue_pending_research(self, job: ResearchJob) -> None:
        for idx, pending in enumerate(self._pending_research):
            if pending.id == job.id:
                self._pending_research[idx] = job
                log_event(
                    logger,
                    "research.announce_pending_coalesced",
                    job_id=job.id,
                    status=job.status,
                )
                return
        self._pending_research.append(job)
        if len(self._pending_research) > self._research_pending_cap:
            dropped = self._pending_research.pop(0)
            log_event(
                logger,
                "research.announce_pending_dropped",
                job_id=dropped.id,
                status=dropped.status,
                cap=self._research_pending_cap,
                level=logging.WARNING,
            )
        log_event(
            logger,
            "research.announce_held",
            job_id=job.id,
            status=job.status,
            pending=len(self._pending_research),
        )

    async def _drain_pending_research(self) -> None:
        # measurement_active is bundled with session here (both mean "don't
        # emit ANY audio right now" — issue #1786), including in the
        # per-iteration re-check below. Without that per-iteration check, a
        # measurement window opening mid-batch loops forever:
        # _speak_research_job's own measurement guard re-queues the job,
        # which re-fills `_pending_research`, which the `while` condition
        # sees as "more work" — a tight busy-spin with no sleep, for as
        # long as the window stays open (potentially minutes for a held
        # crossover-v2 session, unlike the normally-brief
        # `_output_gate.is_active`, which stays out of this check).
        if (
            self._state is State.SESSION
            or self._output_gate.is_active
            or self._measurement_active.is_set()
        ):
            return
        async with self._research_announce_lock:
            if (
                self._state is State.SESSION
                or self._output_gate.is_active
                or self._measurement_active.is_set()
            ):
                return
            while self._pending_research and self._state is State.WAKE:
                batch = self._pending_research
                self._pending_research = []
                for idx, job in enumerate(batch):
                    if (
                        self._state is State.SESSION
                        or self._measurement_active.is_set()
                    ):
                        self._pending_research = (
                            batch[idx:] + self._pending_research
                        )
                        return
                    await self._speak_research_job(job)

    async def _speak_research_job(self, job: ResearchJob) -> None:
        if self._measurement_active.is_set():
            log_event(
                logger,
                "research.announce_suppressed",
                job_id=job.id,
                status=job.status,
                reason="measurement_active",
            )
            self._queue_pending_research(job)
            return
        if self._state is State.SESSION or self._output_gate.is_active:
            self._queue_pending_research(job)
            return
        text: str | None
        if job.status == DONE and job.result:
            text = RESEARCH_READY_CONFIRMATION_TEXT
        elif job.status == DONE:
            log_event(
                logger,
                "research.announce_missing_result",
                job_id=job.id,
                level=logging.WARNING,
            )
            text = RESEARCH_EMPTY_RESULT_TEXT
        elif job.status == FAILED:
            text = None
        else:
            log_event(
                logger,
                "research.announce_skipped",
                job_id=job.id,
                status=job.status,
                reason="unexpected_status",
                level=logging.WARNING,
            )
            return

        if job.status == FAILED:
            remaining = self._research_failure_cooldown_remaining()
            if remaining > 0:
                log_event(
                    logger,
                    "research.announce_suppressed",
                    job_id=job.id,
                    status=job.status,
                    reason="failure_cooldown",
                    remaining_s=round(remaining, 1),
                    level=logging.WARNING,
                )
                self._mark_research_announced(job, read=False)
                return

        if job.status == FAILED:
            log_event(
                logger,
                "research.announce",
                job_id=job.id,
                status=job.status,
                mode="cue",
                cue=RESEARCH_FAILED_CUE_SLUG,
            )
            played = await self._play_cue(RESEARCH_FAILED_CUE_SLUG)
        else:
            assert text is not None
            # Log shape, not content: a research result can carry personal
            # material (medical/financial queries) and the journal is
            # persistent. Full text stays at DEBUG (cue manager) only.
            log_event(
                logger,
                "research.announce",
                job_id=job.id,
                status=job.status,
                mode="confirmation",
                text_len=len(text),
            )
            played = await self._play_dynamic_text(text)
        if not played:
            log_event(
                logger,
                "research.announce_playback_failed",
                job_id=job.id,
                status=job.status,
                level=logging.WARNING,
            )
            return
        if job.status == FAILED:
            self._last_research_failure_announce_at = (
                asyncio.get_event_loop().time()
            )
        elif job.status == DONE and job.result:
            self._mark_research_announced(job, read=False)
            self._refractory_until = max(
                self._refractory_until,
                (
                    asyncio.get_event_loop().time()
                    + RESEARCH_CONFIRMATION_REFRACTORY_SEC
                ),
            )
            await self._open_confirmation_window(job)
            return
        self._mark_research_announced(
            job,
            read=job.status == DONE and bool(job.result),
        )

    async def _open_confirmation_window(self, job: ResearchJob) -> None:
        reason = self._research_confirmation_guard_reason()
        if reason is not None:
            log_event(
                logger,
                "research.confirmation_window_skipped",
                job_id=job.id,
                reason=reason,
            )
            # session_active and measurement_active both mean "don't
            # emit ANY audio right now" — queue for the drain path
            # instead (issue #1786). The other reasons (mic_muted,
            # spend_cap_reached, connection_paused) mean "can't listen
            # for a reply" but speaking is still safe, so those fall
            # through to an immediate read.
            if reason in ("session_active", "measurement_active"):
                self._queue_pending_research(job)
                return
            await self._read_research_job_immediately(job)
            return

        self._research_window_active = True
        self._research_window_job = job
        self._research_window_decided = False
        self._research_window_cancelled_by_wake = False
        opening_done = asyncio.Event()
        self._research_window_opening_done = opening_done
        reset_window = True
        try:
            await self._begin_turn(
                pre_roll=False,
                text_context=_research_confirmation_instruction(job),
            )
            reset_window = False
            if self._research_window_cancelled_by_wake:
                await self._end_turn("research_window_wake")
                return
            log_event(logger, "research.confirmation_window_opened", job_id=job.id)
        except (
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as e:
            if self._research_window_cancelled_by_wake:
                logger.info(
                    "research confirmation window cancelled while opening "
                    "(id=%s): %s",
                    job.id,
                    e,
                )
                if self._turn_output_episode is not None:
                    await self._cleanup_after_failed_begin()
                return
            logger.exception(
                "research confirmation window failed; reading immediately "
                "(id=%s): %s",
                job.id,
                e,
            )
            if self._turn_output_episode is not None:
                await self._cleanup_after_failed_begin()
            await self._read_research_job_immediately(job)
        finally:
            if reset_window:
                self._research_window_active = False
                self._research_window_job = None
                self._research_window_decided = False
                self._research_window_cancelled_by_wake = False
            if self._research_window_opening_done is opening_done:
                self._research_window_opening_done = None
            opening_done.set()

    def _research_confirmation_guard_reason(self) -> str | None:
        if self._state is State.SESSION:
            return "session_active"
        if self._mic_muted:
            return "mic_muted"
        if self._measurement_active.is_set():
            return "measurement_active"
        if not self._spend_cap.allowed():
            return "spend_cap_reached"
        if self._connection.is_paused():
            return "connection_paused"
        return None

    async def _read_research_job_immediately(self, job: ResearchJob) -> None:
        text = (job.result or "").strip()
        if not text:
            text = RESEARCH_EMPTY_RESULT_TEXT
        played = await self._play_dynamic_text(text)
        if not played:
            logger.warning(
                "research immediate readback failed id=%s status=%s",
                job.id,
                job.status,
            )
            return
        self._mark_research_announced(job, read=bool(job.result))

    def record_research_delivery(
        self,
        job: ResearchJob,
        assistant_text: str | None,
        decision: str,
    ) -> None:
        if (
            self._research_window_active
            and self._research_window_job is not None
            and self._research_window_job.id == job.id
        ):
            self._research_window_decided = True
        self._record_conversation_turn(
            job.query,
            assistant_text,
            data_json={"kind": "research", "job_id": job.id},
        )
        self._clear_pending_research(job.id)

    def _clear_pending_research(self, job_id: str) -> None:
        before = len(self._pending_research)
        if before == 0:
            return
        self._pending_research = [
            job for job in self._pending_research if job.id != job_id
        ]
        cleared = before - len(self._pending_research)
        if cleared:
            log_event(
                logger,
                "research.announce_pending_cleared",
                job_id=job_id,
                count=cleared,
            )

    def _research_failure_cooldown_remaining(self) -> float:
        last = self._last_research_failure_announce_at
        if last is None:
            return 0.0
        elapsed = asyncio.get_event_loop().time() - last
        return max(0.0, self._research_failure_cooldown_sec - elapsed)

    def _mark_research_announced(self, job: ResearchJob, *, read: bool) -> None:
        if self._research_scheduler is not None:
            self._research_scheduler.mark_announced(job.id)
            if read:
                self._research_scheduler.mark_read(job.id)
        if read:
            self.record_research_delivery(job, job.result, "yes")

    def _conversation_store_for_settings(
        self,
        settings: ConversationSettings,
    ) -> ConversationStore | None:
        if not settings.capture_enabled:
            return None
        store = self._conversation_store
        if (
            store is not None
            and self._conversation_store_path == settings.db_path
            and store.available
        ):
            return store
        if store is not None:
            store.close()
            self._conversation_store = None
            self._conversation_store_path = None
        store = ConversationStore(settings.db_path)
        self._conversation_store = store
        self._conversation_store_path = settings.db_path
        return store if store.available else None

    def close_conversation_store(self) -> None:
        store = self._conversation_store
        self._conversation_store = None
        self._conversation_store_path = None
        if store is None:
            return
        store.close()

    def _record_conversation_turn(
        self,
        user_text: str | None,
        assistant_text: str | None,
        *,
        data_json: dict | str | None = None,
        provider: str | None = None,
    ) -> None:
        """Persist one conversation-history row.

        The single write path for ordinary wake turns and feature-fed
        entries such as research delivery. Fail-soft by design: capture
        must never block turn teardown or a proactive announcement.
        """
        if self._mic_muted:
            return
        if user_text is None and assistant_text is None and data_json is None:
            return
        try:
            settings = read_conversation_settings()
        except (OSError, TypeError, ValueError) as e:
            logger.warning(
                "conversation capture: settings unavailable (%s: %s)",
                type(e).__name__,
                e,
            )
            return
        if not settings.capture_enabled:
            return
        store = self._conversation_store_for_settings(settings)
        if store is None:
            logger.debug("conversation capture: skipped (store unavailable)")
            return
        data_text: str | None = None
        if isinstance(data_json, dict):
            try:
                data_text = json.dumps(data_json, separators=(",", ":"))
            except (TypeError, ValueError) as e:
                logger.warning(
                    "conversation capture: data_json encode failed (%s: %s)",
                    type(e).__name__,
                    e,
                )
                data_text = None
        elif data_json is not None:
            data_text = str(data_json)
        if user_text is None and assistant_text is None and data_text is None:
            return

        ts_utc = _conversation_ts_utc()
        self._conversation_turn_seq = (
            (self._conversation_turn_seq % 999) + 1
        )
        session_id = getattr(self, "_session_id", None)
        turn = ConversationTurn(
            id=make_turn_id(ts_utc, self._conversation_turn_seq),
            ts_utc=ts_utc,
            provider=provider or self._cfg.voice_provider,
            user_text=user_text,
            assistant_text=assistant_text,
            tool_calls_json=None,
            data_json=data_text,
            session_id=session_id,
        )
        if store.add(turn):
            try:
                prune_for_settings(store, settings, anchor_ts_utc=ts_utc)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning(
                    "conversation capture: retention prune failed (%s: %s)",
                    type(e).__name__,
                    e,
                )

    async def _play_dynamic_text(self, text: str) -> bool:
        """Speak arbitrary `text` through the cue manager, with
        snapshot-based duck/restore around the playback.

        Uses `CueDuck` rather than the daemon's `Ducker` because a cue is
        a brief, passive interruption: the user isn't adjusting volume
        mid-cue, so "music returns to exactly where it was" matters more
        than the remote-twist-wins behaviour `Ducker` is designed for.
        See `jasper/camilla.py:CueDuck`."""
        if self._cues is None:
            logger.warning("dynamic text play skipped: cues unavailable")
            return False
        # Ahead of prerender, which is a paid synthesis plus a disk write:
        # a closed window cannot admit the episode that would speak it.
        refusal = self._output_admission_refusal()
        if refusal is not None:
            log_event(logger, "dynamic_text.skipped", reason=refusal)
            return False
        prerender_text = getattr(self._cues, "prerender_text", None)
        if callable(prerender_text):
            try:
                if not await prerender_text(text):
                    logger.warning("dynamic text play failed: prerender failed")
                    return False
            except Exception as e:  # noqa: BLE001
                logger.warning("dynamic text play failed: prerender failed: %s", e)
                return False
        episode = await self._output_gate.begin_if_idle("proactive")
        if episode is None:
            log_event(
                logger,
                "dynamic_text.skipped",
                reason=self._output_admission_refusal() or "output_active",
                active_kind=self._output_gate.active_kind,
            )
            return False

        def _episode_current() -> bool:
            return self._output_gate.is_current(episode)

        async def _speak() -> bool:
            speak_guarded = getattr(self._cues, "speak_text_guarded", None)
            if callable(speak_guarded):
                return bool(await speak_guarded(text, _episode_current))
            if not _episode_current():
                return False
            await self._cues.speak_text(text)
            return True

        restore: Callable[[], Awaitable[None]] | None = None
        try:
            await self._prepare_feedback_loudness_context(kind="dynamic_text")
            if isinstance(self._ducker, FanInDucker):
                restore = self._ducker.restore
                played = False
                try:
                    await self._ducker.duck()
                    played = await _speak()
                except Exception as e:  # noqa: BLE001
                    logger.warning("dynamic text play failed: %s", e)
                return played
            owner = getattr(self._volume_coordinator, "volume_owner", None)
            if owner is None:
                # No fader owner — degrade to unducked playback rather
                # than crash. The user hears the cue over un-ducked music
                # which is loud but recoverable; better than silence.
                try:
                    return await _speak()
                except Exception as e:  # noqa: BLE001
                    logger.warning("dynamic text play failed: %s", e)
                    return False
            cue_duck = CueDuck(owner, self._cfg.duck_db)

            async def _restore_cue_duck() -> None:
                # Deliberately neutral context: CueDuck.__aexit__ only writes
                # its snapshot and never suppresses an exception. The caller
                # retains the original cancellation/error while the cleanup
                # task owns restore to a known outcome.
                await cue_duck.__aexit__(None, None, None)

            restore = _restore_cue_duck
            await cue_duck.__aenter__()
            try:
                return await _speak()
            except Exception as e:  # noqa: BLE001
                logger.warning("dynamic text play failed: %s", e)
                return False
        finally:
            try:
                if restore is None:
                    await self._finish_output_episode_after_drain(episode)
                else:
                    await self._finish_ducked_output_episode_after_drain(
                        episode,
                        restore,
                        cleanup_label="dynamic text",
                    )
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.warning("dynamic text drain cleanup failed: %s", e)

    async def _play_cue(self, slug: str) -> bool:
        """Best-effort cue playback, ducking music via CamillaDSP for the
        cue's duration. Without ducking the cue is drowned out by playing
        music; TTS-side level math alone cannot make it audible over a
        non-ducked stream.

        No tracker/volume-coordinator manipulation: those exist for
        multi-second voice sessions where the user may adjust volume mid-turn,
        and a ~6 s cue is short enough for plain duck/restore.

        The cue plays even if ducking fails — the usual cause is camilla
        restarting, in which case music is not playing through camilla either,
        so the cue is unducked but audible, and silence on a wake-blocking
        condition is the worse outcome. Ducker.restore short-circuits when the
        duck did not latch, so the finally is unconditional."""
        if self._cues is None:
            # Cues are how the user hears why the speaker did not respond.
            # With no cue manager the speaker is silent on every failure, so
            # make that state diagnosable in the journal. Once per daemon
            # lifetime: the condition is static config.
            if not self._warned_cues_unconfigured:
                self._warned_cues_unconfigured = True
                log_event(
                    logger,
                    "cue.skipped",
                    reason="cues_unconfigured",
                    slug=slug,
                    note=(
                        "no cue manager; failure cues will be SILENT for "
                        "this daemon run (check cue backend/API keys at "
                        "startup logs)"
                    ),
                    level=logging.WARNING,
                )
            return False
        episode = await self._output_gate.begin_if_idle("admin")
        if episode is None:
            log_event(
                logger,
                "cue.skipped",
                reason=self._output_admission_refusal() or "output_active",
                slug=slug,
                active_kind=self._output_gate.active_kind,
            )
            return False
        return await self._play_cue_owned(slug, episode)

    async def _play_cue_owned(
        self,
        slug: str,
        episode: AssistantOutputEpisode,
    ) -> bool:
        ducker = self._ducker
        played = False
        try:
            await self._prepare_feedback_loudness_context(kind="cue", slug=slug)
            try:
                await ducker.duck()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "cue %s: duck failed (cue will play unducked): %s",
                    slug, e,
                )
            try:
                played = await self._cues.play(slug)
            except Exception as e:  # noqa: BLE001
                logger.warning("cue %s play failed: %s", slug, e)
        finally:
            try:
                await self._finish_ducked_output_episode_after_drain(
                    episode,
                    ducker.restore,
                    cleanup_label=f"cue {slug}",
                )
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.warning("cue %s drain cleanup failed: %s", slug, e)
        return played

    async def _finish_ducked_output_episode_after_drain(
        self,
        episode: AssistantOutputEpisode,
        restore: Callable[[], Awaitable[None]],
        *,
        cleanup_label: str,
    ) -> None:
        """Own physical drain, duck restore, and exact gate release as one."""

        async def _drain_restore_and_release() -> None:
            drain_base_error: BaseException | None = None
            try:
                drain_error = await _capture_cleanup_error(
                    lambda: wait_tts_drained_owned(self._tts),
                )
                if isinstance(drain_error, Exception):
                    logger.warning(
                        "%s drain cleanup failed: %s",
                        cleanup_label,
                        drain_error,
                    )
                else:
                    drain_base_error = drain_error
            finally:
                restore_base_error: BaseException | None = None
                try:
                    restore_error = await _capture_cleanup_error(restore)
                    if isinstance(restore_error, Exception):
                        logger.warning(
                            "%s restore failed: %s",
                            cleanup_label,
                            restore_error,
                        )
                    else:
                        restore_base_error = restore_error
                finally:
                    await self._output_gate.end(episode)
                if restore_base_error is not None:
                    raise restore_base_error
            if drain_base_error is not None:
                raise drain_base_error

        await self._await_output_cleanup_owned(
            _drain_restore_and_release(),
            task_name=f"output-cleanup-{episode.kind}-{episode.id}",
        )

    async def _finish_output_episode_after_drain(
        self,
        episode: AssistantOutputEpisode,
    ) -> None:
        """Retain output ownership until accepted PCM is physically silent.

        Socket writes running in worker threads cannot be revoked by task
        cancellation. Once a cue path may have queued PCM, repeated caller
        cancellation is therefore deferred by an ``asyncio.wait`` ownership
        loop until one cleanup task reaches the playout deadline and releases
        the exact episode token.
        """

        async def _drain_and_release() -> None:
            try:
                await wait_tts_drained_owned(self._tts)
            finally:
                await self._output_gate.end(episode)

        await self._await_output_cleanup_owned(
            _drain_and_release(),
            task_name=f"output-drain-{episode.kind}-{episode.id}",
        )

    @staticmethod
    async def _await_output_cleanup_owned(
        operation: Coroutine,
        *,
        task_name: str,
    ) -> None:
        """Defer repeated caller cancellation until one cleanup completes."""

        cleanup = asyncio.create_task(operation, name=task_name)
        deferred_cancel = False
        current = asyncio.current_task()
        while not cleanup.done():
            try:
                await asyncio.wait({cleanup})
            except asyncio.CancelledError:
                if current is None or current.cancelling() == 0:
                    break
                deferred_cancel = True
                current.uncancel()
        if cleanup.cancelled():
            raise asyncio.CancelledError
        error = cleanup.exception()
        if error is not None:
            if deferred_cancel:
                raise asyncio.CancelledError from None
            raise error
        if deferred_cancel:
            raise asyncio.CancelledError

    async def run(self) -> None:
        # One wake-only consumer per non-primary leg; the primary "on" leg
        # is driven by this method's main loop below. A leg is in
        # self._legs only when both its mic and detector were configured,
        # so there is no misconfiguration case to warn about here.
        leg_tasks: list[asyncio.Task] = []
        for _leg_name in self._legs:
            if _leg_name == "on":
                continue
            leg_tasks.append(asyncio.create_task(
                self._wake_leg_loop(_leg_name),
                name=f"wake-leg-{_leg_name}",
            ))
        manual_tasks: list[asyncio.Task] = []
        for _source_id in self._manual_mics:
            manual_tasks.append(asyncio.create_task(
                self._manual_mic_loop(_source_id),
                name=f"manual-mic-{_source_id}",
            ))
        if leg_tasks:
            logger.info(
                "multi-leg wake enabled: %s", " + ".join(self._legs.keys()),
            )
        if manual_tasks:
            log_event(
                logger,
                "manual_mic.sources_enabled",
                sources=",".join(sorted(self._manual_mics)),
            )
        # A push-to-talk-only speaker has no primary mic to iterate, so the
        # heartbeat loses its usual liveness proof (a mic frame is evidence
        # both capture AND the async loop are alive). A keepalive tick
        # still proves the loop is iterating; audio arrives on the
        # manual-mic loops instead. Ticks yield None so the frame body
        # below skips them.
        #
        # Branches on `_push_to_talk_only`, not on `_mic is None`, so the
        # mode has ONE derivation. The two agree on any daemon that
        # started: `_require_usable_input` (jasper/voice/daemon_main.py)
        # refuses to run with neither a wake leg nor a manual mic. If that
        # invariant broke, raising beats keepalive-ing — a daemon patting
        # its watchdog with no input at all is a deaf speaker that looks
        # healthy.
        if self._push_to_talk_only:
            _frames = self._push_to_talk_keepalive_ticks()
            log_event(
                logger,
                "voice.push_to_talk_only",
                sources=",".join(sorted(self._manual_mics)),
                keepalive_sec=PTT_KEEPALIVE_INTERVAL_SEC,
            )
        else:
            if self._mic is None:
                # Unreachable by construction (see above); the guard only
                # chooses park-over-reboot if that invariant breaks.
                # Without it `self._mic.frames()` raises a bare
                # AttributeError, which main() does not special-case, so
                # it exits 1 and Restart=on-failure walks the unit into
                # StartLimitAction=reboot instead of the clean
                # VOICE_MIC_UNAVAILABLE_EXIT park every other input
                # failure gets.
                raise InputDeviceUnavailable(
                    "no primary capture and not push-to-talk-only — "
                    "impossible state; parking"
                )
            _frames = self._mic.frames()
        try:
            async for frame in _frames:
                if self._heartbeat is not None:
                    self._heartbeat.bump()
                if self._stop_event.is_set():
                    if self._state is State.SESSION:
                        await self._end_turn()
                    return
                if frame is None:
                    # Keepalive tick, not audio. The bump above was its whole
                    # purpose; there is no wake detection to run.
                    continue

                # Room-correction measurement window: drop the frame
                # entirely (no wake-word feed, no session dispatch, no
                # pre-roll append). Dropping pre-roll matters — sweep tail
                # in the pre-roll would prepend ~1.4 s of test-tone audio
                # to whatever turn starts right after the window closes.
                # Active sessions never reach this branch: the measurement
                # hold refuses to set the event while State.SESSION (BUSY).
                if self._measurement_active.is_set():
                    continue

                # User has muted the mic. Drain the frame (don't backpressure
                # the AEC bridge / mic capture upstream) but skip wake
                # detection and session forwarding entirely. No pre-roll
                # append either — when unmuted, the user's first "Hey Jarvis"
                # is the natural start of their utterance; carrying a mute-
                # era pre-roll would prepend silence (or whatever room
                # ambience leaked through) to the next turn.
                if self._mic_muted:
                    continue

                self._pre_roll.append(frame)
                # Independent capture ring for wake-event telemetry —
                # sized for the 6 s offline-review window, not the 560 ms
                # turn-open window. Filled in both states so the pre-fire
                # context is already on hand the moment a wake fires.
                self._capture_ring_on.append(frame)

                # Acquire window: between wake firing and the new turn
                # being ready to accept audio. The background acquire task
                # drains this buffer into the turn, so a multi-second
                # context reset doesn't truncate the user's command. See
                # ACQUIRE_BUFFER_MAX_FRAMES.
                if self._acquiring:
                    if self._active_manual_source is None:
                        self._acquire_buffer.append(frame)
                    continue

                if self._state is State.WAKE:
                    await self._handle_wake_frame(frame, leg="on")
                else:
                    if self._active_manual_source is not None:
                        continue
                    if self._research_window_active:
                        await self._handle_wake_frame(frame, leg="on")
                        if self._acquiring or self._state is State.WAKE:
                            continue
                    await self._handle_session_frame(frame)
        finally:
            # Cancel + join every leg loop before sweeping tracked side-work.
            # The leg loops are producers: while they are alive, a late wake
            # frame can still enqueue acquire/finalize tasks into
            # _fire_and_forget. Stop producers first so the cancellation sweep
            # below observes every task created during shutdown.
            for _t in (*leg_tasks, *manual_tasks):
                _t.cancel()
            for _t in (*leg_tasks, *manual_tasks):
                try:
                    await _t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self._cancel_fire_and_forget_tasks()

    async def _push_to_talk_keepalive_ticks(self) -> "AsyncIterator[None]":
        """Yield a liveness tick every PTT_KEEPALIVE_INTERVAL_SEC.

        Stands in for the primary mic's frame stream on a speaker that has
        no always-listening mic. `Heartbeat` only pats systemd when the
        progress sentinel is younger than its stale threshold, so the
        interval must stay comfortably under that or `WatchdogSec=30s`
        would reap a healthy daemon.

        Ticks UNCONDITIONALLY, exactly like a mic's `frames()`. Shutdown
        is the consumer's job: `run()`'s loop checks `_stop_event` on
        every iteration and, if a turn is in flight, awaits `_end_turn()`
        before returning — duck restore, `end_input`, turn telemetry, the
        done-listening chirp. A stop check HERE would end the iteration
        first, so `run()`'s stop branch never runs and a SIGTERM mid-hold
        would leave the music ducked and the turn unfinished.
        """
        while True:
            yield None
            await asyncio.sleep(PTT_KEEPALIVE_INTERVAL_SEC)

    async def _manual_mic_loop(self, source_id: str) -> None:
        """Session-audio consumer for one push-to-talk mic source."""
        rt = self._manual_mics[source_id]
        async for frame in rt.mic.frames():
            if self._stop_event.is_set():
                return
            if self._measurement_active.is_set() or self._mic_muted:
                continue
            if self._active_manual_source != source_id:
                continue
            if self._acquiring:
                self._acquire_buffer.append(frame)
                continue
            if self._state is State.SESSION:
                await self._handle_session_frame(frame)

    async def _wake_leg_loop(self, leg_name: str) -> None:
        """Parallel wake-only consumer for a non-primary leg.

        Dispatches to `_handle_wake_frame(frame, leg=leg_name)`, which
        shares the refractory + OR-gate lock with the primary loop so one
        user attempt fires at most one wake event regardless of which
        leg(s) cross threshold first.

        Wake-detection-only: frames are NOT appended to pre-roll, NOT
        routed to `_acquire_buffer` during the wake→turn-open window, and
        NOT forwarded to live sessions. The primary "on" (AEC) stream
        stays the canonical session audio source, so the LLM is never fed
        mixed multi-leg audio.

        Mirrors the primary-loop gating (measurement window, mic mute,
        acquiring, state) so every "stop listening" signal is honored. In
        SESSION state a leg with a shadow VAD (the AEC-OFF leg) feeds
        `_shadow_vad_score_raw` for telemetry; other legs idle.
        """
        rt = self._legs[leg_name]
        async for frame in rt.mic.frames():
            if self._stop_event.is_set():
                return
            if self._measurement_active.is_set():
                continue
            # Mute is a privacy promise — do NOT record audio for the
            # wake-events corpus when the user has muted the mic. Mirrors
            # the primary loop: the capture ring fills only AFTER the
            # mute / measurement gates.
            if self._mic_muted:
                continue
            # Filled before the acquiring / WAKE-state checks so a wake
            # fire's window has pre-fire context even when it overlaps the
            # turn-open window.
            if rt.capture_ring is not None:
                rt.capture_ring.append(frame)
            if self._acquiring:
                continue
            if self._state is State.WAKE:
                await self._handle_wake_frame(frame, leg=leg_name)
            elif self._state is State.SESSION and self._research_window_active:
                await self._handle_wake_frame(frame, leg=leg_name)
            elif self._state is State.SESSION and rt.shadow_vad is not None:
                await self._shadow_vad_score_raw(frame)

    def _output_admission_refusal(self) -> str | None:
        """The one answer to "may assistant audio be heard right now?".

        Wired into `TtsPlayout.set_emission_admission`, so every emitter is
        asked when its bytes would leave, not when its task started (issue
        #1913). `MeasurementHold.pause` is the only caller that closes
        admission.
        """
        if self._output_gate.admission_paused:
            return "measurement_active"
        return None

    async def _drain_inflight_output(self, *, timeout_sec: float) -> bool:
        """Wait out assistant audio that was already playing when PAUSE
        landed, so its tail cannot enter the window's first capture.

        `MeasurementHold` caps `timeout_sec` at
        `MEASUREMENT_INFLIGHT_DRAIN_SEC`. On timeout the pause and
        cleanup ownership stay armed and the detailed response reports
        ``drained=false`` while preserving ``result=ok`` for old callers.
        Strict callers refuse to capture; the correction caller may keep its
        explicit proceed-anyway policy and still sends RESUME.

        Returns without yielding to the event loop when the gate is idle.
        """
        if not self._output_gate.is_active:
            return True
        active_kind = self._output_gate.active_kind or "unknown"
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_sec):
                drained = await self._output_gate.drain_paused(timeout_sec)
        except TimeoutError:
            drained = False
        waited_ms = int((time.monotonic() - started) * 1000)
        if drained:
            log_event(
                logger,
                "measurement.inflight_drained",
                active_kind=active_kind,
                waited_ms=waited_ms,
            )
            return True
        log_event(
            logger,
            "measurement.inflight_drain_timeout",
            active_kind=self._output_gate.active_kind or active_kind,
            waited_ms=waited_ms,
            bound_sec=timeout_sec,
            detail=(
                "assistant audio still playing; the measurement window is "
                "armed but the caller must not begin a strict capture"
            ),
            level=logging.WARNING,
        )
        return False

    async def _play_mute_click(self, *, going_on: bool) -> None:
        """Best-effort. If the TTS stream isn't open or write fails,
        the visual feedback on the web UI is enough — never raise."""
        episode = await self._output_gate.begin_if_idle("feedback")
        if episode is None:
            log_event(
                logger,
                "mute_click.skipped",
                reason=self._output_admission_refusal() or "output_active",
                active_kind=self._output_gate.active_kind,
            )
            return
        try:
            await self._prepare_feedback_loudness_context(kind="mute_click")
            pcm = (
                self._mute_click_on_pcm
                if going_on else self._mute_click_off_pcm
            )
            profile = (
                self._mute_click_on_profile
                if going_on else self._mute_click_off_profile
            )
            try:
                await self._tts.write_segment(
                    pcm,
                    segment_kind="cue",
                    source_profile=profile,
                    pcm_wide=self._earcon_wide,
                )
            finally:
                await wait_tts_drained_owned(self._tts)
        except Exception as e:  # noqa: BLE001
            logger.warning("mic mute click failed: %s", e)
        finally:
            # A multi-command IPC write can fail after an accepted prefix.
            # The shared cleanup retains this episode over that physical tail.
            await self._finish_output_episode_after_drain(episode)


    async def _prepare_feedback_loudness_context(
        self,
        *,
        kind: str,
        slug: str | None = None,
    ) -> None:
        """Prime fan-in/outputd loudness context for standalone feedback.

        Reactive/proactive cues and mute clicks play outside the turn flow
        that would otherwise prepare this context, so they prime it here:
        content loudness before ducking, or the listening-level-derived
        silence target when the room is quiet. Failure is non-fatal — a cue at
        the wrong loudness beats a silent failure path.
        """
        try:
            await self._prepare_assistant_loudness_context()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
            fields: dict[str, object] = {
                "kind": kind,
                "exc_type": type(e).__name__,
                "err": str(e),
                "level": logging.WARNING,
            }
            if slug:
                fields["slug"] = slug
            log_event(logger, "feedback_loudness.prepare_failed", **fields)


    async def _play_listening_chirp(self, *, going_on: bool) -> None:
        """Best-effort. If the TTS stream isn't ready, the wake or
        end-of-turn happens anyway — never raise. PCM is pre-rendered
        in __init__ to keep this off the wake hot path."""
        try:
            pcm = self._chirp_on_pcm if going_on else self._chirp_off_pcm
            profile = (
                self._chirp_on_profile
                if going_on else self._chirp_off_profile
            )
            await self._tts.write_segment(
                pcm,
                segment_kind="chirp",
                source_profile=profile,
                pcm_wide=self._earcon_wide,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("listening chirp failed: %s", e)

    async def _prepare_assistant_loudness_context(self) -> None:
        provider, model, voice = active_voice_identity(self._cfg)
        tts_envelope = tts_envelope_lufs_for_level(
            self._volume_coordinator.get_listening_level(),
        )
        prepare_kwargs = {
            "provider": provider,
            "model": model,
            "voice": voice,
            "tts_envelope_lufs": tts_envelope,
        }
        # Attach the absolute volume context when the active TTS route
        # interprets it: the pre-DSP fan-in mix (solo/leader) or the confirmed
        # post-DSP outputd mix (a reconciled passive member). The same wire
        # message is sent either way — the post-DSP consumer owns the
        # structural downstream-is-zero fact. Ambiguous/legacy routes stay off.
        route_consumes_context = getattr(
            self._cfg, "duck_transport", ""
        ) == "fanin" and (
            tts_socket_feeds_pre_dsp_fanin(os.environ)
            or tts_socket_feeds_post_dsp_outputd(os.environ)
        )
        context_reader = (
            getattr(self._volume_coordinator, "effective_volume_context", None)
            if route_consumes_context
            else None
        )
        if callable(context_reader):
            try:
                volume_context = await context_reader()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                log_event(
                    logger,
                    "assistant_loudness.context_unavailable",
                    exc_type=type(exc).__name__,
                    detail=str(exc),
                    level=logging.WARNING,
                )
            else:
                prepare_kwargs.update(
                    canonical_volume_db=volume_context.canonical_db,
                    downstream_volume_db=volume_context.downstream_db,
                    context_tts_envelope_lufs=(
                        volume_context.tts_envelope_lufs
                    ),
                    muted=volume_context.muted,
                    context_stamp_boot_ns=volume_context.stamp_boot_ns,
                )
        await self._tts.prepare_assistant_context(
            **prepare_kwargs,
        )

    async def mute_mic(self) -> str:
        """Stop listening: drop mic frames at the wake-loop gate. If a
        voice session is currently active, end the turn first so the
        user gets "stop NOW" semantics rather than the model finishing
        a half-sentence before going silent.

        Idempotent — calling twice is harmless. Always returns "ok".
        """
        if self._mic_muted:
            return "ok"
        if self._state is State.SESSION:
            try:
                await self._end_turn()
            except Exception as e:  # noqa: BLE001
                logger.warning("ending turn on mic mute: %s", e)
        self._mic_muted = True
        # Drop already-buffered room audio, not just future frames. The
        # pre-roll otherwise survives the mute and is replayed into the
        # first turn after unmute (~560 ms of pre-mute room audio sent
        # to the LLM); the telemetry capture rings would likewise write
        # pre-mute audio to disk if a wake fired right after unmute.
        self._pre_roll.clear()
        self._acquire_buffer.clear()
        for _rt in self._legs.values():
            if _rt.capture_ring is not None:
                _rt.capture_ring.clear()
        write_mic_muted(self._cfg.mic_mute_state_path, True)
        log_event(logger, "mic.mute")
        await self._play_mute_click(going_on=False)
        return "ok"

    async def unmute_mic(self) -> str:
        """Resume listening. Idempotent."""
        if not self._mic_muted:
            return "ok"
        self._mic_muted = False
        write_mic_muted(self._cfg.mic_mute_state_path, False)
        log_event(logger, "mic.unmute")
        await self._play_mute_click(going_on=True)
        return "ok"

    def _read_music_dbfs(self) -> float | None:
        """Most-recent playback RMS in dBFS, or None when unavailable.

        Cheap cached read, no async I/O, so it is safe on the wake hot path.
        """
        return self._content_activity.music_dbfs

    def _maybe_refresh_condition(self, now_loop: float) -> None:
        """Refresh `_current_condition` (the acoustic condition the fuser
        keys on) at most once per CONDITION_REFRESH_SEC, so the per-frame
        fire gate works off a ~1 s-fresh condition without paying the
        ring-noise-floor cost every frame."""
        if (now_loop - self._condition_refreshed_at) < CONDITION_REFRESH_SEC:
            return
        # Stamp the timer BEFORE the recompute so a persistent failure retries
        # at ~1 Hz (not every frame). Keep the recompute fail-soft: the wake
        # path must never break because of ancillary condition estimation.
        self._condition_refreshed_at = now_loop
        try:
            self._current_condition = classify_condition(
                music_dbfs=self._read_music_dbfs(),
                noise_floor_dbfs=_ring_noise_floor_dbfs(self._capture_ring_on),
            ).condition
        except Exception:  # noqa: BLE001
            # Keep the last good condition: an unguarded raise here would
            # propagate out of the frame loop and stop wake detection.
            pass

    async def _handle_wake_frame(self, frame, *, leg: str = "on") -> None:
        """Score one frame on the named leg. Legs:
          - 'on'   → post-AEC3 BEST_A (primary, the session audio source)
          - 'off'  → chip-direct raw mic (no AEC)
          - 'dtln' → DTLN-aec output
          - 'chip_aec_150' / 'chip_aec_210' → the XVF3800 hardware-AEC ASR
                     beams (profile-selected and hardware-conditional)

        Always tracks the leg's recent peak. If the threshold is crossed
        AND this leg wins the OR-gate race against the other legs, fires a
        single wake event with ALL legs' recent scores attached.

        Refractory + acquiring checks ensure one user attempt = one
        wake event, regardless of which leg(s) fire first."""
        # Refractory early-out before scoring: the previous wake's TTS may
        # still be bleeding into the mic.
        now_loop = asyncio.get_event_loop().time()
        if now_loop < self._refractory_until:
            return

        # Keep the condition the fuser keys on fresh (~1x/s) so the
        # per-frame gate below works off a live condition.
        self._maybe_refresh_condition(now_loop)

        # Track the raw score regardless of threshold so another leg, when it
        # fires, can pull this leg's most-recent peak into the wake event.
        rt = self._legs.get(leg)
        if rt is None:
            return  # unknown / unconfigured leg
        detector = rt.detector
        score = detector.score_frame(frame)
        rt.recent_score = score
        rt.recent_score_at = now_loop

        firing_threshold = self._fuser.effective_threshold(
            leg, self._current_condition, detector.threshold,
        )
        if score < firing_threshold:
            return

        # Win the OR-gate race against the other legs' loops. The lock covers
        # only the critical section; the rest of the wake flow runs unlocked so
        # both loops stay responsive.
        async with self._wake_fire_lock:
            if asyncio.get_event_loop().time() < self._refractory_until:
                # The other leg won the race while we awaited the
                # lock. Bow out — only one wake event per user attempt.
                return
            # Win. Set refractory IMMEDIATELY so the other leg's next
            # frame backs off cleanly. `_arbitrate_acquire_drain` will
            # extend this in its finally block.
            self._refractory_until = now_loop + WAKE_REFRACTORY_SEC
            # `fired_legs` is which leg(s) crossed threshold at fire time.
            # A non-firing leg counts only if its most-recent score is
            # FRESH (within WAKE_STALE_SCORE_SEC, so a stream that stopped
            # feeding doesn't lie with a stale score) AND above that leg's
            # own threshold. `trigger_kind` records the winner.
            fired_set = {leg}
            for _name, _other in self._legs.items():
                if _name == leg:
                    continue
                if (now_loop - _other.recent_score_at) > WAKE_STALE_SCORE_SEC:
                    continue
                if _other.recent_score >= self._fuser.effective_threshold(
                    _name, self._current_condition, _other.detector.threshold,
                ):
                    fired_set.add(_name)
            fired_legs = ",".join(sorted(fired_set))

        # Reset ALL detectors after a wake fires. openWakeWord's
        # prediction smoothing keeps recent-activation state across
        # calls; without resetting, the post-fire baseline stays
        # elevated and music vocals or TTS-tail bleed can false-fire on
        # the next listening window. Every leg was elevated by the same
        # user utterance, so reset them all.
        for _other in self._legs.values():
            _other.detector.reset()

        # The OR-gate above is RECALL: a leg crossed its threshold and won
        # the race, *proposing* a fire. `verify()` is the PRECISION stage —
        # it corroborates before the turn opens, and fails open. On a
        # suppress the detectors are already reset above (the utterance
        # elevated them either way) and the only refractory held is the
        # short WAKE_REFRACTORY_SEC, so a genuine wake immediately after is
        # not blinded.
        if not self._fuser.verify(leg, fired_set, self._current_condition):
            log_event(
                logger,
                "wake.suppressed",
                leg=leg,
                fired=fired_legs,
                threshold=f"{firing_threshold:.2f}",
            )
            return

        if self._research_window_active:
            self._research_window_cancelled_by_wake = True
            log_event(
                logger,
                "research.confirmation_window_cancelled",
                reason="wake_detected",
                job_id=(
                    self._research_window_job.id
                    if self._research_window_job is not None else ""
                ),
            )
            opening_done = self._research_window_opening_done
            if opening_done is not None:
                try:
                    await asyncio.wait_for(
                        opening_done.wait(),
                        timeout=RESEARCH_CONFIRMATION_OPEN_CANCEL_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "research confirmation window cancellation timed out "
                        "while opening; dropping wake to avoid turn collision"
                    )
                    return
            elif self._state is State.SESSION:
                await self._end_turn("research_window_wake")

        import time as _time
        self._wake_event_at_monotonic = _time.monotonic()
        # Per-leg score summary for the log — ONLY the legs this install
        # actually built, so a single-stream or non-chip-AEC install emits
        # no fields for legs it isn't running. "none" means an ACTIVE leg
        # whose last score is stale (its UDP stream dried up), distinct
        # from an unconfigured leg, which is simply absent.
        _score_fields: dict[str, str] = {}
        for _n, _lr in self._legs.items():
            if _n != leg and (
                _lr.recent_score_at == 0.0
                or (now_loop - _lr.recent_score_at) > WAKE_STALE_SCORE_SEC
            ):
                _score_fields[f"score_{_n}"] = "none"
            else:
                _score_fields[f"score_{_n}"] = f"{_lr.recent_score:.2f}"
        log_event(
            logger,
            "wake.detected",
            leg=leg,
            **_score_fields,
            threshold=f"{firing_threshold:.2f}",
            fired=fired_legs,
        )

        # In peering mode `can_serve` is broadcast in the WAKE message so the
        # fleet's ranking function can prefer a peer that can serve. We bid
        # even when blocked, so exactly one peer plays the failure cue when
        # every peer is blocked; that cue plays below only if we win
        # arbitration and cannot serve.
        spend_allowed = self._spend_cap.allowed()
        conn_paused = self._connection.is_paused()
        can_serve = spend_allowed and not conn_paused

        # Buffer frames into `_acquire_buffer` for the whole arbitration and
        # turn-acquire window; otherwise they dispatch back through
        # `_handle_wake_frame` while peering resolves and either pile up in
        # the asyncio mic queue or re-trigger detection.
        self._acquiring = True
        self._acquire_buffer.clear()

        # Tertiary tiebreaker for the peering ranking function. SNR would rank
        # better but needs rolling-noise-floor state nothing tracks; the
        # ranker falls through to RMS when SNR is missing.
        rms_dbfs = _frame_rms_dbfs(frame)

        # Open a wake-event row for the funnel hooks to update as the event
        # progresses. One SQLite INSERT in WAL mode; failure is logged but
        # never blocks wake response.
        store = self._wake_event_store
        if store is not None:
            event_id = make_event_id()
            self._current_event_id = event_id
            trigger_kind = _LEG_DB[leg]["trigger_kind"]
            # Offset uses the SAME top-of-method `now_loop` (the canonical
            # fire-time), NOT a fresh clock read — recomputing here would
            # fold in the detector.reset() latency and skew the firing
            # leg's offset. Semantics: 0 = leg's last score == fire frame
            # (the firing leg); negative N = that leg last scored N ms
            # before fire.
            wake_fire_time = now_loop
            # Pre-seed every per-leg column to None, derived from _LEG_DB
            # so a new leg's columns are included automatically.
            # begin_event requires peak_score_aec_on/off; configured legs
            # overwrite their own columns below.
            tel: dict[str, object] = {
                col: None
                for _db in _LEG_DB.values()
                for col in (_db["peak_score"], _db["peak_offset"], _db["mic_rms"])
            }
            for _name, _rt in self._legs.items():
                _cols = _LEG_DB[_name]
                tel[_cols["peak_score"]] = (
                    score if _name == leg else _rt.recent_score
                )
                tel[_cols["peak_offset"]] = (
                    int((_rt.recent_score_at - wake_fire_time) * 1000)
                    if _rt.recent_score_at else None
                )
                # Instantaneous mic RMS at fire-time from the last frame
                # in this leg's capture ring — separates low-energy FPs
                # from real attempts in offline review.
                tel[_cols["mic_rms"]] = self._tail_frame_rms_dbfs(_rt.capture_ring)
            # Bridge config snapshot — env-var-driven knobs as seen by the
            # bridge at startup, so post-hoc analysis can ask "what NS
            # level was this event captured under?". Read here rather than
            # from the bridge (a separate process): /etc/jasper/jasper.env
            # is the source of truth, and the bridge is restarted after
            # any change to it.
            bridge_config = {
                "ns_enabled": os.environ.get(NS_ENABLED_ENV, "1"),
                "ns_level": os.environ.get(NS_LEVEL_ENV, "low"),
                "agc1_enabled": os.environ.get(AGC1_ENABLED_ENV, "0"),
                "agc1_target_dbfs": os.environ.get(AGC1_TARGET_DBFS_ENV, "9"),
                "agc1_max_gain_db": os.environ.get(AGC1_MAX_GAIN_DB_ENV, "18"),
                "ref_gain_db": os.environ.get("JASPER_AEC_REF_GAIN_DB", "0"),
                "mic_gain_db": os.environ.get("JASPER_AEC_MIC_GAIN_DB", "0"),
                "ref_hpf_hz": os.environ.get("JASPER_AEC_REF_HPF_HZ", "125"),
                "chip_hpf_hz": os.environ.get("JASPER_AEC_CHIP_HPF_HZ", "125"),
            }
            # Music context — best-effort from ContentActivityTracker's
            # cached playback RMS, which is maintained without async I/O
            # so reading it on the wake hot path is free (a renderer probe
            # would add ~50 ms) and is accurate to within ~1 s. Proxy:
            # louder than -60 dBFS = "music probably playing" — imperfect
            # (TTS uses the same playback chain) but useful for FP
            # correlation.
            music_volume_db = self._read_music_dbfs()
            music_active_proxy = (
                music_volume_db is not None and music_volume_db > -60.0
            )
            # Acoustic condition: music from the playback anchor above;
            # quiet-vs-ambient from the pre-fire mic noise floor over the
            # capture ring. Recorded so production fires carry the same
            # taxonomy the corpus labels use. Best-effort — both
            # _ring_noise_floor_dbfs and classify_condition never raise.
            condition_ctx = classify_condition(
                music_dbfs=music_volume_db,
                noise_floor_dbfs=_ring_noise_floor_dbfs(self._capture_ring_on),
            )
            self._current_condition = condition_ctx.condition
            try:
                await store.begin_event(
                    event_id=event_id,
                    trigger_kind=trigger_kind,
                    threshold=firing_threshold,
                    wake_model=self._cfg.wake_model,
                    voice_provider=getattr(self._cfg, "voice_provider", None),
                    bridge_config=bridge_config,
                    music_active=music_active_proxy,
                    music_volume_db=music_volume_db,
                    condition_class=condition_ctx.condition,
                    mic_muted=self._mic_muted,
                    fired_legs=fired_legs,
                    **tel,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "wake_events: begin_event failed (will skip telemetry "
                    "for this event): %s", e,
                )
                self._current_event_id = None
            # Deliberately not in `_bg_tasks`: those tasks drive turn
            # completion.
            if self._current_event_id is not None:
                self._create_fire_and_forget_task(
                    self._finalize_event_audio(self._current_event_id),
                    name="wake-event-audio-finalize",
                )

        # Background task so the main mic loop stays responsive while
        # frames pile into _acquire_buffer (up to 20 s — see
        # ACQUIRE_BUFFER_MAX_FRAMES).
        self._create_fire_and_forget_task(
            self._arbitrate_acquire_drain(
                score=score,
                rms_dbfs=rms_dbfs,
                spend_allowed=spend_allowed,
                conn_paused=conn_paused,
                can_serve=can_serve,
            ),
            name="wake-arbitrate-acquire-drain",
        )

    async def _finalize_event_audio(self, event_id: str) -> None:
        """Wait the post-fire collection window, then snapshot each configured
        capture ring and persist WAV files via the store.

        Fire-and-forget: failure logs WARN and does not propagate. Truncation
        on daemon shutdown is acceptable — the row keeps its NULL
        audio_*_path, which queries can filter out."""
        if self._wake_event_store is None:
            return
        try:
            await asyncio.sleep(CAPTURE_POST_SEC)
            # Snapshot count = pre + post window in frames. Rings may hold
            # slightly more than this thanks to the slack in the maxlen
            # sizing.
            from .audio_io import MicCapture as _MC
            n_frames = int(
                (CAPTURE_PRE_SEC + CAPTURE_POST_SEC)
                * _MC.OUTPUT_RATE / _MC.OUTPUT_FRAME_SAMPLES
            )
            await self._wake_event_store.attach_audio(
                event_id=event_id,
                audio_on=self._snapshot_leg_audio("on", n_frames),
                audio_off=self._snapshot_leg_audio("off", n_frames),
                audio_dtln=self._snapshot_leg_audio("dtln", n_frames),
                audio_chip_aec_150=self._snapshot_leg_audio(
                    "chip_aec_150", n_frames,
                ),
                audio_chip_aec_210=self._snapshot_leg_audio(
                    "chip_aec_210", n_frames,
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: attach_audio failed for %s: %s", event_id, e,
            )

    def _snapshot_leg_audio(self, leg: str, n_frames: int) -> bytes | None:
        """Snapshot the trailing wake-event window for one configured leg."""
        runtime = self._legs.get(leg)
        if runtime is None:
            return None
        return self._snapshot_ring(runtime.capture_ring, n_frames)

    @staticmethod
    def _snapshot_ring(ring: deque, n_frames: int) -> bytes | None:
        """Concatenate the last `n_frames` of the ring, or None when it is
        empty (e.g. the AEC OFF leg in single-stream mode)."""
        if not ring:
            return None
        # Fewer than n_frames early in startup, before the ring fills.
        take = min(len(ring), n_frames)
        frames = list(ring)[-take:]
        # Each frame is a numpy int16 array.
        return b"".join(f.tobytes() for f in frames)

    @staticmethod
    def _tail_frame_rms_dbfs(ring: "deque | None") -> float | None:
        """RMS in dBFS of the most-recent frame in `ring`, or None when the
        ring is empty or missing."""
        if ring is None or not ring:
            return None
        return _frame_rms_dbfs(ring[-1])

    async def _telemetry_stage(
        self,
        stage: str,
        *,
        tool_name: str | None = None,
    ) -> None:
        """Best-effort funnel-stage update for the in-flight wake event.

        No-op when telemetry is disabled, no event is in flight, or the store
        write fails: the wake and session paths are never blocked by
        telemetry trouble."""
        store = self._wake_event_store
        event_id = self._current_event_id
        if store is None or event_id is None:
            return
        try:
            await store.update_stage(
                event_id,
                stage,
                tool_name=tool_name,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: update_stage(%s) failed: %s", stage, e,
            )

    async def record_tool_dispatch_stage(self, stage: str, name: str) -> None:
        """Translate the shared dispatch observer into wake-funnel stages.

        ``dispatch_tool`` is the only producer, so this observes Gemini,
        OpenAI, and Grok without provider branches. Manual / research turns
        naturally no-op because they have no in-flight wake event id.
        """
        funnel_stage = {
            "called": "tool_called",
            "completed": "tool_completed",
        }.get(stage)
        if funnel_stage is None:
            raise ValueError(f"unknown tool dispatch stage {stage!r}")
        await self._telemetry_stage(
            funnel_stage,
            tool_name=name,
        )

    async def _record_response_started(self) -> None:
        """Record the first provider-neutral assistant-audio boundary."""
        await self._telemetry_stage("response_started")

    async def _telemetry_outcome(
        self, outcome: str, detail: str | None = None,
    ) -> None:
        """Best-effort terminal-outcome UPDATE for the in-flight wake
        event. Same fail-soft pattern as `_telemetry_stage`. Clears
        `_current_event_id` after the write so subsequent funnel hooks
        for the next wake start clean."""
        store = self._wake_event_store
        event_id = self._current_event_id
        if store is None or event_id is None:
            # Still clear the id (if it exists) so the next wake
            # starts from a clean state.
            self._current_event_id = None
            return
        # Clear early so subsequent stray funnel-hook calls don't keep
        # writing against a finalised row.
        self._current_event_id = None
        try:
            await store.set_outcome(event_id, outcome, detail)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: set_outcome(%s) failed for %s: %s",
                outcome, event_id, e,
            )

    async def _arbitrate_acquire_drain(
        self,
        *,
        score: float,
        rms_dbfs: float | None,
        spend_allowed: bool,
        conn_paused: bool,
        can_serve: bool,
    ) -> None:
        """Background coroutine spawned on wake.

        Late-cancel gates abort cleanly: both stop mic frames in the main
        loop, so the session would open with no audio, and the user just did
        something that said "stop listening". Peer arbitration (a no-op when
        peering is off) then asks jasper-control over UDS whether this Pi
        takes the turn; losers back off silently. Gate cues for a reached
        spend cap or a paused connection are played by the arbitration winner
        only, so N peers do not fire N cues.

        On error the failure cue is honest about cause: a connection cue only
        when the live connection is genuinely paused, otherwise
        `internal_error`, since an unexpected throw here is almost always
        local rather than connectivity.
        """
        try:
            # mute_mic / MeasurementHold.pause can fire after
            # _handle_wake_frame spawned this task but before it is scheduled.
            # Both are user-deliberate "stop listening" signals; a chirp plus
            # an LLM session after them is wrong. Checked twice — now, and
            # again after the arbitration await, which can take up to 500 ms.
            if self._wake_late_cancelled("pre_arb"):
                await self._telemetry_stage("late_cancel")
                await self._telemetry_outcome("late_cancel", "pre_arb")
                return  # finally clears _acquiring + buffer

            decision = await self._peer_arbitrate(
                score=score, snr_db=None, rms_dbfs=rms_dbfs,
                can_serve=can_serve,
            )
            if decision == "LOSE":
                # Another peer is handling it: losers play no chirp or cue.
                log_event(logger, "peering.wake.lost", score=f"{score:.2f}")
                await self._telemetry_stage("peer_lost")
                await self._telemetry_outcome("peer_lost")
                return  # finally clears _acquiring + buffer

            if self._wake_late_cancelled("post_arb"):
                await self._telemetry_stage("late_cancel")
                await self._telemetry_outcome("late_cancel", "post_arb")
                return

            # Gate cues: only the arbitration winner pays this cost.
            if not spend_allowed:
                logger.warning("daily spend cap reached; voice disabled until rollover")
                await self._telemetry_stage("gate_blocked")
                await self._telemetry_outcome("gate_blocked", "spend_cap_reached")
                await self._play_cue("spend_cap_reached")
                return
            # `conn_paused` was snapshotted before arbitration. Re-check
            # with a bounded wait: a planned session rotation is a pause
            # that clears on its own, and answering a wake with a
            # "can't connect" cue during one would be a false alarm.
            if conn_paused and not await self._await_connection(
                PAUSED_CONNECTION_WAIT_SEC,
            ):
                logger.warning(
                    "wake detected but live connection is paused (reconnect/backoff); "
                    "ignoring this wake event",
                )
                await self._telemetry_stage("gate_blocked")
                await self._telemetry_outcome("gate_blocked", "connection_paused")
                # The cue comes first: it is the household's answer, and
                # nothing after it may be allowed to swallow it. The
                # early-retry nudge already went out with the wait.
                await self._play_cue(self._connection.wake_cue())
                return

            await self._begin_turn(
                listening_feedback=True,
            )  # ends with state = SESSION
            await self._telemetry_stage("turn_opened")
            # Starts the winner-only heartbeat. Fire-and-forget: voice's own
            # session lifecycle is the source of truth.
            await self._notify_peering_session_started()

            try:
                drained, speech_in_acquire = await self._drain_acquire_audio()
            except Exception as e:  # noqa: BLE001
                drained = 0
                speech_in_acquire = False
                logger.warning("acquire-buffer drain failed: %s", e)
            if drained:
                logger.info(
                    "acquire-buffer drained: %d frames (~%.0fms%s)",
                    drained, drained * 80.0,
                    "; contained speech — silence detector pre-armed"
                    if speech_in_acquire else "",
                )
            # Fast-talker compensation; see `_drain_acquire_audio`.
            if speech_in_acquire and not self._user_speech_seen:
                self._user_speech_seen = True
                await self._telemetry_stage("speech_detected")
        except Exception as e:  # noqa: BLE001
            logger.exception("turn acquire failed: %s", e)
            await self._telemetry_outcome("session_failed", str(e)[:200])
            # A connection cue here is a false alarm unless the connection
            # actually dropped mid-acquire; see the internal_error CueDef.
            try:
                if self._turn_output_episode is not None:
                    await self._cleanup_after_failed_begin()
            except Exception as cleanup_error:  # noqa: BLE001
                logger.warning(
                    "turn acquire cleanup failed before failure cue: %s",
                    cleanup_error,
                )
            if self._connection.is_paused():
                await self._play_cue(self._connection.wake_cue())
            else:
                await self._play_cue(INTERNAL_ERROR_CUE_SLUG)
            self._acquire_buffer.clear()
        finally:
            # Flip the flag last: the main loop reads it per mic frame to
            # choose between buffering and dispatch. With state already
            # SESSION and the buffer drained, clearing it hands the live
            # stream to `_handle_session_frame`; on the LOSE, cue and error
            # paths state is still WAKE, so it returns to wake detection.
            self._acquiring = False
            # Protects against the detector re-firing on the TTS tail (won
            # path) or on a quick repeat-wake (lost path).
            self._refractory_until = max(
                self._refractory_until,
                asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC,
            )

    def _wake_late_cancelled(self, phase: str) -> bool:
        """Whether a user-deliberate "stop listening" gate fired since wake.

        True, with an `event=wake.late_cancel` log, when the mic is muted or a
        room-correction measurement window is open. `phase` is "pre_arb" or
        "post_arb", so the log says which side of the peering arbitration
        await caught it.

        `manual_session_start` bypasses wake detection and so checks the same
        two gates itself."""
        if self._mic_muted:
            log_event(
                logger,
                "wake.late_cancel",
                reason="mic_muted",
                phase=phase,
            )
            return True
        if self._measurement_active.is_set():
            log_event(
                logger,
                "wake.late_cancel",
                reason="measurement_active",
                phase=phase,
            )
            return True
        return False

    async def _peering_send(
        self, cmd: str, *, timeout: float = 0.5,
    ) -> dict | None:
        """Send one command to jasper-control's peering UDS.

        Returns the parsed JSON response, or None if peering is
        disabled / the daemon is unreachable / any error occurs.

        This is the only place that touches the peering UDS — every
        caller is fail-open by construction (no exception escapes,
        no peering issue can silence the speaker). Callers
        differentiate "WIN-by-default" semantics by treating None
        as the no-op response."""
        if not self._cfg.peering_enabled:
            return None
        try:
            from .peering.uds import send_request
        except ImportError:
            # peering package not installed — keep wake working.
            return None
        try:
            return await send_request(
                self._cfg.peering_uds_socket, cmd, timeout=timeout,
            )
        except FileNotFoundError:
            # Peering daemon isn't running (mode=on in voice config
            # but mode=off / failed in jasper-control). Fall back to
            # solo behavior silently — this isn't an error condition.
            return None
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("peering %s failed: %s; treating as solo",
                           cmd.split(maxsplit=1)[0], e)
            return None
        except Exception:  # noqa: BLE001
            logger.exception(
                "peering %s raised; treating as solo",
                cmd.split(maxsplit=1)[0],
            )
            return None

    async def _peer_arbitrate(
        self,
        *,
        score: float,
        snr_db: float | None,
        rms_dbfs: float | None,
        can_serve: bool,
    ) -> str:
        """Ask jasper-control's peering daemon whether this Pi should
        take the turn. Returns "WIN" or "LOSE".

        Side effect: sets `self._peering_current_epoch` from the
        daemon's response so `_notify_peering_session_*` can reference
        the same arbitration round.

        Fast-path: when peering is disabled OR no peering daemon is
        running OR the UDS errors, returns "WIN" immediately, and
        `_peering_send` short-circuits before any I/O.
        """
        self._peering_current_epoch = ""
        import json as _json  # noqa: PLC0415
        payload = _json.dumps({
            "score": float(score),
            "snr_db": snr_db,
            "rms_dbfs": rms_dbfs,
            "can_serve": bool(can_serve),
        })
        resp = await self._peering_send(f"ARBITRATE {payload}")
        if resp is None:
            return "WIN"  # peering disabled or daemon unreachable
        self._peering_current_epoch = str(resp.get("epoch") or "")
        result = (resp.get("result") or "").upper()
        if result not in ("WIN", "LOSE"):
            logger.warning(
                "peer arbitrate returned %r; defaulting to WIN", result,
            )
            return "WIN"
        return result

    async def _notify_peering_session_started(self) -> None:
        """Fire-and-forget notice that this speaker opened a session.

        The peering daemon transitions WINNER → ACTIVE and broadcasts
        heartbeats so peers stay suppressed for the session. No-op when
        peering is disabled; errors are swallowed so voice keeps going.
        """
        if self._turn is None:
            return  # no active turn to announce
        await self._peering_send(
            f"SESSION_STARTED {self._peering_current_epoch}",
        )

    async def _notify_peering_session_ended(self, reason: str) -> None:
        """Fire-and-forget notice. Mirrors _notify_peering_session_started."""
        await self._peering_send(
            f"SESSION_ENDED {self._peering_current_epoch} {reason}",
        )

    def _resolve_barge_in_for_turn(self) -> None:
        """Decide whether in-session barge-in is active for the turn about
        to open, and reset its per-turn run state.

        Reads the per-provider enable flag from the SSOT file (not the
        start-time ``Config``) so a wizard / operator toggle takes effect
        without a daemon restart — jasper-voice is restarted on a *provider*
        switch but not on a barge-in toggle. The read is mtime-gated
        (``read_barge_in_enabled``), so the steady-state per-turn cost is a
        single ``os.stat``, not a full open+read+parse. DEFAULT OFF.

        Self-interrupt-loop guard: when barge-in is requested but the
        primary mic leg has no AEC reference (the ``direct_mic`` profile),
        hard-disable it for the turn and WARN once per daemon, rather than
        let un-cancelled TTS bleed self-trip the gate every turn.

        Push-to-talk turns refuse it loudly for a related reason: the frames
        ``_handle_playback_frame`` would score come from the accessory's mic,
        while ``_barge_in_reference_available`` was computed from
        ``cfg.mic_device`` — a different stream — so the self-interrupt guard
        has not cleared the audio barge-in would run on."""
        self._barge_in_run_started_at = 0.0
        self._barge_in_run_peak = 0.0
        self._barge_in_signalled_this_run = False
        want = read_barge_in_enabled(self._cfg.voice_provider)
        if want and self._manual_endpoint_this_turn:
            # Its own latch, not `_barge_in_no_ref_warned`: on a speaker with
            # both a room mic and a remote, sharing one would let a
            # push-to-talk turn swallow the different no-reference warning a
            # later wake turn owes the operator.
            if not self._barge_in_ptt_warned:
                self._barge_in_ptt_warned = True
                log_event(
                    logger,
                    "barge.disabled_push_to_talk",
                    provider=self._cfg.voice_provider,
                    source=self._active_manual_source or "primary",
                    detail=(
                        "barge-in scores the primary mic leg, which a "
                        "push-to-talk turn does not use"
                    ),
                    level=logging.WARNING,
                )
            want = False
        if want and not self._barge_in_reference_available:
            if not self._barge_in_no_ref_warned:
                self._barge_in_no_ref_warned = True
                log_event(
                    logger,
                    "barge.disabled_no_reference",
                    provider=self._cfg.voice_provider,
                    mic_device=self._cfg.mic_device,
                    level=logging.WARNING,
                )
            want = False
        self._barge_in_active = want

    async def _handle_playback_frame(self, frame) -> None:
        """In-session barge-in detection while the assistant is speaking.

        Reached from ``_handle_session_frame`` once ``_input_ended`` is set
        AND barge-in is active for the turn. Runs local Silero VAD on the
        AEC-cleaned "on" leg — the same ``frame`` the live session consumed
        (leg selection, NOT an AEC topology change) — and, on a sustained
        speech run at or above ``JASPER_VAD_BARGE_IN_THRESHOLD``, sets the
        turn's interrupt event so ``_play_responses`` flushes local TTS
        immediately. The felt experience: the user talks over the assistant
        and the speaker goes quiet.

        Detection and local flush only: this does NOT truncate / cancel
        the provider response, so a real-time provider may resume after
        the flush.

        Runs INLINE (never a ``_bg_task``): completed ``_bg_tasks`` end the
        turn, so a fire-once detector task would race turn-end."""
        if self._turn is None:
            return
        # Same primary-leg Silero the in-session EOU detector scores; a
        # predict error propagates exactly as it does there (unguarded)
        # rather than being silently swallowed here.
        speech_prob = self._vad.predict(frame)
        now = asyncio.get_event_loop().time()
        if speech_prob < self._cfg.vad_barge_in_threshold:
            # Sub-threshold frame breaks the run. A fresh continuous run
            # must re-accumulate from zero (and may re-trigger), mirroring
            # the wake-tail arming reset.
            self._barge_in_run_started_at = 0.0
            self._barge_in_run_peak = 0.0
            self._barge_in_signalled_this_run = False
            return
        if self._barge_in_run_started_at == 0.0:
            self._barge_in_run_started_at = now
            self._barge_in_run_peak = speech_prob
        else:
            self._barge_in_run_peak = max(self._barge_in_run_peak, speech_prob)
        if self._barge_in_signalled_this_run:
            return
        sustained = now - self._barge_in_run_started_at
        if sustained < BARGE_IN_SUSTAINED_SPEECH_SEC:
            return
        self._barge_in_signalled_this_run = True
        self._barge_in_count += 1
        self._barge_in_last_leg = "on"
        self._barge_in_last_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        )
        log_event(
            logger,
            "barge.detected",
            leg="on",
            silero=f"{self._barge_in_run_peak:.2f}",
            sustained_ms=int(sustained * 1000),
            # Durable (needs_client_truncate) vs cosmetic (server_self_truncates,
            # where a real-time provider may resume) — see _barge_in_reconcile.
            reconcile=self._barge_in_reconcile.value,
        )
        # Set the turn's interrupt event (provider-agnostic; getattr so an
        # adapter without the capability degrades to no local flush rather
        # than crashing). _play_responses is awaiting wait_for_interrupt.
        trigger = getattr(self._turn, "request_local_interrupt", None)
        if callable(trigger):
            trigger()

    async def _send_session_audio(self, frame) -> None:
        """Forward one frame to the live turn; end the turn if it refuses.

        One implementation for all three endpointer paths (server VAD,
        local Silero, push-to-talk), so the failure handling cannot
        drift between them.
        """
        try:
            await self._turn.send_audio(frame.tobytes())
        except Exception as e:  # noqa: BLE001
            logger.warning("send_audio failed (will end turn): %s", e)
            await self._end_turn()

    async def _end_session_input(self, where: str) -> None:
        """Close the user's input side: mark it ended and tell the turn.

        ``where`` names the caller in the failure log so a stuck
        ``end_input`` is attributable to end-of-utterance, the hard cap,
        or the push-to-talk cap without a stack trace.
        """
        self._input_ended = True
        try:
            await self._turn.end_input()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "end_input failed at %s (will end turn): %s", where, e,
            )
            await self._end_turn()

    def _endpointer_label(self) -> str:
        """Which mechanism closes the current turn's user input.

        One vocabulary, two readers: ``/state.voice.endpointer`` (live)
        and the wake-events ``endpointer`` column (at turn end). Kept in
        one place so a new endpointer can't be named two things.

        ``push_to_talk`` is only observable on the ``/state`` side today.
        The corpus row is created by ``begin_event`` on the wake path, and
        a button turn never takes that path, so it has no row to label —
        see ``_corpus_endpointer_label``.
        """
        if self._manual_endpoint_this_turn:
            return "push_to_talk"
        if self._server_vad_this_turn:
            return "server_vad"
        return "silero_aec"

    def _corpus_endpointer_label(self, *, user_speech_seen: bool) -> str:
        """The wake-events ``endpointer`` value for the finished turn.

        Same vocabulary as ``_endpointer_label`` plus ``no_speech_abort``,
        which is a verdict about *listening* and so only meaningful when
        something was listening for speech. Keyed on the resolved label
        rather than on ``not _server_vad_this_turn`` so that if button
        turns ever gain corpus rows, one cannot be recorded as a
        no-speech abort it never performed.
        """
        label = self._endpointer_label()
        if label == "silero_aec" and not user_speech_seen:
            return "no_speech_abort"
        return label

    def _ptt_input_cap_sec(self) -> float:
        """How long a held button may hold the user's input open.

        Not simply ``HARD_RECORDING_CAP_SEC``. ``_idle_watchdog``'s
        pre-response timer is anchored at turn open and fires at
        ``JASPER_IDLE_TIMEOUT_SEC`` (default 20 s) when no model chunk has
        arrived — and none can while input is still open, because
        ``last_activity_at()`` tracks *model* activity and stays at the
        turn-start value. So the 30 s cap is unreachable at the shipped
        default: the watchdog wins by ~10 s, and because ``_end_turn`` cancels
        ``_play_responses`` before it calls ``end_input``, the user gets no
        answer at all rather than a truncated one.

        Deriving the cap from the same ``idle_timeout_sec`` the watchdog uses
        keeps the two in step when an operator retunes either, and aims to
        leave the model ``PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC`` to start
        speaking after the cap closes input.

        ``PTT_MIN_INPUT_CAP_SEC`` can defeat that aim, because the floor is a
        constant while the watchdog is not: a low enough ``idle_timeout_sec``
        walks the watchdog down through the floor (see that constant's
        comment). Two degraded bands result, each warned once per daemon:

        * ``cap < idle_timeout < cap + allowance`` — the cap still fires
          first but leaves the model less than the allowance, so a slow first
          chunk loses the answer. ``event=manual_mic.idle_timeout_too_low``.
        * ``idle_timeout <= cap`` — the watchdog fires first and the cap is
          unreachable, so every long hold loses its answer whatever the chunk
          speed. ``event=manual_mic.hold_cap_unreachable``.

        The floor stands in both — a usable button beats one that closes
        mid-sentence — because the remedy is the operator's timeout, which
        both events name in ``needs_sec``.

        ``idle_timeout_sec`` is guaranteed > 0 (``Config._validate`` rejects
        anything else at daemon start), so there is no "watchdog disabled"
        case to reason about.
        """
        headroom = (
            self._cfg.idle_timeout_sec
            - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
        )
        cap = min(HARD_RECORDING_CAP_SEC, max(PTT_MIN_INPUT_CAP_SEC, headroom))
        needs = cap + PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
        # One latch for both: the two bands are mutually exclusive by
        # construction, and `idle_timeout_sec` is fixed for the daemon's
        # life, so at most one can ever apply.
        if not self._ptt_cap_warned:
            common = {
                "cap_sec": f"{cap:.1f}",
                "idle_timeout_sec": f"{float(self._cfg.idle_timeout_sec):.1f}",
                "needs_sec": f"{needs:.1f}",
            }
            if cap >= self._cfg.idle_timeout_sec:
                self._ptt_cap_warned = True
                log_event(
                    logger,
                    "manual_mic.hold_cap_unreachable",
                    **common,
                    detail=(
                        "the idle watchdog reaps a held button BEFORE the "
                        "hold cap can close input, so every long hold ends "
                        "with no answer at all; raise "
                        "JASPER_IDLE_TIMEOUT_SEC to at least needs_sec"
                    ),
                    level=logging.WARNING,
                )
            elif needs > self._cfg.idle_timeout_sec:
                self._ptt_cap_warned = True
                log_event(
                    logger,
                    "manual_mic.idle_timeout_too_low",
                    **common,
                    detail=(
                        "raise JASPER_IDLE_TIMEOUT_SEC to at least "
                        "needs_sec; a long hold may end with no answer"
                    ),
                    level=logging.WARNING,
                )
        return cap

    async def _handle_manual_session_frame(self, frame) -> None:
        """Forward one push-to-talk frame. The button owns end-of-input.

        Reached from ``_handle_session_frame`` when
        ``_manual_endpoint_this_turn`` is set. Deliberately does NOT run
        local Silero:

        * ``END_OF_UTTERANCE_SILENCE_SEC`` would call ``end_input()``
          after 0.8 s of detected silence — while the user is still
          holding the button. Release already does exactly that, so
          Silero here is a second writer of "input is over" and the
          faster of the two wins.
        * ``NO_SPEECH_ABORT_SEC`` would end the turn outright 5 s into a
          held button. A user who presses and then gathers their thought
          is not a false wake; there was no wake to be false.

        What replaces them is ``_ptt_input_cap_sec``, and something must:
        a button held but never released (wedged under a cushion, a
        release event the accessory never sends) would otherwise hold the
        duck, the LLM session, and the mic open until ``_idle_watchdog``
        reaps the turn — and that teardown cancels ``_play_responses``
        before asking the model anything, so the user hears nothing.
        Closing input at the cap turns that into an answer to what was
        said so far.

        The cap only covers a button whose frames keep arriving: this method
        is its sole evaluator and runs per frame, so if the frames stop
        instead — BLE drop mid-hold, adapter killed — the cap never runs. The
        source's ``frames()`` is an untimed queue read
        (``UdpMicCapture.frames``) and the primary mic loop does not feed a
        button turn, so ``_idle_watchdog`` reaps it; ``_end_turn`` still
        finalises it through the ``_manual_endpoint_this_turn`` term in its
        ``end_input()`` gate, and the operator gets the ``HOLD TIMEOUT`` line
        rather than the wake-turn text.
        """
        now = asyncio.get_event_loop().time()
        elapsed = now - self._turn_started_at_loop
        cap = self._ptt_input_cap_sec()
        if elapsed >= cap:
            # `_input_ended` gates re-entry: once set, subsequent
            # held-button frames are dropped by `_handle_session_frame`'s
            # input-closed branch and never reach here, so this fires at
            # most once per turn.
            log_event(
                logger,
                "manual_mic.hold_cap",
                source=self._active_manual_source or "primary",
                cap_sec=f"{cap:.1f}",
                idle_timeout_sec=f"{float(self._cfg.idle_timeout_sec):.1f}",
                level=logging.WARNING,
            )
            await self._end_session_input("push-to-talk hold cap")
            return

        await self._send_session_audio(frame)

    async def _handle_session_frame(self, frame) -> None:
        # If any background task ended, the turn is over. Cleanup, then
        # this frame is silently consumed (no double-dispatch into detector).
        if any(t.done() for t in self._bg_tasks):
            await self._end_turn()
            return

        assert self._turn is not None

        if self._input_ended:
            # Input closed: the assistant is (or is about to be) speaking.
            # With barge-in active, score this frame for an interruption;
            # otherwise drop it (mic ignored during playback).
            if self._barge_in_active:
                await self._handle_playback_frame(frame)
            return

        # ---- Push-to-talk branch ----
        # The button already carries both turn boundaries, so nothing
        # here may close the user's input early. Must come BEFORE the
        # server-VAD and Silero branches: both of those end input on
        # their own schedule.
        if self._manual_endpoint_this_turn:
            await self._handle_manual_session_frame(frame)
            return

        # ---- Server-side VAD branch ----
        # When server_vad is active, the server owns end-of-utterance
        # detection. Skip local Silero for turn-control decisions; just
        # forward audio and watch for the server's committed event.
        if self._server_vad_this_turn:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._turn_started_at_loop

            # Shadow telemetry: run Silero on the primary stream so
            # wake_events.max_silero_aec is populated on server-VAD turns
            # too, keeping local-VAD permissiveness comparable across
            # stream configs (AEC vs raw+AGC). Does NOT affect turn
            # behavior.
            try:
                shadow_prob = self._vad.predict(frame)
                if shadow_prob > self._max_silero_score_in_turn:
                    self._max_silero_score_in_turn = shadow_prob
            except Exception:  # noqa: BLE001
                pass

            ss_fn = getattr(self._turn, "server_speech_started", None)
            server_heard_speech = bool(ss_fn()) if callable(ss_fn) else False
            if server_heard_speech and not self._user_speech_seen:
                self._user_speech_seen = True
                await self._telemetry_stage("speech_detected")

            if not server_heard_speech and not self._user_speech_seen \
                    and elapsed >= NO_SPEECH_ABORT_SEC:
                log_event(
                    logger,
                    "server_vad.no_speech",
                    timeout_sec=f"{NO_SPEECH_ABORT_SEC:.1f}",
                )
                await self._end_turn()
                return

            if elapsed >= HARD_RECORDING_CAP_SEC:
                log_event(
                    logger,
                    "server_vad.hard_cap",
                    elapsed_sec=f"{HARD_RECORDING_CAP_SEC:.1f}",
                )
                self._input_ended = True
                await self._end_turn()
                return

            eou_check = getattr(self._turn, "server_speech_detected", None)
            if eou_check is not None and callable(eou_check) and eou_check():
                self._input_ended = True

            await self._send_session_audio(frame)
            return

        # ---- Local Silero VAD path (manual VAD) ----
        # End-of-utterance detection: run Silero VAD on the frame and arm
        # the silence detector once the user has been speaking
        # continuously for SUSTAINED_SPEECH_TO_ARM_SEC AND the run peaked
        # at SPEECH_RUN_PEAK_MIN. A real spoken command — even one
        # delivered immediately after the wake word with no pause —
        # clears both within ~200 ms; wake-word tail clears the duration
        # bar but not the peak. See those two constants.
        speech_prob = self._vad.predict(frame)
        if speech_prob > self._max_silero_score_in_turn:
            self._max_silero_score_in_turn = speech_prob
        now = asyncio.get_event_loop().time()
        elapsed = now - self._turn_started_at_loop

        # Bail out fast if no real speech has been detected within the
        # abort window. Avoids the "ducked the music for 10 s and then
        # nothing happened" UX when the wake word fires but the user
        # doesn't follow up with a question (or speaks too quietly).
        # Logging the max silero score helps disambiguate "wake fired
        # but user really didn't speak" (max ~0) from "user did speak
        # but score never crossed threshold" (max close to threshold).
        if not self._user_speech_seen and elapsed >= NO_SPEECH_ABORT_SEC:
            logger.info(
                "no user speech detected within %.1fs (silero max=%.2f, threshold=%.2f); aborting turn",
                NO_SPEECH_ABORT_SEC,
                self._max_silero_score_in_turn,
                END_OF_UTTERANCE_SPEECH_THRESHOLD,
            )
            await self._end_turn()
            return

        # Hard recording cap: defends against stuck-on TVs / continuous
        # noise / runaway dictation by force-ending the turn after a
        # generous window. Sends activity_end so the server can finalise
        # whatever audio it has, then ends the turn locally.
        if elapsed >= HARD_RECORDING_CAP_SEC and not self._input_ended:
            logger.info(
                "hard recording cap reached (%.1fs); ending input",
                HARD_RECORDING_CAP_SEC,
            )
            await self._end_session_input("cap")
            return

        if speech_prob >= END_OF_UTTERANCE_SPEECH_THRESHOLD:
            if self._speech_run_started_at == 0.0:
                self._speech_run_started_at = now
                self._speech_run_max_silero = speech_prob
            else:
                self._speech_run_max_silero = max(
                    self._speech_run_max_silero, speech_prob,
                )
            sustained = now - self._speech_run_started_at
            if (not self._user_speech_seen
                    and sustained >= SUSTAINED_SPEECH_TO_ARM_SEC
                    and self._speech_run_max_silero >= SPEECH_RUN_PEAK_MIN):
                logger.info(
                    "user speech detected (sustained=%.0fms, "
                    "silero=%.2f, peak_in_run=%.2f) "
                    "— silence detector armed",
                    sustained * 1000, speech_prob,
                    self._speech_run_max_silero,
                )
                self._user_speech_seen = True
                if self._silero_aec_armed_at_ms is None:
                    self._silero_aec_armed_at_ms = int(
                        (now - self._turn_started_at_loop) * 1000
                    )
                await self._telemetry_stage("speech_detected")
            self._silence_started_at = 0.0
        else:
            # Sub-threshold frame breaks the run. Both the duration
            # anchor and the peak-tracker reset together so the next
            # run starts fresh — partial accumulation across silence
            # gaps would defeat the wake-tail-rejection design.
            self._speech_run_started_at = 0.0
            self._speech_run_max_silero = 0.0
            if self._user_speech_seen:
                if self._silence_started_at == 0.0:
                    self._silence_started_at = now
                elif now - self._silence_started_at >= END_OF_UTTERANCE_SILENCE_SEC:
                    silence_ms = (now - self._silence_started_at) * 1000
                    logger.info(
                        "end-of-utterance: %.0fms user silence; sending activity_end",
                        silence_ms,
                    )
                    await self._end_session_input("end-of-utterance")
                    return

        await self._send_session_audio(frame)

    async def _drain_acquire_audio(self) -> tuple[int, bool]:
        """Forward buffered wake/acquire frames into the newly opened turn.

        The VAD pass exists for one purpose: pre-arm ``_user_speech_seen``
        so the live end-of-utterance detector doesn't abort a fast-talker
        turn whose whole question landed in the acquire window. A
        push-to-talk turn runs neither the detector nor the abort, so
        scoring those frames would buy nothing and cost a Silero pass per
        frame on the exact class of box (Pi Zero 2 W) that can least
        afford it. ``drain_acquire_buffer`` already contracts
        ``vad_predict=None`` -> ``sustained_speech_detected=False``.
        """
        vad_predict = (
            None
            if self._manual_endpoint_this_turn
            else self._vad.predict
        )
        return await drain_acquire_buffer(
            self._acquire_buffer,
            self._turn,  # type: ignore[arg-type]
            vad_predict=vad_predict,
            speech_threshold=END_OF_UTTERANCE_SPEECH_THRESHOLD,
            peak_min=SPEECH_RUN_PEAK_MIN,
        )

    async def _await_connection(self, timeout_sec: float) -> bool:
        """Nudge a paused connection and wait a bounded time for it.

        Returns whether the connection became usable. A planned session
        rotation usually clears in a few hundred ms, so a press landing
        in one should still get its turn; a real outage never clears,
        which is what the bound is for."""
        self._connection.request_reconnect_now()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_sec
        while self._connection.is_paused():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def manual_session_start(self, source: str | None = None) -> str:
        """Trigger a voice session from external IPC (remote hold-to-talk).
        Bypasses the openWakeWord trigger but honors the same gates
        wake does: the user-deliberate stop-listening signals
        (mic-mute, room-correction measurement window), spend cap, and
        connection-paused. Returns one of
        OK / BUSY / MUTED / MEASURING / CAP / PAUSED / UNKNOWN_SOURCE /
        NO_ROOM_MIC / ERROR for the caller's logging.
        """
        if source and source not in self._manual_mics:
            log_event(
                logger,
                "session.manual_refused",
                reason="unknown_source",
                source=source,
            )
            return "UNKNOWN_SOURCE"
        if source is None and self._push_to_talk_only:
            # No source named, and this speaker has no always-listening mic to
            # be the implied one — the push-to-talk-only shape (issue #2205).
            # Accepting would open a turn nothing can feed: `_pre_roll` is
            # empty because no primary loop fills it, `_manual_mic_loop` drops
            # every frame while `_active_manual_source` is None, and the turn
            # ducks the music, chirps, and dies to the idle watchdog ~20 s
            # later having sent zero bytes — which misses both end-of-turn
            # warnings (keyed on bytes_sent > 0), so the household gets
            # silence and the journal gets nothing.
            #
            # Refused ahead of the mute/measuring/cap/paused gates: those
            # describe transient state, this the speaker's permanent shape, so
            # ranking it lower would let a passing BUSY or MUTED mask a
            # request that can never succeed. Cued, like the paused refusal
            # below and unlike the mute/measuring ones: those answer a state
            # the household just chose, these answer "I pressed something and
            # nothing happened".
            log_event(
                logger,
                "session.manual_refused",
                reason="no_room_microphone",
                sources=",".join(sorted(self._manual_mics)) or "<none>",
                detail=(
                    "this speaker has no always-listening microphone; "
                    "start the turn with a push-to-talk source id"
                ),
                level=logging.WARNING,
            )
            await self._play_cue(NO_ROOM_MIC_CUE_SLUG)
            return "NO_ROOM_MIC"
        if self._state is State.SESSION:
            return "BUSY"
        # User-deliberate "stop listening" gates — mirror the wake path's
        # _wake_late_cancelled. Mic-mute and an open room-correction
        # measurement window both mean the household has asked the speaker
        # not to listen; opening a paid LLM turn and ducking music from the
        # remote long-press / POST /session/start would bypass that. Refuse
        # silently — like the wake path, no cue and no duck, because the
        # household just asked for exactly this silence.
        if self._mic_muted:
            log_event(logger, "session.manual_refused", reason="mic_muted")
            return "MUTED"
        if self._measurement_active.is_set():
            # reason matches the wake path's `event=wake.late_cancel
            # reason=measurement_active` so one exact-match query covers
            # both refusal surfaces.
            log_event(
                logger,
                "session.manual_refused",
                reason="measurement_active",
            )
            return "MEASURING"
        if not self._spend_cap.allowed():
            return "CAP"
        if self._connection.is_paused() and not await self._await_connection(
            PAUSED_CONNECTION_WAIT_SEC,
        ):
            # Still paused after the wait: this is a real outage, not a
            # rotation. Cue first — a press that produces nothing is the
            # one refusal the household cannot explain to itself.
            log_event(
                logger,
                "session.manual_refused",
                reason="connection_paused",
                waited_sec=PAUSED_CONNECTION_WAIT_SEC,
            )
            await self._play_cue(self._connection.wake_cue())
            return "PAUSED"
        if source:
            self._active_manual_source = source
            self._acquiring = True
            self._acquire_buffer.clear()
        try:
            if source:
                await self._begin_turn(
                    pre_roll=False,
                    listening_feedback=True,
                )
            else:
                await self._begin_turn(listening_feedback=True)
            if source:
                drained, speech_in_acquire = await self._drain_acquire_audio()
                if drained:
                    log_event(
                        logger,
                        "manual_mic.acquire_drained",
                        source=source,
                        frames=drained,
                    )
                if speech_in_acquire:
                    self._user_speech_seen = True
                    self._silence_started_at = 0.0
            log_event(
                logger,
                "session.manual_started",
                source=source or "primary",
            )
            return "OK"
        except Exception as e:  # noqa: BLE001
            logger.exception("manual session start failed: %s", e)
            if self._turn_output_episode is not None:
                await self._cleanup_after_failed_begin()
            # A turn that died because the connection went down between
            # the paused gate above and here (the idle context reset
            # reopens inside `_begin_turn`) must still answer the press
            # — same condition and cue as the wake path's acquire
            # failure. See `_arbitrate_acquire_drain`.
            if self._connection.is_paused():
                await self._play_cue(self._connection.wake_cue())
            return "ERROR"
        finally:
            if source:
                self._acquiring = False

    async def manual_session_end(self) -> str:
        """Finalize the input side of an in-progress session (remote
        button release). This is the same operation the silence
        detector performs at end-of-utterance: send activity_end so
        Gemini stops listening and starts responding.
        """
        if self._state is not State.SESSION or self._turn is None:
            return "NO_SESSION"
        if self._input_ended:
            return "OK"
        self._input_ended = True
        try:
            await self._turn.end_input()
            return "OK"
        except Exception as e:  # noqa: BLE001
            logger.warning("manual session end failed: %s", e)
            return "ERROR"

    def session_status(self) -> dict:
        """Diagnostic snapshot — exposed via the control socket so
        jasper-control clients can render correct state without polling
        the spend-cap or connection state separately.

        ``camilla_volume_locked`` is the authoritative cross-daemon signal
        for whether a remote/web-slider Camilla write must be deferred. Fan-in
        can duck program audio while leaving this false, so ``duck_active``
        remains user-facing session telemetry rather than a volume lock.
        """
        return {
            "state": self._state.name,
            "input_ended": self._input_ended,
            "spend_allowed": self._spend_cap.allowed(),
            # usage.db writes are failing, so turns are served but their cost
            # is not recorded and the spend cap cannot enforce. Surfaced so
            # /state and jasper-control can show "recorded spend may be stale"
            # instead of the cap silently flatlining. See
            # UsageStore.write_degraded.
            "usage_tracking_degraded": self._usage_store.write_degraded,
            "connection_paused": self._connection.is_paused(),
            # The provider's own reason for the outage that
            # connection_paused only reports the existence of.
            "connection_error": self._connection.last_failure_detail(),
            "mic_muted": self._mic_muted,
            "measurement_active": self._measurement_active.is_set(),
            "duck_active": self._ducker.is_ducked,
            "camilla_volume_locked": bool(
                self._ducker.is_ducked
                and getattr(self._ducker, "locks_camilla_volume", True)
            ),
            "assistant_output": {
                "active": self._output_gate.is_active,
                "kind": self._output_gate.active_kind,
                "epoch": self._output_gate.epoch,
            },
            "manual_mic_sources": sorted(self._manual_mics),
            "active_manual_mic_source": self._active_manual_source,
            # This speaker has no room mic of its own: zero wake legs, every
            # turn opened by an accessory button. Surfaced because it is a
            # mode, not an absence — inferring it from an empty `wake_legs`
            # would read identically to a daemon whose legs all failed to
            # open, the opposite diagnosis. /state.voice.push_to_talk_only
            # reads this field verbatim; jasper-doctor's
            # _push_to_talk_only_speaker re-derives it from the same two
            # published facts (env tri-state + accessory file) so it still
            # reports correctly when jasper-voice is down.
            "push_to_talk_only": self._push_to_talk_only,
            # Who closes the in-flight turn's input — the daemon's own
            # decision, not a re-derivation, since "the remote cut me off" and
            # "the remote never cut me off" are the bug reports it answers.
            # Set at turn start and not cleared at turn end, so while `state`
            # is WAKE it reports the previous turn's mechanism (`input_ended`
            # above has the same shape). Read either alongside `state`.
            "endpointer": self._endpointer_label(),
            "music_dbfs": (
                round(self._content_activity.music_dbfs, 1)
                if self._content_activity.music_dbfs is not None else None
            ),
            # Actually-armed wake legs (runtime truth, by jasper.wake_legs
            # token order). /aec reports configured *intent* from
            # aec_mode.env; this is what the daemon actually opened, so a
            # startup leg-skip (event=wake.leg_skipped) is visible in
            # /state.voice, not only in the journal.
            "wake_legs": list(self._legs),
            # Per-pack tool-registration outcomes (registered / skipped /
            # failed), same motivation as wake_legs: a tool family that
            # silently failed to build (event=tool_pack.build_failed) is
            # visible in /state.voice + jasper-doctor, not only the journal.
            "tool_packs": self._tool_packs,
            # In-session barge-in firing telemetry → /state.voice.barge_in
            # (the `enabled` flag is read fresh in jasper-control's
            # aggregator, not here — it can change without restarting this
            # daemon). count is daemon-lifetime; last_at is UTC ISO.
            "barge_in_count_session": self._barge_in_count,
            "barge_in_last_at": self._barge_in_last_at,
            "barge_in_last_leg": self._barge_in_last_leg,
            # Reconcile kind for the active provider so the dashboard can show
            # whether a barge-in durably stops the assistant (OpenAI/Grok) or
            # only flushes locally while the server may resume (Gemini).
            "barge_in_reconcile": self._barge_in_reconcile.value,
            "research": {
                "configured": self._research_scheduler is not None,
                "provider": self._research_provider_id,
                "model": self._research_model,
                "pending_announcements": len(self._pending_research),
                "confirmation_window_active": self._research_window_active,
            },
        }

    async def _shadow_vad_score_raw(self, frame) -> None:
        """Score a raw-stream frame through the shadow Silero VAD.

        Pure telemetry — records what raw-stream Silero sees during the
        session but makes no endpointing decisions. The active endpointer
        (server_vad or AEC-stream Silero) is unaffected."""
        if self._vad_off is None or self._input_ended:
            return
        try:
            speech_prob = self._vad_off.predict(frame)
            if speech_prob > self._max_silero_raw_in_turn:
                self._max_silero_raw_in_turn = speech_prob
            if (
                self._silero_raw_armed_at_ms is None
                and speech_prob >= SPEECH_RUN_PEAK_MIN
            ):
                elapsed_ms = int(
                    (asyncio.get_event_loop().time() - self._turn_started_at_loop) * 1000
                )
                self._silero_raw_armed_at_ms = elapsed_ms
                log_event(
                    logger,
                    "shadow_vad.raw_armed",
                    elapsed_ms=elapsed_ms,
                    silero=f"{speech_prob:.2f}",
                )
        except Exception:  # noqa: BLE001
            pass

    async def _begin_turn(
        self,
        *,
        pre_roll: bool = True,
        text_context: str | None = None,
        listening_feedback: bool = False,
    ) -> None:
        completed = False
        try:
            if listening_feedback:
                # Prime the TTS IPC owner's loudness context before the chirp
                # as well as before assistant TTS. The chirp is fire-and-forget,
                # so waiting for the inner turn prepare would race it back onto
                # the no-context fallback.
                await self._begin_turn_output_episode()
                await self._prepare_assistant_loudness_context()
                # Fire-and-forget so the "Now listening" chirp overlaps turn
                # opening instead of adding ~70 ms to time-to-listen. It is not
                # a response task: any completed `_bg_tasks` member ends a turn.
                self._create_fire_and_forget_task(
                    self._play_listening_chirp(going_on=True),
                    name="listening-chirp-on",
                )
            await self._begin_turn_inner(
                pre_roll=pre_roll,
                text_context=text_context,
            )
            completed = True
        finally:
            if not completed:
                cleanup_error = await _capture_cleanup_error(
                    lambda: self._await_output_cleanup_owned(
                        self._cleanup_after_failed_begin(),
                        task_name="turn-begin-cleanup",
                    ),
                )
                if cleanup_error is not None and not isinstance(
                    cleanup_error,
                    asyncio.CancelledError,
                ):
                    log_event(
                        logger,
                        "turn.begin_cleanup_failed",
                        level=logging.ERROR,
                        exc_type=type(cleanup_error).__name__,
                        err=str(cleanup_error),
                    )

    async def _begin_turn_inner(
        self,
        *,
        pre_roll: bool = True,
        text_context: str | None = None,
    ) -> None:
        import time as _time
        await self._begin_turn_output_episode()
        # Anchor on the wake-fire moment (set in _handle_wake_frame) so
        # sched_lag captures the gap between wake firing and this coroutine
        # being picked up by the event loop; remote paths that bypass
        # _handle_wake_frame fall back to now.
        t_wake = self._wake_event_at_monotonic or _time.monotonic()
        t_begin = _time.monotonic()
        # One endpointer decision per turn. A turn whose audio comes from a
        # push-to-talk source is closed by the button release
        # (`manual_session_end`), so local Silero must not also try.
        # `_active_manual_source` is set by `manual_session_start` before it
        # calls us and is the same flag `_manual_mic_loop` gates on, so "the
        # button owns this turn" and "manual-source frames are the session
        # audio" are one fact, not two.
        self._manual_endpoint_this_turn = self._active_manual_source is not None
        # Silero's internal LSTM state must not leak across turns. A
        # push-to-talk-only daemon has no VAD to reset (see __init__).
        if self._vad is not None:
            self._vad.reset()
        # `_turn_started_at_loop` anchors NO_SPEECH_ABORT_SEC,
        # HARD_RECORDING_CAP_SEC and the push-to-talk hold cap; it is read on
        # the asyncio loop clock to match what the silence detector reads.
        self._user_speech_seen = False
        self._silence_started_at = 0.0
        self._speech_run_started_at = 0.0
        self._speech_run_max_silero = 0.0
        self._input_ended = False
        self._turn_started_at_loop = asyncio.get_event_loop().time()
        self._max_silero_score_in_turn = 0.0
        self._max_silero_raw_in_turn = 0.0
        self._silero_raw_armed_at_ms = None
        self._silero_aec_armed_at_ms = None
        self._resolve_barge_in_for_turn()
        if self._vad_off is not None:
            self._vad_off.reset()
        t_after_state = _time.monotonic()
        await self._content_activity.refresh_now()
        await self._prepare_assistant_loudness_context()
        await self._tts.pause_content_meter()
        self._content_activity.pause()
        # Tell the volume coordinator a session is active so its
        # source-transition handler doesn't fight the ducker's
        # additive math on camilla.
        self._volume_coordinator.note_voice_session(
            True,
            camilla_volume_locked=getattr(
                self._ducker, "locks_camilla_volume", True,
            ),
        )
        t_after_loudness_prepare = _time.monotonic()
        await self._ducker.duck()
        t_after_duck = _time.monotonic()
        self._session_id = self._usage_store.open_session(
            provider=self._cfg.voice_provider,
        )
        self._turn = await self._connection.acquire_turn()
        t_after_acquire = _time.monotonic()

        if text_context:
            send_text_context = getattr(self._turn, "send_text_context", None)
            if not callable(send_text_context):
                raise RuntimeError("live turn cannot accept text context")
            await send_text_context(text_context)
            if self._turn.turn_lost():
                raise RuntimeError("live turn lost while sending text context")

        self._server_vad_this_turn = False
        # Computed once, then either refused or armed, so the refusal below
        # can only claim to have blocked something that would otherwise have
        # been negotiated on this turn. `music_is_playing()` is a per-turn
        # condition: a button turn in silence never had server VAD to refuse
        # and must not say it did.
        want_server_vad = (
            self._cfg.server_vad_enabled
            and self._connection.supports_server_vad()
            and self._content_activity.music_is_playing()
        )
        if want_server_vad and self._manual_endpoint_this_turn:
            # On a button turn server VAD is a second writer of "input is
            # over": at `server_vad_silence_ms` (500 ms default) the server
            # declares end-of-utterance and answers while the button is still
            # held. Its own latch, not `_barge_in_ptt_warned` — sharing one
            # would let whichever fires first swallow the other's only WARN.
            if not self._server_vad_ptt_warned:
                self._server_vad_ptt_warned = True
                log_event(
                    logger,
                    "server_vad.disabled_push_to_talk",
                    silence_ms=self._cfg.server_vad_silence_ms,
                    source=self._active_manual_source or "primary",
                    detail="the button already closes input on release",
                    level=logging.WARNING,
                )
        elif want_server_vad:
            set_td = getattr(self._connection, "set_turn_detection", None)
            if set_td is not None and callable(set_td):
                try:
                    await set_td({
                        "type": "server_vad",
                        "threshold": self._cfg.server_vad_threshold,
                        "silence_duration_ms": self._cfg.server_vad_silence_ms,
                        "prefix_padding_ms": self._cfg.server_vad_prefix_ms,
                        "create_response": False,
                        "interrupt_response": False,
                    })
                    self._server_vad_this_turn = True
                    mark = getattr(self._turn, "mark_server_vad", None)
                    if callable(mark):
                        mark()
                    log_event(
                        logger,
                        "server_vad.enabled",
                        music_dbfs=f"{self._content_activity.music_dbfs or float('-inf'):.1f}",
                    )
                except Exception as e:  # noqa: BLE001
                    log_event(
                        logger,
                        "server_vad.enable_failed",
                        error=f"{type(e).__name__}: {e}",
                        level=logging.WARNING,
                    )

        logger.info(
            "turn acquire done in %.0fms "
            "(sched_lag=%.0f state=%.0f loudness_prepare=%.0f duck=%.0f acquire=%.0f) "
            "(wake→activity_start%s)",
            (_time.monotonic() - t_wake) * 1000,
            (t_begin - t_wake) * 1000,
            (t_after_state - t_begin) * 1000,
            (t_after_loudness_prepare - t_after_state) * 1000,
            (t_after_duck - t_after_loudness_prepare) * 1000,
            (t_after_acquire - t_after_duck) * 1000,
            ", server_vad" if self._server_vad_this_turn else "",
        )
        # Drain the recent-mic ring into the turn so the user's first phoneme,
        # which preceded the wake firing, reaches the model. The frame that
        # fired the wake is the most-recently-appended entry and is included.
        pre_roll_frames = list(self._pre_roll) if pre_roll else []
        for f in pre_roll_frames:
            try:
                await self._turn.send_audio(f.tobytes())
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "pre-roll send failed (will continue with live frames): %s", e,
                )
                break
        if pre_roll_frames:
            logger.info(
                "pre-roll sent: %d frames (~%.0fms)",
                len(pre_roll_frames), len(pre_roll_frames) * 80.0,
            )
        playback = asyncio.create_task(
            _play_responses(
                self._turn, self._tts, barge_in_enabled=self._barge_in_active,
                on_response_started=self._record_response_started,
            )
        )
        idle = asyncio.create_task(
            _idle_watchdog(
                self._turn,
                self._tts,
                self._cfg.idle_timeout_sec,
                self._cfg.response_stall_timeout_sec,
            )
        )
        self._bg_tasks = {playback, idle}
        if self._server_vad_this_turn:
            vad_trigger = asyncio.create_task(
                _server_vad_response_trigger(self._turn, self._connection)
            )
            self._bg_tasks.add(vad_trigger)
        self._arm_session_task_watcher()
        self._state = State.SESSION
        self._arm_turn_background_end()

    async def _begin_turn_output_episode(self) -> None:
        if self._turn_output_episode is not None and self._output_gate.is_current(
            self._turn_output_episode,
        ):
            return
        self._turn_output_episode = await self._output_gate.begin_turn()

    async def _cleanup_after_failed_begin(self) -> None:
        first_base_error: BaseException | None = None

        def record_failure(phase: str, error: BaseException) -> None:
            nonlocal first_base_error
            if isinstance(error, Exception):
                log_event(
                    logger,
                    "turn.begin_cleanup_phase_failed",
                    phase=phase,
                    exc_type=type(error).__name__,
                    err=str(error),
                    level=logging.WARNING,
                )
            elif first_base_error is None:
                # Cancellation and other BaseExceptions must not skip later
                # cleanup: re-raise the first only after every phase has run.
                first_base_error = error

        async def run_phase(
            phase: str,
            operation: Callable[[], object],
        ) -> None:
            error = await _capture_cleanup_error(operation)
            if error is not None:
                record_failure(phase, error)

        turn = self._turn
        session_id = self._session_id
        episode = self._turn_output_episode
        if turn is not None:
            await run_phase("turn_release", turn.release)
        await run_phase("duck_restore", self._ducker.restore)
        await run_phase(
            "volume_session",
            lambda: self._volume_coordinator.note_voice_session(False),
        )
        await run_phase("content_resume", self._content_activity.resume)
        await run_phase("content_meter_resume", self._tts.resume_content_meter)
        if session_id is not None:
            await run_phase(
                "usage_session_close",
                lambda: self._usage_store.close_session(session_id, 0, 0),
            )

        def reset_local_state() -> None:
            self._turn = None
            self._session_id = None
            self._bg_tasks = set()
            self._bg_end_scheduled = False
            self._active_manual_source = None
            self._acquiring = False
            self._state = State.WAKE

        await run_phase("local_state_reset", reset_local_state)
        await run_phase(
            "refractory_reset",
            lambda: setattr(
                self,
                "_refractory_until",
                asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC,
            ),
        )
        await run_phase(
            "output_episode_release",
            lambda: self._output_gate.end_turn(episode),
        )
        self._turn_output_episode = None

        if first_base_error is not None:
            raise first_base_error

    async def _end_turn(self, reason: str = "ended") -> None:
        # Re-entrancy guard. `_end_turn_inner` awaits repeatedly and only
        # clears _session_id and flips _state at its last lines, so the
        # control-socket mute_mic handler and the mic loop's
        # _handle_session_frame can both enter it; a second entrant would trip
        # `assert self._session_id is not None` and crash the daemon.
        #
        # It is a dedicated _ending flag, NOT an early flip of _state to WAKE:
        # _state must stay SESSION for the whole teardown, because the teardown
        # plays a chirp on the single TTS stream and the coroutines that could
        # collide with it gate on SESSION (play_supervisor_cue skips,
        # announce_timer defers, the mic loops route to
        # _handle_session_frame). An early WAKE would let a supervisor cue or
        # timer announcement garble the teardown chirp, or — during a
        # mute-initiated teardown, before _mic_muted is set — let a fresh wake
        # frame begin a new turn that this teardown then tears down.
        if self._ending or self._turn is None:
            return
        self._ending = True
        try:
            await self._end_turn_inner(reason)
        finally:
            self._ending = False

    async def _end_turn_inner(self, reason: str = "ended") -> None:
        # Capture drain timing before any await adds latency: time from last
        # server activity to turn end, meaningful only when audio was actually
        # received (otherwise it is the abort timeout, logged separately by the
        # caller).
        drain_wait_sec: float | None = None
        if self._turn is not None and self._turn.last_chunk_at() > 0:
            drain_wait_sec = max(
                0.0, time.monotonic() - self._turn.last_activity_at(),
            )
        research_window_job = (
            self._research_window_job if self._research_window_active else None
        )
        research_window_decided = self._research_window_decided
        research_window_cancelled_by_wake = self._research_window_cancelled_by_wake
        # `_user_speech_seen` false means the session got no real user input:
        # a likely false positive (music transient, TTS bleed) or a changed
        # mind. Either way the outcome is 'no_speech', which dual-stream
        # false-positive analysis keys off.
        await self._telemetry_stage("turn_complete")
        # Capture event_id BEFORE _telemetry_outcome clears it.
        session_vad_store = self._wake_event_store
        session_vad_eid = self._current_event_id
        terminal_outcome = (
            "completed" if self._user_speech_seen else "no_speech"
        )
        await self._telemetry_outcome(terminal_outcome, reason)

        # Shadow telemetry: what each stream's Silero saw, so the weekly
        # review can cross-tab scores.
        store = session_vad_store
        eid = session_vad_eid
        if store is not None and eid is not None:
            endpointer_label = self._corpus_endpointer_label(
                user_speech_seen=self._user_speech_seen,
            )
            try:
                await store.update_session_vad(
                    eid,
                    max_silero_aec=self._max_silero_score_in_turn or None,
                    max_silero_raw=self._max_silero_raw_in_turn or None,
                    silero_aec_armed_at_ms=self._silero_aec_armed_at_ms,
                    silero_raw_armed_at_ms=self._silero_raw_armed_at_ms,
                    endpointer=endpointer_label,
                    music_playing_at_turn=self._content_activity.music_is_playing(),
                    music_db_at_turn=self._content_activity.music_dbfs,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("wake_events: session VAD telemetry failed: %s", e)

        # Notify the peering daemon before the slow cleanup so peers
        # un-suppress promptly: waiting for our chirp and duck restore would
        # add ~300 ms of suppression before other devices can arbitrate a
        # fresh wake. No-op when peering is off or the session was untracked.
        await self._notify_peering_session_ended(reason)
        self._peering_current_epoch = ""

        for t in self._bg_tasks:
            t.cancel()
        for t in self._bg_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._bg_tasks = set()
        self._bg_end_scheduled = False

        # _play_responses reaches its own end_segment() only when the provider
        # closes the audio iterator at turn end: OpenAI does (response.done),
        # Gemini's closes only on release(), which runs after the cancel above.
        # Without this call the cancelled playback task discards the passive
        # loudness measurement, so Gemini earns no source profile and fanin
        # plays it at the louder fallback gain. Idempotent — the meter clears
        # on first save.
        try:
            await self._tts.end_segment()
        except Exception as e:  # noqa: BLE001
            logger.warning("teardown end_segment failed: %s", e)

        if self._turn is not None:
            # `_manual_endpoint_this_turn` is the third term because on a
            # push-to-talk turn `_user_speech_seen` never flips (nothing
            # scores those frames). Without it, a turn torn down mid-hold —
            # idle watchdog, stop event, a release that never came — would
            # skip end_input() entirely.
            if (self._input_ended or self._user_speech_seen
                    or self._manual_endpoint_this_turn):
                try:
                    await asyncio.wait_for(self._turn.end_input(), timeout=2.0)
                except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                    logger.debug("end_input ignored: %s", e)
            try:
                await self._turn.release()
            except Exception as e:  # noqa: BLE001
                logger.debug("turn release error (ignored): %s", e)

            tokens = self._turn.usage_tokens()
            # Modality breakdown when the provider exposes one: OpenAI
            # Realtime does; Gemini Live returns None and the store falls back
            # to scalar all-audio pricing.
            breakdown = None
            getter = getattr(self._turn, "usage_breakdown", None)
            if callable(getter):
                breakdown = getter()
            assert self._session_id is not None
            cost = self._usage_store.close_session(
                self._session_id,
                tokens["input_tokens"],
                tokens["output_tokens"],
                usage=breakdown,
            )
            if research_window_job is None:
                self._record_conversation_turn(
                    _optional_turn_text(self._turn, "user_transcript"),
                    _optional_turn_text(self._turn, "assistant_transcript"),
                    data_json=_optional_turn_data_json(self._turn),
                )
            # Per-turn no-audio detection splits into two phenomena, gated on
            # whether the wake loop explicitly ended input (silence detector,
            # hard cap, or manual end). Bytes sent with `_input_ended` never
            # flipped means the model never got a clean end-of-utterance
            # signal before the watchdog closed the turn — a different fault
            # from a model that was asked and answered with silence.
            bytes_sent = self._turn.bytes_sent()
            chunks_received = self._turn.chunks_received()
            expected_research_silence_dismiss = (
                research_window_job is not None
                and not research_window_decided
                and not research_window_cancelled_by_wake
                and not self._user_speech_seen
                and not self._input_ended
            )
            if (
                bytes_sent > 0
                and chunks_received == 0
                and not self._turn.turn_lost()
                and not expected_research_silence_dismiss
            ):
                model = _active_model(self._cfg)
                if self._input_ended:
                    if not self._silent_response_warned:
                        self._silent_response_warned = True
                        log_event(
                            logger,
                            "turn.silent_response",
                            provider=self._cfg.voice_provider,
                            model=model,
                            bytes_sent=bytes_sent,
                            endpointer=self._endpointer_label(),
                            level=logging.WARNING,
                        )
                elif self._manual_endpoint_this_turn:
                    # Same shape as RECORDING TIMEOUT below, but that text
                    # names a silence detector and a wake fire, neither of
                    # which exists on a button turn — it would send an
                    # operator hunting a wake-threshold problem that isn't
                    # there. The button was held past the point where the
                    # idle watchdog gave up waiting for the model.
                    logger.warning(
                        "HOLD TIMEOUT: sent %d bytes of audio to %s but the "
                        "button was never released and the hold cap did not "
                        "close input first — the idle watchdog "
                        "(JASPER_IDLE_TIMEOUT_SEC=%.0fs) ended the turn "
                        "before the model was asked to answer. Check the "
                        "accessory's release event, and that the hold cap "
                        "sits below the idle timeout.",
                        bytes_sent, model, float(self._cfg.idle_timeout_sec),
                    )
                else:
                    logger.warning(
                        "RECORDING TIMEOUT: sent %d bytes of audio to %s "
                        "but the silence detector never tripped — idle "
                        "watchdog ended the turn before the wake loop "
                        "asked for a response. Common cause: low-confidence "
                        "wake firing on background audio, or user speaking "
                        "continuously past the idle window without a pause.",
                        bytes_sent, model,
                    )
            drain_part = (
                f", drain wait {drain_wait_sec:.2f}s"
                if drain_wait_sec is not None else ""
            )
            # Writer-side pacing visibility: nonzero means TTS writes slept to
            # stay under the IPC owner's pending budget. Fanin logs drops, but
            # over-pacing has no receiver-side signature, so this is the only
            # journal evidence of it.
            paced_sec = self._tts.take_paced_sec()
            paced_part = f", paced {paced_sec:.2f}s" if paced_sec > 0.05 else ""
            logger.info(
                "turn ended: %s tokens, est $%.4f (sent=%dB, recv=%d chunks%s%s%s)",
                tokens, cost, bytes_sent, chunks_received, drain_part,
                paced_part,
                ", turn_lost" if self._turn.turn_lost() else "",
            )

        # "Done listening" chirp, bookending the wake chirp on every path into
        # _end_turn. Awaited so it lands in the TTS queue before the unduck
        # below, behind any LLM-response tail still buffered: the audible order
        # is response → chirp → music returns.
        await self._play_listening_chirp(going_on=False)
        try:
            # Queue completion is not acoustic completion. Keep the turn's
            # output/duck ownership until the final chirp has cleared the
            # physical route, for both fan-in and member-local outputd TTS.
            await self._tts.wait_drained()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
            logger.warning("teardown TTS drain wait failed: %s", e)

        await self._ducker.restore()
        self._volume_coordinator.note_voice_session(False)
        self._content_activity.resume()
        await self._tts.resume_content_meter()
        self._turn = None
        self._session_id = None
        self._active_manual_source = None
        self._state = State.WAKE
        await self._output_gate.end_turn(self._turn_output_episode)
        self._turn_output_episode = None
        if research_window_job is not None:
            self._research_window_active = False
            self._research_window_job = None
            self._research_window_decided = False
            self._research_window_cancelled_by_wake = False
            if (
                not research_window_decided
                and not research_window_cancelled_by_wake
            ):
                self._mark_research_announced(research_window_job, read=False)
                log_event(
                    logger,
                    "research.confirmation_window_dismissed",
                    reason="silence",
                    job_id=research_window_job.id,
                )
        # No detector.reset() here: `_handle_wake_frame` reset every detector
        # when the wake fired and none was fed a frame since (state was
        # SESSION), so a second reset would only delay the buffer refilling
        # when refractory expires.
        self._refractory_until = asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC
        await self._drain_pending_research()


def _active_model(*args, **kwargs):
    from .voice.daemon_main import _active_model as impl
    return impl(*args, **kwargs)


def _conversation_ts_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _optional_turn_text(turn: object, method_name: str) -> str | None:
    getter = getattr(turn, method_name, None)
    if not callable(getter):
        return None
    try:
        text = getter()
    except (RuntimeError, TypeError, ValueError) as e:
        logger.debug("conversation capture: %s failed: %s", method_name, e)
        return None
    if text is None:
        return None
    text = str(text).strip()
    return text or None


def _optional_turn_data_json(turn: object) -> dict | str | None:
    getter = getattr(turn, "conversation_metadata", None)
    if not callable(getter):
        return None
    try:
        data = getter()
    except (RuntimeError, TypeError, ValueError) as e:
        logger.debug("conversation capture: conversation_metadata failed: %s", e)
        return None
    if data is None or isinstance(data, (dict, str)):
        return data
    logger.debug(
        "conversation capture: conversation_metadata returned unsupported %s",
        type(data).__name__,
    )
    return None


async def run() -> None:
    from .voice.daemon_main import run as impl
    await impl()


def main() -> None:
    from .voice.daemon_main import main as impl
    impl()


if __name__ == "__main__":
    main()
