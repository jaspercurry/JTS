# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timezone
from enum import Enum

from jasper.log_event import log_event

from .audio_buffer import AudioBuffer
from .audio_io import (
    InputDeviceUnavailable,
    MicCapture,
    TtsPlayout,
)
from .wake_events import (
    WakeEventStore,
    make_event_id,
    CAPTURE_PRE_SEC,
    CAPTURE_POST_SEC,
)
from .cues import AudioCueManager
from .cues.registry import NO_ROOM_MIC_CUE_SLUG
from .vad import SpeechVAD
from .wake_legs import LegSpec, wake_input_legs
from .wake_condition_context import AMBIENT_FLOOR_DBFS, classify_condition
from .wake_conditions import DEFAULT_CONDITION
from .wake_fusion import WakeFuser
from .config import Config
from .conversation_history import ConversationStore
from .watchdog import Heartbeat
from .timers import Timer, announcement_text
from .research import ResearchJob, ResearchScheduler
from .usage import (
    SpendCap,
    UsageStore,
)
from .voice.session import LiveConnection, LiveTurn
from .voice.content_activity import ContentActivityTracker
from .voice.conversation_capture import ConversationCapture
from .voice.catalog import InterruptReconcile, resolve_interrupt_reconcile
from .voice.provider_state import read_barge_in_enabled
from .voice.measurement_hold import MeasurementHold
from .voice.peering_client import PeeringClient
from .voice.push_to_talk import (
    HARD_RECORDING_CAP_SEC,
    PTT_KEEPALIVE_INTERVAL_SEC,
    ManualMicRuntime,
    PushToTalk,
    keepalive_ticks,
)
from .voice.research_announcer import HostCondition, ResearchAnnouncer
from .voice.wake_telemetry import LEG_DB, LegFireScore, WakeTelemetry
from .voice.assistant_output import (
    INTERNAL_ERROR_CUE_SLUG,
    AssistantOutput,
    FanInDucker,
    await_output_cleanup_owned,
    capture_cleanup_error,
)
from .voice.output_gate import (
    AssistantOutputEpisode,
    AssistantOutputGate,
)
from .voice.turn_playback import (  # noqa: F401
    idle_watchdog,
    play_responses,
)
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


def track_task(
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


async def cancel_tracked_tasks(task_set: set[asyncio.Task]) -> None:
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


# Acoustic tail margin after the output owner has drained a turn.
WAKE_REFRACTORY_SEC = 0.2

# `_end_turn` reasons the household or the daemon itself chose: whoever
# muted, shut down or spoke over the turn already knows why it went quiet,
# so no failure cue is owed however little the model said.
NO_ANSWER_CUE_SUPPRESSED_REASONS = frozenset({
    "mic_muted",
    "stopping",
    "research_window_wake",
})

# How long a wake or a manual (button) session start waits out a paused
# connection before taking the turn anyway. Most pauses are a planned
# session rotation, whose gap is one teardown plus one connect (p50
# ~350 ms, p99 5.3 s); refusing instantly turns the common ones into
# a dead press and a false 'can't connect' cue. The bound keeps the
# SUCCESS path — which returns as soon as the turn opens — inside
# jasper.platform.control_client.DEFAULT_TIMEOUT (2.0 s) with room for the
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
# the run() wiring site and handed to its LegRuntime.
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

# Published latency stages; absent stages remain absent.
_TURN_TIMELINE_STAGES = (
    "cue_attempt",
    "cue_accepted",
    "first_audio_to_provider",
    "speech_end",
    "end_input",
    "first_response",
    # Hand-off to fan-in: the first assistant PCM write the playout socket
    # accepted. The ring/CamillaDSP/outputd tail past it is not timeable here.
    "first_write",
)


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


class _InputAdmissionClosed(RuntimeError):
    def __init__(self, result: str) -> None:
        self.result = result
        super().__init__(result)


class State(Enum):
    WAKE = "wake"
    SESSION = "session"


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



class LegRuntime:
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


def configured_wake_legs(
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


class _ResearchTurnHost:
    """`ResearchAnnouncer`'s whole view of the wake loop."""

    def __init__(self, loop: "WakeLoop") -> None:
        self._loop = loop

    def condition(self) -> HostCondition:
        loop = self._loop
        return HostCondition(
            in_session=loop._state is State.SESSION,
            in_wake=loop._state is State.WAKE,
            output_active=loop._output_gate.is_active,
            measurement_active=loop._measurement_active.is_set(),
            mic_muted=loop._mic_muted,
            spend_allowed=loop._spend_cap.allowed(),
            connection_paused=loop._connection.is_paused(),
        )

    def turn_episode_active(self) -> bool:
        return self._loop._turn_output_episode is not None

    def hold_wake_refractory(self, sec: float) -> None:
        loop = self._loop
        loop._refractory_until = max(
            loop._refractory_until,
            asyncio.get_event_loop().time() + sec,
        )

    def record_conversation_turn(
        self,
        query: str | None,
        assistant_text: str | None,
        *,
        data_json: dict,
    ) -> None:
        loop = self._loop
        loop._conversation_capture.record(
            query,
            assistant_text,
            data_json=data_json,
            session_id=loop._session_id,
            mic_muted=loop._mic_muted,
        )

    async def play_dynamic_text(self, text: str) -> bool:
        return await self._loop._play_dynamic_text(text)

    async def play_cue(self, slug: str) -> bool:
        return await self._loop._play_cue(slug)

    async def begin_turn(
        self, *, pre_roll: bool, text_context: str | None,
    ) -> None:
        await self._loop._begin_turn(
            pre_roll=pre_roll, text_context=text_context,
        )

    async def end_turn(self, reason: str) -> None:
        await self._loop._end_turn(reason)

    async def cleanup_after_failed_begin(self) -> None:
        await self._loop._cleanup_after_failed_begin()

    async def play_cancel_timeout_cue(self) -> None:
        """Transfer the stalled opener's output and duck to its refusal cue."""
        loop = self._loop
        surrendered = loop._turn_output_episode
        cue_episode = (
            await loop._output_gate.hand_over_if_current(
                surrendered, "admin",
            )
            if surrendered is not None else None
        )
        await loop._play_cue(
            INTERNAL_ERROR_CUE_SLUG, episode=cue_episode,
        )


class WakeLoop:
    """Sole consumer of the primary mic. Dispatches each frame to either
    the wake-word detector (WAKE state) or the active live turn (SESSION
    state).

    `self._legs` holds one `LegRuntime` per configured wake leg (keyed
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
        # Wake-detection legs, keyed by jasper.wake_legs token. Assembled
        # by run(), which opens each leg's mic under the AsyncExitStack
        # and builds its detector, capture ring and — for "off" — a
        # session shadow VAD.
        self._legs: dict[str, LegRuntime] = {
            leg.spec.token: leg for leg in legs
        }
        self._push_to_talk = PushToTalk(
            manual_mics or [], have_wake_legs=bool(self._legs),
        )
        # A configured leg without a LEG_DB telemetry mapping would raise an
        # uncaught KeyError in the wake hot path, where telemetry must be
        # fail-soft; fail at startup instead of at fire time.
        _unmapped = [tok for tok in self._legs if tok not in LEG_DB]
        if _unmapped:
            raise RuntimeError(
                f"wake legs missing a LEG_DB telemetry mapping: "
                f"{sorted(_unmapped)} (add them to LEG_DB in "
                "voice/wake_telemetry.py)"
            )
        # `_on` is absent on a push-to-talk-only speaker: no
        # always-listening microphone, so `configured_wake_legs` planned no
        # legs. Every read site reachable in that mode is None-tolerant —
        # `run()` branches to a keepalive loop, and the capture-ring readers
        # sit behind a wake fire that cannot happen without a detector. The
        # ring still gets a real deque so no reader special-cases it.
        _on = self._legs.get("on")
        self._mic = _on.mic if _on is not None else None
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
        # last_wake_at is daemon-lifetime and never nulled.
        self._last_wake_at: float | None = None
        # Derived by _maybe_refresh_condition; session_status() reads these
        # as None while the mic is not feeding the refresh (muted or a
        # measurement hold), since the refresh stops ticking then.
        self._idle_rms_dbfs: float | None = None
        self._input_last_above_floor_at: float | None = None
        self._connection = connection
        self._turn_output_episode: AssistantOutputEpisode | None = None
        self._content_activity = content_activity
        self._usage_store = usage_store
        self._spend_cap = spend_cap
        self._conversation_capture = ConversationCapture(
            store=conversation_store, voice_provider=cfg.voice_provider,
        )
        self._research = ResearchAnnouncer(host=_ResearchTurnHost(self))
        self._stop_event = stop_event
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
        if self._vad is None and not self._push_to_talk.only:
            self._vad = SpeechVAD()
        # Session-state shadow VAD for the chip-direct ("off") leg, when
        # configured. Created in run() and carried on that leg's
        # LegRuntime.
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

        # Monotonic wallclock at the moment wake fires. Used by
        # _begin_turn to break the wake→activity_start latency into
        # named segments (state reset, loudness prepare, duck,
        # acquire_turn) so a slow turn-acquire can be localized.
        # 0.0 means "no wake yet this session"; replaced on every fire.
        # Only a turn the wake path itself opens reads it (passed as
        # `_begin_turn(anchor_at=...)`), because a wake that opens no turn
        # leaves it set.
        self._wake_event_at_monotonic: float = 0.0
        # Per-turn latency timeline: stage -> time.monotonic(). Reset at
        # turn start; rendered as integer-ms deltas from `_turn_anchor`.
        self._turn_timeline: dict[str, float] = {}
        self._turn_event_id: str | None = None
        self._turn_anchor: float = 0.0
        self._turn_anchor_kind: str = "manual"
        self._last_turn_ms: dict[str, object] = {}

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
        # Turns since daemon start that were asked a question and produced no
        # answer. Published as /state.voice.silent_responses_session.
        self._silent_responses_session: int = 0
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
        self._acquiring: bool = False
        self._acquire_input_epoch = 0.0
        self._acquire_buffer = AudioBuffer()
        self._input_gaps = 0
        self._acquire_drops_reported = 0
        self._input_last_age_ms = 0
        self._input_max_age_ms = 0
        self._input_suspended: set[str] = set()
        self._input_admit_after = 0.0

        self._peering = PeeringClient(
            enabled=cfg.peering_enabled, socket_path=cfg.peering_uds_socket,
        )

    @property
    def _output_gate(self) -> AssistantOutputGate:
        return self._assistant_output.gate

    @property
    def _cfg(self) -> Config:
        return self._assistant_output._cfg

    @property
    def _tts(self) -> TtsPlayout:
        return self._assistant_output._tts

    @property
    def _cues(self) -> AudioCueManager | None:
        return self._assistant_output._cues

    @property
    def _ducker(self) -> FanInDucker:
        return self._assistant_output._ducker

    @property
    def _volume_coordinator(self) -> VolumeCoordinator:
        return self._assistant_output._volume_coordinator

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
        await cancel_tracked_tasks(self._fire_and_forget)

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
        if self._state is State.SESSION:
            return "skipped_session_active"
        if self._output_gate.is_active:
            return self._assistant_output.admission_refusal() or "skipped_output_active"
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
        self._research.set_scheduler(
            scheduler, provider_id=provider_id, model=model,
        )

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

    async def announce_research_ready(self, job: ResearchJob) -> None:
        """Public hook `ResearchScheduler` calls when a job finishes."""
        await self._research.announce_ready(job)

    def record_research_delivery(
        self,
        job: ResearchJob,
        assistant_text: str | None,
        decision: str,
    ) -> None:
        self._research.record_delivery(job, assistant_text, decision)

    def close_conversation_store(self) -> None:
        self._conversation_capture.close()

    async def _play_dynamic_text(self, text: str) -> bool:
        return await self._assistant_output.play_dynamic_text(text)

    async def _play_cue(
        self,
        slug: str,
        *,
        episode: AssistantOutputEpisode | None = None,
    ) -> bool:
        return await self._assistant_output.play_cue(slug, episode=episode)

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
        # self._legs only when both its mic and detector were configured,
        # so there is no misconfiguration case to warn about here.
        self._leg_tasks = {}
        for _leg_name in self._legs:
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
                "multi-leg wake enabled: %s", " + ".join(self._legs.keys()),
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
                    if self._state is State.SESSION:
                        await self._end_turn("stopping")
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
                self._capture_ring_on.append(frame)

                if self._acquiring:
                    if self._push_to_talk.active_source is None:
                        self._acquire_buffer.append(frame, captured_at, discontinuity=gap)
                    continue

                if self._state is State.WAKE:
                    await self._handle_wake_frame(frame, leg="on")
                else:
                    if self._push_to_talk.active_source is not None:
                        continue
                    if self._research.window_active:
                        await self._handle_wake_frame(frame, leg="on")
                        if self._acquiring or self._state is State.WAKE:
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
            if self._state is State.SESSION:
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
        rt = self._legs[leg_name]
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
            if self._state is State.WAKE:
                await self._handle_wake_frame(frame, leg=leg_name)
            elif self._state is State.SESSION and self._research.window_active:
                await self._handle_wake_frame(frame, leg=leg_name)
            elif self._state is State.SESSION and rt.shadow_vad is not None:
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
            rt = self._legs.get(source)
            if rt is not None:
                rt.detector.reset()
                rt.recent_score = rt.recent_score_at = 0.0
                if rt.capture_ring is not None:
                    rt.capture_ring.clear()
                if rt.shadow_vad is not None:
                    rt.shadow_vad.reset()
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

    async def _drain_inflight_output(self, *, timeout_sec: float) -> bool:
        return await self._assistant_output.drain_inflight(
            timeout_sec=timeout_sec,
        )

    async def _play_mute_click(self, *, going_on: bool) -> None:
        await self._assistant_output.play_mute_click(going_on=going_on)

    def _play_listening_chirp(self, *, going_on: bool) -> Coroutine[object, object, None]:
        return self._assistant_output.listening_chirp(
            going_on=going_on,
            on_attempt=self._turn_observer("cue_attempt") if going_on else None,
            on_first_write=self._turn_observer("cue_accepted") if going_on else None,
        )

    async def _prepare_assistant_loudness_context(self) -> None:
        await self._assistant_output.prepare_loudness()

    async def mute_mic(self) -> str:
        """Stop listening: drop mic frames at the wake-loop gate. If a
        voice session is currently active, end the turn first so the
        user gets "stop NOW" semantics rather than the model finishing
        a half-sentence before going silent.

        Idempotent — calling twice is harmless. Always returns "ok".
        """
        if self._mic_muted:
            return "ok"
        self._mic_muted = True
        if self._state is State.SESSION:
            try:
                await self._end_turn("mic_muted")
            except Exception as e:  # noqa: BLE001
                logger.warning("ending turn on mic mute: %s", e)
        # Drop already-buffered room audio, not just future frames. The
        # pre-roll otherwise survives the mute and is replayed into the
        # first turn after unmute (~560 ms of pre-mute room audio sent
        # to the LLM); the telemetry capture rings would likewise write
        # pre-mute audio to disk if a wake fired right after unmute.
        self._pre_roll.clear()
        self._frozen_pre_roll = None
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
        self._input_admit_after = time.monotonic()
        self._input_suspended.update(self._legs)
        self._input_suspended.update(self._push_to_talk.sources)
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
            noise_floor_dbfs = _ring_noise_floor_dbfs(self._capture_ring_on)
            self._current_condition = classify_condition(
                music_dbfs=self._read_music_dbfs(),
                noise_floor_dbfs=noise_floor_dbfs,
            ).condition
            self._idle_rms_dbfs = noise_floor_dbfs
            if noise_floor_dbfs is not None and noise_floor_dbfs > AMBIENT_FLOOR_DBFS:
                self._input_last_above_floor_at = time.time()
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

        self._wake_event_at_monotonic = time.monotonic()
        # Reset ALL detectors after a wake fires. openWakeWord's
        # prediction smoothing keeps recent-activation state across
        # calls; without resetting, the post-fire baseline stays
        # elevated and music vocals or TTS-tail bleed can false-fire on
        # the next listening window. Every leg was elevated by the same
        # user utterance, so reset them all.
        for _other in self._legs.values():
            _other.detector.reset()

        self._frozen_pre_roll = tuple(self._pre_roll)
        self._acquire_input_epoch = self._input_admit_after
        self._acquiring = True
        self._acquire_buffer.clear()
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
        # Marks the wake pipeline alive even if this attempt isn't served.
        self._last_wake_at = time.time()

        # In peering mode `can_serve` is broadcast in the WAKE message so the
        # fleet's ranking function can prefer a peer that can serve. We bid
        # even when blocked, so exactly one peer plays the failure cue when
        # every peer is blocked; that cue plays below only if we win
        # arbitration and cannot serve.
        spend_allowed = self._spend_cap.allowed()
        conn_paused = self._connection.is_paused()
        can_serve = spend_allowed and not conn_paused

        # Tertiary tiebreaker for the peering ranking function. SNR would rank
        # better but needs rolling-noise-floor state nothing tracks; the
        # ranker falls through to RMS when SNR is missing.
        rms_dbfs = _frame_rms_dbfs(frame)

        wake_event = None
        if self._wake_telemetry.store is not None:
            condition_ctx = classify_condition(
                music_dbfs=self._read_music_dbfs(),
                noise_floor_dbfs=_ring_noise_floor_dbfs(self._capture_ring_on),
            )
            wake_event = dict(
                leg=leg,
                score=score,
                now_loop=now_loop,
                legs={
                    _name: LegFireScore(
                        score=_rt.recent_score,
                        score_at=_rt.recent_score_at,
                        # Instantaneous mic RMS at fire-time from the last
                        # frame in this leg's capture ring — separates
                        # low-energy FPs from real attempts in offline
                        # review.
                        mic_rms_dbfs=self._tail_frame_rms_dbfs(
                            _rt.capture_ring,
                        ),
                    )
                    for _name, _rt in self._legs.items()
                },
                firing_threshold=firing_threshold,
                fired_legs=fired_legs,
                condition=condition_ctx,
                mic_muted=self._mic_muted,
            )
        # Background task so the main mic loop stays responsive while
        # input continues to enter the bounded acquire buffer.
        self._create_fire_and_forget_task(
            self._arbitrate_acquire_drain(
                score=score,
                rms_dbfs=rms_dbfs,
                spend_allowed=spend_allowed,
                conn_paused=conn_paused,
                can_serve=can_serve,
                wake_event=wake_event,
            ),
            name="wake-arbitrate-acquire-drain",
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

    def bind_tool_dispatch(self) -> Callable[[str, str], Awaitable[None]]:
        return self._wake_telemetry.bind_tool_dispatch()

    def _turn_observer(
        self, stage: str, *, event_stage: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        timeline, anchor = self._turn_timeline, self._turn_anchor
        event_id = self._turn_event_id

        async def observe() -> None:
            if timeline is not self._turn_timeline or not anchor or anchor != self._turn_anchor:
                return
            self._stamp_turn_stage(stage)
            if event_stage is not None and event_id is not None:
                await self._wake_telemetry.stage(event_stage, event_id=event_id)

        return observe

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
            if not await self._research.cancel_for_wake():
                return
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
                            event_id, snapshot=self._snapshot_leg_audio,
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
            await self._peering.session_started(has_turn=self._turn is not None)

            await self._drain_acquire_audio()
        except _InputAdmissionClosed:
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
            self._acquire_buffer.clear()
            self._frozen_pre_roll = None
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
                    source=self._push_to_talk.active_source or "primary",
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
        if self._turn is None:
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
        # Set the turn's interrupt event. play_responses is awaiting
        # wait_for_interrupt.
        self._turn.request_local_interrupt()

    async def _send_session_audio(self, frame) -> None:
        """Forward one frame to the live turn; end the turn if it refuses.

        One implementation for both endpointer paths (local Silero,
        push-to-talk), so the failure handling cannot drift between
        them.
        """
        self._stamp_turn_stage("first_audio_to_provider")
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
        self._stamp_turn_stage("end_input")
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
        return "silero_aec"

    def _corpus_endpointer_label(self, *, user_speech_seen: bool) -> str:
        """The wake-events ``endpointer`` value for the finished turn.

        Same vocabulary as ``_endpointer_label`` plus ``no_speech_abort``,
        which is a verdict about *listening* and so only meaningful when
        something was listening for speech. Keyed on the resolved label so
        that if button turns ever gain corpus rows, one cannot be recorded
        as a no-speech abort it never performed.
        """
        label = self._endpointer_label()
        if label == "silero_aec" and not user_speech_seen:
            return "no_speech_abort"
        return label

    async def _handle_manual_session_frame(self, frame, *, captured_at: float | None = None) -> None:
        now = time.monotonic() if captured_at is None else captured_at
        if self._push_to_talk.hold_cap_exceeded(
            now - self._turn_started_at_loop, self._cfg.idle_timeout_sec,
        ):
            await self._end_session_input("push-to-talk hold cap")
            return
        await self._send_session_audio(frame)

    async def _handle_session_frame(self, frame, *, captured_at: float | None = None) -> None:
        if self._mic_muted or self._measurement_active.is_set():
            return
        if captured_at is not None:
            self._note_input_age(captured_at)
        if any(t.done() for t in self._bg_tasks):
            await self._end_turn()
            return
        assert self._turn is not None
        if self._input_ended:
            if self._barge_in_active:
                await self._handle_playback_frame(frame, captured_at=captured_at)
            return
        if self._manual_endpoint_this_turn:
            await self._handle_manual_session_frame(frame, captured_at=captured_at)
            return

        speech_prob = self._vad.predict(frame)
        self._max_silero_score_in_turn = max(self._max_silero_score_in_turn, speech_prob)
        now = time.monotonic() if captured_at is None else captured_at
        elapsed = now - self._turn_started_at_loop
        if not self._user_speech_seen and elapsed >= NO_SPEECH_ABORT_SEC:
            log_event(logger, "voice.no_speech", max_silero=self._max_silero_score_in_turn)
            await self._end_turn()
            return
        if elapsed >= HARD_RECORDING_CAP_SEC:
            await self._end_session_input("cap")
            return

        if speech_prob >= END_OF_UTTERANCE_SPEECH_THRESHOLD:
            if self._speech_run_started_at == 0.0:
                self._speech_run_started_at = now
            self._speech_run_max_silero = max(self._speech_run_max_silero, speech_prob)
            if (not self._user_speech_seen
                    and now - self._speech_run_started_at >= SUSTAINED_SPEECH_TO_ARM_SEC
                    and self._speech_run_max_silero >= SPEECH_RUN_PEAK_MIN):
                self._user_speech_seen = True
                self._silero_aec_armed_at_ms = int(elapsed * 1000)
                await self._wake_telemetry.stage("speech_detected")
            self._silence_started_at = 0.0
        else:
            self._speech_run_started_at = self._speech_run_max_silero = 0.0
            if self._user_speech_seen:
                if self._silence_started_at == 0.0:
                    self._silence_started_at = now
                    self._stamp_turn_stage("speech_end", first=False)
                elif now - self._silence_started_at >= END_OF_UTTERANCE_SILENCE_SEC:
                    await self._end_session_input("end-of-utterance")
                    return
        await self._send_session_audio(frame)

    async def _drain_acquire_audio(self) -> tuple[int, bool]:
        count = 0
        while self._turn is not None and not self._input_ended:
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
        return count, self._user_speech_seen

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
        if self._state is State.SESSION or self._acquiring:
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
        except _InputAdmissionClosed as error:
            return error.result
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
        if self._state is not State.SESSION or self._turn is None:
            return "NO_SESSION"
        if self._input_ended:
            return "OK"
        await self._end_session_input("push-to-talk release")
        return "OK"

    def _anchor_turn_timeline(self, anchor_at: float = 0.0) -> None:
        """Wake turns use fire time; manual turns start their own clock."""
        self._turn_timeline = {}
        self._turn_event_id = (
            self._wake_telemetry.current_event_id if anchor_at else None
        ) or make_event_id()
        self._turn_anchor = anchor_at or time.monotonic()
        self._turn_anchor_kind = "wake" if anchor_at else "manual"

    def _stamp_turn_stage(self, stage: str, *, first: bool = True) -> None:
        """Record one latency stage of the in-flight turn.

        A `time.monotonic()` assignment and nothing else — every caller is
        on a hot path (wake frame, session frame, response playout).
        `first=False` keeps the LAST occurrence, which is what the
        end-of-utterance silence clock wants after a mid-sentence pause.
        """
        if self._turn_anchor == 0.0:
            return
        if first and stage in self._turn_timeline:
            return
        self._turn_timeline[stage] = time.monotonic()

    def _turn_timeline_ms(self) -> dict[str, int]:
        """Integer-ms deltas from this turn's anchor, stages that did not
        happen omitted. Empty when no turn has been anchored."""
        if self._turn_anchor == 0.0:
            return {}
        deltas = {
            f"{stage}_ms": int((at - self._turn_anchor) * 1000)
            for stage in _TURN_TIMELINE_STAGES
            if (at := self._turn_timeline.get(stage)) is not None
        }
        deltas["total_ms"] = int((time.monotonic() - self._turn_anchor) * 1000)
        return deltas

    def _emit_turn_timeline(self, outcome: str) -> None:
        """Publish complete turns to status; close every timeline before teardown."""
        timeline = self._turn_timeline_ms()
        try:
            if timeline:
                log_event(
                    logger,
                    "turn.timeline",
                    event_id=self._turn_event_id,
                    anchor=self._turn_anchor_kind,
                    endpointer=self._endpointer_label(),
                    outcome=outcome,
                    **timeline,
                )
                if outcome == "complete":
                    self._last_turn_ms = {
                        "event_id": self._turn_event_id,
                        "anchor": self._turn_anchor_kind,
                        "outcome": outcome,
                        **timeline,
                    }
        finally:
            self._turn_anchor = 0.0

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
        _wake_legs = [
            leg for leg in self._legs
            if leg == "on"
            or leg not in self._leg_tasks
            or not self._leg_tasks[leg].done()
        ]
        _wake_legs_dead = [
            leg for leg, task in self._leg_tasks.items()
            if self._leg_task_dead(task)
        ]
        # Neither gate feeds _maybe_refresh_condition (see the dispatch
        # sites in run() / _manual_mic_loop / _wake_leg_loop), so the level
        # fields below go stale, not just missing, while either is set.
        mic_feeding = not (self._mic_muted or self._measurement_active.is_set())
        return {
            "state": self._state.name,
            "input_ended": self._input_ended,
            "input_audio": {
                "last_age_ms": self._input_last_age_ms,
                "max_age_ms": self._input_max_age_ms,
                "gaps": self._input_gaps,
                "acquire_dropped_frames": self._acquire_buffer.dropped_frames,
                "capture_dropped_frames": sum(
                    getattr(rt.mic, "dropped_frames", 0)
                    for rt in (*self._legs.values(), *self._push_to_talk.sources.values())
                ),
            },
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
            "endpointer": self._endpointer_label(),
            # The last COMPLETE turn's `event=turn.timeline` deltas
            # (`anchor` says what ms 0 is). Same not-cleared-at-turn-end
            # shape as `endpointer`; `{}` until this daemon served a turn.
            "last_turn_ms": dict(self._last_turn_ms),
            "turn_event_id": self._turn_event_id if self._turn_anchor else None,
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
            "last_wake_at": self._last_wake_at,
            "idle_rms_dbfs": self._idle_rms_dbfs if mic_feeding else None,
            "input_last_above_floor_at": (
                self._input_last_above_floor_at if mic_feeding else None
            ),
            "wake_legs": _wake_legs,
            "wake_legs_dead": _wake_legs_dead,
            # Per-pack tool-registration outcomes (registered / skipped /
            # failed), same motivation as wake_legs: a tool family that
            # silently failed to build (event=tool_pack.build_failed) is
            # visible in /state.voice + jasper-doctor, not only the journal.
            "tool_packs": self._tool_packs,
            # Turns the model was asked to answer and either answered with
            # nothing or lost the link before finishing. Daemon-lifetime,
            # like barge_in_count_session below.
            "silent_responses_session": self._silent_responses_session,
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
            "research": self._research.status(),
            "cues": self._cues.snapshot() if self._cues is not None else None,
        }

    async def _shadow_vad_score_raw(self, frame) -> None:
        """Score a raw-stream frame through the shadow Silero VAD.

        Pure telemetry — records what raw-stream Silero sees during the
        session but makes no endpointing decisions. The active endpointer
        (AEC-stream Silero) is unaffected."""
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
        anchor_at: float = 0.0,
    ) -> None:
        acquiring_at_begin = self._acquiring
        completed = False
        self._anchor_turn_timeline(anchor_at)
        try:
            if acquiring_at_begin:
                self._check_input_admission(self._acquire_input_epoch)
            if listening_feedback:
                # Prime the TTS IPC owner's loudness context before the chirp
                # as well as before assistant TTS. The chirp is fire-and-forget,
                # so waiting for the inner turn prepare would race it back onto
                # the no-context fallback.
                await self._begin_turn_output_episode()
                await self._prepare_assistant_loudness_context()
                # Overlap turn acquisition; output cleanup joins the chirp.
                self._assistant_output.start_turn_feedback(
                    self._turn_output_episode,
                    self._play_listening_chirp(going_on=True),
                )
            await self._begin_turn_inner(
                pre_roll=pre_roll,
                text_context=text_context,
                anchor_at=anchor_at,
            )
            completed = True
        finally:
            if not completed:
                cleanup_error = await capture_cleanup_error(
                    lambda: await_output_cleanup_owned(
                        self._cleanup_after_failed_begin(),
                        task_name="turn-begin-cleanup",
                    ),
                )
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
        if self._mic_muted or input_epoch != self._input_admit_after:
            raise _InputAdmissionClosed("MUTED")
        if self._measurement_active.is_set():
            raise _InputAdmissionClosed("MEASURING")

    async def _begin_turn_inner(
        self,
        *,
        pre_roll: bool = True,
        text_context: str | None = None,
        anchor_at: float = 0.0,
    ) -> None:
        input_epoch = (
            self._acquire_input_epoch if self._acquiring else self._input_admit_after
        )
        self._check_input_admission(input_epoch)
        pre_roll_frames = (
            tuple(self._pre_roll) if self._frozen_pre_roll is None else self._frozen_pre_roll
        ) if pre_roll else ()
        await self._begin_turn_output_episode()
        t_begin = time.monotonic()
        # sched_lag is wake→picked-up-by-the-loop; a turn no wake opened has
        # no lag to report and must not charge itself the episode await.
        t_wake = anchor_at or t_begin
        # One endpointer decision per turn. A turn whose audio comes from a
        # push-to-talk source is closed by the button release
        # (`manual_session_end`), so local Silero must not also try.
        # `active_source` is set by `manual_session_start` before it
        # calls us and is the same flag `_manual_mic_loop` gates on, so "the
        # button owns this turn" and "manual-source frames are the session
        # audio" are one fact, not two.
        self._manual_endpoint_this_turn = (
            self._push_to_talk.active_source is not None
        )
        # Silero's internal LSTM state must not leak across turns. A
        # push-to-talk-only daemon has no VAD to reset (see __init__).
        self._reset_session_input()
        # `_turn_started_at_loop` anchors NO_SPEECH_ABORT_SEC,
        # HARD_RECORDING_CAP_SEC and the push-to-talk hold cap; it is read on
        # the asyncio loop clock to match what the silence detector reads.
        self._user_speech_seen = False
        self._input_ended = False
        self._turn_started_at_loop = (
            anchor_at or (self._input_admit_after if self._acquiring else 0.0)
            or asyncio.get_event_loop().time()
        )
        self._max_silero_score_in_turn = 0.0
        self._max_silero_raw_in_turn = 0.0
        self._silero_raw_armed_at_ms = None
        self._silero_aec_armed_at_ms = None
        self._resolve_barge_in_for_turn()
        if self._vad_off is not None:
            self._vad_off.reset()
        t_after_state = time.monotonic()
        await self._content_activity.refresh_now()
        await self._prepare_assistant_loudness_context()
        await self._tts.pause_content_meter()
        self._content_activity.pause()
        self._volume_coordinator.note_voice_session(
            True,
            camilla_volume_locked=getattr(
                self._ducker, "locks_camilla_volume", True,
            ),
        )
        t_after_loudness_prepare = time.monotonic()
        await self._ducker.duck()
        t_after_duck = time.monotonic()
        self._session_id = self._usage_store.open_session(
            provider=self._cfg.voice_provider,
        )
        self._turn = await self._connection.acquire_turn()
        t_after_acquire = time.monotonic()
        self._check_input_admission(input_epoch)

        if text_context:
            await self._turn.send_text_context(text_context)
            if self._turn.turn_lost():
                raise RuntimeError("live turn lost while sending text context")

        logger.info(
            "turn acquire done in %.0fms "
            "(sched_lag=%.0f state=%.0f loudness_prepare=%.0f duck=%.0f acquire=%.0f) "
            "(wake→activity_start)",
            (time.monotonic() - t_wake) * 1000,
            (t_begin - t_wake) * 1000,
            (t_after_state - t_begin) * 1000,
            (t_after_loudness_prepare - t_after_state) * 1000,
            (t_after_duck - t_after_loudness_prepare) * 1000,
            (t_after_acquire - t_after_duck) * 1000,
        )
        # Drain the recent-mic ring into the turn so the user's first phoneme,
        # which preceded the wake firing, reaches the model. The frame that
        # fired the wake is the most-recently-appended entry and is included.
        if pre_roll_frames:
            self._stamp_turn_stage("first_audio_to_provider")
        for frame in pre_roll_frames:
            self._check_input_admission(input_epoch)
            await self._turn.send_audio(frame.tobytes())
        self._check_input_admission(input_epoch)
        playback = asyncio.create_task(
            play_responses(
                self._turn, self._tts, barge_in_enabled=self._barge_in_active,
                on_response_started=self._turn_observer("first_response", event_stage="response_started"),
                on_first_write=self._turn_observer("first_write"),
            )
        )
        idle = asyncio.create_task(
            idle_watchdog(
                self._turn,
                self._tts,
                self._cfg.idle_timeout_sec,
                self._cfg.response_stall_timeout_sec,
            )
        )
        self._bg_tasks = {playback, idle}
        self._state = State.SESSION
        self._arm_turn_background_end()

    async def _begin_turn_output_episode(self) -> None:
        self._turn_output_episode = await self._assistant_output.begin_turn_episode(
            self._turn_output_episode,
        )

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
            error = await capture_cleanup_error(operation)
            if error is not None:
                record_failure(phase, error)

        turn = self._turn
        session_id = self._session_id
        episode = self._turn_output_episode
        # First, so `total_ms` is the failure moment rather than the failure
        # plus the cleanup awaits below.
        await run_phase(
            "turn_timeline",
            lambda: self._emit_turn_timeline("aborted"),
        )
        if turn is not None:
            await run_phase("turn_release", turn.release)
        await run_phase(
            "output_cleanup",
            lambda: self._assistant_output.finish_turn_episode(episode, completed=False),
        )
        if session_id is not None:
            await run_phase(
                "usage_session_close",
                lambda: self._usage_store.close_session(session_id, 0, 0),
            )

        await run_phase("local_state_reset", self._reset_turn)

        if first_base_error is not None:
            raise first_base_error

    def _reset_turn(self) -> None:
        try:
            self._content_activity.resume()
        finally:
            self._turn = None
            self._session_id = None
            self._turn_output_episode = None
            self._bg_tasks = set()
            self._bg_end_scheduled = False
            self._push_to_talk.active_source = None
            self._barge_in_active = False
            self._state = State.WAKE
            self._refractory_until = (
                asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC
            )

    def _log_no_answer(
        self,
        event: str,
        /,
        *,
        end_reason: str,
        counted: bool = False,
        **fields: object,
    ) -> bool:
        """Journal one no-answer turn; say whether it is owed a cue.

        An ending the household or the daemon chose (`end_reason` in
        `NO_ANSWER_CUE_SUPPRESSED_REASONS`) is not "asked and got no
        answer": it names itself in the record and is neither counted,
        nor warned about, nor spoken about — but it is still journalled,
        or a zero-answer turn would leave no trace at all.
        """
        suppressed = end_reason in NO_ANSWER_CUE_SUPPRESSED_REASONS
        if counted and not suppressed:
            self._silent_responses_session += 1
            fields["count"] = self._silent_responses_session
        log_event(
            logger,
            event,
            fields={
                "provider": self._cfg.voice_provider,
                "model": self._cfg.active_voice_model,
                **fields,
                **({"suppressed": end_reason} if suppressed else {}),
            },
            level=logging.INFO if suppressed else logging.WARNING,
        )
        return not suppressed

    async def _end_turn(self, reason: str = "ended") -> None:
        # SESSION must cover the chirp and refusal cue so neither wakes itself.
        if self._ending or self._turn is None:
            return
        self._ending = True
        try:
            await await_output_cleanup_owned(
                self._end_turn_inner(reason), task_name="turn-end-cleanup",
            )
        finally:
            self._ending = False

    async def _end_turn_inner(self, reason: str = "ended") -> None:
        episode = self._turn_output_episode
        play_no_answer_cue = False
        try:
            play_no_answer_cue = await self._record_and_release_turn(reason, episode)
        finally:
            try:
                await self._assistant_output.finish_turn_episode(episode, completed=True)
                self._barge_in_active = False
                if play_no_answer_cue:
                    # A paused connection owns its remedy cue. Keep SESSION
                    # through its drain so the cue cannot wake the detectors.
                    error = await capture_cleanup_error(lambda: self._play_cue(
                        self._connection.wake_cue()
                        if self._connection.is_paused() else INTERNAL_ERROR_CUE_SLUG
                    ))
                    if isinstance(error, Exception):
                        logger.warning("teardown no-answer cue failed: %s", error)
                    elif error is not None:
                        raise error
            finally:
                self._reset_turn()
        await self._research.drain()

    async def _record_turn_outcome(self, reason: str) -> None:
        self._emit_turn_timeline("complete")
        # `_user_speech_seen` false means the session got no real user input:
        # a likely false positive (music transient, TTS bleed) or a changed
        # mind. Either way the outcome is 'no_speech', which dual-stream
        # false-positive analysis keys off.
        await self._wake_telemetry.stage("turn_complete")
        # Capture event_id BEFORE the outcome write clears it.
        session_vad_eid = self._wake_telemetry.current_event_id
        terminal_outcome = (
            "completed" if self._user_speech_seen else "no_speech"
        )
        await self._wake_telemetry.outcome(terminal_outcome, reason)

        if session_vad_eid is not None:
            await self._wake_telemetry.record_session_vad(
                session_vad_eid,
                max_silero_aec=self._max_silero_score_in_turn or None,
                max_silero_raw=self._max_silero_raw_in_turn or None,
                silero_aec_armed_at_ms=self._silero_aec_armed_at_ms,
                silero_raw_armed_at_ms=self._silero_raw_armed_at_ms,
                endpointer=self._corpus_endpointer_label(
                    user_speech_seen=self._user_speech_seen,
                ),
                music_playing_at_turn=self._content_activity.music_is_playing(),
                music_db_at_turn=self._content_activity.music_dbfs,
            )

    async def _record_and_release_turn(
        self, reason: str, episode: AssistantOutputEpisode | None,
    ) -> bool:
        drain_wait_sec: float | None = None
        if self._turn is not None and self._turn.last_chunk_at() > 0:
            drain_wait_sec = max(
                0.0, time.monotonic() - self._turn.last_activity_at(),
            )
        research_window = self._research.window_snapshot()
        turn = self._turn
        assert turn is not None
        phases: list[tuple[str, Callable[[], object]]] = [
            ("turn_outcome", lambda: self._record_turn_outcome(reason)),
            ("peering_end", lambda: self._peering.session_ended(reason)),
            ("background_stop", lambda: cancel_tracked_tasks(self._bg_tasks)),
        ]
        async def end_segment() -> None:
            if episode is not None and self._output_gate.is_current(episode):
                await self._tts.end_segment()

        phases.append(("end_segment", end_segment))
        if self._input_ended or self._user_speech_seen or self._manual_endpoint_this_turn:
            phases.append(("end_input", lambda: asyncio.wait_for(turn.end_input(), timeout=2.0)))
        phases.append(("turn_release", turn.release))
        release_base_error: BaseException | None = None
        for phase, operation in phases:
            error = await capture_cleanup_error(operation)
            if isinstance(error, Exception):
                log_event(
                    logger, "turn.cleanup_phase_failed", phase=phase,
                    exc_type=type(error).__name__, err=str(error), level=logging.WARNING,
                )
            elif release_base_error is None:
                release_base_error = error

        play_no_answer_cue = False
        usage = turn.usage()
        assert self._session_id is not None
        cost = self._usage_store.close_session(
            self._session_id,
            usage.input_tokens,
            usage.output_tokens,
            usage=usage.breakdown,
        )
        if research_window.job is None:
            try:
                capture = turn.capture()
            except (RuntimeError, TypeError, ValueError) as exc:
                log_event(
                    logger,
                    "turn.capture_failed",
                    exc_type=type(exc).__name__,
                    level=logging.WARNING,
                )
                capture = None
            if capture is not None:
                self._conversation_capture.record(
                    capture.user_text,
                    capture.assistant_text,
                    data_json=capture.data,
                    session_id=self._session_id,
                    mic_muted=self._mic_muted,
                )
        bytes_sent = turn.bytes_sent()
        chunks_received = turn.chunks_received()
        expected_research_silence_dismiss = (
            research_window.undecided
            and not self._user_speech_seen
            and not self._input_ended
        )
        lost_mid_reply = (
            turn.turn_lost()
            and not turn.server_turn_complete()
        )
        silent = chunks_received == 0 and not turn.turn_lost()
        if bytes_sent == 0 and not expected_research_silence_dismiss:
            self._log_no_answer(
                "turn.silent_response",
                end_reason=reason,
                reason="no_audio_sent",
                bytes_sent=bytes_sent,
                chunks_received=chunks_received,
                turn_lost=lost_mid_reply,
                endpointer=self._endpointer_label(),
            )
        elif (
            bytes_sent > 0
            and (silent or lost_mid_reply)
            and not expected_research_silence_dismiss
        ):
            model = self._cfg.active_voice_model
            if self._input_ended:
                diagnosis: dict[str, object] = (
                    {}
                    if reason in NO_ANSWER_CUE_SUPPRESSED_REASONS
                    else {
                        "reason": reason,
                        "bytes_sent": bytes_sent,
                        "endpointer": self._endpointer_label(),
                    }
                )
                play_no_answer_cue = self._log_no_answer(
                    "turn.silent_response",
                    end_reason=reason,
                    counted=True,
                    **diagnosis,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                )
            elif silent and self._manual_endpoint_this_turn:
                log_event(
                    logger,
                    "turn.silent_response",
                    provider=self._cfg.voice_provider,
                    model=model,
                    reason="hold_timeout",
                    bytes_sent=bytes_sent,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                    idle_timeout_sec=float(self._cfg.idle_timeout_sec),
                    endpointer=self._endpointer_label(),
                    level=logging.WARNING,
                )
            elif silent:
                log_event(
                    logger,
                    "turn.silent_response",
                    provider=self._cfg.voice_provider,
                    model=model,
                    reason="recording_timeout",
                    bytes_sent=bytes_sent,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                    endpointer=self._endpointer_label(),
                    level=logging.WARNING,
                )
            else:
                play_no_answer_cue = self._log_no_answer(
                    "turn.silent_response",
                    end_reason=reason,
                    counted=True,
                    reason="connection_lost",
                    bytes_sent=bytes_sent,
                    chunks_received=chunks_received,
                    turn_lost=lost_mid_reply,
                    endpointer=self._endpointer_label(),
                )
        elif (
            bytes_sent > 0
            and turn.audio_dropped_bytes() > 0
            and not expected_research_silence_dismiss
        ):
            play_no_answer_cue = self._log_no_answer(
                "turn.truncated_response",
                end_reason=reason,
                dropped_bytes=turn.audio_dropped_bytes(),
                chunks_received=chunks_received,
                endpointer=self._endpointer_label(),
            )
        drain_part = (
            f", drain wait {drain_wait_sec:.2f}s"
            if drain_wait_sec is not None else ""
        )
        paced_sec = self._tts.take_paced_sec()
        paced_part = f", paced {paced_sec:.2f}s" if paced_sec > 0.05 else ""
        logger.info(
            "turn ended: in=%d out=%d tokens, est $%.4f "
            "(sent=%dB, recv=%d chunks%s%s%s)",
            usage.input_tokens, usage.output_tokens, cost,
            bytes_sent, chunks_received, drain_part,
            paced_part,
            ", turn_lost" if turn.turn_lost() else "",
        )

        self._research.finish_window(research_window)
        if release_base_error is not None:
            raise release_base_error
        return play_no_answer_cue


def main() -> None:
    from .voice.daemon_main import main as impl  # lazy: composition root imports WakeLoop
    impl()


if __name__ == "__main__":
    main()
