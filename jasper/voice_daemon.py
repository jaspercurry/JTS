from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections import deque
from collections.abc import Coroutine
from enum import Enum
from types import SimpleNamespace

from jasper.log_event import log_event

from .audio_buffer import (
    ACQUIRE_BUFFER_MAX_FRAMES,
    drain_acquire_buffer,
)
from .audio_io import (
    MicCapture,
    TtsPlayout,
)
from .assistant_loudness import (
    active_voice_identity,
    silence_target_lufs_for_level,
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
from .watchdog import Heartbeat
from .timers import Timer, announcement_text
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
from .voice.turn_playback import (  # noqa: F401
    _idle_watchdog,
    _play_responses,
    _turn_audio_chunks,
)
from .volume_coordinator import VolumeCoordinator
from .mic_mute_persistence import read_mic_muted, write_mic_muted

logger = logging.getLogger(__name__)
EX_CONFIG_EXIT = 78
VOICE_PROVIDER_NOT_CONFIGURED_EXIT = EX_CONFIG_EXIT
VOICE_STARTUP_CONFIG_ERROR_EXIT = EX_CONFIG_EXIT


def _synthetic_audio_profile(
    *,
    model: str,
    voice: str,
    pcm: bytes,
    fallback_source_lufs: float = -24.0,
    fallback_peak_dbfs: float = -12.0,
):
    _earcons.measure_pcm_24k_mono = measure_pcm_24k_mono
    return _earcons._synthetic_audio_profile(
        model=model,
        voice=voice,
        pcm=pcm,
        fallback_source_lufs=fallback_source_lufs,
        fallback_peak_dbfs=fallback_peak_dbfs,
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

    async def duck(self) -> None:
        if self._ducked:
            return
        ok = await asyncio.to_thread(
            self._send_command, b"PROGRAM_DUCK_ON\nCLOSE\n"
        )
        if not ok:
            return
        self._ducked = True
        log_event(
            logger,
            "duck",
            on="true",
            transport="fanin",
            socket=self._socket_path,
            duck_db=f"{self._duck_db:.1f}",
        )

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
# Strictly bounds the one transient that's a self-loop risk: TTS
# audio still in the ALSA dmix playout buffer when _end_turn runs.
# The dongle dmix is configured at 4096 frames @ 48 kHz ≈ 85 ms
# of buffering; 700 ms gives ~8x margin for any drain stragglers.
#
# What this is NOT for:
#   - Music false-firing wake. Music plays continuously, refractory
#     or not — the detector has to handle music interference during
#     normal listening anyway. Extending refractory only postpones
#     the same risk. Real fix: AEC reference (TODO).
#   - Music un-duck transient. Camilla's restore is a single-step
#     volume jump but the change happens in the music chain, after
#     the wake-word capture path's perspective on what's happening
#     in the room, so it doesn't add detector bias beyond what
#     normal listening produces.
#
# Earlier values: 5 s (defensive but felt like a 15-20 s dead zone
# end-to-end once detector buffer warmup was added), 10 s (original,
# pre-persistent-connection era when each wake cost a Live slot),
# 0.7 s (May 2026). Even 0.7 s combined with the old post-response
# idle window produced a ~2.2 s total deadzone after the model
# finished speaking — long enough that quick follow-ups got dropped
# silently. The TtsPlayout drain primitive now anchors turn-end on
# samples actually queued, so the refractory only needs to cover
# the dmix tail itself. 0.2 s is ~2.5x the 85 ms dmix buffer —
# still a margin, but won't swallow conversational pacing.
WAKE_REFRACTORY_SEC = 0.2

# Per-leg score-freshness window. When a leg fires, another leg's most-
# recent score counts toward `fired_legs` (and the per-leg log line) only
# if it landed within this window — so a stream that stopped feeding (e.g.
# the bridge died) surfaces as "none" rather than lying with a stale
# score. 4x MicCapture's 80 ms frame period.
WAKE_STALE_SCORE_SEC = 0.32

# How often the WAKE loop recomputes the acoustic condition the fuser keys
# on (Phase 1.3a). The fire gate reads a cached `_current_condition`; this
# bounds its staleness while keeping the ring-noise-floor cost off the per-
# frame path (recompute ~1x/s, not ~12x/s/leg). Conditions — music starting,
# the room going quiet — change on a human timescale, so ~1 s is ample.
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
# (Mycroft, Silero defaults, OpenAI Realtime, Vapi) cluster around;
# was 1.2 s previously, dropped here to cut perceived "I stopped
# talking → response starts" latency by ~400 ms. If we see premature
# `activity_end` fires (logs show speech being chopped during a
# natural mid-sentence pause), nudge back up to 1.0 s.
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

# Silero speech-probability threshold for marking "the user has
# actually spoken" within a turn. Decoupled from
# JASPER_VAD_BARGE_IN_THRESHOLD (default 0.5) — that one is tuned
# strict to avoid TTS-bleed false-positives in the barge-in gate;
# this one is tuned LOOSE so soft / quiet speech still flips
# `_user_speech_seen` so the silence detector arms.
# 0.10 was too loose: AirPlay music vocals scored 0.13 and flipped
# the flag, which let a wake-word false-fire run all the way through
# end-of-utterance and hit the model with garbage audio (which it
# then narrated via get_now_playing). Real user speech in the same
# session bottomed out at 0.19, so 0.15 sits comfortably between
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
# detector. After wake fires, we wait for Silero to report ≥ THRESHOLD
# speech-probability for at least this many seconds *continuously*
# before flipping `_user_speech_seen`. Then — and only then — does
# trailing silence start counting toward end-of-utterance.
#
# This replaces an earlier "fixed 500 ms grace window" approach that
# discarded ALL Silero detections in the first 500 ms (to filter the
# wake-word's audio tail). That broke fast talkers who said the wake
# word immediately followed by a command ("Hey Jarvis volume up", no
# pause): the entire command landed inside the grace window and was
# discarded — Silero saw it, but `_user_speech_seen` never flipped,
# and the 5 s no-speech abort fired even though the user clearly
# spoke. The grace was sized for slow talkers' thinking-pauses, not
# for the no-pause case.
#
# Switching to a sustained-speech requirement handles both cases with
# one primitive: a real spoken command — fast or slow — clears 200 ms
# continuous easily. Pattern borrowed from OpenVoiceOS's dinkum-listener
# (`speech_begin` parameter, default 0.3 s); see ovos-dinkum-listener
# voice_loop.py. We use 200 ms instead of 300 ms because our short
# single-word commands ("next", "pause") only span ~250 ms of audio,
# and 300 ms would miss them.
#
# What was wrong: the original premise that "wake-tail audio is too
# short to ever hit 200 ms continuous" was empirically false. A
# 2026-05-23 sweep across 83 captured wake events found 55 % of them
# had the duration-only gate armed inside the wake-tail window (0-400
# ms post-wake) — wake-word phoneme tail + room reverb routinely
# clears 3 consecutive 80 ms frames at Silero ≥ 0.15. The original
# tail-too-short claim was a guess that survived because most user
# turns where the wake-tail armed the gate also had the user starting
# their question within the next 800 ms (silence window), masking the
# bug. The failure mode showed up when the user paused ≥ 1.4 s after
# the wake before starting to speak: the wake-tail armed the gate,
# 800 ms of silence fired end-of-utterance, the LLM received only
# pre-roll + acquire-buffer audio plus its cached prior-turn context
# and confabulated a response while the user was still mid-pause.
SUSTAINED_SPEECH_TO_ARM_SEC = 0.20

# Minimum PEAK Silero score that the arming speech-run must reach.
# The duration gate alone (3 frames at >= 0.15) is too permissive
# against wake-tail residual; real user speech reliably peaks well
# above this within 2-3 frames while wake-tail residual maxes out in
# the 0.15-0.55 band. The 2026-05-23 corpus sweep found 0.60 cleanly
# rejects the broken event (tail peak 0.52) while keeping every
# real-speech turn in the 83-event corpus armed within 2 s. See
# scripts/probe-wake-gate.py for the harness used to derive this.
#
# Trade-off: a frame at >= 0.60 must appear within the arming run.
# Borderline cases — user mumbles or speaks very quietly — may delay
# arming until a louder frame lands. The fallback is the 5 s
# NO_SPEECH_ABORT_SEC, which still applies; degradation is at worst
# "turn aborts and user re-wakes," vs the silent failure of "model
# hallucinates a response while user is still trying to start."
SPEECH_RUN_PEAK_MIN = 0.60


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
    """Compute waveform RMS in dBFS for a single int16 mic frame.

    Sent to the peering ranking function as a tertiary tiebreaker.
    Cheap (≤80 µs per 1280-sample frame on Pi 5). Returns None on
    any error so the ranker falls through to the next tier rather
    than crashing on a malformed frame.

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
    # Do NOT return. This task lives in WakeLoop._bg_tasks; the
    # session-frame handler treats any completed _bg_tasks task as
    # "turn over" and tears down the turn. If we returned here, the
    # model's response would arrive after turn release and be dropped
    # — exactly the regression observed on 2026-05-24 (response.done
    # arrived AFTER turn release: 7 audio deltas dropped). Idle here
    # until _end_turn's cleanup loop cancels us.
    await asyncio.Event().wait()


class _LegRuntime:
    """Live state for one wake-detection leg.

    Replaces the old paired per-leg attributes
    (`_mic_off`/`_detector_off`/`_recent_score_*`/`_capture_ring_*`). The
    set of legs is declared in `jasper.wake_legs`; adding a leg is a
    registry entry + a config-driven construction in `WakeLoop.__init__`,
    not new attributes and duplicated loop bodies scattered through the
    class.
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
        # Session-state shadow VAD — set only on the AEC-OFF leg today.
        # When present, the generic leg loop scores it during SESSION for
        # telemetry (`_shadow_vad_score_raw`); other legs idle in SESSION.
        self.shadow_vad = shadow_vad
        # Most-recent raw wake score + the loop-clock time it was set.
        # Read at fire time so the wake event carries every leg's recent
        # peak, and to gate `fired_legs` on freshness.
        self.recent_score = 0.0
        self.recent_score_at = 0.0


# Per-leg wake_events column mapping. The peak_score column is irregular
# for back-compat with the historical corpus (aec_on/aec_off vs
# dtln_aec), so the columns are listed explicitly rather than derived
# from the token. A new leg adds an entry here + the matching additive
# columns in jasper.wake_events. The chip-AEC beam legs use a regular
# `<field>_chip_aec_{150,210}` column shape (no historical corpus to
# stay back-compat with — they shipped already-additive).
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

# Which Config field carries each wake leg's mic device string. Kept here
# (a voice-daemon construction concern) rather than on the frozen
# jasper.wake_legs registry, which stays a pure cross-process identity
# table. Note the deliberate token/field-name skew: the chip-direct leg's
# token is "off" but its device var is cfg.mic_device_raw — the
# operator-facing "raw" vocabulary (JASPER_MIC_DEVICE_RAW). The reconciler
# sets/clears these vars from the JASPER_WAKE_LEG_* booleans; an empty
# string means the leg is not configured. A new leg adds an entry here.
# The chip-AEC beam legs map their token straight to the matching cfg
# field (no token/field skew, unlike "off"->mic_device_raw).
_LEG_DEVICE_ATTR: dict[str, str] = {
    "on": "mic_device",
    "off": "mic_device_raw",
    "dtln": "mic_device_dtln",
    "chip_aec_150": "mic_device_chip_aec_150",
    "chip_aec_210": "mic_device_chip_aec_210",
}


def _configured_wake_legs(cfg: Config) -> list[tuple[LegSpec, str]]:
    """Decide which wake legs to build and each one's device string.

    Pure (no I/O) so it is unit-testable on its own — the run() wiring
    layers mic-open + AsyncExitStack lifecycle on top. The "on"
    (AEC3/primary) leg is always built: it carries session audio and the
    Tier-1 heartbeat, and the AEC reconciler is responsible for ensuring
    its device is present (or parking voice). Optional "off"/"dtln" legs
    are built only when their device var is non-empty, so voice never
    opens a UDP listener nobody feeds.
    """
    legs: list[tuple[LegSpec, str]] = []
    for spec in wake_input_legs():
        device = getattr(cfg, _LEG_DEVICE_ATTR[spec.token])
        if spec.token == "on" or device:
            legs.append((spec, device))
    return legs


class WakeLoop:
    """Mic consumer. Dispatches each primary-mic frame to either the
    wake-word detector (WAKE state) or the active live turn (SESSION
    state). One consumer iterating over the primary `mic.frames()` —
    eliminates implicit frame-ownership coupling between wake-listen
    and active-turn paths.

    Multi-leg wake detection: `self._legs` holds one `_LegRuntime` per
    configured wake leg (keyed by jasper.wake_legs token), assembled by
    run() and passed in via `legs`. The primary "on" (AEC3) leg drives
    this main loop and carries session audio + the Tier-1 heartbeat;
    optional "off" (chip-direct) and "dtln" legs run as parallel
    `_wake_leg_loop` tasks, each with its own `WakeWordDetector`. Any
    leg crossing threshold fires the wake event (OR-gate); a shared
    refractory + asyncio lock guarantees one user attempt = one wake
    event regardless of which leg(s) crossed first. Secondary legs are
    wake-detection-only: their frames don't populate pre-roll or flow
    into sessions — the primary "on" stream stays the canonical session
    audio source.
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
    ) -> None:
        self._cfg = cfg
        self._tts = tts
        # Per-pack tool-registration outcomes (already serialized to the
        # /state.voice.tool_packs wire shape by outcomes_to_state). Opaque
        # to the wake loop — held only so session_status can surface which
        # tool families registered / were gated off / failed to build,
        # making a silently-missing family visible without grepping the
        # journal. Empty when constructed without the pack walk (tests).
        self._tool_packs: list[dict] = tool_packs or []
        # Wake-detection legs, keyed by jasper.wake_legs token. Assembled
        # by run() (the "leg factory": opens each leg's mic under the
        # AsyncExitStack, then builds its detector, capture ring, and —
        # for "off" — a session shadow VAD). "on" is the primary/session
        # leg, always present; "off"/"dtln" are present when configured.
        # Each _LegRuntime also holds that leg's recent wake score +
        # timestamp, read at fire time.
        self._legs: dict[str, _LegRuntime] = {
            leg.spec.token: leg for leg in legs
        }
        # Fail loud at construction if a configured leg lacks a _LEG_DB
        # telemetry mapping — otherwise it would raise an uncaught
        # KeyError in the wake hot path (telemetry must be fail-soft,
        # never block wake). Caught here at daemon startup, not at fire
        # time; the registry-wide invariant is also covered by
        # test_leg_db_covers_all_wake_input_legs.
        _unmapped = [tok for tok in self._legs if tok not in _LEG_DB]
        if _unmapped:
            raise RuntimeError(
                f"wake legs missing a _LEG_DB telemetry mapping: "
                f"{sorted(_unmapped)} (add them to _LEG_DB in "
                "voice_daemon.py)"
            )
        # Convenience aliases onto the primary "on" leg (plus the optional
        # legs' capture rings), so the established read sites — run()'s
        # main loop, _finalize_event_audio, _shadow_vad_score_raw,
        # _begin_turn, begin_event — keep reading flat attributes.
        _on = self._legs["on"]
        self._mic = _on.mic
        self._detector = _on.detector
        self._capture_ring_on = _on.capture_ring
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
        # The fire-decision seam (Phase 1.2): the single place a leg's fire
        # threshold is decided, so per-condition thresholds (1.3) and any
        # future corroboration/veto land here, not in the parallel leg loops.
        # Empty offsets today => behavior-preserving OR-gate.
        # `_current_condition` is the acoustic condition the fuser keys on,
        # refreshed at fire time by the estimator; the empty-offset fuser
        # ignores it today, so its staleness between fires is moot until 1.3
        # fills offsets (which must also refresh it on the hot path).
        self._fuser: WakeFuser = WakeFuser()
        self._current_condition: str = DEFAULT_CONDITION
        # Loop-clock timestamp of the last condition recompute (Phase 1.3a);
        # 0.0 forces a refresh on the first WAKE frame.
        self._condition_refreshed_at: float = 0.0
        self._connection = connection
        self._ducker = ducker
        # Direct camilla handle for `CueDuck` (snapshot-based duck
        # around dynamic-text cues). Optional for back-compat with
        # tests / out-of-tree callers; without it, dynamic-text cues
        # play unducked rather than crashing.
        self._camilla = camilla
        self._content_activity = content_activity
        self._usage_store = usage_store
        self._spend_cap = spend_cap
        self._stop_event = stop_event
        self._volume_coordinator = volume_coordinator
        self._cues = cues
        # One-shot latch for the "cue requested but no cue manager"
        # WARN in _play_cue — see that method for why it must not be
        # silent, and why it logs once rather than per-cue.
        self._warned_cues_unconfigured = False
        # Tier 1 of the resilience ladder. Bumped on every mic frame
        # — i.e. proof that audio capture is alive AND the async loop
        # is iterating. If either dies (PortAudio wedge, asyncio
        # deadlock, mic device disappearance), the heartbeat thread
        # stops patting systemd and `Restart=on-watchdog` revives us
        # with a fresh process. See jasper/watchdog.py.
        self._heartbeat = heartbeat

        # Local Silero VAD for in-session barge-in gating. While the
        # model is producing TTS, mic frames are forwarded to Gemini
        # ONLY if the local VAD detects user speech — TTS bleed-through
        # is filtered out, real interrupts pass through.
        self._vad = SpeechVAD()
        # Session-state shadow VAD for the chip-direct ("off") leg, when
        # configured. Created in run() and carried on that leg's
        # _LegRuntime; aliased here for _shadow_vad_score_raw / _begin_turn.
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
        self._fire_and_forget: set[asyncio.Task] = set()
        self._refractory_until: float = 0.0

        # Room-correction measurement window. When set, the WakeLoop
        # drops mic frames (no wake-word feed, no session forward) and
        # asks outputd to ignore content-meter samples so sweeps don't
        # become the next assistant-loudness baseline. Set / cleared via
        # MEASURE_PAUSE / MEASURE_RESUME; the safety task auto-clears
        # after 2 min so a coordinator crash can't strand the speaker
        # silent.
        self._measurement_active: asyncio.Event = asyncio.Event()
        self._measurement_safety_task: asyncio.Task | None = None

        # User-driven mic mute. Set via mute_mic()/unmute_mic() (driven
        # through the MUTE / UNMUTE UDS commands). When True, the wake
        # loop drains frames from the mic queue but skips both wake
        # detection and session forwarding — frames never reach
        # openWakeWord, and any active session is ended at the moment
        # of mute so the user gets "stop NOW" semantics. The state is
        # persisted to mic_mute_state_path so it survives daemon
        # restarts (deploy, watchdog, AEC reconciler, web-wizard saves);
        # mute is a privacy promise and a silent un-mute on every
        # restart broke that promise. The dashboard's mic chip surfaces
        # the persisted state so users aren't left wondering why wake
        # isn't responding.
        self._mic_muted: bool = read_mic_muted(cfg.mic_mute_state_path)
        if self._mic_muted:
            logger.info(
                "mic mute: restored from %s (mic is muted at startup)",
                cfg.mic_mute_state_path,
            )

        # Pre-render generated earcons once. Synthesis is pure (no
        # instance state used), so caching the PCM bytes keeps hot paths
        # off any per-call cost. Same shape `TtsPlayout.write()` accepts
        # (24 kHz int16 mono).
        self._chirp_on_pcm: bytes = _generate_listening_chirp(
            going_on=True,
        )
        self._chirp_off_pcm: bytes = _generate_listening_chirp(
            going_on=False,
        )
        self._chirp_on_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="wake_start",
            pcm=self._chirp_on_pcm,
        )
        self._chirp_off_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="turn_end",
            pcm=self._chirp_off_pcm,
        )
        self._mute_click_on_pcm: bytes = _generate_mute_click(going_on=True)
        self._mute_click_off_pcm: bytes = _generate_mute_click(going_on=False)
        self._mute_click_on_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="unmute",
            pcm=self._mute_click_on_pcm,
        )
        self._mute_click_off_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="mute",
            pcm=self._mute_click_off_pcm,
        )

        # Monotonic wallclock at the moment wake fires. Used by
        # _begin_turn to break the wake→activity_start latency into
        # named segments (state reset, loudness prepare, duck,
        # acquire_turn) so a slow turn-acquire can be localized.
        # 0.0 means "no wake yet this session"; replaced on every fire.
        self._wake_event_at_monotonic: float = 0.0

        # End-of-utterance detection state (per-turn). With server-side
        # auto VAD enabled, we MUST send `audio_stream_end=True` the
        # moment the user stops speaking — not at turn cleanup. Without
        # this signal the server stays in "listening for end of turn-1"
        # state and turn-2 audio gets silently swallowed (the
        # deterministic-second-turn-silent-fail symptom). Silero VAD
        # gives us per-frame speech probability; we accumulate
        # consecutive-silence-after-speech and call turn.end_input()
        # (which sends audio_stream_end) once the silence window
        # crosses the threshold.
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
        self._max_silero_raw_in_turn: float = 0.0
        self._silero_raw_armed_at_ms: int | None = None
        self._silero_aec_armed_at_ms: int | None = None
        # Rolling ring buffer of the most recent mic frames. Always
        # appended-to (regardless of WAKE/SESSION state); drained into
        # the new turn at _begin_turn so the first phoneme of the
        # command isn't clipped.
        self._pre_roll: deque = deque(maxlen=PRE_ROLL_FRAMES)

        # Wake-event telemetry (HANDOFF-wake-telemetry.md PR 3). The store
        # handles the SQLite writes + per-leg audio capture + retention;
        # the WakeLoop's contribution is the per-leg capture rings
        # (allocated in run(), sized CAPTURE_RING_FRAMES, aliased above)
        # and the in-flight event id. The rings are kept separate from
        # `_pre_roll` because they're tuned for offline review (~6 s
        # windows around each wake event) while the pre-roll is tuned for
        # first-phoneme preservation in turn-open (~560 ms); conflating
        # them would force one to compromise on the other's dimension.
        self._wake_event_store: WakeEventStore | None = wake_event_store
        # The wake event currently in flight, or None when in WAKE
        # state with no pending event. Set in `_handle_wake_frame` on
        # fire; cleared in `_end_turn` after the final outcome write.
        # Funnel-stage hooks scattered through the wake / session /
        # arbitrate flow consult this to know which row to UPDATE.
        self._current_event_id: str | None = None
        # Buffer for frames captured during the wake → turn-acquired
        # window. When wake fires, `_handle_wake_frame` kicks off
        # `_acquire_and_drain` as a background task and sets
        # `_acquiring=True`. The main mic loop sees the flag and
        # routes incoming frames here instead of through the wake or
        # session handlers; the background task drains this buffer
        # into the turn in order once acquire_turn() resolves. This
        # keeps the user's full utterance flowing even when a
        # context reset or network blip stretches the acquire window
        # to several seconds — the LiveKit / Pipecat / Home Assistant
        # canonical pattern for not dropping audio across a
        # connection-establishment gap.
        self._acquiring: bool = False
        self._acquire_buffer: deque = deque(maxlen=ACQUIRE_BUFFER_MAX_FRAMES)

        # Multi-device peering: epoch UUID assigned by the peering
        # daemon when this Pi wins arbitration. Used to correlate the
        # SESSION_STARTED / SESSION_ENDED notifications back to the
        # specific wake event. Empty string means "no peer-tracked
        # session" — either peering is disabled, or this is a
        # dial-driven session that didn't go through arbitration.
        self._peering_current_epoch: str = ""

    @classmethod
    def for_tests(cls, **overrides):
        """Build a fully-shaped WakeLoop without opening hardware.

        This is the supported seam for unit tests that exercise individual
        methods. It keeps production code free of defensive probes for
        objects constructed via ``__new__`` and manual partial init.
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

            def supports_server_vad(self) -> bool:
                return False

            async def acquire_turn(self):
                raise AssertionError("WakeLoop.for_tests acquire_turn stub used")

        class _TestDucker:
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

        class _TestVad:
            def predict(self, _frame) -> float:
                return 0.0

            def reset(self) -> None:
                return None

        self = cls.__new__(cls)
        cfg = SimpleNamespace(
            duck_db=0.0,
            idle_timeout_sec=10.0,
            mic_mute_state_path="/tmp/jasper-voice-daemon-test-mute.env",
            peering_enabled=False,
            peering_uds_socket="/tmp/jasper-peering-test.sock",
            response_stall_timeout_sec=120.0,
            server_vad_enabled=False,
            server_vad_prefix_ms=300,
            server_vad_silence_ms=500,
            server_vad_threshold=0.5,
            voice_provider="test",
            wake_model="test_model",
        )
        mic = _TestMic()
        detector = _TestDetector()
        on_ring = deque(maxlen=CAPTURE_RING_FRAMES)
        self._cfg = cfg
        self._tts = _TestTts()
        self._legs = {
            "on": _LegRuntime(
                by_token("on"),
                mic,
                detector,
                on_ring,
            ),
        }
        self._mic = mic
        self._detector = detector
        self._capture_ring_on = on_ring
        self._capture_ring_off = deque(maxlen=CAPTURE_RING_FRAMES)
        self._capture_ring_dtln = deque(maxlen=CAPTURE_RING_FRAMES)
        self._wake_fire_lock = asyncio.Lock()
        self._fuser = WakeFuser()
        self._current_condition = DEFAULT_CONDITION
        self._condition_refreshed_at = 0.0
        self._connection = _TestConnection()
        self._ducker = _TestDucker()
        self._camilla = None
        self._content_activity = _TestContentActivity()
        self._usage_store = _TestUsageStore()
        self._spend_cap = _TestSpendCap()
        self._stop_event = asyncio.Event()
        self._volume_coordinator = _TestVolumeCoordinator()
        self._cues = None
        self._warned_cues_unconfigured = False
        self._heartbeat = None
        self._vad = _TestVad()
        self._vad_off = None
        self._state = State.WAKE
        self._turn = None
        self._session_id = None
        self._ending = False
        self._bg_tasks = set()
        self._fire_and_forget = set()
        self._refractory_until = 0.0
        self._measurement_active = asyncio.Event()
        self._measurement_safety_task = None
        self._mic_muted = False
        self._chirp_on_pcm = _generate_listening_chirp(going_on=True)
        self._chirp_off_pcm = _generate_listening_chirp(going_on=False)
        self._chirp_on_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="wake_start",
            pcm=self._chirp_on_pcm,
        )
        self._chirp_off_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="turn_end",
            pcm=self._chirp_off_pcm,
        )
        self._mute_click_on_pcm = _generate_mute_click(going_on=True)
        self._mute_click_off_pcm = _generate_mute_click(going_on=False)
        self._mute_click_on_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="unmute",
            pcm=self._mute_click_on_pcm,
        )
        self._mute_click_off_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="mute",
            pcm=self._mute_click_off_pcm,
        )
        self._wake_event_at_monotonic = 0.0
        self._user_speech_seen = False
        self._silence_started_at = 0.0
        self._input_ended = False
        self._turn_started_at_loop = 0.0
        self._max_silero_score_in_turn = 0.0
        self._speech_run_started_at = 0.0
        self._speech_run_max_silero = 0.0
        self._server_vad_this_turn = False
        self._max_silero_raw_in_turn = 0.0
        self._silero_raw_armed_at_ms = None
        self._silero_aec_armed_at_ms = None
        self._pre_roll = deque(maxlen=PRE_ROLL_FRAMES)
        self._wake_event_store = None
        self._current_event_id = None
        self._tool_packs = []
        self._acquiring = False
        self._acquire_buffer = deque(maxlen=ACQUIRE_BUFFER_MAX_FRAMES)
        self._peering_current_epoch = ""
        for key, value in overrides.items():
            setattr(self, key if key.startswith("_") else f"_{key}", value)
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

    async def _cancel_fire_and_forget_tasks(self) -> None:
        await _cancel_tracked_tasks(self._fire_and_forget)

    async def play_cue(self, slug: str) -> str:
        """Public wrapper for `_play_cue`, callable via the control
        socket so external clients (jasper-control HTTP, the
        `jasper-cues play` CLI) can play cues through the daemon's
        fan-in-backed TtsPlayout."""
        if not slug:
            return "missing_slug"
        if self._cues is None:
            return "cues_not_configured"
        from .cues.registry import find as _find
        if _find(slug) is None:
            return "unknown_slug"
        await self._play_cue(slug)
        return "ok"

    async def play_supervisor_cue(self, slug: str) -> str:
        """Cue trigger reserved for proactive notifications from
        background supervisors (e.g. the GeminiLiveConnection's
        consecutive-failure escalation).

        Differs from `play_cue` by skipping if a user-driven turn is
        in flight: TtsPlayout has a single PortAudio stream, so
        layering an escalation cue on top of an active TTS turn would
        garble both. Suppressing the cue mid-session is the safe
        default — if the connection is wedged, the next wake event
        will fire `cant_connect` reactively anyway."""
        if self._state is State.SESSION:
            return "skipped_session_active"
        return await self.play_cue(slug)

    async def announce_timer(self, timer: "Timer") -> None:
        """Public hook called by `TimerScheduler` when a timer fires.

        Speaks the announcement via dynamic-text TTS. Defers up to
        5 s if a voice session is currently active (don't cross-talk
        the LLM's TTS); after the grace window the announcement is
        skipped — the user is already engaged and a delayed timer
        chime would be more confusing than a missed one. The user
        can `list_timers` to recover state in either case.
        """
        text = announcement_text(timer)
        deadline = asyncio.get_event_loop().time() + 5.0
        while self._state is State.SESSION:
            if asyncio.get_event_loop().time() >= deadline:
                logger.warning(
                    "timer announce: skipped (id=%s) — session still "
                    "active after 5s grace window",
                    timer.id,
                )
                return
            await asyncio.sleep(0.5)
        logger.info(
            "timer announce: id=%s label=%r text=%r",
            timer.id, timer.label, text,
        )
        await self._play_dynamic_text(text)

    async def _play_dynamic_text(self, text: str) -> None:
        """Speak arbitrary `text` through the cue manager, with
        snapshot-based duck/restore around the playback. Used for
        timer announcements (and any future variable-content cue).

        Uses `CueDuck` rather than the daemon's `Ducker` because a
        cue is a brief, passive interruption: the user isn't
        actively adjusting volume mid-cue, so the predictable
        "music returns to exactly where it was" semantics matter
        more than the dial-twist-wins behavior `Ducker` is designed
        for. See `jasper/camilla.py:CueDuck` for the rationale."""
        if self._cues is None:
            return
        if isinstance(self._ducker, FanInDucker):
            try:
                await self._ducker.duck()
                await self._cues.speak_text(text)
            except Exception as e:  # noqa: BLE001
                logger.warning("dynamic text play failed: %s", e)
            finally:
                await self._ducker.restore()
            return
        if self._camilla is None:
            # No camilla handle — degrade to unducked playback rather
            # than crash. The user hears the cue over un-ducked music
            # which is loud but recoverable; better than silence.
            try:
                await self._cues.speak_text(text)
            except Exception as e:  # noqa: BLE001
                logger.warning("dynamic text play failed: %s", e)
            return
        async with CueDuck(self._camilla, self._cfg.duck_db):
            try:
                await self._cues.speak_text(text)
            except Exception as e:  # noqa: BLE001
                logger.warning("dynamic text play failed: %s", e)

    async def _play_cue(self, slug: str) -> None:
        """Best-effort cue playback. Ducks music via CamillaDSP for
        the duration of the cue (same wrapping a normal Jarvis voice
        response uses), then restores. Without ducking, the cue is
        drowned out by playing music — the level math from the TTS
        side alone can't make a cue audible over a non-ducked stream.

        Tracker / volume-coordinator manipulation is intentionally
        omitted — those are needed for multi-second voice sessions
        where the user might also be adjusting volume mid-turn. A
        ~6 second cue is short enough that simple duck/restore is
        the right primitive.

        Cue plays even if ducking fails. The most common reason a
        duck would fail is camilla restarting — and in that scenario
        music isn't playing through camilla anyway, so the cue plays
        unducked but audible. Silent failure on a wake-blocking
        condition is the worse outcome. Ducker.restore short-circuits
        when the duck didn't latch, so the finally is safe to call
        unconditionally."""
        if self._cues is None:
            # Cues are the "no silent failure paths" promise — they're
            # how the user hears WHY the speaker didn't respond. With no
            # cue manager the speaker is silent on every failure, so
            # make that state diagnosable in the journal. Once per
            # daemon lifetime (not per cue): the condition is static
            # config, repeating it would be journal spam.
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
            return
        try:
            try:
                await self._ducker.duck()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "cue %s: duck failed (cue will play unducked): %s",
                    slug, e,
                )
            try:
                await self._cues.play(slug)
            except Exception as e:  # noqa: BLE001
                logger.warning("cue %s play failed: %s", slug, e)
        finally:
            try:
                await self._ducker.restore()
            except Exception as e:  # noqa: BLE001
                logger.warning("cue %s restore failed: %s", slug, e)

    async def run(self) -> None:
        # Spawn one wake-only consumer per non-primary leg (off / dtln /
        # any future leg). The primary "on" leg is driven by this method's
        # main loop below. self._legs was built in __init__ from
        # jasper.wake_legs; a leg is present only when both its mic and
        # detector were configured, so there's no misconfig case to warn
        # about here anymore.
        leg_tasks: list[asyncio.Task] = []
        for _leg_name in self._legs:
            if _leg_name == "on":
                continue
            leg_tasks.append(asyncio.create_task(
                self._wake_leg_loop(_leg_name),
                name=f"wake-leg-{_leg_name}",
            ))
        if leg_tasks:
            logger.info(
                "multi-leg wake enabled: %s", " + ".join(self._legs.keys()),
            )
        try:
            async for frame in self._mic.frames():
                if self._heartbeat is not None:
                    self._heartbeat.bump()
                if self._stop_event.is_set():
                    if self._state is State.SESSION:
                        await self._end_turn()
                    return

                # Room-correction measurement window: drop the frame
                # entirely (no wake-word feed, no session dispatch, no
                # pre-roll append). Dropping pre-roll matters — sweep tail
                # in the pre-roll would prepend ~1.4 s of test-tone audio
                # to whatever turn the user starts immediately after the
                # window closes. Active sessions never reach this branch
                # because measurement_pause() refuses to set the event
                # while State.SESSION (returns BUSY).
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

                # Continuously fill the pre-roll ring. When wake fires, the
                # last N frames already in this deque are what we replay
                # into the turn so the user's first phoneme isn't lost.
                self._pre_roll.append(frame)
                # Independent capture ring for wake-event telemetry — sized
                # for the 6 s offline-review window, not the 560 ms turn-
                # open window. Always-on regardless of state so the moment
                # a wake fires, the pre-fire context is already on hand.
                self._capture_ring_on.append(frame)

                # Acquire window: between wake firing and the new turn
                # being ready to accept audio. `_acquire_and_drain`
                # opens the turn in the background and drains this
                # buffer into it; the main loop just collects frames
                # here so a multi-second context reset doesn't truncate
                # the user's command. See ACQUIRE_BUFFER_MAX_FRAMES.
                if self._acquiring:
                    self._acquire_buffer.append(frame)
                    continue

                if self._state is State.WAKE:
                    await self._handle_wake_frame(frame, leg="on")
                else:
                    await self._handle_session_frame(frame)
        finally:
            # Cancel + join every leg loop before sweeping tracked side-work.
            # The leg loops are producers: while they are alive, a late wake
            # frame can still enqueue acquire/finalize tasks into
            # _fire_and_forget. Stop producers first so the cancellation sweep
            # below observes every task created during shutdown.
            for _t in leg_tasks:
                _t.cancel()
            for _t in leg_tasks:
                try:
                    await _t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self._cancel_fire_and_forget_tasks()

    async def _wake_leg_loop(self, leg_name: str) -> None:
        """Parallel wake-only consumer for a non-primary leg.

        Scores every frame through the leg's detector and dispatches to
        `_handle_wake_frame(frame, leg=leg_name)`, which shares the
        refractory + OR-gate lock with the primary loop so one user
        attempt fires at most one wake event regardless of which leg(s)
        cross threshold first.

        Wake-detection-only: frames are NOT appended to pre-roll, NOT
        routed to `_acquire_buffer` during the wake→turn-open window, and
        NOT forwarded to live sessions. The primary "on" (AEC) stream
        stays the canonical session audio source — keeps session quality
        unchanged and avoids feeding the LLM mixed multi-leg audio.

        Mirrors the primary-loop gating (measurement window, mic mute,
        acquiring, state) so every "stop listening" signal is honored. In
        SESSION state a leg with a shadow VAD (the AEC-OFF leg today)
        feeds `_shadow_vad_score_raw` for telemetry; other legs idle.
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
            # Fill this leg's capture ring while the user is "live", before
            # the acquiring / WAKE-state checks so a wake fire's window has
            # pre-fire context even if it overlaps the turn-open window.
            if rt.capture_ring is not None:
                rt.capture_ring.append(frame)
            if self._acquiring:
                continue
            if self._state is State.WAKE:
                await self._handle_wake_frame(frame, leg=leg_name)
            elif self._state is State.SESSION and rt.shadow_vad is not None:
                await self._shadow_vad_score_raw(frame)

    async def measurement_pause(self) -> str:
        """Open a measurement window. Set the gate event, pause content
        activity observation, and arm a 2-minute auto-clear safety timer.

        Refuses with `BUSY` when a voice session is currently active
        — yanking the session would orphan the user's turn. The
        coordinator (jasper.correction.coordinator) is expected to
        check STATUS first; this is defense-in-depth.

        Idempotent — calling twice is harmless. Returns:
          - "ok" when the window is now open.
          - "BUSY" when refused due to an active session.
        """
        if self._state is State.SESSION:
            return "BUSY"
        self._measurement_active.set()
        self._content_activity.pause()
        await self._tts.pause_content_meter()

        # Cancel any prior safety timer (idempotent re-pause path).
        prev = self._measurement_safety_task
        if prev is not None and not prev.done():
            prev.cancel()

        # Arm new safety timer. If the coordinator crashes (kill -9)
        # without sending RESUME, this auto-clears the gate so the
        # speaker doesn't stay silent forever. Logged at WARNING so
        # the operator can see something went wrong.
        loop = asyncio.get_running_loop()

        async def _safety() -> None:
            try:
                await asyncio.sleep(120.0)
            except asyncio.CancelledError:
                return
            if self._measurement_active.is_set():
                logger.warning(
                    "measurement window auto-clearing after 2 min — "
                    "coordinator likely crashed without sending "
                    "MEASURE_RESUME"
                )
                self._measurement_active.clear()
                self._content_activity.resume()
                await self._tts.resume_content_meter()

        # Note: this is a fire-once-and-exit task that we deliberately
        # do NOT add to self._bg_tasks — the WakeLoop run loop's
        # bg-task done-checker treats any done task as "turn ended
        # early," so adding short-lived tasks there would corrupt the
        # turn lifecycle. Single-slot reference is enough; we cancel
        # via that slot on RESUME or repeated PAUSE.
        self._measurement_safety_task = loop.create_task(_safety())
        return "ok"


    async def _play_mute_click(self, *, going_on: bool) -> None:
        """Best-effort. If the TTS stream isn't open or write fails,
        the visual feedback on the web UI is enough — never raise."""
        try:
            pcm = (
                self._mute_click_on_pcm
                if going_on else self._mute_click_off_pcm
            )
            profile = (
                self._mute_click_on_profile
                if going_on else self._mute_click_off_profile
            )
            await self._tts.write_segment(
                pcm,
                segment_kind="cue",
                source_profile=profile,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("mic mute click failed: %s", e)


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
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("listening chirp failed: %s", e)

    async def _prepare_assistant_loudness_context(self) -> None:
        provider, model, voice = active_voice_identity(self._cfg)
        silence_target = silence_target_lufs_for_level(
            self._volume_coordinator.get_listening_level(),
        )
        await self._tts.prepare_assistant_context(
            provider=provider,
            model=model,
            voice=voice,
            silence_target_lufs=silence_target,
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
        # Mute means "everything captured before this instant is gone."
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

    async def measurement_resume(self) -> str:
        """Close a measurement window: clear the gate, resume content
        observation, cancel the safety timer.

        Idempotent — calling twice (or before any PAUSE) is harmless.
        Always returns "ok".
        """
        self._measurement_active.clear()
        self._content_activity.resume()
        await self._tts.resume_content_meter()
        if self._measurement_safety_task is not None:
            if not self._measurement_safety_task.done():
                self._measurement_safety_task.cancel()
            self._measurement_safety_task = None
        return "ok"

    def _read_music_dbfs(self) -> float | None:
        """Most-recent playback RMS in dBFS, or None when unavailable.

        Cheap cached read, no async I/O, so it is safe on the wake hot path.
        """
        return self._content_activity.music_dbfs

    def _maybe_refresh_condition(self, now_loop: float) -> None:
        """Refresh `_current_condition` (the acoustic condition the fuser
        keys on) at most once per CONDITION_REFRESH_SEC. Lets the per-frame
        fire gate work off a live (~1 s fresh) condition once the fuser has
        offsets, without paying the ring-noise-floor cost every frame.
        Behavior-neutral while offsets are empty — the fuser ignores the
        condition — and allocation-free on the common timer-not-elapsed
        path."""
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
            # Keep the last good condition. The sub-helpers are already
            # fail-soft; this is belt-and-suspenders against a future
            # classify_condition change raising on the per-frame loop — an
            # unguarded raise here would propagate out of the frame loop and
            # stop wake detection.
            pass

    async def _handle_wake_frame(self, frame, *, leg: str = "on") -> None:
        """Score one frame on the named leg. Legs:
          - 'on'   → post-AEC3 BEST_A (primary, the session audio source)
          - 'off'  → chip-direct raw mic (no AEC; the original dual-stream
                     fallback)
          - 'dtln' → DTLN-aec output (the triple-stream tertiary leg
                     added 2026-05-23 per the triple-stream plan)
          - 'chip_aec_150' / 'chip_aec_210' → the XVF3800 hardware-AEC ASR
                     beams (profile-selected and hardware-conditional).
                     Scored exactly like the software legs —
                     this method is leg-agnostic via self._legs.

        Always tracks the leg's recent peak. If the threshold is crossed
        AND we win the OR-gate race against the other legs, fires a
        single wake event with ALL legs' recent scores attached.

        Refractory + acquiring checks ensure one user attempt = one
        wake event, regardless of which leg(s) fire first."""
        # Quick refractory check — all legs early-out without scoring
        # while the previous wake's TTS may still be bleeding into the
        # mic. Cheap to do per-leg per-frame.
        now_loop = asyncio.get_event_loop().time()
        if now_loop < self._refractory_until:
            return

        # Keep the condition the fuser keys on fresh (Phase 1.3a): recompute
        # ~1x/s so the per-frame gate below works off a live condition once
        # offsets exist. Behavior-neutral while offsets are empty.
        self._maybe_refresh_condition(now_loop)

        # Look up this leg's runtime. Always track the raw score
        # (regardless of threshold) so the OTHER legs, when they fire,
        # can pull this leg's most-recent peak into the wake-event
        # payload — even if it never crossed threshold this utterance.
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

        # Threshold crossed on this leg. Try to win the OR-gate race
        # against the other leg's loop. The lock is held only for the
        # critical section (re-check refractory + set refractory + read
        # the other leg's recent score); the rest of the wake flow
        # happens with the lock released so both loops stay responsive.
        async with self._wake_fire_lock:
            if asyncio.get_event_loop().time() < self._refractory_until:
                # The other leg won the race while we awaited the
                # lock. Bow out — only one wake event per user attempt.
                return
            # Win. Set refractory IMMEDIATELY so the other leg's next
            # frame backs off cleanly. `_arbitrate_acquire_drain` will
            # extend this in its finally block.
            self._refractory_until = now_loop + WAKE_REFRACTORY_SEC
            # Compute `fired_legs` — which leg(s) crossed threshold at
            # fire time. The firing leg is always in the set; another leg
            # is included only if its most-recent score is FRESH (within
            # WAKE_STALE_SCORE_SEC, so a stream that stopped feeding
            # doesn't lie with a stale score) AND above that leg's own
            # threshold. One user attempt = one event; `trigger_kind`
            # records the winner.
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

        # Verify stage (recall → verify, Phase 1.4 seam). The OR-gate above is
        # RECALL: a leg crossed its threshold and won the race, *proposing* a
        # fire. `verify()` is the PRECISION stage — it corroborates before the
        # turn opens. Default is always-fire (behavior-identical to the
        # OR-gate); corroboration rules fill in later and fail open. On a
        # suppress the detectors are already reset above (the utterance
        # elevated them either way) and the only refractory held is the short
        # WAKE_REFRACTORY_SEC, so a genuine wake immediately after is not
        # blinded.
        if not self._fuser.verify(leg, fired_set, self._current_condition):
            log_event(
                logger,
                "wake.suppressed",
                leg=leg,
                fired=fired_legs,
                threshold=f"{firing_threshold:.2f}",
            )
            return

        import time as _time
        self._wake_event_at_monotonic = _time.monotonic()
        # Per-leg score summary for the log — ONLY the legs this install
        # actually built (self._legs), in priority order. A single-stream
        # or non-chip-AEC install never emits score fields for legs it
        # isn't running; the log adapts to the active mic/leg set rather
        # than the static universe of every possible leg. "none" here means
        # an ACTIVE leg whose last score is stale (its UDP stream dried up)
        # — distinct from an unconfigured leg, which is simply absent. The
        # firing leg's recent_score == `score` (just set).
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

        # Pre-compute the "can we actually serve this turn?" gates.
        # In peering mode this flag is broadcast in the WAKE message
        # so the fleet's ranking function can prefer a peer that *can*
        # serve over us (we still bid so exactly one peer plays the
        # failure cue when ALL peers are blocked). The actual cue plays
        # below only if we win arbitration AND can't serve.
        spend_allowed = self._spend_cap.allowed()
        conn_paused = self._connection.is_paused()
        can_serve = spend_allowed and not conn_paused

        # Enter acquiring state immediately so the main mic loop buffers
        # frames into `_acquire_buffer` for the entire arbitration +
        # turn-acquire window. Without this, frames would dispatch back
        # through `_handle_wake_frame` while we're waiting for peering
        # to resolve, and either pile up in the asyncio mic queue or
        # re-trigger detection.
        self._acquiring = True
        self._acquire_buffer.clear()

        # Cheap RMS estimate from the wake-firing frame. Sent to the
        # peering ranking function as a tertiary tiebreaker. SNR would
        # be more useful but needs rolling-noise-floor state we don't
        # currently track — None is acceptable (the ranker falls
        # through to RMS when SNR is missing). Frame is int16 PCM;
        # divide by full-scale to get linear amplitude.
        rms_dbfs = _frame_rms_dbfs(frame)

        # Wake-event telemetry — open a row for the funnel hooks to
        # UPDATE as the event progresses. Cheap (single SQLite INSERT
        # in WAL mode); failure is logged but does not block wake
        # response (telemetry is not a wake-blocking dependency).
        store = self._wake_event_store
        if store is not None:
            event_id = make_event_id()
            self._current_event_id = event_id
            # Build the per-leg telemetry columns. Pre-seed every column
            # to None — begin_event requires peak_score_aec_on/off, and
            # the dtln columns default to None for non-DTLN installs —
            # then fill each CONFIGURED leg via its _LEG_DB column map.
            # The firing leg reports `score` (this exact frame); other
            # legs report their most-recent raw score. `trigger_kind` is
            # the single winner; `fired_legs` (computed above) is the set.
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
            # Bridge config snapshot — env-var-driven knobs as seen
            # by the bridge at startup. Useful when post-hoc analysis
            # asks "what NS level was this event captured under?".
            # Read here (not from the bridge) since the bridge is a
            # separate process; we trust /etc/jasper/jasper.env to be
            # the source of truth and that the bridge was restarted
            # after any change.
            bridge_config = {
                "ns_enabled": os.environ.get("JASPER_AEC_NS_ENABLED", "1"),
                "ns_level": os.environ.get("JASPER_AEC_NS_LEVEL", "low"),
                "agc1_enabled": os.environ.get("JASPER_AEC_AGC1_ENABLED", "0"),
                "agc1_target_dbfs": os.environ.get("JASPER_AEC_AGC1_TARGET_DBFS", "9"),
                "agc1_max_gain_db": os.environ.get("JASPER_AEC_AGC1_MAX_GAIN_DB", "18"),
                "ref_gain_db": os.environ.get("JASPER_AEC_REF_GAIN_DB", "0"),
                "mic_gain_db": os.environ.get("JASPER_AEC_MIC_GAIN_DB", "0"),
                "ref_hpf_hz": os.environ.get("JASPER_AEC_REF_HPF_HZ", "125"),
                "chip_hpf_hz": os.environ.get("JASPER_AEC_CHIP_HPF_HZ", "125"),
            }
            # Music context — best-effort from ContentActivityTracker's
            # cached playback RMS. That value is already maintained
            # without async I/O, so reading it on the wake hot path is
            # free. Not a renderer probe (would add ~50 ms of async
            # work); the value is recent-ish, accurate to within ~1 s.
            # Proxy: louder than -60 dBFS = "music probably playing."
            # Imperfect (TTS uses the same playback chain) but useful for FP
            # correlation. Same cached value the live-condition refresh reads.
            music_volume_db = self._read_music_dbfs()
            music_active_proxy = (
                music_volume_db is not None and music_volume_db > -60.0
            )
            # Acoustic condition (Phase 1.1): music from the playback anchor
            # above; quiet-vs-ambient from the pre-fire mic noise floor over
            # the capture ring. Recorded so production fires carry the same
            # taxonomy the corpus labels use (and the 1.2 fuser keys
            # per-condition thresholds on it). Best-effort — both
            # _ring_noise_floor_dbfs and classify_condition never raise.
            condition_ctx = classify_condition(
                music_dbfs=music_volume_db,
                noise_floor_dbfs=_ring_noise_floor_dbfs(self._capture_ring_on),
            )
            # Refresh the condition the fuser keys on (Phase 1.2). Updates on
            # each fire; the empty-offset fuser ignores it today, so this is
            # behavior-neutral. Phase 1.3 makes it refresh on the hot path.
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
                    # Per-leg score/offset/RMS columns, built from
                    # self._legs via _LEG_DB (configured legs only;
                    # absent legs stay None).
                    **tel,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "wake_events: begin_event failed (will skip telemetry "
                    "for this event): %s", e,
                )
                self._current_event_id = None
            # Schedule the audio capture finalize as a fire-and-forget
            # task. Sleeps for the post-fire window, then snapshots the
            # capture rings and writes WAVs. Not added to `_bg_tasks`
            # per the "fire-once-and-exit tasks not in bg_tasks" rule
            # in this file.
            if self._current_event_id is not None:
                self._create_fire_and_forget_task(
                    self._finalize_event_audio(self._current_event_id),
                    name="wake-event-audio-finalize",
                )

        # Spawn the arbitrate+acquire+drain pipeline as a background
        # task so the main mic loop stays responsive (frames continue
        # piling into _acquire_buffer for up to 20 s — see
        # ACQUIRE_BUFFER_MAX_FRAMES). When peering is disabled this
        # task immediately proceeds to the existing acquire-and-drain
        # flow; when enabled, it first awaits the peering UDS verdict.
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
        """Wait the post-fire collection window, then snapshot each
        configured capture ring and persist WAV files via the store.

        Fire-and-forget — failure logs WARN but doesn't propagate.
        Capture truncation is acceptable on daemon shutdown (the row
        keeps its NULL audio_*_path, queries can filter them out)."""
        if self._wake_event_store is None:
            return
        try:
            await asyncio.sleep(CAPTURE_POST_SEC)
            # Snapshot count = pre + post window in frames. Take the
            # most recent N frames from each ring; rings may hold
            # slightly more than this thanks to the slack in the
            # maxlen sizing. concatenating bytes is cheap.
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
        """Take the LAST `n_frames` from the ring and concatenate
        their bytes. Returns None if the ring is empty (e.g. AEC OFF
        leg not present in single-stream mode)."""
        if not ring:
            return None
        # Take the trailing n_frames; if fewer are available, take
        # everything (early-startup case: rings haven't filled yet).
        take = min(len(ring), n_frames)
        frames = list(ring)[-take:]
        # Each frame is a numpy int16 array; tobytes() is cheap.
        return b"".join(f.tobytes() for f in frames)

    @staticmethod
    def _tail_frame_rms_dbfs(ring: "deque | None") -> float | None:
        """RMS in dBFS of the most-recent frame in `ring`, or None
        if the ring is empty / missing. Used by `_handle_wake_frame`
        to capture per-leg instantaneous mic level at fire-time for
        the wake-event telemetry row."""
        if ring is None or not ring:
            return None
        return _frame_rms_dbfs(ring[-1])

    async def _telemetry_stage(self, stage: str) -> None:
        """Best-effort funnel-stage UPDATE for the in-flight wake
        event. No-op when telemetry is disabled, when no event is
        currently in flight, or when the store write fails — the
        wake / session path is never blocked or interrupted by
        telemetry trouble (logged at WARN; row stays with the
        missing column NULL).

        Tests that exercise individual methods should construct via
        WakeLoop.for_tests(), which populates these attrs."""
        store = self._wake_event_store
        event_id = self._current_event_id
        if store is None or event_id is None:
            return
        try:
            await store.update_stage(event_id, stage)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: update_stage(%s) failed: %s", stage, e,
            )

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
        """Background coroutine spawned on wake. Steps, in order:

        0. **Late-cancel gates**: if the user muted the mic or a
           room-correction measurement started between the wake-frame
           dispatch and this task starting, abort cleanly. Both gates
           are checked in the main mic loop before frames flow, so
           we'd be entering a session with no audio — and the user
           just did something that explicitly said "stop listening".
        1. **Peer arbitration** (no-op when peering is off): ask
           jasper-control via UDS whether this Pi should take the turn.
           If we LOSE, log + return — another peer handles it.
        2. **Gate cues** (winner only): if we WIN arbitration but can't
           serve (spend cap / connection paused), play the appropriate
           cue locally. Done after arbitration so we don't fire N
           cues across N peers.
        3. **Chirp + begin turn + drain**: existing wake flow.

        On error in step 3: play a failure cue, cleanup, clear buffer,
        return to WAKE. The cue is honest about cause — `cant_connect`
        only when the live connection is genuinely paused, otherwise
        `internal_error` (an unexpected throw here is almost always a
        local problem, not connectivity). The `_acquiring` flag flips
        back to False in the finally so the loop returns to wake
        detection.
        """
        try:
            # Step 0: late-cancel gates. mute_mic / measurement_pause
            # can fire AFTER _handle_wake_frame spawned this task but
            # BEFORE we get scheduled. Both are user-deliberate "stop
            # listening" signals; firing a chirp + opening an LLM
            # session after them is bad UX. We check twice: now, and
            # again after the arbitration await (which can take up
            # to 500 ms — plenty of time for the user to mute).
            if self._wake_late_cancelled("pre_arb"):
                await self._telemetry_stage("late_cancel")
                await self._telemetry_outcome("late_cancel", "pre_arb")
                return  # finally clears _acquiring + buffer

            # Step 1: peer arbitration.
            decision = await self._peer_arbitrate(
                score=score, snr_db=None, rms_dbfs=rms_dbfs,
                can_serve=can_serve,
            )
            if decision == "LOSE":
                # Another peer is handling it. Stay silent — losers
                # don't play chirps or cues; they just back off.
                log_event(logger, "peering.wake.lost", score=f"{score:.2f}")
                await self._telemetry_stage("peer_lost")
                await self._telemetry_outcome("peer_lost")
                return  # finally clears _acquiring + buffer

            if self._wake_late_cancelled("post_arb"):
                await self._telemetry_stage("late_cancel")
                await self._telemetry_outcome("late_cancel", "post_arb")
                return

            # Step 2: gate cues — only the winner pays this cost.
            if not spend_allowed:
                logger.warning("daily spend cap reached; voice disabled until rollover")
                await self._telemetry_stage("gate_blocked")
                await self._telemetry_outcome("gate_blocked", "spend_cap_reached")
                await self._play_cue("spend_cap_reached")
                return
            if conn_paused:
                logger.warning(
                    "wake detected but live connection is paused (reconnect/backoff); "
                    "ignoring this wake event",
                )
                await self._telemetry_stage("gate_blocked")
                await self._telemetry_outcome("gate_blocked", "connection_paused")
                await self._play_cue("cant_connect")
                return

            # Step 3: existing chirp + acquire + drain flow.
            #
            # Prime the TTS IPC owner's loudness context before the chirp as well
            # as before assistant TTS. The chirp is fire-and-forget
            # below, so waiting for _begin_turn's prepare would race it
            # back onto the no-context fallback.
            await self._prepare_assistant_loudness_context()
            # "Now listening" chirp. Fire-and-forget so it plays in
            # parallel with `_begin_turn` opening rather than adding
            # ~70 ms to time-to-listen. NOT added to self._bg_tasks —
            # any done task in that set would end the turn early.
            self._create_fire_and_forget_task(
                self._play_listening_chirp(going_on=True),
                name="listening-chirp-on",
            )

            await self._begin_turn()  # ends with state = SESSION
            await self._telemetry_stage("turn_opened")
            # Notify peering that we've opened a session (winner-only
            # heartbeat starts firing). Fire-and-forget — voice's own
            # session lifecycle is the source of truth.
            await self._notify_peering_session_started()

            try:
                drained, speech_in_acquire = await drain_acquire_buffer(
                    self._acquire_buffer, self._turn,  # type: ignore[arg-type]
                    vad_predict=self._vad.predict,
                    speech_threshold=END_OF_UTTERANCE_SPEECH_THRESHOLD,
                    peak_min=SPEECH_RUN_PEAK_MIN,
                )
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
            # Fast-talker compensation: see _begin_turn comment block.
            if speech_in_acquire and not self._user_speech_seen:
                self._user_speech_seen = True
                await self._telemetry_stage("speech_detected")
        except Exception as e:  # noqa: BLE001
            logger.exception("turn acquire failed: %s", e)
            await self._telemetry_outcome("session_failed", str(e)[:200])
            # Be honest about WHY we couldn't serve. The conn_paused gate
            # above already played cant_connect for the expected
            # connection-down case and returned, so reaching this
            # catch-all means the connection looked healthy and something
            # ELSE broke during turn-open — almost always local/internal
            # (a failed state write, a disk error), NOT connectivity.
            # Saying "I can't connect, I'll keep trying" there is a false
            # alarm (the 2026-06-19 incident). Only claim a connection
            # problem if the connection actually dropped into
            # paused/failed mid-acquire; otherwise play the honest,
            # low-alarm internal-error cue.
            if self._connection.is_paused():
                await self._play_cue("cant_connect")
            else:
                await self._play_cue("internal_error")
            await self._cleanup_after_failed_begin()
            self._acquire_buffer.clear()
        finally:
            # Flip the flag last — the main loop checks it on every
            # mic frame to decide whether to buffer or dispatch. With
            # state already SESSION (set by `_begin_turn`) and the
            # buffer drained, clearing the flag hands the live mic
            # stream to `_handle_session_frame` cleanly. On LOSE / cue
            # / error paths, state is still WAKE — clearing _acquiring
            # returns the loop to wake detection.
            self._acquiring = False
            # Refractory: protects against detector re-firing on TTS
            # tail (won path) or on a quick repeat-wake (lost path).
            self._refractory_until = max(
                self._refractory_until,
                asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC,
            )

    def _wake_late_cancelled(self, phase: str) -> bool:
        """Check whether a user-deliberate "stop listening" gate fired
        between wake detection and now. Returns True (and logs an
        `event=wake.late_cancel`) if either the mic is muted or a
        room-correction measurement window is open.

        `phase` is "pre_arb" or "post_arb" — included in the log so we
        can tell which side of the peering arbitration await caught
        the late-cancel.

        Mirrored by `manual_session_start` (dial long-press /
        POST /session/start) — the manual entry path bypasses wake
        detection, so it checks the same two gates itself. If you add a
        third stop-listening gate here, add it there too (or extract a
        shared helper once there are three)."""
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
            # peering package not installed (shouldn't happen in
            # production, but defensive — keep wake working).
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
        running OR the UDS errors, returns "WIN" immediately. Single-Pi
        installs pay zero observable cost — `_peering_send` short-
        circuits before any I/O when peering_enabled is False.
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
        """Fire-and-forget notice to the peering daemon that this
        speaker just opened a session. The daemon transitions from
        WINNER → ACTIVE and starts broadcasting heartbeats so peers
        stay suppressed for the session's duration.

        No-op when peering is disabled. Errors swallowed — peering
        is best-effort; voice keeps going.
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

    async def _handle_session_frame(self, frame) -> None:
        # If any background task ended, the turn is over. Cleanup, then
        # this frame is silently consumed (no double-dispatch into detector).
        if any(t.done() for t in self._bg_tasks):
            await self._end_turn()
            return

        assert self._turn is not None

        if self._input_ended:
            return

        # ---- Server-side VAD branch ----
        # When server_vad is active, the server owns end-of-utterance
        # detection. Skip local Silero for turn-control decisions; just
        # forward audio and watch for the server's committed event.
        if self._server_vad_this_turn:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._turn_started_at_loop

            # Shadow telemetry: still run Silero on the primary stream
            # so wake_events.max_silero_aec is populated for cross-cell
            # comparison. Does NOT affect turn behavior — purely an
            # observer. Without this, server-VAD turns leave that column
            # NULL and we can't compare local-VAD-permissiveness across
            # stream configs (AEC vs raw+AGC).
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

            try:
                await self._turn.send_audio(frame.tobytes())
            except Exception as e:  # noqa: BLE001
                logger.warning("send_audio failed (will end turn): %s", e)
                await self._end_turn()
            return

        # ---- Local Silero VAD path (manual VAD) ----
        # End-of-utterance detection: run Silero VAD on the frame and
        # arm the silence detector once the user has been speaking
        # continuously for SUSTAINED_SPEECH_TO_ARM_SEC. Wake-word tail
        # (the brief mic residual after openWakeWord fires) is too
        # short to clear that bar, so it can't false-arm. A real
        # spoken command — even one delivered immediately after the
        # wake word with no pause — clears it within ~200 ms and arms
        # normally. See SUSTAINED_SPEECH_TO_ARM_SEC for the design
        # note on why this replaced an earlier fixed-grace-window
        # scheme.
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
            self._input_ended = True
            try:
                await self._turn.end_input()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "end_input failed at cap (will end turn): %s", e,
                )
                await self._end_turn()
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
                    self._input_ended = True
                    try:
                        await self._turn.end_input()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "end_input failed (will end turn): %s", e,
                        )
                        await self._end_turn()
                    return

        try:
            await self._turn.send_audio(frame.tobytes())
        except Exception as e:  # noqa: BLE001
            logger.warning("send_audio failed (will end turn): %s", e)
            await self._end_turn()

    async def manual_session_start(self) -> str:
        """Trigger a voice session from external IPC (dial hold-to-talk).
        Bypasses the openWakeWord trigger but honors the same gates
        wake does: the user-deliberate stop-listening signals
        (mic-mute, room-correction measurement window), spend cap, and
        connection-paused. Returns one of
        OK / BUSY / MUTED / MEASURING / CAP / PAUSED / ERROR for the
        caller's logging.
        """
        if self._state is State.SESSION:
            return "BUSY"
        # User-deliberate "stop listening" gates — mirror the wake
        # path's _wake_late_cancelled. Mic-mute and an open room-
        # correction measurement window both mean the household has
        # asked the speaker not to listen; opening a paid LLM turn and
        # ducking music from the dial long-press / POST /session/start
        # would bypass that. Refuse silently — like the wake path, no
        # cue and no duck (see _handle_wake_acquire Step 0).
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
        if self._connection.is_paused():
            return "PAUSED"
        await self._prepare_assistant_loudness_context()
        self._create_fire_and_forget_task(
            self._play_listening_chirp(going_on=True),
            name="listening-chirp-on",
        )
        try:
            await self._begin_turn()
            return "OK"
        except Exception as e:  # noqa: BLE001
            logger.exception("manual session start failed: %s", e)
            await self._cleanup_after_failed_begin()
            return "ERROR"

    async def manual_session_end(self) -> str:
        """Finalize the input side of an in-progress session (dial
        button release). This is the same operation the silence
        detector performs at end-of-utterance: send activity_end so
        Gemini stops listening and starts responding.
        """
        if self._state is not State.SESSION or self._turn is None:
            return "NO_SESSION"
        if self._input_ended:
            return "ALREADY_ENDED"
        self._input_ended = True
        try:
            await self._turn.end_input()
            return "OK"
        except Exception as e:  # noqa: BLE001
            logger.warning("manual session end failed: %s", e)
            return "ERROR"

    def session_status(self) -> dict:
        """Diagnostic snapshot — exposed via the control socket so
        jasper-control / the dial can render correct UI without polling
        the spend-cap or connection state separately.

        `duck_active` is the authoritative signal for "is the Ducker
        currently holding camilla main_volume below the canonical
        listening_level target?" — consumed by jasper-control's
        VolumeCoordinator to decide whether to defer a dial/web-slider
        camilla write. See docs/HANDOFF-volume.md "Cross-daemon defer
        signal" for the design.
        """
        return {
            "state": self._state.name,
            "input_ended": self._input_ended,
            "spend_allowed": self._spend_cap.allowed(),
            "connection_paused": self._connection.is_paused(),
            "mic_muted": self._mic_muted,
            "duck_active": self._ducker.is_ducked,
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

    async def _begin_turn(self) -> None:
        import time as _time
        # Anchor on the actual wake-fire moment (set in
        # _handle_wake_frame) so sched_lag captures the gap between
        # wake firing and this coroutine getting picked up by the
        # event loop. Fall back to _time.monotonic() for dial paths
        # that bypass _handle_wake_frame.
        t_wake = self._wake_event_at_monotonic or _time.monotonic()
        t_begin = _time.monotonic()
        # Reset Silero VAD's internal LSTM state at turn start so
        # state from a previous turn doesn't leak into this one.
        self._vad.reset()
        # Reset end-of-utterance tracking. _input_ended must be False
        # so we resume forwarding mic frames; _user_speech_seen,
        # _silence_started_at, and _speech_run_started_at must be
        # cleared so the silence detector doesn't fire on prior-turn
        # state. _turn_started_at_loop anchors NO_SPEECH_ABORT_SEC and
        # HARD_RECORDING_CAP_SEC — measured here on the asyncio loop
        # clock to match what the silence detector reads.
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
        self._volume_coordinator.note_voice_session(True)
        t_after_loudness_prepare = _time.monotonic()
        await self._ducker.duck()
        t_after_duck = _time.monotonic()
        self._session_id = self._usage_store.open_session(
            provider=self._cfg.voice_provider,
        )
        self._turn = await self._connection.acquire_turn()
        t_after_acquire = _time.monotonic()

        self._server_vad_this_turn = False
        if (
            self._cfg.server_vad_enabled
            and self._connection.supports_server_vad()
            and self._content_activity.music_is_playing()
        ):
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
        # Pre-roll: drain the recent-mic ring buffer into the turn so
        # the user's first phoneme (which preceded the wake firing)
        # reaches the model. The frame that fired the wake itself is
        # the most-recently-appended entry and is included.
        pre_roll_frames = list(self._pre_roll)
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
            _play_responses(self._turn, self._tts)
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
        self._state = State.SESSION

    async def _cleanup_after_failed_begin(self) -> None:
        if self._turn is not None:
            try:
                await self._turn.release()
            except Exception:  # noqa: BLE001
                pass
        await self._ducker.restore()
        self._volume_coordinator.note_voice_session(False)
        self._content_activity.resume()
        await self._tts.resume_content_meter()
        if self._session_id is not None:
            self._usage_store.close_session(self._session_id, 0, 0)
        self._turn = None
        self._session_id = None
        self._bg_tasks = set()
        self._state = State.WAKE
        self._refractory_until = asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC

    async def _end_turn(self, reason: str = "ended") -> None:
        # Re-entrancy guard. The teardown (_end_turn_inner) runs many
        # awaits — telemetry, peering notify, bg-task join, end_input with
        # a 2 s timeout, release, "done listening" chirp, duck restore —
        # and only flips _state to WAKE at its very last line, clearing
        # _session_id just before. Two callers can race into it on the
        # single event loop: the control-socket mute_mic handler and the
        # main mic loop's _handle_session_frame. Without a guard the
        # second entrant re-runs teardown and trips
        # `assert self._session_id is not None` after it was cleared
        # (the main-loop caller does not swallow that → daemon crash).
        #
        # We guard with a dedicated _ending flag, NOT by flipping _state
        # to WAKE up front. _state must stay SESSION for the whole
        # teardown: the teardown itself plays a chirp on the single
        # PortAudio TTS stream, and several concurrently-runnable
        # coroutines gate on SESSION to avoid colliding with it —
        # play_supervisor_cue() skips while SESSION, announce_timer()
        # defers while SESSION, and the mic loops route to
        # _handle_session_frame (not _handle_wake_frame) while SESSION.
        # Flipping to WAKE early would let a supervisor cue / timer
        # announcement garble the teardown chirp, or (during a mute-
        # initiated teardown, before _mic_muted is set) let a fresh wake
        # frame begin a NEW turn whose _turn/_session_id this teardown
        # would then tear down.
        if self._ending or self._turn is None:
            return
        self._ending = True
        try:
            await self._end_turn_inner(reason)
        finally:
            self._ending = False

    async def _end_turn_inner(self, reason: str = "ended") -> None:
        # Capture drain timing before any await adds latency. Measured
        # as "time from last server activity to turn end" — meaningful
        # only when audio was actually received (otherwise it's the
        # abort timeout, which is logged separately by the path that
        # called us). Same for both bg-task paths (consumer or
        # watchdog), so observability is symmetric.
        drain_wait_sec: float | None = None
        if self._turn is not None and self._turn.last_chunk_at() > 0:
            drain_wait_sec = max(
                0.0, time.monotonic() - self._turn.last_activity_at(),
            )
        # Wake-event telemetry: record the terminal state of the
        # in-flight event. `_user_speech_seen` tells us whether the
        # session got real user input — if not, the wake was likely
        # a false positive (music transient, TTS bleed) or the user
        # changed their mind. Either way the outcome is 'no_speech',
        # which dual-stream FP analysis keys off.
        await self._telemetry_stage("turn_complete")
        # Capture event_id BEFORE _telemetry_outcome clears it.
        session_vad_store = self._wake_event_store
        session_vad_eid = self._current_event_id
        terminal_outcome = (
            "completed" if self._user_speech_seen else "no_speech"
        )
        await self._telemetry_outcome(terminal_outcome, reason)

        # Session VAD shadow telemetry — record what each stream's
        # Silero saw so the weekly review can cross-tab scores.
        store = session_vad_store
        eid = session_vad_eid
        if store is not None and eid is not None:
            endpointer_label = "server_vad" if self._server_vad_this_turn else "silero_aec"
            if not self._user_speech_seen and not self._server_vad_this_turn:
                endpointer_label = "no_speech_abort"
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

        # Notify peering daemon EARLY (before slow cleanup) so peers
        # un-suppress promptly. Other devices' next wake events should
        # start a fresh arbitration; waiting for our chirp + duck
        # restore would add ~300 ms of unnecessary suppression. No-op
        # when peering is off or this wasn't a peer-tracked session.
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

        # Finalize the assistant TTS segment after cancelling playback.
        # _play_responses only reaches its own end_segment() when the
        # provider closes the audio iterator at turn end — OpenAI does
        # (response.done), Gemini's closes only on release(), which runs
        # AFTER the cancel above. Without this call the cancelled playback
        # task discards the passive loudness measurement, so Gemini never
        # earned a source profile and fanin played it at the louder
        # fallback gain. Idempotent: the meter clears on first save, so
        # the OpenAI path's second call is a no-op.
        try:
            await self._tts.end_segment()
        except Exception as e:  # noqa: BLE001
            logger.warning("teardown end_segment failed: %s", e)

        if self._turn is not None:
            try:
                await asyncio.wait_for(self._turn.end_input(), timeout=2.0)
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug("end_input ignored: %s", e)
            try:
                await self._turn.release()
            except Exception as e:  # noqa: BLE001
                logger.debug("turn release error (ignored): %s", e)

            tokens = self._turn.usage_tokens()
            # Pull the modality breakdown if the provider exposes one
            # (OpenAI Realtime does; Gemini Live returns None and the
            # store falls back to scalar all-audio pricing). The
            # `getattr` guard keeps this compatible with any older
            # turn implementations that predate the protocol method.
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
            # Per-turn no-audio detection. Splits into two distinct
            # phenomena, gated on whether the wake loop explicitly ended
            # the user's input (silence detector / hard cap / manual
            # end). The old combined "SILENT FAILURE" label conflated
            # both, which masked the more common case: idle watchdog
            # times out before the silence detector ever trips, and the
            # only `commit + response.create` is the belated one issued
            # by _end_turn itself — by then the turn is being released,
            # so any audio chunks arrive too late and are dropped (see
            # openai_session._dispatch_event's audio.delta branch and
            # the orphan-response warning in _handle_response_done).
            bytes_sent = self._turn.bytes_sent()
            chunks_received = self._turn.chunks_received()
            if bytes_sent > 0 and chunks_received == 0 and not self._turn.turn_lost():
                model = _active_model(self._cfg)
                if self._input_ended:
                    logger.warning(
                        "SILENT RESPONSE: sent %d bytes of audio to %s "
                        "and called end_input, but received 0 audio "
                        "chunks back. Likely service-side: quota "
                        "exhausted, billing not yet propagated to this "
                        "model, or outage of %s. Non-realtime endpoints "
                        "on the same provider may still work (separate "
                        "quota bucket). Switch providers with "
                        "switch-voice-provider.sh if this keeps happening.",
                        bytes_sent, model, model,
                    )
                else:
                    logger.warning(
                        "RECORDING TIMEOUT: sent %d bytes of audio to %s "
                        "but the silence detector never tripped — idle "
                        "watchdog ended the turn before the wake loop "
                        "asked for a response. _end_turn issued a "
                        "belated commit, so any %s output arrives after "
                        "turn release and is dropped. Common cause: "
                        "low-confidence wake firing on background audio, "
                        "or user speaking continuously past the idle "
                        "window without a pause.",
                        bytes_sent, model, model,
                    )
            drain_part = (
                f", drain wait {drain_wait_sec:.2f}s"
                if drain_wait_sec is not None else ""
            )
            # Writer-side pacing visibility: nonzero means TTS writes
            # slept to stay under the IPC owner's pending budget (the
            # burst-delivery shape). Without this the only journal
            # evidence of pacing misbehaviour would be its absence —
            # fanin logs drops, but over-pacing has no receiver-side
            # signature.
            paced_sec = self._tts.take_paced_sec()
            paced_part = f", paced {paced_sec:.2f}s" if paced_sec > 0.05 else ""
            logger.info(
                "turn ended: %s tokens, est $%.4f (sent=%dB, recv=%d chunks%s%s%s)",
                tokens, cost, bytes_sent, chunks_received, drain_part,
                paced_part,
                ", turn_lost" if self._turn.turn_lost() else "",
            )

        # "Done listening" chirp — bookends the wake chirp. Awaited so
        # it lands in the TTS queue before the unduck below; queueing
        # after any LLM-response tail still in the buffer means the
        # cue order is: response → chirp → music returns. Covers all
        # paths into _end_turn: VAD silence, hard cap (wake without
        # speech), dial release, idle-watchdog turn-complete.
        await self._play_listening_chirp(going_on=False)

        await self._ducker.restore()
        self._volume_coordinator.note_voice_session(False)
        self._content_activity.resume()
        await self._tts.resume_content_meter()
        self._turn = None
        self._session_id = None
        self._state = State.WAKE
        # No detector.reset() here. The detector was already reset in
        # _handle_wake_frame the moment the wake fired (line ~622),
        # and it has not been fed any frames since (state was SESSION
        # for the duration of the turn). Calling reset() again is a
        # no-op for the model state. Skipping it lets the detector
        # buffer start filling immediately when refractory expires,
        # which keeps post-turn wake responsiveness fast.
        self._refractory_until = asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC

def _active_model(*args, **kwargs):
    from .voice.daemon_main import _active_model as impl
    return impl(*args, **kwargs)


def _active_voice(*args, **kwargs):
    from .voice.daemon_main import _active_voice as impl
    return impl(*args, **kwargs)


def _tts_ready_detail(*args, **kwargs):
    from .voice.daemon_main import _tts_ready_detail as impl
    return impl(*args, **kwargs)


def _make_connection(*args, **kwargs):
    from .voice.daemon_main import _make_connection as impl
    return impl(*args, **kwargs)


def _build_cues_manager(*args, **kwargs):
    from .voice.daemon_main import _build_cues_manager as impl
    return impl(*args, **kwargs)


def _schedule_cue_regen(*args, **kwargs):
    from .voice.daemon_main import _schedule_cue_regen as impl
    return impl(*args, **kwargs)


def _schedule_assistant_loudness_seed(*args, **kwargs):
    from .voice.daemon_main import _schedule_assistant_loudness_seed as impl
    return impl(*args, **kwargs)


def _build_router(*args, **kwargs):
    from .voice.daemon_main import _build_router as impl
    return impl(*args, **kwargs)


def _build_registry(*args, **kwargs):
    from .voice.daemon_main import _build_registry as impl
    return impl(*args, **kwargs)


async def _start_control_socket(*args, **kwargs):
    from .voice.daemon_main import _start_control_socket as impl
    return await impl(*args, **kwargs)


async def run() -> None:
    from .voice.daemon_main import run as impl
    await impl()


def main() -> None:
    from .voice.daemon_main import main as impl
    impl()


if __name__ == "__main__":
    main()
