# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The wake -> LLM loop: ``WakeLoop`` is the sole consumer of the primary mic."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timezone

from jasper.log_event import log_event

from .audio_buffer import AudioBuffer
from .mic_capture import InputDeviceUnavailable
from .tts_playout import TtsPlayout
from .wake_events import WakeEventStore
from .cues import AudioCueManager
from .cues.registry import NO_ROOM_MIC_CUE_SLUG
from .vad import SpeechVAD
from .config import Config
from .conversation_history import ConversationStore
from .watchdog import Heartbeat
from .timers import Timer, announcement_text
from .usage import (
    SpendCap,
    UsageStore,
)
from .voice.session import LiveConnection
from .voice.content_activity import ContentActivityTracker
from .voice.conversation_capture import ConversationCapture
from .voice.catalog import InterruptReconcile, resolve_interrupt_reconcile
from .voice.conversation import (
    END_OF_UTTERANCE_SILENCE_SEC, NO_SPEECH_ABORT_SEC,
)
from .voice._base import SESSION_CLOSE_TIMEOUT_SEC
from .voice._tasks import cancel_tracked_tasks, track_task
from .voice.input_policy import contract_from_config
from .voice.measurement_hold import MeasurementHold
from .voice.peering_client import PeeringClient
from .voice.wake_detect import (
    WAKE_REFRACTORY_SEC,
    LegRuntime,
    WakeLegs,
)
from .voice.turn_timeline import TurnTimeline
from .voice.turn_lifecycle import (
    InputAdmissionClosed,
    State,
    TurnInput,
    TurnLifecycle,
)
from .voice.push_to_talk import (
    HARD_RECORDING_CAP_SEC,
    PTT_KEEPALIVE_INTERVAL_SEC,
    ManualMicRuntime,
    PushToTalk,
    keepalive_ticks,
)
from .voice.wake_telemetry import WakeTelemetry
from .voice.assistant_output import (
    INTERNAL_ERROR_CUE_SLUG,
    AssistantOutput,
    FanInDucker,
    capture_cleanup_error,
)
from .voice.output_gate import AssistantOutputGate
from .volume_coordinator import VolumeCoordinator
from .mic_mute_persistence import read_mic_muted, write_mic_muted

logger = logging.getLogger(__name__)
EX_CONFIG_EXIT = 78
VOICE_PROVIDER_NOT_CONFIGURED_EXIT = EX_CONFIG_EXIT
VOICE_STARTUP_CONFIG_ERROR_EXIT = EX_CONFIG_EXIT
# Primary microphone could not be opened at startup (os.EX_NOINPUT). A
# DISTINCT code from EX_CONFIG (78) so the unit, doctor, and /state can
# tell "no usable mic" from "no provider configured". Listed in
# jasper-voice.service's SuccessExitStatus + RestartPreventExitStatus so
# the daemon parks cleanly (waiting for the AEC reconciler / udev to
# restart it on plug-in) instead of crash-looping toward
# StartLimitAction=reboot.
VOICE_MIC_UNAVAILABLE_EXIT = 66


# How long a wake or a manual (button) session start waits out a paused
# connection before taking the turn anyway. Most pauses are a planned
# session rotation, whose gap is one teardown plus one connect (p50
# ~350 ms, p99 5.3 s); refusing instantly turns the common ones into
# a dead press and a false 'can't connect' cue. The bound keeps the
# SUCCESS path — which returns as soon as the turn opens — inside
# jasper.platform.control_client.DEFAULT_TIMEOUT (2.0 s) with room for the
# round trip. A refusal outlives that either way: it cues first.
PAUSED_CONNECTION_WAIT_SEC = 1.2

# Pre-roll: when wake fires, replay the most recent ~560 ms of mic
# audio into the turn so the first phoneme of the user's command
# isn't lost. openWakeWord fires when the END of "Hey Jarvis" passes
# its window — by that point the user is already 200-400 ms into
# their command. Without pre-roll we throw those frames away.
# 7 × 80 ms = 560 ms covers the wake-word tail + the start of the
# command for fast speakers.
PRE_ROLL_FRAMES = 7

# Silero speech-probability threshold for marking "the user has
# actually spoken" within a turn. Decoupled from
# JASPER_VAD_BARGE_IN_THRESHOLD (default 0.5) — that one is tuned
# strict to avoid TTS-bleed false-positives in the barge-in gate;
# this one is tuned LOOSE so soft / quiet speech still flips
# `_turns.user_speech_seen` so the silence detector arms.
# Range: AirPlay music vocals scored 0.13 (0.10 was loose enough to let
# them flip the flag and feed a false wake to the model); real user
# speech in the same session bottomed out at 0.19. 0.15 sits between
# music transients and the softest real speech observed.
END_OF_UTTERANCE_SPEECH_THRESHOLD = 0.15

# End-of-turn timing — owned by TtsPlayout.expected_drain_at /
# wait_drained. Drain tail configured via JASPER_TTS_DRAIN_TAIL_SEC.

# Sustained-speech threshold for arming the end-of-utterance silence
# detector. After wake fires, Silero must report ≥ THRESHOLD
# speech-probability for at least this many seconds *continuously*
# before `_turns.user_speech_seen` flips. Then — and only then — does
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


class WakeLoop:
    """Sole consumer of the primary mic. Dispatches each frame to either
    the wake-word detector (WAKE state) or the active live turn (SESSION
    state).

    `self._wake_legs` (voice/wake_detect.py) owns the configured legs and
    decides which one heard the wake word. The primary "on" (AEC3) leg
    drives this main loop and carries session audio plus the watchdog
    heartbeat; optional "off" (chip-direct) and "dtln" legs run as
    parallel `_wake_leg_loop` tasks. Secondary legs are
    wake-detection-only: their frames don't populate pre-roll or flow into
    sessions — the primary "on" stream stays the canonical session audio
    source.
    """

    def __init__(
        self,
        cfg: Config,
        tts: TtsPlayout,
        connection: LiveConnection,
        ducker: FanInDucker,
        content_activity: ContentActivityTracker,
        usage_store: UsageStore,
        spend_cap: SpendCap,
        stop_event: asyncio.Event,
        volume_coordinator: "VolumeCoordinator",
        *,
        legs: "list[LegRuntime]",
        cues: AudioCueManager | None = None,
        heartbeat: "Heartbeat | None" = None,
        wake_event_store: WakeEventStore | None = None,
        tool_packs: list[dict] | None = None,
        conversation_store: ConversationStore | None = None,
        manual_mics: "list[ManualMicRuntime] | None" = None,
        vad: SpeechVAD | None = None,
        initial_mic_muted: bool | None = None,
        barge_in_reconcile: InterruptReconcile | None = None,
    ) -> None:
        self._assistant_output = AssistantOutput(
            cfg, tts, ducker, cues, volume_coordinator,
        )
        # Per-pack tool-registration outcomes, already serialized to the
        # /state.voice.tool_packs wire shape by outcomes_to_state. Opaque
        # here; held only so session_status can surface which tool
        # families registered / were gated off / failed to build.
        self._tool_packs: list[dict] = tool_packs or []
        self._connection = connection
        self._content_activity = content_activity
        self._wake_legs = WakeLegs(
            legs,
            music_dbfs=self._read_music_dbfs,
            mic_muted=lambda: self._mic_muted,
        )
        self._push_to_talk = PushToTalk(
            manual_mics or [], have_wake_legs=bool(self._wake_legs.legs),
        )
        # Absent on a push-to-talk-only speaker: no always-listening
        # microphone, so `configured_wake_legs` planned no legs and `run()`
        # branches to a keepalive loop instead of iterating frames.
        _on = self._wake_legs.legs.get("on")
        self._mic = _on.mic if _on is not None else None
        self._usage_store = usage_store
        self._spend_cap = spend_cap
        self._conversation_capture = ConversationCapture(
            store=conversation_store, voice_provider=cfg.voice_provider,
        )
        self._stop_event = stop_event
        # Bumped on every mic frame — proof that audio capture is alive
        # AND the async loop is iterating. If either dies (PortAudio
        # wedge, asyncio deadlock, mic device disappearance), the
        # heartbeat thread stops patting systemd and
        # `Restart=on-watchdog` revives us. See jasper/watchdog.py.
        self._heartbeat = heartbeat

        # None on a push-to-talk-only daemon: every reader below is already
        # off on a button turn (barge-in refused, server VAD refused, the
        # endpointer bypassed), and `SpeechVAD()` is what pulls openwakeword
        # + onnxruntime into resident memory. See ADR-0217.
        self._vad: SpeechVAD | None = vad
        if self._vad is None and not self._push_to_talk.only:
            self._vad = SpeechVAD()
        # Session-state shadow VAD for the chip-direct ("off") leg, when
        # configured. Built by jasper.voice.daemon_main and carried on that
        # leg's LegRuntime.
        self._vad_off: SpeechVAD | None = (
            self._wake_legs.legs["off"].shadow_vad
            if "off" in self._wake_legs.legs else None
        )

        self._fire_and_forget: set[asyncio.Task] = set()
        # Populated by run() when it starts the leg/manual-mic consumer
        # loops; emptied again by its finally sweep. session_status's
        # wake_legs derives from _leg_tasks (see there).
        self._leg_tasks: dict[str, asyncio.Task[None]] = {}
        self._manual_tasks: list[asyncio.Task[None]] = []

        # Room-correction measurement window. When set, mic frames are
        # dropped (no wake-word feed, no session forward) and outputd is
        # asked to ignore content-meter samples so sweeps don't become
        # the next assistant-loudness baseline. `MeasurementHold` is the
        # only writer; every path here only reads the gate.
        self._measurement_active: asyncio.Event = asyncio.Event()
        self.measurement_hold = MeasurementHold(
            self, session_active=lambda: self._turns.state is State.SESSION,
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

        # Monotonic wallclock at the moment wake fires. Used by
        # _begin_turn to break the wake→activity_start latency into
        # named segments (state reset, loudness prepare, duck,
        # acquire_turn) so a slow turn-acquire can be localized.
        # 0.0 means "no wake yet this session"; replaced on every fire.
        # Only a turn the wake path itself opens reads it (passed as
        # `_begin_turn(anchor_at=...)`), because a wake that opens no turn
        # leaves it set.
        self._wake_event_at_monotonic: float = 0.0

        self._silence_started_at: float = 0.0
        # Anchor and peak of the current speech run; `_sustained_run` owns
        # both lifetimes. The peak rejects wake-tail audio — see
        # SPEECH_RUN_PEAK_MIN.
        self._speech_run_started_at: float = 0.0
        self._speech_run_max_silero: float = 0.0

        self._barge_in_reference_available = contract_from_config(cfg).echo_cancelled
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
        # The frozen prefix belongs to the accepted wake/manual start;
        # only later frames enter the acquire buffer.
        self._pre_roll: deque = deque(maxlen=PRE_ROLL_FRAMES)
        self._frozen_pre_roll: tuple | None = None

        # Wake-event telemetry. It owns every store write and the
        # in-flight event id; the WakeLoop contributes the per-leg
        # capture rings. Those rings stay separate from `_pre_roll`:
        # they are sized for offline review (~6 s windows around each
        # wake event) while the pre-roll is sized for first-phoneme
        # preservation at turn-open (~560 ms).
        self._wake_telemetry = WakeTelemetry(
            store=wake_event_store,
            wake_model=cfg.wake_model,
            voice_provider=cfg.voice_provider,
        )
        self._turn_timeline = TurnTimeline(
            self._wake_telemetry,
            # Late-bound: the label is a live read of the turn in flight.
            endpointer=lambda: self._turns.endpointer_label(),
        )
        self._acquiring: bool = False
        self._acquire_input_epoch = 0.0
        self._acquire_buffer = AudioBuffer()
        self._input_gaps = 0
        self._acquire_drops_reported = 0
        self._input_last_age_ms = 0
        self._input_max_age_ms = 0
        self._input_suspended: set[str] = set()
        self._input_admit_after = 0.0
        self._input_invalidation_reason = "MUTED"

        self._peering = PeeringClient(
            enabled=cfg.peering_enabled, socket_path=cfg.peering_uds_socket,
        )
        # Every callable is late-bound: a test that rebinds `_play_cue` or
        # `_create_fire_and_forget_task` on the instance must still be the
        # one the teardown reaches.
        self._turns = TurnLifecycle(
            self._assistant_output,
            connection,
            content_activity,
            usage_store,
            spend_cap,
            self._push_to_talk,
            timeline=self._turn_timeline,
            peering=self._peering,
            wake_telemetry=self._wake_telemetry,
            conversation_capture=self._conversation_capture,
            measurement_active=self._measurement_active,
            check_admission=lambda epoch: self._check_input_admission(epoch),
            turn_input=lambda *, pre_roll: TurnInput(
                self._acquire_input_epoch if self._acquiring
                else self._input_admit_after,
                (
                    tuple(self._pre_roll) if self._frozen_pre_roll is None
                    else self._frozen_pre_roll
                ) if pre_roll else (),
            ),
            acquire_anchor=lambda: (
                self._input_admit_after if self._acquiring else 0.0
            ),
            reset_input=lambda: self._reset_turn_input(),
            prepare_loudness=lambda: self._prepare_assistant_loudness_context(),
            barge_in_reference=lambda: self._barge_in_reference_available,
            spawn=lambda coro, *, name: self._create_fire_and_forget_task(
                coro, name=name,
            ),
            arm_refractory=lambda: self._arm_wake_refractory(),
            play_cue=lambda slug: self._play_cue(slug),
            mic_muted=lambda: self._mic_muted,
        )

    def _arm_wake_refractory(self) -> None:
        self._wake_legs.refractory_until = (
            asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC
        )

    @property
    def _output_gate(self) -> AssistantOutputGate:
        return self._assistant_output.gate

    @property
    def _cfg(self) -> Config:
        return self._assistant_output.cfg

    @property
    def _tts(self) -> TtsPlayout:
        return self._assistant_output.tts

    @property
    def _cues(self) -> AudioCueManager | None:
        return self._assistant_output._cues

    @property
    def _ducker(self) -> FanInDucker:
        return self._assistant_output.ducker

    @property
    def _volume_coordinator(self) -> VolumeCoordinator:
        return self._assistant_output.volume_coordinator

    def _create_fire_and_forget_task(
        self,
        coro: Coroutine[object, object, object],
        *,
        name: str,
    ) -> asyncio.Task:
        return track_task(
            asyncio.create_task(coro, name=name),
            self._fire_and_forget,
            label=name,
        )

    async def _cancel_fire_and_forget_tasks(self) -> None:
        """Sweep tracked side-work, giving a turn release its close first.

        A cancelled release never reaches the provider's `session.close`,
        and a live session left open keeps billing per connected minute.
        """
        if (release := self._turns.take_pending_release()) is not None:
            await asyncio.wait({release}, timeout=SESSION_CLOSE_TIMEOUT_SEC)
        await cancel_tracked_tasks(self._fire_and_forget)

    def _sustained_run(self, score: float, threshold: float, now: float) -> bool:
        """Advance the speech run with this frame; True once it has armed.

        A frame at or above `threshold` extends the run and its peak; any
        frame below ends it. Armed means the run has lasted
        SUSTAINED_SPEECH_TO_ARM_SEC and peaked at SPEECH_RUN_PEAK_MIN, and
        stays true for every later frame of the same run — what each caller
        does on that is its own.
        """
        if score < threshold:
            self._speech_run_started_at = self._speech_run_max_silero = 0.0
            return False
        if not self._speech_run_started_at:
            self._speech_run_started_at = now
        self._speech_run_max_silero = max(self._speech_run_max_silero, score)
        return (now - self._speech_run_started_at >= SUSTAINED_SPEECH_TO_ARM_SEC
                and self._speech_run_max_silero >= SPEECH_RUN_PEAK_MIN)

    async def play_cue(self, slug: str) -> str:
        return await self._assistant_output.play_cue_admitted(slug)

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
        if self._turns.state is State.SESSION:
            return "skipped_session_active"
        if self._output_gate.is_active:
            return self._assistant_output.admission_refusal() or "skipped_output_active"
        return await self.play_cue(slug)

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
        while self._turns.state is State.SESSION or self._output_gate.is_active:
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

    def close_conversation_store(self) -> None:
        self._conversation_capture.close()

    def request_conversation_end(self) -> None:
        self._turns.request_conversation_end()

    async def _play_dynamic_text(self, text: str) -> bool:
        return await self._assistant_output.play_dynamic_text(text)

    async def _play_cue(self, slug: str) -> bool:
        return await self._assistant_output.play_cue(slug)

    def _leg_task_dead(self, task: "asyncio.Task[None]") -> bool:
        """A leg or manual-mic consumer task that exited on its own — not
        via cancellation, and not by returning cleanly because
        `_stop_event` was set (the leg loop's normal shutdown path, not a
        death). Shared by `_on_leg_task_done`'s wake.leg_died log, which
        fires for legs and manual mics alike, and `session_status`'s
        `wake_legs_dead`, which only iterates `_leg_tasks` — manual-mic
        tasks never appear there.
        """
        if not task.done() or task.cancelled():
            return False
        return not (task.exception() is None and self._stop_event.is_set())

    def _on_leg_task_done(
        self, name: str, task: "asyncio.Task[None]",
    ) -> None:
        """Log a leg/manual-mic consumer loop dying unexpectedly.

        Cancellation (shutdown) is not a death, and neither is returning
        cleanly once `_stop_event` is set — `_leg_task_dead` is the one
        place that rule lives. No restart, no cue — just the event, so a
        leg going deaf is visible in the journal instead of surfacing only
        as asyncio's silent "exception never retrieved" warning at task
        GC. An `asyncio.TaskGroup` would cancel every sibling leg the
        instant one fails; a dead leg must not take the others down, so
        each task is tracked and reaped independently instead.
        """
        if not self._leg_task_dead(task):
            return
        exc = task.exception()
        if exc is None:
            log_event(
                logger, "wake.leg_died", leg=name, exc_type="none",
                level=logging.WARNING,
            )
            return
        log_event(
            logger, "wake.leg_died", leg=name, exc_type=type(exc).__name__,
            level=logging.WARNING,
        )

    async def run(self) -> None:
        # One wake-only consumer per non-primary leg; the primary "on" leg
        # is driven by this method's main loop below. A leg is in
        # _wake_legs.legs only when both its mic and detector were
        # configured, so there is no misconfiguration case to warn about.
        self._leg_tasks = {}
        for _leg_name in self._wake_legs.legs:
            if _leg_name == "on":
                continue
            _leg_task = asyncio.create_task(
                self._wake_leg_loop(_leg_name),
                name=f"wake-leg-{_leg_name}",
            )
            _leg_task.add_done_callback(
                lambda _t, _name=_leg_name: self._on_leg_task_done(_name, _t)
            )
            self._leg_tasks[_leg_name] = _leg_task
        self._manual_tasks = []
        for _source_id in self._push_to_talk.sources:
            _manual_task = asyncio.create_task(
                self._manual_mic_loop(_source_id),
                name=f"manual-mic-{_source_id}",
            )
            _manual_task.add_done_callback(
                lambda _t, _name=_source_id: self._on_leg_task_done(_name, _t)
            )
            self._manual_tasks.append(_manual_task)
        if self._leg_tasks:
            logger.info(
                "multi-leg wake enabled: %s",
                " + ".join(self._wake_legs.legs.keys()),
            )
        if self._manual_tasks:
            log_event(
                logger,
                "manual_mic.sources_enabled",
                sources=",".join(sorted(self._push_to_talk.sources)),
            )
        # A push-to-talk-only speaker has no primary mic to iterate, so the
        # heartbeat loses its usual liveness proof (a mic frame is evidence
        # both capture AND the async loop are alive). A keepalive tick
        # still proves the loop is iterating; audio arrives on the
        # manual-mic loops instead. Ticks yield None so the frame body
        # below skips them.
        #
        # Branches on `_push_to_talk.only`, not on `_mic is None`, so the
        # mode has ONE derivation. The two agree on any daemon that
        # started: `_require_usable_input` (jasper/voice/daemon_main.py)
        # refuses to run with neither a wake leg nor a manual mic. If that
        # invariant broke, raising beats keepalive-ing — a daemon patting
        # its watchdog with no input at all is a deaf speaker that looks
        # healthy.
        if self._push_to_talk.only:
            _frames = keepalive_ticks()
            log_event(
                logger,
                "voice.push_to_talk_only",
                sources=",".join(sorted(self._push_to_talk.sources)),
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
                    if self._turns.state is State.SESSION:
                        await self._turns.end("stopping")
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
                    self._input_suspended.add("on")
                    continue

                # User has muted the mic. Drain the frame (don't backpressure
                # the AEC bridge / mic capture upstream) but skip wake
                # detection and session forwarding entirely. No pre-roll
                # append either — when unmuted, the user's first "Hey Jarvis"
                # is the natural start of their utterance; carrying a mute-
                # era pre-roll would prepend silence (or whatever room
                # ambience leaked through) to the next turn.
                if self._mic_muted:
                    self._input_suspended.add("on")
                    continue

                captured_at, gap = self._capture_input(self._mic, "on")
                if captured_at < self._input_admit_after:
                    continue
                self._pre_roll.append(frame)
                # Independent capture ring for wake-event telemetry —
                # sized for the 6 s offline-review window, not the 560 ms
                # turn-open window. Filled in both states so the pre-fire
                # context is already on hand the moment a wake fires.
                self._wake_legs.capture_ring_on.append(frame)

                if self._acquiring:
                    if self._push_to_talk.active_source is None:
                        self._acquire_buffer.append(frame, captured_at, discontinuity=gap)
                    continue

                if self._turns.state is State.WAKE:
                    await self._handle_wake_frame(frame, leg="on")
                else:
                    if self._push_to_talk.active_source is not None:
                        continue
                    await self._handle_session_frame(frame, captured_at=captured_at)
        finally:
            # Cancel + join every leg loop before sweeping tracked side-work.
            # The leg loops are producers: while they are alive, a late wake
            # frame can still enqueue acquire/finalize tasks into
            # _fire_and_forget. Stop producers first so the cancellation sweep
            # below observes every task created during shutdown.
            _all_tasks = (
                *self._leg_tasks.values(), *self._manual_tasks,
            )
            for _t in _all_tasks:
                _t.cancel()
            for _t in _all_tasks:
                try:
                    await _t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # Empty both containers so session_status's wake_legs falls back
            # to the configured list, matching its state before run() started.
            self._leg_tasks = {}
            self._manual_tasks = []
            await self._cancel_fire_and_forget_tasks()

    async def _manual_mic_loop(self, source_id: str) -> None:
        """Session-audio consumer for one push-to-talk mic source."""
        rt = self._push_to_talk.sources[source_id]
        async for frame in rt.mic.frames():
            if self._stop_event.is_set():
                return
            if self._measurement_active.is_set() or self._mic_muted:
                self._input_suspended.add(source_id)
                continue
            if self._push_to_talk.active_source != source_id:
                continue
            captured_at, gap = self._capture_input(rt.mic, source_id)
            if captured_at < self._input_admit_after:
                continue
            if self._acquiring:
                self._acquire_buffer.append(frame, captured_at, discontinuity=gap)
                continue
            if self._turns.state is State.SESSION:
                await self._handle_session_frame(frame, captured_at=captured_at)

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
        rt = self._wake_legs.legs[leg_name]
        async for frame in rt.mic.frames():
            if self._stop_event.is_set():
                return
            if self._measurement_active.is_set():
                self._input_suspended.add(leg_name)
                continue
            # Mute is a privacy promise — do NOT record audio for the
            # wake-events corpus when the user has muted the mic. Mirrors
            # the primary loop: the capture ring fills only AFTER the
            # mute / measurement gates.
            if self._mic_muted:
                self._input_suspended.add(leg_name)
                continue
            captured_at, _ = self._capture_input(rt.mic, leg_name)
            if captured_at < self._input_admit_after:
                continue
            # Filled before the acquiring / WAKE-state checks so a wake
            # fire's window has pre-fire context even when it overlaps the
            # turn-open window.
            if rt.capture_ring is not None:
                rt.capture_ring.append(frame)
            if self._acquiring:
                continue
            if self._turns.state is State.WAKE:
                await self._handle_wake_frame(frame, leg=leg_name)
            elif self._turns.state is State.SESSION and rt.shadow_vad is not None:
                await self._shadow_vad_score_raw(frame)

    def _note_input_age(self, captured_at: float) -> None:
        self._input_last_age_ms = max(0, round((time.monotonic() - captured_at) * 1000))
        self._input_max_age_ms = max(self._input_max_age_ms, self._input_last_age_ms)

    def _capture_input(self, mic, source: str) -> tuple[float, bool]:
        captured = getattr(mic, "last_frame", None)
        captured_at = captured.captured_at if captured is not None else time.monotonic()
        if source == (self._push_to_talk.active_source or "on"):
            self._note_input_age(captured_at)
        gap = bool(captured and captured.discontinuity) or source in self._input_suspended
        self._input_suspended.discard(source)
        if gap:
            self._input_gaps += 1
            self._wake_legs.reset_leg(source)
            if source == "on":
                self._pre_roll.clear()
            if not self._acquiring and source == (self._push_to_talk.active_source or "on"):
                self._reset_session_input()
            log_event(logger, "voice.input_gap", source=source,
                      dropped_frames=getattr(mic, "dropped_frames", 0))
        return captured_at, gap

    def _reset_session_input(self) -> None:
        if self._vad is not None:
            self._vad.reset()
        self._speech_run_started_at = self._silence_started_at = 0.0
        self._speech_run_max_silero = 0.0
        self._barge_in_run_started_at = self._barge_in_run_peak = 0.0
        self._barge_in_signalled_this_run = False

    def _reset_turn_input(self) -> None:
        """Both VADs. The mid-session resets re-arm the primary leg only."""
        self._reset_session_input()
        if self._vad_off is not None:
            self._vad_off.reset()

    async def _drain_inflight_output(self, *, timeout_sec: float) -> bool:
        return await self._assistant_output.drain_inflight(
            timeout_sec=timeout_sec,
        )

    async def _play_mute_click(self, *, going_on: bool) -> None:
        await self._assistant_output.play_mute_click(going_on=going_on)

    def _play_listening_chirp(self, *, going_on: bool) -> Coroutine[object, object, None]:
        return self._assistant_output.listening_chirp(
            going_on=going_on,
            on_attempt=self._turn_timeline.observer("cue_attempt") if going_on else None,
            on_first_write=self._turn_timeline.observer("cue_accepted") if going_on else None,
        )

    async def _prepare_assistant_loudness_context(self) -> None:
        await self._assistant_output.prepare_loudness()

    def _invalidate_input(self, reason: str) -> None:
        if self._turns.turn is not None:
            self._turns.turn.discard_input()
        self._input_admit_after = time.monotonic()
        self._input_invalidation_reason = reason
        self._input_suspended.update(self._wake_legs.legs)
        self._input_suspended.update(self._push_to_talk.sources)
        self._pre_roll.clear()
        self._frozen_pre_roll = None
        self._acquire_buffer.clear()
        self._wake_legs.clear_rings()

    async def mute_mic(self) -> str:
        """Pause input and end the active turn; repeated calls are no-ops."""
        if self._mic_muted:
            return "ok"
        self._mic_muted = True
        self._invalidate_input("MUTED")
        if self._turns.state is State.SESSION:
            try:
                await self._turns.end("mic_muted")
            except Exception as e:  # noqa: BLE001
                logger.warning("ending turn on mic mute: %s", e)
        write_mic_muted(self._cfg.mic_mute_state_path, True)
        log_event(logger, "mic.mute")
        await self._play_mute_click(going_on=False)
        return "ok"

    async def unmute_mic(self) -> str:
        """Resume listening. Idempotent."""
        if not self._mic_muted:
            return "ok"
        self._mic_muted = False
        self._invalidate_input("MUTED")
        write_mic_muted(self._cfg.mic_mute_state_path, False)
        log_event(logger, "mic.unmute")
        await self._play_mute_click(going_on=True)
        return "ok"

    def _read_music_dbfs(self) -> float | None:
        """Most-recent playback RMS in dBFS, or None when unavailable.

        Cheap cached read, no async I/O, so it is safe on the wake hot path.
        """
        return self._content_activity.music_dbfs

    async def _handle_wake_frame(self, frame, *, leg: str = "on") -> None:
        """Dispatch one WAKE-state frame to the leg fan-in, and on a fire
        hand the acquire window to `_arbitrate_acquire_drain`.

        The fan-in owns the decision (which leg heard it, with what
        evidence); what is left here is what a fire costs this loop — the
        pre-roll freeze that preserves the user's first phoneme, and the
        background task that opens the turn."""
        fire = await self._wake_legs.score_frame(
            frame,
            leg=leg,
            capture_event=self._wake_telemetry.store is not None,
        )
        if fire is None:
            return

        self._wake_event_at_monotonic = fire.at_monotonic
        self._frozen_pre_roll = tuple(self._pre_roll)
        self._acquire_input_epoch = self._input_admit_after
        self._acquiring = True
        self._acquire_buffer.clear()

        # In peering mode `can_serve` is broadcast in the WAKE message so the
        # fleet's ranking function can prefer a peer that can serve. We bid
        # even when blocked, so exactly one peer plays the failure cue when
        # every peer is blocked; that cue plays below only if we win
        # arbitration and cannot serve.
        spend_allowed = self._spend_cap.allowed()
        conn_paused = self._connection.is_paused()
        can_serve = spend_allowed and not conn_paused

        # Background task so the main mic loop stays responsive while
        # input continues to enter the bounded acquire buffer.
        self._create_fire_and_forget_task(
            self._arbitrate_acquire_drain(
                score=fire.score,
                rms_dbfs=fire.rms_dbfs,
                spend_allowed=spend_allowed,
                conn_paused=conn_paused,
                can_serve=can_serve,
                wake_event=fire.wake_event,
            ),
            name="wake-arbitrate-acquire-drain",
        )

    def bind_tool_dispatch(self) -> Callable[[str, str], Awaitable[None]]:
        return self._wake_telemetry.bind_tool_dispatch()

    async def _arbitrate_acquire_drain(
        self,
        *,
        score: float,
        rms_dbfs: float | None,
        spend_allowed: bool,
        conn_paused: bool,
        can_serve: bool,
        wake_event: dict | None = None,
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
            # mute_mic / MeasurementHold.pause_response can fire after
            # _handle_wake_frame spawned this task but before it is scheduled.
            # Both are user-deliberate "stop listening" signals; a chirp plus
            # an LLM session after them is wrong. Checked twice — now, and
            # again after the arbitration await, which can take up to 500 ms.
            if self._wake_late_cancelled("pre_arb"):
                await self._wake_telemetry.stage("late_cancel")
                await self._wake_telemetry.outcome("late_cancel", "pre_arb")
                return  # finally clears _acquiring + buffer

            self._check_input_admission(self._acquire_input_epoch)
            if wake_event is not None:
                event_id = await self._wake_telemetry.on_fire(**wake_event)
                if event_id is not None:
                    self._create_fire_and_forget_task(
                        self._wake_telemetry.finalize_event_audio(
                            event_id, snapshot=self._wake_legs.snapshot,
                        ),
                        name="wake-event-audio-finalize",
                    )

            decision = await self._peering.arbitrate(
                score=score, snr_db=None, rms_dbfs=rms_dbfs,
                can_serve=can_serve,
            )
            if decision == "LOSE":
                # Another peer is handling it: losers play no chirp or cue.
                log_event(logger, "peering.wake.lost", score=f"{score:.2f}")
                await self._wake_telemetry.stage("peer_lost")
                await self._wake_telemetry.outcome("peer_lost")
                return  # finally clears _acquiring + buffer

            if self._wake_late_cancelled("post_arb"):
                await self._wake_telemetry.stage("late_cancel")
                await self._wake_telemetry.outcome("late_cancel", "post_arb")
                return

            # Gate cues: only the arbitration winner pays this cost.
            if not spend_allowed:
                log_event(logger, "wake.refused", reason="spend_cap_reached")
                await self._wake_telemetry.stage("gate_blocked")
                await self._wake_telemetry.outcome("gate_blocked", "spend_cap_reached")
                await self._play_cue("spend_cap_reached")
                return
            # `conn_paused` was snapshotted before arbitration. Re-check
            # with a bounded wait: a planned session rotation is a pause
            # that clears on its own, and answering a wake with a
            # "can't connect" cue during one would be a false alarm.
            if conn_paused and not await self._await_connection(
                PAUSED_CONNECTION_WAIT_SEC,
            ):
                log_event(logger, "wake.refused", reason="connection_paused")
                await self._wake_telemetry.stage("gate_blocked")
                await self._wake_telemetry.outcome("gate_blocked", "connection_paused")
                # The cue comes first: it is the household's answer, and
                # nothing after it may be allowed to swallow it. The
                # early-retry nudge already went out with the wait.
                await self._play_cue(self._connection.wake_cue())
                return

            await self._begin_turn(
                listening_feedback=True,
                anchor_at=self._wake_event_at_monotonic,
            )  # ends with state = SESSION
            await self._wake_telemetry.stage("turn_opened")
            # Starts the winner-only heartbeat. Fire-and-forget: voice's own
            # session lifecycle is the source of truth.
            await self._peering.session_started(has_turn=self._turns.turn is not None)

            await self._drain_acquire_audio()
        except InputAdmissionClosed:
            await self._wake_telemetry.stage("late_cancel")
            await self._wake_telemetry.outcome("late_cancel", "acquire")
        except Exception as e:  # noqa: BLE001
            logger.exception("turn acquire failed: %s", e)
            log_event(
                logger,
                "wake.refused",
                reason="acquire_error",
                exc_type=type(e).__name__,
            )
            await self._wake_telemetry.outcome("session_failed", str(e)[:200])
            # A connection cue here is a false alarm unless the connection
            # actually dropped mid-acquire; see the internal_error CueDef.
            try:
                if self._turns.output_episode is not None:
                    await self._turns.cleanup_after_failed_begin()
            except Exception as cleanup_error:  # noqa: BLE001
                logger.warning(
                    "turn acquire cleanup failed before failure cue: %s",
                    cleanup_error,
                )
            if self._connection.is_paused() or self._connection.last_failure_detail():
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
            self._acquire_buffer.clear()
            self._frozen_pre_roll = None
            # Protects against the detector re-firing on the TTS tail (won
            # path) or on a quick repeat-wake (lost path).
            self._wake_legs.refractory_until = max(
                self._wake_legs.refractory_until,
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

    async def _handle_playback_frame(self, frame, *, captured_at: float | None = None) -> None:
        """In-session barge-in detection while the assistant is speaking.

        Reached from ``_handle_session_frame`` once ``_input_ended`` is set
        AND barge-in is active for the turn. Runs local Silero VAD on the
        AEC-cleaned "on" leg — the same ``frame`` the live session consumed
        (leg selection, NOT an AEC topology change) — and, on a sustained
        speech run at or above ``JASPER_VAD_BARGE_IN_THRESHOLD``, sets the
        turn's interrupt event so ``play_responses`` flushes local TTS
        immediately. The felt experience: the user talks over the assistant
        and the speaker goes quiet.

        Runs INLINE (never a ``_bg_task``): completed ``_bg_tasks`` end the
        turn, so a fire-once detector task would race turn-end."""
        if self._turns.turn is None:
            return
        # Same primary-leg Silero the in-session EOU detector scores; a
        # predict error propagates exactly as it does there (unguarded)
        # rather than being silently swallowed here.
        speech_prob = self._vad.predict(frame)
        now = time.monotonic() if captured_at is None else captured_at
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
        self._signal_barge_in(silero=self._barge_in_run_peak, sustained=sustained)

    def _signal_barge_in(self, *, silero: float, sustained: float) -> None:
        """Flush local TTS for one detected barge-in, and record that it fired.

        Both endpointer paths score the same AEC-cleaned "on" leg, so both
        report it identically: a barge-in only one of them counts is one
        nobody can debug from /state or the journal.

        A turn whose provider owns acoustic interruption gets neither — it
        stops itself when the user really talks over it, and what the local
        detector scored here is as likely to be the assistant's own echo.
        """
        if self._turns.turn.owns_interruption:
            return
        self._barge_in_count += 1
        self._barge_in_last_leg = "on"
        self._barge_in_last_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        )
        log_event(
            logger,
            "barge.detected",
            leg="on",
            silero=f"{silero:.2f}",
            sustained_ms=int(sustained * 1000),
            # Durable (needs_client_truncate) vs cosmetic (server_self_truncates,
            # where a real-time provider may resume) — see _barge_in_reconcile.
            reconcile=self._barge_in_reconcile.value,
        )
        # Set the turn's interrupt event. play_responses is awaiting
        # wait_for_interrupt.
        self._turns.turn.request_local_interrupt()

    async def _send_session_audio(self, frame) -> None:
        """Forward one frame to the live turn; end the turn if it refuses.

        One implementation for both endpointer paths (local Silero,
        push-to-talk), so the failure handling cannot drift between
        them.
        """
        self._turn_timeline.stamp("first_audio_to_provider")
        try:
            await self._turns.turn.send_audio(frame.tobytes())
        except Exception as e:  # noqa: BLE001
            logger.warning("send_audio failed (will end turn): %s", e)
            await self._turns.end()

    async def _end_session_input(self, where: str) -> None:
        """Close the user's input side: mark it ended and tell the turn.

        ``where`` names the caller in the failure log so a stuck
        ``end_input`` is attributable to end-of-utterance, the hard cap,
        or the push-to-talk cap without a stack trace.
        """
        self._turns.input_ended = True
        self._turn_timeline.stamp("end_input")
        try:
            await self._turns.turn.end_input()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "end_input failed at %s (will end turn): %s", where, e,
            )
            await self._turns.end()

    async def _handle_manual_session_frame(self, frame, *, captured_at: float | None = None) -> None:
        now = time.monotonic() if captured_at is None else captured_at
        if self._push_to_talk.hold_cap_exceeded(
            now - self._turns.started_at_loop, self._cfg.idle_timeout_sec,
        ):
            await self._end_session_input("push-to-talk hold cap")
            return
        if self._turns.turn is not None and self._turns.turn.continuous_input:
            if not self._turns.continuous_speech_started:
                self._turns.continuous_speech_started = time.monotonic()
            self._turns.continuous_last_speech = time.monotonic()
            self._turns.user_speech_seen = True
        await self._send_session_audio(frame)

    async def _handle_session_frame(self, frame, *, captured_at: float | None = None) -> None:
        if self._mic_muted or self._measurement_active.is_set():
            return
        if captured_at is not None:
            self._note_input_age(captured_at)
        if self._turns.conversation_end_requested:
            await self._turns.end("conversation_ended")
            return
        if reason := self._turns.background_end_reason():
            await self._turns.finish_response(reason)
            return
        assert self._turns.turn is not None
        if self._turns.turn.continuous_input and not self._turns.manual_endpoint_this_turn:
            await self._handle_continuous_frame(frame, captured_at=captured_at)
            return
        if self._turns.input_ended:
            if self._turns.barge_in_active:
                await self._handle_playback_frame(frame, captured_at=captured_at)
            return
        if self._turns.manual_endpoint_this_turn:
            await self._handle_manual_session_frame(frame, captured_at=captured_at)
            return

        speech_prob = self._vad.predict(frame)
        self._turns.max_silero_aec = max(self._turns.max_silero_aec, speech_prob)
        now = time.monotonic() if captured_at is None else captured_at
        elapsed = now - self._turns.started_at_loop
        if not self._turns.user_speech_seen and elapsed >= NO_SPEECH_ABORT_SEC:
            log_event(logger, "voice.no_speech", max_silero=self._turns.max_silero_aec)
            await self._turns.end()
            return
        if elapsed >= HARD_RECORDING_CAP_SEC:
            await self._end_session_input("cap")
            return

        armed = self._sustained_run(speech_prob, END_OF_UTTERANCE_SPEECH_THRESHOLD, now)
        if speech_prob >= END_OF_UTTERANCE_SPEECH_THRESHOLD:
            if armed and not self._turns.user_speech_seen:
                self._turns.user_speech_seen = True
                self._turns.silero_aec_armed_at_ms = int(elapsed * 1000)
                await self._wake_telemetry.stage("speech_detected")
            self._silence_started_at = 0.0
        elif self._turns.user_speech_seen:
            if self._silence_started_at == 0.0:
                self._silence_started_at = now
                self._turn_timeline.stamp("speech_end", first=False)
            elif now - self._silence_started_at >= END_OF_UTTERANCE_SILENCE_SEC:
                await self._end_session_input("end-of-utterance")
                return
        await self._send_session_audio(frame)

    async def _handle_continuous_frame(self, frame, *, captured_at=None) -> None:
        now = time.monotonic()
        speaking = (self._turns.turn.audio_chunks_pending() > 0
                    or self._tts.expected_drain_at() > now)
        if speaking and not self._barge_in_reference_available:
            # Digital silence keeps the HOST's own endpointer off this
            # unreferenced echo, but a turn that owns interruption is only
            # stoppable by the provider's VAD, which needs the real room.
            if self._turns.turn.owns_interruption:
                await self._send_session_audio(frame)
            else:
                await self._turns.turn.send_audio(bytes(frame.nbytes))
            return
        score = self._vad.predict(frame)
        threshold = self._cfg.vad_barge_in_threshold if speaking else END_OF_UTTERANCE_SPEECH_THRESHOLD
        self._turns.max_silero_aec = max(self._turns.max_silero_aec, score)
        armed = self._sustained_run(score, threshold, now)
        if score >= threshold:
            if armed:
                if not self._turns.continuous_speech_started or now - self._turns.continuous_last_speech >= END_OF_UTTERANCE_SILENCE_SEC:
                    self._turns.continuous_speech_started = self._speech_run_started_at
                    if speaking and self._turns.barge_in_active:
                        self._signal_barge_in(
                            silero=self._speech_run_max_silero,
                            sustained=now - self._speech_run_started_at,
                        )
                self._turns.continuous_last_speech = now
                self._turns.user_speech_seen = True
                self._turns.input_ended = False
        elif (self._turns.user_speech_seen and not self._turns.input_ended
                and now - self._turns.continuous_last_speech >= END_OF_UTTERANCE_SILENCE_SEC):
            await self._end_session_input("continuous speech pause")
        await self._send_session_audio(frame)

    async def _drain_acquire_audio(self) -> tuple[int, bool]:
        count = 0
        while self._turns.turn is not None and not self._turns.input_ended:
            if self._mic_muted or self._measurement_active.is_set():
                break
            frame = self._acquire_buffer.pop()
            drops = self._acquire_buffer.dropped_frames
            if drops != self._acquire_drops_reported:
                self._input_gaps += 1
                self._acquire_drops_reported = drops
                self._reset_session_input()
                log_event(logger, "voice.input_gap", source="acquire", dropped_frames=drops)
            elif frame is not None and frame.discontinuity:
                self._reset_session_input()
            if frame is None:
                break
            await self._handle_session_frame(frame.pcm, captured_at=frame.captured_at)
            count += 1
        if self._acquire_buffer:
            self._reset_session_input()
        self._acquire_buffer.clear()
        return count, self._turns.user_speech_seen

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

    def _spawn_manual_refusal_cue(self, slug: str) -> None:
        """Play a manual-start refusal cue without holding up the reply.

        `manual_session_start` answers a control-socket command whose
        caller times out at 5 s (`jasper/platform/uds.py`), while a cue runs ~6 s
        with duck and drain; awaiting one here answers the button with a
        503 and writes the real result to a closed socket.
        """
        self._create_fire_and_forget_task(
            self._play_cue(slug), name=f"manual-refusal-cue:{slug}",
        )

    async def manual_session_start(self, source: str | None = None) -> str:
        """Trigger a voice session from external IPC (remote hold-to-talk).
        Bypasses the openWakeWord trigger but honors the same gates
        wake does: the user-deliberate stop-listening signals
        (mic-mute, room-correction measurement window), spend cap, and
        connection-paused. Returns one of
        OK / BUSY / MUTED / MEASURING / CAP / PAUSED / UNKNOWN_SOURCE /
        NO_ROOM_MIC / ERROR for the caller's logging.
        """
        if source and source not in self._push_to_talk.sources:
            log_event(
                logger,
                "session.manual_refused",
                reason="unknown_source",
                source=source,
            )
            return "UNKNOWN_SOURCE"
        if source is None and self._push_to_talk.only:
            # No source named, and this speaker has no always-listening mic to
            # be the implied one — the push-to-talk-only shape (issue #2205).
            # Accepting would open a turn nothing can feed: `_pre_roll` is
            # empty because no primary loop fills it, `_manual_mic_loop` drops
            # every frame while `active_source` is None, and the turn
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
                sources=",".join(sorted(self._push_to_talk.sources)) or "<none>",
                detail=(
                    "this speaker has no always-listening microphone; "
                    "start the turn with a push-to-talk source id"
                ),
                level=logging.WARNING,
            )
            self._spawn_manual_refusal_cue(NO_ROOM_MIC_CUE_SLUG)
            return "NO_ROOM_MIC"
        if self._turns.state is State.SESSION or self._acquiring:
            log_event(logger, "session.manual_refused", reason="busy")
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
            log_event(
                logger,
                "session.manual_refused",
                reason="spend_cap_reached",
            )
            self._spawn_manual_refusal_cue("spend_cap_reached")
            return "CAP"
        self._push_to_talk.active_source = source
        self._frozen_pre_roll = () if source else tuple(self._pre_roll)
        self._input_admit_after = time.monotonic()
        self._acquire_input_epoch = self._input_admit_after
        self._acquiring = True
        self._acquire_buffer.clear()
        try:
            if self._connection.is_paused() and not await self._await_connection(
                PAUSED_CONNECTION_WAIT_SEC,
            ):
                # Still paused after the wait: this is a real outage, not a
                # rotation. Cued — a press that produces nothing is the
                # one refusal the household cannot explain to itself.
                log_event(
                    logger,
                    "session.manual_refused",
                    reason="connection_paused",
                    waited_sec=PAUSED_CONNECTION_WAIT_SEC,
                )
                self._spawn_manual_refusal_cue(self._connection.wake_cue())
                self._push_to_talk.active_source = None
                return "PAUSED"
            if source:
                await self._begin_turn(
                    pre_roll=False,
                    listening_feedback=True,
                )
            else:
                await self._begin_turn(listening_feedback=True)
            drained, _ = await self._drain_acquire_audio()
            if source:
                if drained:
                    log_event(
                        logger,
                        "manual_mic.acquire_drained",
                        source=source,
                        frames=drained,
                    )
            log_event(
                logger,
                "session.manual_started",
                source=source or "primary",
            )
            return "OK"
        except InputAdmissionClosed as error:
            return error.result
        except Exception as e:  # noqa: BLE001
            logger.exception("manual session start failed: %s", e)
            try:
                if self._turns.output_episode is not None:
                    await self._turns.cleanup_after_failed_begin()
            except Exception as cleanup_error:  # noqa: BLE001
                # `_release_failed_turn` re-raises a stored BaseException
                # after every phase runs; without this try/except that
                # escape skips both refusal-cue branches below and the
                # button press goes unanswered.
                log_event(
                    logger,
                    "turn.begin_cleanup_failed",
                    level=logging.ERROR,
                    exc_type=type(cleanup_error).__name__,
                    err=str(cleanup_error),
                )
            # A turn that died because the connection went down between
            # the paused gate above and here (the idle context reset
            # reopens inside `_begin_turn`) must still answer the press
            # — same condition and cue as the wake path's acquire
            # failure. See `_arbitrate_acquire_drain`.
            if self._connection.is_paused() or self._connection.last_failure_detail():
                self._spawn_manual_refusal_cue(self._connection.wake_cue())
            else:
                self._spawn_manual_refusal_cue(INTERNAL_ERROR_CUE_SLUG)
            return "ERROR"
        finally:
            self._acquiring = False
            self._acquire_buffer.clear()
            self._frozen_pre_roll = None

    async def manual_session_end(self) -> str:
        """Finalize the input side of an in-progress session (remote
        button release). This is the same operation the silence
        detector performs at end-of-utterance: send activity_end so
        Gemini stops listening and starts responding.
        """
        if self._turns.state is not State.SESSION or self._turns.turn is None:
            return "NO_SESSION"
        if self._turns.input_ended:
            return "OK"
        await self._end_session_input("push-to-talk release")
        return "OK"

    def session_status(self) -> dict:
        """Diagnostic snapshot — exposed via the control socket so
        jasper-control clients can render correct state without polling
        the spend-cap or connection state separately.

        ``camilla_volume_locked`` is the authoritative cross-daemon signal
        for whether a remote/web-slider Camilla write must be deferred. Fan-in
        can duck program audio while leaving this false, so ``duck_active``
        remains user-facing session telemetry rather than a volume lock.
        """
        # Legs whose consumer loop is alive right now, not merely
        # configured — /aec reports configured intent from aec_mode.env.
        live_wake_legs = [
            leg for leg in self._wake_legs.legs
            if leg == "on"
            or leg not in self._leg_tasks
            or not self._leg_tasks[leg].done()
        ]
        _wake_legs_dead = [
            leg for leg, task in self._leg_tasks.items()
            if self._leg_task_dead(task)
        ]
        # Neither gate feeds the fan-in's condition refresh (see the
        # dispatch sites in run() / _manual_mic_loop / _wake_leg_loop), so
        # the level fields below go stale, not just missing, while either
        # is set.
        mic_feeding = not (self._mic_muted or self._measurement_active.is_set())
        return {
            "state": self._turns.state.name,
            "input_ended": self._turns.input_ended,
            "input_audio": {
                "last_age_ms": self._input_last_age_ms,
                "max_age_ms": self._input_max_age_ms,
                "gaps": self._input_gaps,
                "acquire_dropped_frames": self._acquire_buffer.dropped_frames,
                "capture_dropped_frames": sum(
                    getattr(rt.mic, "dropped_frames", 0)
                    for rt in (
                        *self._wake_legs.legs.values(),
                        *self._push_to_talk.sources.values(),
                    )
                ),
            },
            "spend_allowed": self._spend_cap.allowed(),
            "followup_timeout_sec": self._cfg.followup_timeout_sec,
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
            "manual_mic_sources": sorted(self._push_to_talk.sources),
            "active_manual_mic_source": self._push_to_talk.active_source,
            # This speaker has no room mic of its own: zero wake legs, every
            # turn opened by an accessory button. Surfaced because it is a
            # mode, not an absence — inferring it from an empty `wake_legs`
            # would read identically to a daemon whose legs all failed to
            # open, the opposite diagnosis. /state.voice.push_to_talk_only
            # reads this field verbatim; jasper-doctor's
            # _push_to_talk_only_speaker re-derives it from the same two
            # published facts (env tri-state + accessory file) so it still
            # reports correctly when jasper-voice is down.
            "push_to_talk_only": self._push_to_talk.only,
            # Who closes the in-flight turn's input — the daemon's own
            # decision, not a re-derivation, since "the remote cut me off" and
            # "the remote never cut me off" are the bug reports it answers.
            # Set at turn start and not cleared at turn end, so while `state`
            # is WAKE it reports the previous turn's mechanism (`input_ended`
            # above has the same shape). Read either alongside `state`.
            "endpointer": self._turns.endpointer_label(),
            # The last COMPLETE turn's `event=turn.timeline` deltas
            # (`anchor` says what ms 0 is). Same not-cleared-at-turn-end
            # shape as `endpointer`; `{}` until this daemon served a turn.
            "last_turn_ms": dict(self._turn_timeline.last_turn_ms),
            "turn_event_id": (
                self._turn_timeline.event_id if self._turn_timeline.anchor else None
            ),
            "wake_event_store": (
                self._wake_telemetry.store.status() if self._wake_telemetry.store else None
            ),
            "music_dbfs": (
                round(self._content_activity.music_dbfs, 1)
                if self._content_activity.music_dbfs is not None else None
            ),
            # Epoch-second floats (never ISO strings), or None before the
            # daemon has seen the signal. last_wake_at is daemon-lifetime
            # and never nulled; the other two read None while mic_feeding
            # is false (see above) rather than a stale frozen value.
            "last_wake_at": self._wake_legs.last_wake_at,
            "idle_rms_dbfs": (
                self._wake_legs.idle_rms_dbfs if mic_feeding else None
            ),
            "input_last_above_floor_at": (
                self._wake_legs.input_last_above_floor_at
                if mic_feeding else None
            ),
            "wake_legs": live_wake_legs,
            "wake_legs_dead": _wake_legs_dead,
            # Per-pack tool-registration outcomes (registered / skipped /
            # failed), same motivation as wake_legs: a tool family that
            # silently failed to build (event=tool_pack.build_failed) is
            # visible in /state.voice + jasper-doctor, not only the journal.
            "tool_packs": self._tool_packs,
            # Turns the model was asked to answer and either answered with
            # nothing or lost the link before finishing. Daemon-lifetime,
            # like barge_in_count_session below.
            "silent_responses_session": self._turns.silent_responses_session,
            "turns_pre_response_capped": self._turns.turns_pre_response_capped,
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
            "cues": self._cues.snapshot() if self._cues is not None else None,
        }

    async def _shadow_vad_score_raw(self, frame) -> None:
        """Score a raw-stream frame through the shadow Silero VAD.

        Pure telemetry — records what raw-stream Silero sees during the
        session but makes no endpointing decisions. The active endpointer
        (AEC-stream Silero) is unaffected."""
        if self._vad_off is None or self._turns.input_ended:
            return
        try:
            speech_prob = self._vad_off.predict(frame)
            if speech_prob > self._turns.max_silero_raw:
                self._turns.max_silero_raw = speech_prob
            if (
                self._turns.silero_raw_armed_at_ms is None
                and speech_prob >= SPEECH_RUN_PEAK_MIN
            ):
                elapsed_ms = int(
                    (asyncio.get_event_loop().time() - self._turns.started_at_loop) * 1000
                )
                self._turns.silero_raw_armed_at_ms = elapsed_ms
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
        anchor_at: float = 0.0,
    ) -> None:
        acquiring_at_begin = self._acquiring
        completed = False
        self._turn_timeline.anchor_at(anchor_at)
        try:
            if acquiring_at_begin:
                self._check_input_admission(self._acquire_input_epoch)
            if listening_feedback:
                # Prime the TTS IPC owner's loudness context before the chirp
                # as well as before assistant TTS. The chirp is fire-and-forget,
                # so waiting for the inner turn prepare would race it back onto
                # the no-context fallback.
                await self._turns.begin_output_episode()
                await self._prepare_assistant_loudness_context()
                # Overlap turn acquisition; output cleanup joins the chirp.
                self._assistant_output.start_turn_feedback(
                    self._turns.output_episode,
                    self._play_listening_chirp(going_on=True),
                )
            await self._turns.begin_inner(
                pre_roll=pre_roll,
                text_context=text_context,
                anchor_at=anchor_at,
            )
            completed = True
        finally:
            if not completed:
                cleanup_error = await capture_cleanup_error(self._turns.cleanup_after_failed_begin)
                if acquiring_at_begin:
                    self._acquiring = False
                if isinstance(cleanup_error, asyncio.CancelledError) and not isinstance(
                    sys.exception(), asyncio.CancelledError,
                ):
                    raise cleanup_error
                if cleanup_error is not None:
                    log_event(
                        logger,
                        "turn.begin_cleanup_failed",
                        level=logging.ERROR,
                        exc_type=type(cleanup_error).__name__,
                        err=str(cleanup_error),
                    )

    def _check_input_admission(self, input_epoch: float) -> None:
        if self._mic_muted:
            raise InputAdmissionClosed("MUTED")
        if self._measurement_active.is_set():
            raise InputAdmissionClosed("MEASURING")
        if input_epoch != self._input_admit_after:
            raise InputAdmissionClosed(self._input_invalidation_reason)


def main() -> None:
    from .voice.daemon_main import main as impl  # lazy: composition root imports WakeLoop
    impl()


if __name__ == "__main__":
    main()
