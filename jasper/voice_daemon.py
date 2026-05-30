from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from enum import Enum

from .accounts import Registry, maybe_migrate_legacy
from .audio_buffer import (
    ACQUIRE_BUFFER_MAX_FRAMES,
    drain_acquire_buffer,
)
from .audio_io import (
    MicCapture,
    TtsPlayout,
    make_mic_capture,
    make_tts_playout,
)
from .wake_events import (
    WakeEventStore,
    make_event_id,
    CAPTURE_PRE_SEC,
    CAPTURE_POST_SEC,
)
from .cues import AudioCueManager, build_cue_tts_backend
from .vad import SpeechVAD
from .wake_legs import LegSpec, wake_input_legs
from .wake_condition_context import classify_condition
from .wake_conditions import DEFAULT_CONDITION
from .wake_fusion import WakeFuser
from .camilla import CamillaController, CueDuck, Ducker
from .config import Config
from .watchdog import Heartbeat
from .google_creds import GoogleClients, build_google_clients
from .home_assistant import HAClient, build_ha_client
from .renderer import RendererClient
from .spotify_router import BuildResult, Router, build_clients
from .bus import BusClient
from .citibike import CitiBikeClient
from .subway import SubwayClient
from .timers import Timer, TimerScheduler, announcement_text
from .tools import ToolRegistry
from .tools.audio import make_audio_tools
from .tools.calendar import make_calendar_tools
from .tools.diagnostic import make_diagnostic_tools
from .tools.gmail import make_gmail_tools
from .tools.spotify import make_spotify_tools
from .tools.bus import make_bus_tools
from .tools.citibike import make_citibike_tools
from .tools.home_assistant import make_home_assistant_tools
from .tools.subway import make_subway_tools
from .tools.time import make_time_tools
from .tools.timer import make_timer_tools
from .tools.transport import make_transport_tools
from .tools.weather import make_weather_tools
from .usage import (
    ConnectionUptimeMeter,
    SpendCap,
    UsageStore,
    load_pricing_overrides,
    pricing_for_model,
)
from .voice.session import AudioOutChunk, LiveConnection, LiveTurn
from .volume_coordinator import VolumeCoordinator
from .volume_observers import VolumeObserver
from .mic_mute_persistence import read_mic_muted, write_mic_muted
from .volume_persistence import (
    DEFAULT_ANCHOR_DBFS,
    VolumePersistence,
)
from .wake import WakeWordDetector
from .weather import WeatherClient

logger = logging.getLogger(__name__)

# Canonical playbook for editing this constant (and any tool
# description in jasper/tools/) lives at docs/HANDOFF-prompting.md
# — cross-provider principles, provider deltas, pitfalls catalog,
# recommended edits. Read it before tuning.
#
# Structured per OpenAI's Realtime Prompting Guide
# (cookbook.openai.com/examples/realtime_prompting_guide):
#   Role & Objective → Personality & Tone → Verbosity →
#   Tools (when to call, preambles) → Unclear audio →
#   After a tool returns → Out of scope.
#
# Two design principles from that guide and the official "Using
# realtime models" docs that we previously violated:
#
#   1. POSITIVE framing for tool calls — "Call X when Y", not "Don't
#      forget X". An earlier version of this prompt had ~15 "Do NOT"
#      clauses and zero positive "Call the tool when…" instructions,
#      which is exactly the pattern OpenAI says causes gpt-realtime to
#      drift from rules, skip phases, or misuse tools. Verified
#      2026-05-21 via voice-eval: that prompt produced ZERO tool calls
#      across 5 consecutive read-only scenarios.
#
#   2. CONDITIONAL framing for preamble suppression — "Skip the
#      preamble when X, Y, Z" instead of "Never preamble". Absolute
#      prohibitions get partially ignored (~33% compliance per the
#      OpenAI community thread); the model has been RLHF-trained on
#      the conditional pattern.
#
# Path B applied 2026-05-23: per-tool conditional rules (when to call,
# voice-answer style, response-shape handling) now live in each
# tool's docstring and reach the model via build_tool() sending the
# full cleaned docstring. This system instruction keeps only
# cross-tool meta-rules — role, persona, verbosity, preamble policy,
# unclear-audio handling, tool-result meta-rules, and the small set
# of cross-tool routing rules where two similar tools need
# disambiguation.
SYSTEM_INSTRUCTION = (
    # ---- Role & Objective ------------------------------------------------
    "You are Jarvis, a voice assistant in a household smart speaker. "
    "The user's name is Jasper. Your job is to answer the user's "
    "questions and control music, volume, timers, calendar, and email "
    "by calling the available tools. "

    # ---- Personality & Tone ----------------------------------------------
    "Voice style is terse and factual — like Alexa or Siri. After "
    "answering, stop: don't ask follow-up questions, don't offer "
    "related actions, don't invite further conversation, don't "
    "restate the question. Ask a clarifying question only when the "
    "user's request is genuinely ambiguous and you cannot proceed "
    "otherwise — in that case ask one specific question and nothing "
    "else. "

    # ---- Verbosity -------------------------------------------------------
    # Per OpenAI's Realtime Prompting Guide: define verbosity per task
    # type rather than a global "be concise."
    "Direct answers: 1-2 short sentences. Clarifying questions: ask "
    "one specific question and nothing else. Tool results: follow "
    "the tool's own voice-answer style guidance in its description, "
    "then stop — don't recap the question, don't offer related "
    "actions. "

    # ---- Tools — when to call them ---------------------------------------
    # POSITIVE framing. Each tool's description documents WHEN to
    # call it; only cross-tool routing rules (disambiguating between
    # similar tools) live here.
    "The tools have data and capabilities you do not — answering "
    "from memory or guessing is incorrect. Each tool's description "
    "documents when to call it and how to phrase the answer; trust "
    "that guidance. Music control commands ('play', 'pause', 'skip', "
    "'previous', 'resume', 'volume up', 'mute', etc.) → call the "
    "matching tool without asking for confirmation.\n"
    # The "home_assistant tool isn't available → tell the user
    # smart-home isn't set up + don't misroute to other tools" guard
    # lives in _build_system_instruction's HA addendum (only added
    # when ha_configured=False) with the hostname-aware URL. Keeping
    # the guidance there rather than here keeps the static prompt
    # the same length whether HA is configured or not.
    "Cross-tool routing rules where two similar tools need "
    "disambiguation:\n"
    "  - Bare 'play' / 'resume' / 'keep playing' with no song or "
    "artist named → call resume (un-pauses paused music). Call "
    "spotify_play only when the user names a song, artist, album, "
    "or playlist.\n"
    "  - 'Play the new/newest/latest X' where X is an artist → call "
    "spotify_play_latest_by_artist with `artist=X`. The model MUST "
    "use this tool (NOT spotify_play) when the user includes a "
    "recency word; spotify_play has no concept of release date and "
    "will return whatever ranks highest in catalog search.\n"
    "  - 'What's playing?' / 'Who is this?' → call get_now_playing. "
    "Do NOT call get_now_playing as a chaser after spotify_play — "
    "Spotify's current_playback lags by several seconds and may "
    "report the previous track.\n"
    "  - Calendar questions about today → calendar_today_summary; "
    "questions about a window of hours/days → calendar_upcoming "
    "(pass `hours` appropriately — 6 for 'this afternoon', 168 for "
    "'this week').\n"
    "  - Email follow-up after a summary ('read me the first one' / "
    "'open that email') → call gmail_read_thread with the "
    "thread_id from the prior gmail_unread_summary response.\n"
    "  - Changing an existing timer's duration ('make it 2 minutes "
    "instead', 'change the pasta timer to 10 minutes', 'actually, "
    "make that an hour') → call update_timer in ONE call. Do NOT "
    "call cancel_timer followed by set_timer — the two-step "
    "sequence prompts a spoken preamble between calls that "
    "describes the wrong action.\n"

    # ---- Tools — preambles -----------------------------------------------
    # CONDITIONAL framing per OpenAI's documented pattern. List when
    # to skip; don't ban absolutely.
    "Preambles are brief spoken text before a tool call ('checking "
    "the live arrivals now…'). Skip the preamble in these cases:\n"
    "  - the answer can be given immediately;\n"
    "  - the user is only confirming, correcting, or declining;\n"
    "  - the tool call is lightweight and the user gains nothing "
    "from a status update (every tool here returns in well under "
    "two seconds, so this case typically applies);\n"
    "  - the latest audio is silence, background noise, hold music, "
    "TV audio, or side conversation.\n"
    "When a preamble does fit, keep it to one short sentence "
    "describing the action, not your reasoning. Skipping the "
    "preamble does not mean skipping the tool call — call the "
    "tool, then speak the result.\n"

    # ---- Unclear audio ---------------------------------------------------
    # Per OpenAI's Realtime Prompting Guide. Mic mishears are a real
    # input on a voice-only device; without this rule the model
    # confidently answers a wrong-interpreted utterance.
    #
    # The "fragment" and "empty-string arguments" clauses were added
    # 2026-05-24 after the VAD test matrix surfaced a dangerous
    # failure mode: when STT returned empty or one-word transcripts
    # ("What?", "That's...", ""), the model would still confidently
    # call tools — calendar_today_summary, get_subway_arrivals with
    # `direction=''`, set_volume(60), and in one case home_assistant
    # ("turn on the bedroom lights") which actually executed and
    # turned the lights on while the user was asking about weather.
    # The original "don't call any tool" rule was being interpreted
    # too narrowly — the model didn't perceive "transcript is a
    # fragment" as "unclear audio." Enumerating those triggers
    # explicitly and flagging the empty-arguments anti-pattern is
    # per the prompting playbook's "enumerate triggers; conditional
    # rules over absolutes" guidance.
    # See docs/HANDOFF-vad-experiments.md "Known product bug".
    "If the user's audio is unclear — partial, garbled, talking-"
    "over-music, side conversation, words trailing off, a short "
    "fragment like 'What?' or 'That's', or nothing intelligible "
    "after the wake word — ask once for clarification with a short "
    "English phrase like 'Sorry, I didn't catch that.' Don't guess "
    "at the request; don't call any tool; don't reason about what "
    "was probably said. If you find yourself about to call a tool "
    "with empty-string arguments or arguments you're inventing "
    "without having heard them, you don't have enough information "
    "— say the clarification line instead. One clarification "
    "request, then wait.\n"

    # ---- After a tool returns --------------------------------------------
    # Per-tool voice-answer style lives in each tool's description.
    # These are the cross-tool meta-rules that apply to every tool.
    "After a tool returns, follow the tool's own voice-answer "
    "guidance in its description. Two cross-tool meta-rules apply "
    "to every tool:\n"
    "  - When a tool returns an `error` field, speak it verbatim "
    "— the message tells the user what's wrong and (often) how to "
    "fix it. Don't apologize at length; don't paraphrase.\n"
    "  - When a tool returns a `confirm` field, speak that sentence "
    "verbatim. Don't substitute 'Done.' or 'OK.'.\n"

    # ---- Out of scope ----------------------------------------------------
    "You can't do sports scores, news headlines, or general web "
    "search. Reply briefly: 'Sorry, I don't have <thing>.' Don't "
    "apologize at length."
)

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


def _pcm_peak_dbfs(pcm: bytes) -> float | None:
    """Peak level (dBFS) of a mono int16 PCM chunk, or None if the chunk
    is empty/unreadable. Peak (not RMS) so it matches the semantic of the
    seed constant it feeds, preserving the existing music-vs-TTS
    calibration. Fail-soft by contract — a measurement error must never
    break playback."""
    try:
        import numpy as _np  # local — keep module import cheap
        arr = _np.frombuffer(pcm, dtype=_np.int16)
        if arr.size == 0:
            return None
        # Peak magnitude without an int32 copy of the whole chunk:
        # int() promotes to Python int, so -(-32768) can't overflow.
        peak = max(int(arr.max()), -int(arr.min()))
        if peak <= 0:
            return -120.0  # digital silence floor
        return 20.0 * _np.log10(peak / 32768.0)
    except Exception:  # noqa: BLE001
        return None


class TtsVolumeTracker:
    """Keeps TtsPlayout's gain matched to the actual loudness of music
    playing through the speaker, regardless of where the music's
    attenuation came from.

    Why measure rather than guess. There are several volume stages on
    the music chain that TTS bypasses:

        track_loudness × airplay_sender_vol × spotify_connect_vol
            × camilla_main_volume × room_correction → DAC

    Adding TTS gain = `main_volume + offset` only matches the LAST
    stage. If the user's iPhone AirPlay slider is at 50%, music plays
    ~6 dB quieter than `main_volume` implies; TTS at the legacy fixed
    offset comes out audibly louder than music in that exact scenario.

    What we do. Poll CamillaDSP's `levels.playback_rms()` — the signal
    AFTER every attenuation stage, immediately before the DAC. Maintain
    a windowed peak (max RMS over MUSIC_WINDOW_SEC) so quick quiet
    passages don't let TTS climb between phrases. Set TTS gain so that
    the TTS source peak ends up `music_headroom_db` above the windowed
    music RMS:

        tts_gain_db = (windowed_rms + headroom) - source_peak_dbfs

    where `source_peak_dbfs` is the active provider's TTS output level,
    MEASURED per provider via note_source_chunk() (a seeded EWMA) rather
    than assumed. Measuring it is what makes a quieter provider's voice
    (e.g. OpenAI) land at the same level as a louder one (Gemini); the
    old static constant made the quiet provider come out below target.

    Ceiling policy is branch-specific. The pre-tracker formula treated
    `master_volume + offset` as an absolute ceiling in all branches —
    the intent was "master_volume controls max possible TTS loudness."
    That assumption was reasonable when main_volume was the single
    canonical loudness knob; it breaks down once source-side sliders
    (iPhone, Spotify, BT) and external amplifiers carry user intent
    instead. At non-max listening_level on a loud-source music chain
    (e.g. AirPlay with main_volume = -15 dB) the ceiling actively
    defeated the tracker, leaving TTS several dB QUIETER than music
    instead of the +6 dB above music the headroom formula targets.

    So:
      - Music actively playing: NO master ceiling. The whole point of
        measuring playback_rms is to compute the right TTS level from
        the actual signal; layering an unmeasured cap on top defeats
        that. Hearing safety is enforced by TtsPlayout's MAX_TTS_GAIN_DB.
      - Silence with a valid anchor: ceiling APPLIES. Anchor can be
        stale (we played loud music yesterday, now it's a quiet bedroom
        at low main_volume), and master+offset is the right backstop
        against blasting in that case.
      - No anchor ever recorded (first boot, sentinel): ceiling IS
        the target — main_volume is the only loudness signal we have.

    Hearing-safety belt is in TtsPlayout.set_gain_db (MIN/MAX clamp).
    This class is defense-in-depth on top of that.

    Pause/resume around voice sessions so duck-induced volume changes
    don't pull TTS down DURING the very turn TTS is playing.
    """

    POLL_INTERVAL_SEC = 0.25
    # Cold-start seed for the per-provider source-loudness estimate
    # (dBFS peak). Gemini's TTS output peaks ~-3 dBFS, so this seed is
    # correct for Gemini and a safe, slightly-conservative start for
    # quieter providers (OpenAI, Grok) until the live EWMA below
    # converges — which it does within the first turn of audio. The bug
    # this replaces: a *static* -3 assumed every provider was equally
    # loud, so a quieter provider's TTS came out below the music+headroom
    # target. We now MEASURE the source (note_source_chunk) instead of
    # guessing it — the same "measure, don't guess" rule this class
    # already applies to the music signal.
    SOURCE_PEAK_SEED_DBFS = -3.0
    # EWMA smoothing for the source-loudness estimate. ~0.2 converges
    # ~90% within ~10 voiced chunks (≈1 s of speech) yet absorbs a single
    # loud transient. Source loudness is constant per (provider, model,
    # voice) and jasper-voice restarts on a provider switch, so this is
    # "learn a constant once", not a fast control loop.
    _SOURCE_PEAK_EWMA_ALPHA = 0.2
    # Only learn from voiced chunks. Inter-word/sentence gaps are near
    # silence; folding them in would drag the estimate down and over-
    # boost TTS. Anything below this peak is treated as a gap.
    _SOURCE_PEAK_VOICED_FLOOR_DBFS = -45.0

    def __init__(
        self,
        camilla: CamillaController,
        tts: TtsPlayout,
        offset_db: float,
        music_headroom_db: float,
        silence_threshold_dbfs: float,
        music_window_sec: float,
        volume_persistence: VolumePersistence | None = None,
        initial_anchor_dbfs: float = DEFAULT_ANCHOR_DBFS,
    ) -> None:
        self._camilla = camilla
        self._tts = tts
        self._offset_db = float(offset_db)
        self._headroom_db = float(music_headroom_db)
        self._silence_threshold_dbfs = float(silence_threshold_dbfs)
        self._window_sec = float(music_window_sec)
        # (monotonic_time, max(L_rms, R_rms)) entries, oldest first.
        self._peak_buffer: deque[tuple[float, float]] = deque()
        self._paused = False
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Optional disk persistence: every poll reads main_volume; we
        # opportunistically debounce-write it so external changes (mpc,
        # hardware knob, anything that bypasses our voice tools) get
        # captured. The same path also persists the loudness anchor as
        # it updates.
        self._volume_persistence = volume_persistence
        # Loudness anchor: the last observed playback RMS while music
        # was actually playing, used as TTS's reference during silence
        # so TTS doesn't get loud just because main_volume is high
        # while iPhone (or whatever upstream attenuator) is low.
        # Initialized from disk at boot, or DEFAULT_ANCHOR_DBFS for
        # first-boot. Updated continuously while music plays. Never
        # expires — the Pi doesn't move, the room context is stable.
        self._anchor_dbfs: float = float(initial_anchor_dbfs)
        # Live per-provider source-loudness estimate (dBFS peak), seeded
        # until note_source_chunk() has observed real audio. Persists
        # across turns for the process lifetime; a provider switch
        # restarts jasper-voice, which re-seeds it.
        self._source_peak_dbfs: float = self.SOURCE_PEAK_SEED_DBFS

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def music_is_playing(self) -> bool:
        """True when the music chain has audible signal — used by the
        server_vad switching logic to decide whether to delegate
        end-of-utterance detection to the provider."""
        return self._anchor_dbfs > self._silence_threshold_dbfs

    @property
    def source_peak_dbfs(self) -> float:
        """Current estimate of the active provider's TTS source loudness
        (dBFS peak) — measured, not assumed. The gain formula uses this
        so every provider's TTS lands at the same level relative to music
        regardless of its native output level."""
        return self._source_peak_dbfs

    def note_source_chunk(self, pcm: bytes) -> None:
        """Fold one provider TTS chunk's loudness into the source-peak
        EWMA. Called for every assistant chunk as it is dequeued for
        playback (see _play_responses), independent of pause state —
        measurement never stops, only gain *recompute* pauses during a
        turn, so a turn's audio refines the estimate the next turn uses.
        Fail-soft: a measurement error must never disrupt playback."""
        peak = _pcm_peak_dbfs(pcm)
        if peak is None or peak < self._SOURCE_PEAK_VOICED_FLOOR_DBFS:
            return
        a = self._SOURCE_PEAK_EWMA_ALPHA
        self._source_peak_dbfs = a * peak + (1.0 - a) * self._source_peak_dbfs

    def _record_rms(self, rms_dbfs: float) -> float:
        """Append latest RMS reading and return windowed peak."""
        now = asyncio.get_event_loop().time()
        self._peak_buffer.append((now, rms_dbfs))
        cutoff = now - self._window_sec
        while self._peak_buffer and self._peak_buffer[0][0] < cutoff:
            self._peak_buffer.popleft()
        return max(p for _, p in self._peak_buffer)

    def _compute_gain(
        self, vol_db: float, windowed_rms: float, source_peak_dbfs: float,
    ) -> float:
        """Pure: given current main_volume, the windowed music RMS peak,
        and the measured TTS source peak, return target gain.

        Three branches with branch-specific ceiling policy (see class
        docstring for the why):
          1. Music currently playing (windowed_rms above threshold) →
             match observed loudness directly. No master+offset
             ceiling; hearing safety lives in TtsPlayout's MAX cap.
          2. Silence, but we have a loudness anchor (the last-known
             music level, possibly from a previous session) → target
             that level, CAPPED at master+offset. Anchor can be stale
             (yesterday's loud party at today's quiet bedroom volume),
             so the cap defends against blasting.
          3. No anchor ever recorded (sentinel < -120) → target IS the
             ceiling. main_volume is the only loudness signal we have.
             With initial_anchor_dbfs defaulting to DEFAULT_ANCHOR_DBFS
             (-30 dBFS = 40%), branch 3 is rarely hit in practice; it's
             a backstop for genuine first-boot."""
        ceiling = vol_db + self._offset_db
        if windowed_rms > self._silence_threshold_dbfs:
            target = (
                windowed_rms + self._headroom_db - source_peak_dbfs
            )
            # No ceiling clamp on the music-playing branch — the
            # measured signal IS the answer. MAX_TTS_GAIN_DB in
            # TtsPlayout handles hearing safety.
        elif self._anchor_dbfs > -120.0:
            target = min(
                self._anchor_dbfs + self._headroom_db
                - source_peak_dbfs,
                ceiling,
            )
        else:
            target = ceiling
        # Quantize to 1 dB to avoid log spam and rapid micro-adjustments
        # below human-perceivable change (~3 dB JND for loudness).
        return round(target)

    def _maybe_update_anchor(self, windowed_rms: float) -> None:
        """If music is currently playing (above silence threshold),
        update the in-memory anchor and opportunistically persist it.
        During silence, the anchor stays frozen at the last recorded
        music level — that's the whole point."""
        if windowed_rms <= self._silence_threshold_dbfs:
            return
        self._anchor_dbfs = windowed_rms
        if self._volume_persistence is not None:
            self._volume_persistence.maybe_save_anchor(windowed_rms)

    def _apply_gain(self, vol_db: float, windowed_rms: float) -> None:
        """Compute target → apply via set_gain_db → emit structured event
        on user-perceptible change. Centralizes the pattern shared by
        apply_now and _loop.

        The structured `event=tts_gain.compute` line fires only when
        `tts.gain_db` actually moves (set_gain_db is a no-op on equal
        values), so log volume stays proportional to perceptible change.
        Carries every input the formula consumed plus the branch and
        all clamping stages — enough to reconstruct any gain choice
        from logs alone, without correlating across the three separate
        log lines that used to be required."""
        source_peak = self._source_peak_dbfs
        target = self._compute_gain(vol_db, windowed_rms, source_peak)
        old_gain = self._tts.gain_db
        self._tts.set_gain_db(target)
        if self._tts.gain_db == old_gain:
            return
        # Reconstruct branch for the structured event. Cheap: just
        # comparisons + arithmetic on three floats. Mirrors the
        # branching in _compute_gain by design — keep these two
        # in lockstep if you add a fourth branch.
        ceiling = vol_db + self._offset_db
        if windowed_rms > self._silence_threshold_dbfs:
            branch = "music"
        elif self._anchor_dbfs > -120.0:
            branch = "anchor"
        else:
            branch = "no_anchor"
        logger.info(
            "event=tts_gain.compute branch=%s windowed_rms=%.1f "
            "source_peak_dbfs=%.1f anchor_dbfs=%.1f main_volume_db=%.1f "
            "offset_db=%.1f ceiling_db=%.1f target_db=%.1f final_db=%.1f "
            "max_cap_db=%.1f",
            branch, windowed_rms, source_peak, self._anchor_dbfs, vol_db,
            self._offset_db, ceiling, target,
            self._tts.gain_db, TtsPlayout.MAX_TTS_GAIN_DB,
        )

    async def apply_now(self) -> None:
        result = await self._camilla.get_volume_and_mute(best_effort=True)
        if result is None:
            logger.warning(
                "tts volume tracker: camilla unavailable; "
                "falling to silent gain",
            )
            self._tts.set_gain_db(self._tts.MIN_TTS_GAIN_DB)
            return
        vol_db, muted = result
        if muted:
            self._tts.set_gain_db(self._tts.MIN_TTS_GAIN_DB)
            return
        rms_pair = await self._camilla.get_playback_rms(best_effort=True)
        rms = max(rms_pair) if rms_pair is not None else float("-inf")
        windowed = self._record_rms(rms)
        self._maybe_update_anchor(windowed)
        self._apply_gain(vol_db, windowed)

    async def start(self) -> None:
        await self.apply_now()
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
                await asyncio.sleep(self.POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                return
            if self._paused:
                continue
            result = await self._camilla.get_volume_and_mute(best_effort=True)
            if result is None:
                # Camilla restart blip — hold last gain rather than
                # blasting at TTS_FULL. This poll runs at ~1 Hz; we'll
                # pick up the new state on the next iteration.
                continue
            vol_db, muted = result
            if muted:
                self._tts.set_gain_db(self._tts.MIN_TTS_GAIN_DB)
                continue
            # Debounced persistence catches external main_volume changes
            # (mpc, hardware knob, anything that bypasses our voice
            # tools). Voice-tool-driven changes already persist
            # immediately via tools/audio.py; this is the catch-all.
            if self._volume_persistence is not None:
                self._volume_persistence.maybe_save(vol_db)
            rms_pair = await self._camilla.get_playback_rms(best_effort=True)
            rms = max(rms_pair) if rms_pair is not None else float("-inf")
            windowed = self._record_rms(rms)
            self._maybe_update_anchor(windowed)
            self._apply_gain(vol_db, windowed)


def _build_system_instruction(
    location: str = "",
    *,
    google_accounts: list[str] | None = None,
    default_google_account: str = "",
    transit_configured: bool = True,
    ha_configured: bool = True,
    hostname: str = "jts.local",
) -> str:
    """Return the system instruction with current local time, the
    user's home location, and the linked Google account names
    injected.

    Called at every connection (re)open — the persistent connection
    lives across the 5-min context-reset window, so calling this on
    every fresh open keeps the time accurate to within that window.

    `location` should be the user's home location (a city/neighborhood
    string the geocoder can resolve). When set, the model stops asking
    "what city are you in?" for location-sensitive questions — both
    inside the weather tool's scope (weather/sunset/sunrise/forecast,
    all returned by get_weather) and outside it (nearby places,
    traffic — for which we have no tool and the model must refuse).

    `google_accounts` should be the list of household-member labels
    that have linked Google accounts (e.g. ["jasper", "brittany"]).
    When non-empty, the addendum tells the model which `account`
    values are valid for the calendar/gmail tools. Account changes
    in the wizard trigger a `systemctl restart jasper-voice`, so
    capturing the list at startup is fine — the lambda re-reads on
    every connection open within the same daemon lifetime, but the
    list itself only changes across restarts."""
    from datetime import datetime
    now_local = datetime.now().astimezone()
    # The session-open timestamp is provided as orienting context only —
    # it goes stale across the session's lifetime (potentially many
    # hours; idle context-reset is opt-in and default off). For any
    # actual time/date question, the model is told above to call
    # get_current_time. Don't tell the model "use this directly" for
    # time queries — that's the staleness bug the tool exists to fix.
    addendum = (
        f" Session opened at {now_local.strftime('%A, %B %-d %Y, %-I:%M %p %Z')}"
        f" ({now_local.tzname()}). For the actual current time, day, "
        "or date, call get_current_time — the session-open timestamp "
        "above goes stale within hours."
    )
    if location:
        addendum += (
            f" The user's home location is {location}. Use this directly "
            "for any location-sensitive question (weather, sunset/sunrise, "
            "nearby places, local time elsewhere) — do not ask the user "
            "where they are."
        )
    if google_accounts:
        names = ", ".join(google_accounts)
        default = default_google_account or google_accounts[0]
        addendum += (
            f" Linked Google accounts on this speaker: {names} "
            f"(default: {default}). When the user names a person whose "
            f"calendar or email they want, pass that name as the "
            f"`account` arg to the calendar/gmail tools. When no person "
            f"is named, omit the `account` arg — the default ({default}) "
            f"is used. If the user names someone who isn't in this list, "
            f"ask which linked account to use."
        )
    if not transit_configured:
        # Conditional rule (not absolute) per the provider-prompt
        # guidance in CLAUDE.md. Models obey "in this specific case,
        # say X" better than "never do Y". Provider-agnostic phrasing
        # — no mention of Gemini/OpenAI/Grok.
        # Hostname is interpolated so multi-Pi households see the
        # right speaker URL ("jts2.local/transit") rather than the
        # default. cfg.hostname is the canonical source.
        addendum += (
            " Transit tools (subway, bus, Citi Bike) aren't set up on "
            "this speaker yet — no get_subway_arrivals, get_bus_arrivals, "
            "or get_citibike_status tool is available. If the user asks "
            "about the next train, the next bus, or Citi Bike, briefly "
            f"say: 'Transit isn't set up yet — visit {hostname}/transit "
            "to configure it.' Don't promise to check or look it up; "
            "the data source is genuinely absent."
        )
    if not ha_configured:
        # Same conditional pattern as transit above. Critical that the
        # model also DOES NOT call any other tool in this case — we've
        # observed (May 22 voice log) the model misrouting "turn on the
        # bedroom lights" to get_current_time + get_now_playing when no
        # home_assistant tool exists. The "do not call any other tool"
        # clause prevents that misroute. The specific URL with the
        # configured hostname lets the user actually find the wizard
        # — multi-speaker households on the same LAN have
        # jts2.local / jts3.local hostnames, so hardcoding "jts.local"
        # would point the wrong way.
        addendum += (
            " Home Assistant smart-home control isn't set up on this "
            "speaker yet — no home_assistant tool is available. If the "
            "user asks to control any smart-home device (lights, switches, "
            "thermostats, locks, blinds, scenes, scripts, household "
            "automations) or asks about the state of devices in the home, "
            f"say exactly: 'Smart-home control isn't set up yet — visit "
            f"{hostname}/ha to enable it.' Do not call any other "
            "tool in this case — not get_current_time, not get_now_playing, "
            "not get_weather. The user's request cannot be fulfilled without "
            "the home_assistant tool; redirecting them to the setup page is "
            "the correct response."
        )
    return SYSTEM_INSTRUCTION + addendum


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


def _active_model(cfg: Config) -> str:
    """Return the model name for the currently selected provider — used
    by startup-readiness logging and the silent-failure heuristic in
    `_end_turn` so journalctl shows the actual model in flight. Resolution
    lives on `Config.active_voice_model` (shared with jasper-doctor); the
    `<unknown:…>` sentinel keeps log lines legible for an unset provider."""
    return cfg.active_voice_model or f"<unknown:{cfg.voice_provider}>"


def _tts_ready_detail(cfg: Config) -> str:
    """Return the startup-log fields for the selected TTS transport."""
    if cfg.tts_transport == "outputd":
        return f"tts_transport=outputd tts_socket={cfg.tts_outputd_socket}"
    return (
        f"tts_transport={cfg.tts_transport} "
        f"tts_device={cfg.tts_device}"
    )


def _make_connection(cfg: Config) -> LiveConnection:
    """Construct the long-lived voice connection for the active provider.

    Single switch point — `JASPER_VOICE_PROVIDER` selects which adapter
    runs. Daemon code above this function is provider-agnostic; daemon
    code below it talks only to the `LiveConnection` / `LiveTurn`
    Protocols and works equally for any provider that implements them.

    Adapter modules are imported lazily inside each branch. Loading
    `gemini_session` pulls in `google.genai` (~49 MB resident); loading
    `openai_session`/`grok_session` skips that cost when the active
    provider isn't Gemini. Symmetric for the OpenAI/Grok branches."""
    if cfg.voice_provider == "gemini":
        from .voice.gemini_session import GeminiLiveConnection
        return GeminiLiveConnection(
            api_key=cfg.gemini_api_key,
            model=cfg.gemini_model,
            voice=cfg.gemini_voice,
            context_reset_sec=float(cfg.gemini_context_reset_sec),
        )
    if cfg.voice_provider == "openai":
        from .voice.openai_session import OpenAIRealtimeConnection
        return OpenAIRealtimeConnection(
            api_key=cfg.openai_api_key,
            model=cfg.openai_model,
            voice=cfg.openai_voice,
            reasoning_effort=cfg.openai_reasoning_effort,
            context_reset_sec=float(cfg.openai_context_reset_sec),
            session_max_sec=float(cfg.openai_session_max_sec),
            proactive_buffer_sec=float(cfg.openai_proactive_buffer_sec),
        )
    if cfg.voice_provider == "grok":
        from .voice.grok_session import GrokRealtimeConnection
        return GrokRealtimeConnection(
            api_key=cfg.grok_api_key,
            model=cfg.grok_model,
            voice=cfg.grok_voice,
            context_reset_sec=float(cfg.grok_context_reset_sec),
            session_max_sec=float(cfg.grok_session_max_sec),
            proactive_buffer_sec=float(cfg.grok_proactive_buffer_sec),
        )
    raise RuntimeError(f"unsupported voice provider: {cfg.voice_provider}")


def _build_cues_manager(
    cfg: Config, tts: TtsPlayout | None = None,
) -> AudioCueManager:
    """Construct the audio-cue manager. Hostname for templates is
    extracted from JASPER_MANAGEMENT_URL ("https://jts.local" →
    "jts.local") so cues say "visit jts.local" rather than reading
    out the full URL with scheme/path. The TTS backend is picked
    by the shared `build_cue_tts_backend` factory so daemon and
    `jasper-cues` CLI dispatch identically.

    `tts` may be None at construction time when the daemon needs to
    register cue-aware tools (timer pre-render) before the
    TtsPlayout has opened. Call `attach_tts` later once it does."""
    import urllib.parse
    hostname = (
        urllib.parse.urlparse(cfg.management_url).hostname or "this speaker"
    )
    backend, voice = build_cue_tts_backend(cfg)
    if backend is not None:
        logger.info(
            "cue tts: provider=%s model=%s voice=%s",
            cfg.voice_provider, getattr(backend, "model", "?"), voice,
        )
    return AudioCueManager(
        sounds_dir=cfg.sounds_dir,
        hostname=hostname,
        voice=voice,
        backend=backend,
        tts_playout=tts,
    )


def _schedule_cue_regen(manager: AudioCueManager) -> None:
    """Background task: bake any missing / stale cues. Failures
    (network down, API key wrong, quota) are logged but never raised
    — the daemon should still come up if regeneration can't run."""
    async def _run() -> None:
        try:
            written = await asyncio.to_thread(manager.regenerate)
        except RuntimeError as e:
            logger.warning("cue regen skipped: %s", e)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("cue regen failed: %s", e)
            return
        if written:
            logger.info("cue regen wrote %d new cue(s): %s", len(written), written)
        else:
            logger.info("cue regen: all cues already cached")

    asyncio.create_task(_run(), name="jasper-cues-regen")


def _build_router(cfg: Config) -> Router | None:
    """Build the multi-account spotify router, or None if Spotify
    isn't configured at the env level.

    The returned router carries a `rebuild_fn` so it can recover from
    a startup-time revocation (or a re-link via the web wizard)
    without a daemon restart: when `router.clients` is empty, the next
    tool call triggers a rebuild via Router.refresh_if_empty(). The
    rebuild also picks up a wizard-changed default account (POST
    /default mutates registry.default_name; BuildResult carries it
    forward; Router.refresh_if_empty updates self.default_name)."""
    if not cfg.spotify_enabled:
        return None

    def _do_build() -> BuildResult:
        # Re-load the registry on every build — the wizard may have
        # added/removed accounts, written a fresh cache file, or
        # changed the default since the daemon started.
        # maybe_migrate_legacy is a no-op after the first call so it's
        # safe to run each time.
        accounts = Registry.load(cfg.spotify_accounts_path)
        maybe_migrate_legacy(
            accounts, cfg.spotify_cache_path, default_name="default",
        )
        return build_clients(
            accounts,
            client_id=cfg.spotify_client_id,
            redirect_uri=cfg.spotify_redirect_uri,
        )

    result = _do_build()
    if not result.clients:
        # Surface the per-account reasons at startup so a "Spotify
        # tools are silent" report has a forensic trail.
        logger.info(
            "event=spotify.startup_empty statuses=%s setup_url=%s",
            [(s.name, s.state) for s in result.statuses],
            cfg.spotify_setup_url,
        )
    return Router(
        clients=result.clients,
        default_name=result.default_name,
        statuses=result.statuses,
        rebuild_fn=_do_build,
    )


def _build_registry(
    cfg: Config,
    camilla: CamillaController,
    renderer: RendererClient,
    weather: WeatherClient,
    subway: SubwayClient | None,
    volume_coordinator: "VolumeCoordinator",
    volume_persistence: VolumePersistence | None = None,
    spotify_router: Router | None = None,
    timer_scheduler: TimerScheduler | None = None,
    cues_manager: AudioCueManager | None = None,
    google_clients: GoogleClients | None = None,
    bus: BusClient | None = None,
    ha: HAClient | None = None,
    citibike: CitiBikeClient | None = None,
    wake_event_store: "WakeEventStore | None" = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    for fn in make_audio_tools(volume_coordinator):
        registry.register(fn)
    # Reuse the router built once for the coordinator; if not passed,
    # build it here for backward-compat with any caller that doesn't
    # plumb the shared instance through.
    router = spotify_router if spotify_router is not None else _build_router(cfg)
    for fn in make_transport_tools(renderer, router):
        registry.register(fn)
    for fn in make_spotify_tools(
        router, renderer, cfg.spotify_device_name, cfg.spotify_setup_url,
    ):
        registry.register(fn)
    for fn in make_weather_tools(weather):
        registry.register(fn)
    for fn in make_subway_tools(subway):
        registry.register(fn)
    for fn in make_bus_tools(bus):
        registry.register(fn)
    # Citi Bike — keyless GBFS-backed. Gated on saved-station count
    # (citibike.enabled) so the model never sees a tool whose every
    # call would return zero stations. See jasper.citibike for the
    # data layer (TTL cache, stale-on-error).
    for fn in make_citibike_tools(citibike):
        registry.register(fn)
    # Home Assistant — single tool surface (home_assistant) that wraps
    # HA's /api/conversation/process endpoint. Gated on ha being non-None
    # so the model never sees a tool whose every call would fail when
    # HA isn't configured. See docs/HANDOFF-homeassistant.md for the
    # architecture rationale (conversation API, not MCP).
    for fn in make_home_assistant_tools(ha):
        registry.register(fn)
    for fn in make_time_tools():
        registry.register(fn)
    if timer_scheduler is not None:
        for fn in make_timer_tools(timer_scheduler):
            registry.register(fn)
    # Calendar + Gmail are gated on (a) CLIENT_ID/SECRET being present
    # AND (b) at least one account having an OAuth refresh token. The
    # tool factories return [] when their accessor is unusable, but we
    # also skip registration when there are zero accounts so the model
    # doesn't see tools whose every call would fail with "no accounts
    # linked". The wizard at /google triggers a daemon restart on add,
    # so a fresh OAuth flow makes the tools appear on the next session.
    if google_clients is not None and google_clients.list_account_names():
        for fn in make_calendar_tools(google_clients):
            registry.register(fn)
        for fn in make_gmail_tools(google_clients):
            registry.register(fn)
    # Diagnostic tools (flag_recent_issue). Gated on the wake-event
    # store being open — when telemetry is disabled the flag tool
    # can't actually persist anything, so the model never sees it.
    # See jasper/tools/diagnostic.py + jasper/wake_events.py
    # `record_flag` for the storage semantics. Registered HERE (not
    # later) because the LLM session sends `session.update` with the
    # tool list immediately after WS handshake; tools registered
    # after that point are invisible to the live session until the
    # next reconnect.
    for fn in make_diagnostic_tools(wake_event_store):
        registry.register(fn)
    return registry


async def _turn_audio_chunks(turn: LiveTurn):
    chunks = getattr(turn, "audio_out_chunks", None)
    if callable(chunks):
        async for chunk in chunks():
            if isinstance(chunk, bytes):
                chunk = AudioOutChunk(pcm=chunk)
            yield chunk
        return
    async for pcm in turn.audio_out():
        yield AudioOutChunk(pcm=pcm)


async def _play_responses(
    turn: LiveTurn,
    tts: TtsPlayout,
    *,
    on_source_pcm: Callable[[bytes], None] | None = None,
) -> None:
    """Drain turn.audio_out() to the speaker. Barge-in handling: race
    each write against an interrupt signal so a user-interrupted-the-model
    event immediately cancels in-flight playback and flushes the audio
    buffer. Without this, ALSA/sounddevice buffering causes 100-300ms of
    overrun where the model talks over the user.

    Cleanup contract: both per-iteration helpers (the interrupt waiter
    and the in-flight write) MUST be cancelled and awaited before this
    function returns, otherwise they leak as `Task destroyed but it is
    pending` warnings. The waiter is held alive by a reference cycle
    through `turn._interrupt_event`, so dropping the local without
    explicit cleanup means GC eventually breaks the cycle and Task.__del__
    fires. The OpenAI / Grok adapters never set `_interrupt_event` (no
    barge-in implemented), so the waiter is always pending at turn end
    and the leak would fire every turn without this try/finally."""
    interrupt_task: asyncio.Task | None = None
    write_task: asyncio.Task | None = None
    try:
        async for chunk in _turn_audio_chunks(turn):
            # Tee the raw provider PCM to the loudness meter before the
            # write race so the per-provider source estimate stays fresh
            # even for chunks a barge-in later flushes.
            if on_source_pcm is not None:
                on_source_pcm(chunk.pcm)
            if interrupt_task is None or interrupt_task.done():
                interrupt_task = asyncio.create_task(turn.wait_for_interrupt())
            write_task = asyncio.create_task(
                tts.write_segment(
                    chunk.pcm,
                    provider_item_id=chunk.provider_item_id,
                    segment_kind=chunk.kind,
                )
            )
            done, _ = await asyncio.wait(
                {write_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupt_task in done:
                write_task.cancel()
                try:
                    await write_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                ack = await tts.flush()
                flush_handler = getattr(turn, "on_tts_flush", None)
                if callable(flush_handler):
                    await flush_handler(ack)
                if ack is not None:
                    logger.info(
                        "event=tts_flush.playout_ack max_audio_played_ms=%s "
                        "segments=%s flushed_frames=%s",
                        ack.get("max_audio_played_ms"),
                        ack.get("segments"),
                        ack.get("flushed_frames"),
                    )
                turn.clear_interrupted()
                interrupt_task = None
            elif write_task in done:
                try:
                    await write_task
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "event=tts_write.failed error=%s detail=%s",
                        type(e).__name__, e,
                    )
                    raise
                finally:
                    write_task = None
            if write_task is not None and write_task.done():
                write_task = None
        await tts.end_segment()
        # Block until the last sample we wrote has cleared the OS
        # audio stack — see TtsPlayout.wait_drained. Cheap if the ring
        # is already empty; otherwise a single sleep for the residual.
        # Anchors on samples queued (not network arrivals), so an
        # OpenAI-style burst delivery and a Gemini-style real-time
        # pacing both end the turn at the right moment.
        await tts.wait_drained()
    finally:
        for t in (interrupt_task, write_task):
            if t is None or t.done():
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _idle_watchdog(
    turn: LiveTurn, tts: TtsPlayout, timeout: float,
) -> None:
    """Close the turn based on explicit server-side signals where
    possible, falling back to a timer when the server stays silent.

    Three cases:
      * `turn.server_turn_complete()` is True → server says "model is
        done speaking". Defer while audio remains in flight, anchored
        on TtsPlayout's sample-counted drain deadline (see
        ``expected_drain_at``). Canonical clean close.
      * No chunks received yet → model hasn't started speaking;
        wait the full `timeout` for the first chunk to arrive (Live
        API can take 3-5 s, sometimes longer).
      * Chunks arriving but turn_complete hasn't fired → mid-response
        chunk gaps can be > 1.5 s during normal speech pauses, so a
        timer here would race with real output. Wait for either
        turn_complete (case 1) or connection drop.

    Coordinates with ``_play_responses``: the consumer awaits
    ``tts.wait_drained()`` after its final write, while this watchdog
    polls ``expected_drain_at()`` cooperatively. Both consult the same
    drain anchor, so whichever observes "drained" first triggers
    ``_end_turn`` (via the bg-task done check at
    ``_handle_session_frame``). End-of-turn drain timing is logged
    by ``_end_turn`` itself so observability is symmetric across
    whichever side wins the race."""
    while True:
        await asyncio.sleep(0.25)
        if turn.turn_lost():
            logger.warning("idle watchdog: connection lost mid-turn, ending turn")
            return
        now = time.monotonic()
        idle_for = now - turn.last_activity_at()
        if turn.server_turn_complete():
            # Defer while chunks are still queued in the inter-task
            # buffer — the consumer hasn't yet pushed them to TtsPlayout.
            pending_getter = getattr(turn, "audio_chunks_pending", None)
            if callable(pending_getter) and pending_getter() > 0:
                continue
            if tts.expected_drain_at() > now:
                continue
            return
        any_chunk_received = turn.last_chunk_at() > 0
        if not any_chunk_received and idle_for > timeout:
            logger.info(
                "idle timeout (pre-response phase, %.1fs); no chunks, ending turn",
                float(timeout),
            )
            return


async def _server_vad_response_trigger(turn, connection) -> None:
    """Wait for the server's VAD to signal end-of-utterance, then fire
    response.create. Only spawned when server_vad is active for the turn."""
    wait_eou = getattr(turn, "wait_for_server_eou", None)
    if wait_eou is None or not callable(wait_eou):
        return
    try:
        await asyncio.wait_for(wait_eou(), timeout=NO_SPEECH_ABORT_SEC + 5.0)
    except asyncio.TimeoutError:
        logger.warning("event=server_vad.eou_timeout")
        return
    except asyncio.CancelledError:
        raise
    if turn.turn_lost():
        return
    create = getattr(connection, "_create_response_only", None)
    if create is not None and callable(create):
        try:
            await create()
            logger.info("event=server_vad.response_create")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "event=server_vad.response_create_failed error=%s: %s",
                type(e).__name__, e,
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
# from the token. A 4th leg adds an entry here + the matching additive
# columns in jasper.wake_events.
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
}

# Which Config field carries each wake leg's mic device string. Kept here
# (a voice-daemon construction concern) rather than on the frozen
# jasper.wake_legs registry, which stays a pure cross-process identity
# table. Note the deliberate token/field-name skew: the chip-direct leg's
# token is "off" but its device var is cfg.mic_device_raw — the
# operator-facing "raw" vocabulary (JASPER_MIC_DEVICE_RAW). The reconciler
# sets/clears these vars from the JASPER_WAKE_LEG_* booleans; an empty
# string means the leg is not configured. A 4th leg adds an entry here.
_LEG_DEVICE_ATTR: dict[str, str] = {
    "on": "mic_device",
    "off": "mic_device_raw",
    "dtln": "mic_device_dtln",
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
        ducker: Ducker,
        tts_volume_tracker: TtsVolumeTracker,
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
    ) -> None:
        self._cfg = cfg
        self._tts = tts
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
        self._connection = connection
        self._ducker = ducker
        # Direct camilla handle for `CueDuck` (snapshot-based duck
        # around dynamic-text cues). Optional for back-compat with
        # tests / out-of-tree callers; without it, dynamic-text cues
        # play unducked rather than crashing.
        self._camilla = camilla
        self._tts_volume_tracker = tts_volume_tracker
        self._usage_store = usage_store
        self._spend_cap = spend_cap
        self._stop_event = stop_event
        self._volume_coordinator = volume_coordinator
        self._cues = cues
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
        self._bg_tasks: set[asyncio.Task] = set()
        self._refractory_until: float = 0.0

        # Room-correction measurement window. When set, the WakeLoop
        # drops mic frames (no wake-word feed, no session forward) and
        # the TtsVolumeTracker is paused so it doesn't read the sweep
        # as "loud music" and skew the loudness anchor. Set / cleared
        # via the MEASURE_PAUSE / MEASURE_RESUME UDS commands; the
        # `_measurement_safety_task` auto-clears the event after 2 min
        # so a coordinator crash can't strand the speaker silent.
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

        # Pre-render the two-tone listening chirps once. Synthesis is
        # pure (no instance state used), so caching the PCM bytes
        # keeps the wake-to-audio hot path off any per-call cost.
        # Same shape `TtsPlayout.write()` accepts (24 kHz int16 mono).
        self._chirp_on_pcm: bytes = self._generate_listening_chirp(going_on=True)
        self._chirp_off_pcm: bytes = self._generate_listening_chirp(going_on=False)

        # Monotonic wallclock at the moment wake fires. Used by
        # _begin_turn to break the wake→activity_start latency into
        # named segments (state reset, tts-tracker apply, duck,
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

    async def play_cue(self, slug: str) -> str:
        """Public wrapper for `_play_cue`, callable via the control
        socket so external clients (jasper-control HTTP, the
        `jasper-cues play` CLI) can play cues through the daemon's
        already-correctly-gained TtsPlayout.

        Standalone clients can't easily replicate the daemon's
        TtsVolumeTracker math; routing through here means they
        don't have to."""
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
            # Cancel + join every leg loop on any exit path. Without this
            # a task could outlive run() and keep scoring frames against a
            # stopped detector / closed mic.
            for _t in leg_tasks:
                _t.cancel()
            for _t in leg_tasks:
                try:
                    await _t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

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
        """Open a measurement window. Set the gate event, pause the
        TTS volume tracker, and arm a 2-minute auto-clear safety
        timer.

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
        self._tts_volume_tracker.pause()

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
                self._tts_volume_tracker.resume()

        # Note: this is a fire-once-and-exit task that we deliberately
        # do NOT add to self._bg_tasks — the WakeLoop run loop's
        # bg-task done-checker treats any done task as "turn ended
        # early," so adding short-lived tasks there would corrupt the
        # turn lifecycle. Single-slot reference is enough; we cancel
        # via that slot on RESUME or repeated PAUSE.
        self._measurement_safety_task = loop.create_task(_safety())
        return "ok"

    def _generate_mute_click(self, *, going_on: bool) -> bytes:
        """Synthesize a short decay-sine click as 24 kHz int16 mono PCM
        — same shape `TtsPlayout.write()` accepts. Higher pitch on
        unmute, lower on mute, so the user gets a directional cue.

        Intentionally not a registered cue: cues are TTS-generated and
        spoken, this is a sub-100 ms synthesized blip. Kept inline so
        the cue cache / regen system isn't paid for two trivial WAVs.
        """
        import math
        sr = 24000
        dur_samples = int(sr * 0.06)  # 60 ms
        freq = 900.0 if going_on else 600.0
        peak = 0.25  # ~-12 dBFS before TtsPlayout's gain stage
        out = bytearray(dur_samples * 2)
        for i in range(dur_samples):
            t = i / sr
            env = math.exp(-t * 50.0)  # ~20 ms half-life
            s = int(math.sin(2.0 * math.pi * freq * t) * env * peak * 32767.0)
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            # little-endian int16
            out[2 * i] = s & 0xFF
            out[2 * i + 1] = (s >> 8) & 0xFF
        return bytes(out)

    async def _play_mute_click(self, *, going_on: bool) -> None:
        """Best-effort. If the TTS stream isn't open or write fails,
        the visual feedback on the web UI is enough — never raise."""
        try:
            await self._tts.write(self._generate_mute_click(going_on=going_on))
        except Exception as e:  # noqa: BLE001
            logger.warning("mic mute click failed: %s", e)

    def _generate_listening_chirp(self, *, going_on: bool) -> bytes:
        """Synthesize a two-tone listening cue as 24 kHz int16 mono PCM
        — same shape `TtsPlayout.write()` accepts. Wake = ascending
        musical fifth in the upper register (A5 880 Hz → E6 1320 Hz);
        end-of-turn = descending fifth one octave lower (E5 660 Hz →
        A4 440 Hz). Same interval shape so the pair reads as a matched
        family; distinct registers so "starting" vs "ending" lands
        without the listener having to think about it. End-chirp's
        highest note (660 Hz) sits below the wake-chirp's lowest note
        (880 Hz) so the contrast is unmistakable. Phase-continuous
        through the note change so each pair reads as one connected
        cue rather than two beeps.

        Distinct from `_generate_mute_click`: two-note interval (vs.
        single-tone decay) so start/stop listening is clearly
        different from mic mute/unmute. Inline for the same reason
        as the mute click — sub-100 ms synthesized blip, not worth
        a TTS-cached WAV.
        """
        import math
        sr = 24000
        seg_samples = int(sr * 0.035)  # 35 ms per note → 70 ms total
        total = seg_samples * 2
        ramp = int(sr * 0.005)  # 5 ms cosine attack/release
        if going_on:
            f1, f2 = 880.0, 1320.0  # wake: upper register, ascending
        else:
            f1, f2 = 660.0, 440.0   # end: lower register, descending
        peak = 0.18  # ~-15 dBFS — subtler than mute click since these fire often
        out = bytearray(total * 2)
        phase = 0.0
        for i in range(total):
            freq = f1 if i < seg_samples else f2
            phase += 2.0 * math.pi * freq / sr
            if i < ramp:
                env = 0.5 * (1.0 - math.cos(math.pi * i / ramp))
            elif i >= total - ramp:
                env = 0.5 * (1.0 - math.cos(math.pi * (total - i) / ramp))
            else:
                env = 1.0
            s = int(math.sin(phase) * env * peak * 32767.0)
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            out[2 * i] = s & 0xFF
            out[2 * i + 1] = (s >> 8) & 0xFF
        return bytes(out)

    async def _play_listening_chirp(self, *, going_on: bool) -> None:
        """Best-effort. If the TTS stream isn't ready, the wake or
        end-of-turn happens anyway — never raise. PCM is pre-rendered
        in __init__ to keep this off the wake hot path."""
        try:
            pcm = self._chirp_on_pcm if going_on else self._chirp_off_pcm
            await self._tts.write(pcm)
        except Exception as e:  # noqa: BLE001
            logger.warning("listening chirp failed: %s", e)

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
        write_mic_muted(self._cfg.mic_mute_state_path, True)
        logger.info("event=mic.mute")
        await self._play_mute_click(going_on=False)
        return "ok"

    async def unmute_mic(self) -> str:
        """Resume listening. Idempotent."""
        if not self._mic_muted:
            return "ok"
        self._mic_muted = False
        write_mic_muted(self._cfg.mic_mute_state_path, False)
        logger.info("event=mic.unmute")
        await self._play_mute_click(going_on=True)
        return "ok"

    async def measurement_resume(self) -> str:
        """Close a measurement window: clear the gate, resume the
        tracker, cancel the safety timer.

        Idempotent — calling twice (or before any PAUSE) is harmless.
        Always returns "ok".
        """
        self._measurement_active.clear()
        self._tts_volume_tracker.resume()
        if self._measurement_safety_task is not None:
            if not self._measurement_safety_task.done():
                self._measurement_safety_task.cancel()
            self._measurement_safety_task = None
        return "ok"

    async def _handle_wake_frame(self, frame, *, leg: str = "on") -> None:
        """Score one frame on the named leg. Legs:
          - 'on'   → post-AEC3 BEST_A (primary, the session audio source)
          - 'off'  → chip-direct raw mic (no AEC; the original dual-stream
                     fallback)
          - 'dtln' → DTLN-aec output (the triple-stream tertiary leg
                     added 2026-05-23 per the triple-stream plan)

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

        if score < self._fuser.effective_threshold(
            leg, self._current_condition, detector.threshold,
        ):
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

        import time as _time
        self._wake_event_at_monotonic = _time.monotonic()
        # Per-leg score summary for the log: each configured leg's most-
        # recent score, or "none" if the leg is unconfigured or its last
        # score is stale (a stopped stream shouldn't show a misleading old
        # value). The firing leg's recent_score == `score` (just set).
        _parts = []
        for _n in _LEG_DB:
            _lr = self._legs.get(_n)
            if _lr is None or (
                _n != leg
                and (_lr.recent_score_at == 0.0
                     or (now_loop - _lr.recent_score_at) > WAKE_STALE_SCORE_SEC)
            ):
                _parts.append(f"score_{_n}=none")
            else:
                _parts.append(f"score_{_n}={_lr.recent_score:.2f}")
        logger.info(
            "event=wake.detected leg=%s %s threshold=%.2f fired=%s",
            leg, " ".join(_parts), detector.threshold, fired_legs,
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
        # `getattr` so this stays safe for tests that build WakeLoop
        # via `__new__` + manual attribute init (peering tests,
        # dual-stream wake-handler tests).
        store = getattr(self, "_wake_event_store", None)
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
            # Music context — best-effort from the TtsVolumeTracker's
            # cached anchor (the loudness it last observed on the
            # music chain via the 1-Hz `_anchor` poll). That value is
            # already maintained without async I/O, so reading it on
            # the wake hot path is free. Not a renderer probe (would
            # add ~50 ms of async work); the anchor is a recent-ish
            # cached number, accurate to within ~1 s.
            music_volume_db = None
            music_active_proxy = False
            tracker = getattr(self, "_tts_volume_tracker", None)
            if tracker is not None:
                # `_anchor_dbfs` is the most recent observed
                # music-chain RMS in dBFS (defaults to
                # DEFAULT_ANCHOR_DBFS until the first tick observes
                # real music). Proxy: louder than -60 dBFS = "music
                # probably playing." Imperfect (TTS uses the same
                # chain) but useful for FP correlation.
                anchor = getattr(tracker, "_anchor_dbfs", None)
                if anchor is not None and anchor > -120.0:
                    music_volume_db = float(anchor)
                    music_active_proxy = anchor > -60.0
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
                    threshold=self._detector.threshold,
                    wake_model=self._cfg.wake_model,
                    voice_provider=getattr(self._cfg, "voice_provider", None),
                    bridge_config=bridge_config,
                    music_active=music_active_proxy,
                    music_volume_db=music_volume_db,
                    condition_class=condition_ctx.condition,
                    mic_muted=getattr(self, "_mic_muted", None),
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
                asyncio.create_task(
                    self._finalize_event_audio(self._current_event_id),
                    name="wake-event-audio-finalize",
                )

        # Spawn the arbitrate+acquire+drain pipeline as a background
        # task so the main mic loop stays responsive (frames continue
        # piling into _acquire_buffer for up to 20 s — see
        # ACQUIRE_BUFFER_MAX_FRAMES). When peering is disabled this
        # task immediately proceeds to the existing acquire-and-drain
        # flow; when enabled, it first awaits the peering UDS verdict.
        asyncio.create_task(
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
        """Wait the post-fire collection window, then snapshot both
        capture rings and persist as WAV files via the store.

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
            audio_on = self._snapshot_ring(self._capture_ring_on, n_frames)
            audio_off = self._snapshot_ring(self._capture_ring_off, n_frames)
            audio_dtln = self._snapshot_ring(
                getattr(self, "_capture_ring_dtln", None), n_frames,
            ) if getattr(self, "_capture_ring_dtln", None) else None
            await self._wake_event_store.attach_audio(
                event_id=event_id,
                audio_on=audio_on,
                audio_off=audio_off,
                audio_dtln=audio_dtln,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: attach_audio failed for %s: %s", event_id, e,
            )

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

        Uses `getattr` so it stays safe for callers that construct
        WakeLoop via `__new__` + manual attribute init (the peering
        tests do this) and don't populate the telemetry attrs."""
        store = getattr(self, "_wake_event_store", None)
        event_id = getattr(self, "_current_event_id", None)
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
        event. Same fail-soft + `getattr`-tolerant pattern as
        `_telemetry_stage`. Clears `_current_event_id` after the
        write so subsequent funnel hooks for the next wake start
        clean."""
        store = getattr(self, "_wake_event_store", None)
        event_id = getattr(self, "_current_event_id", None)
        if store is None or event_id is None:
            # Still clear the id (if it exists) so the next wake
            # starts from a clean state.
            if hasattr(self, "_current_event_id"):
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

        On error in step 3: play `cant_connect`, cleanup, clear buffer,
        return to WAKE. The `_acquiring` flag flips back to False in
        the finally so the loop returns to wake detection.
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
                logger.info("event=peering.wake.lost score=%.2f", score)
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
            # "Now listening" chirp. Fire-and-forget so it plays in
            # parallel with `_begin_turn` opening rather than adding
            # ~70 ms to time-to-listen. NOT added to self._bg_tasks —
            # any done task in that set would end the turn early.
            asyncio.create_task(
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
            await self._play_cue("cant_connect")
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
        the late-cancel."""
        if self._mic_muted:
            logger.info("event=wake.late_cancel reason=mic_muted phase=%s", phase)
            return True
        if self._measurement_active.is_set():
            logger.info(
                "event=wake.late_cancel reason=measurement_active phase=%s",
                phase,
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
                logger.info(
                    "event=server_vad.no_speech timeout_sec=%.1f",
                    NO_SPEECH_ABORT_SEC,
                )
                await self._end_turn()
                return

            if elapsed >= HARD_RECORDING_CAP_SEC:
                logger.info(
                    "event=server_vad.hard_cap elapsed_sec=%.1f",
                    HARD_RECORDING_CAP_SEC,
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
        wake does: spend cap and connection-paused. Returns one of
        OK / BUSY / CAP / PAUSED / ERROR for the caller's logging.
        """
        if self._state is State.SESSION:
            return "BUSY"
        if not self._spend_cap.allowed():
            return "CAP"
        if self._connection.is_paused():
            return "PAUSED"
        asyncio.create_task(
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
            # Measured per-provider TTS source loudness (dBFS peak) so the
            # learned level is observable on /state without log-grepping.
            "tts_source_peak_dbfs": round(
                self._tts_volume_tracker.source_peak_dbfs, 1,
            ),
            # Actually-armed wake legs (runtime truth, by jasper.wake_legs
            # token order). /aec reports configured *intent* from
            # aec_mode.env; this is what the daemon actually opened, so a
            # startup leg-skip (event=wake.leg_skipped) is visible in
            # /state.voice, not only in the journal.
            "wake_legs": list(self._legs),
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
                logger.info(
                    "event=shadow_vad.raw_armed elapsed_ms=%d silero=%.2f",
                    elapsed_ms, speech_prob,
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
        # Pin TTS gain to the user's pre-duck master volume + offset
        # BEFORE ducking. The duck about to fire will drop main_volume
        # by JASPER_DUCK_DB; if we let the tracker observe that drop,
        # TTS would go quiet for the response we're about to play —
        # exactly backward (we duck music so the user can hear TTS).
        # Pause the tracker for the lifetime of the turn so it doesn't
        # re-read main_volume mid-turn.
        await self._tts_volume_tracker.apply_now()
        self._tts_volume_tracker.pause()
        # Tell the volume coordinator a session is active so its
        # source-transition handler doesn't fight the ducker's
        # additive math on camilla.
        self._volume_coordinator.note_voice_session(True)
        t_after_tts_apply = _time.monotonic()
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
            and self._tts_volume_tracker.music_is_playing()
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
                    mark = getattr(self._turn, "_mark_server_vad", None)
                    if callable(mark):
                        mark()
                    logger.info(
                        "event=server_vad.enabled music_anchor_dbfs=%.1f",
                        self._tts_volume_tracker._anchor_dbfs,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "event=server_vad.enable_failed error=%s: %s",
                        type(e).__name__, e,
                    )

        logger.info(
            "turn acquire done in %.0fms "
            "(sched_lag=%.0f state=%.0f tts_apply=%.0f duck=%.0f acquire=%.0f) "
            "(wake→activity_start%s)",
            (_time.monotonic() - t_wake) * 1000,
            (t_begin - t_wake) * 1000,
            (t_after_state - t_begin) * 1000,
            (t_after_tts_apply - t_after_state) * 1000,
            (t_after_duck - t_after_tts_apply) * 1000,
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
            _play_responses(
                self._turn, self._tts,
                on_source_pcm=self._tts_volume_tracker.note_source_chunk,
            )
        )
        idle = asyncio.create_task(
            _idle_watchdog(self._turn, self._tts, self._cfg.idle_timeout_sec)
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
        self._tts_volume_tracker.resume()
        if self._session_id is not None:
            self._usage_store.close_session(self._session_id, 0, 0)
        self._turn = None
        self._session_id = None
        self._bg_tasks = set()
        self._state = State.WAKE
        self._refractory_until = asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC

    async def _end_turn(self, reason: str = "ended") -> None:
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
        session_vad_store = getattr(self, "_wake_event_store", None)
        session_vad_eid = getattr(self, "_current_event_id", None)
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
                    music_playing_at_turn=self._tts_volume_tracker.music_is_playing(),
                    music_db_at_turn=self._tts_volume_tracker._anchor_dbfs,
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
            logger.info(
                "turn ended: %s tokens, est $%.4f (sent=%dB, recv=%d chunks%s%s)",
                tokens, cost, bytes_sent, chunks_received, drain_part,
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
        # Resume the TTS volume tracker AFTER the duck has been
        # restored, so the next poll reads the user's actual master
        # volume, not the still-ducked one.
        self._tts_volume_tracker.resume()
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


async def _start_control_socket(
    wake_loop: WakeLoop, socket_path: str,
) -> asyncio.AbstractServer:
    """Listen for one-line commands on a Unix domain socket so external
    daemons (jasper-control, in particular) can drive voice-session
    state without going through the wake word.

    Wire format: line of ASCII, terminated by `\\n`. Response: a single
    JSON object terminated by `\\n`.

    Commands:
        START               → manual_session_start  (long-press begin)
        END                 → manual_session_end    (long-press release)
        STATUS              → session_status        (diagnostic snapshot)
        CUE_PLAY <slug>     → play a registered audio cue through the
                              daemon's TtsPlayout (which has the
                              tracker-set gain). Routed here so a
                              standalone CLI doesn't have to recreate
                              the volume math — and can't accidentally
                              blast at -6 dB when the daemon's at -27.
        MEASURE_PAUSE       → open a room-correction measurement
                              window. Drops mic frames, pauses the
                              TTS volume tracker. Refuses (BUSY) if a
                              session is active. Auto-clears in 2 min
                              if RESUME is never sent.
        MEASURE_RESUME      → close the measurement window.
                              Idempotent.
        MUTE                → user-driven mic mute. Drops mic frames
                              at the wake-loop gate, ends any active
                              session, plays a low-pitch click. Runtime
                              only (no persistence). Idempotent.
        UNMUTE              → resume listening. Plays a higher-pitch
                              click. Idempotent.

    The socket lives in /run (tmpfs) so it gets created fresh each boot
    via systemd's RuntimeDirectory=jasper. Both jasper-voice and
    jasper-control run as root, so default 0o600 perms are fine."""
    import json as _json

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            line = raw.decode("ascii", errors="replace").strip()
            parts = line.split(maxsplit=1)
            cmd = parts[0].upper() if parts else ""
            arg = parts[1] if len(parts) > 1 else ""
            if cmd == "START":
                result = {"result": await wake_loop.manual_session_start()}
            elif cmd == "END":
                result = {"result": await wake_loop.manual_session_end()}
            elif cmd == "STATUS":
                result = wake_loop.session_status()
            elif cmd == "CUE_PLAY":
                result = {"result": await wake_loop.play_cue(arg)}
            elif cmd == "MEASURE_PAUSE":
                result = {"result": await wake_loop.measurement_pause()}
            elif cmd == "MEASURE_RESUME":
                result = {"result": await wake_loop.measurement_resume()}
            elif cmd == "MUTE":
                result = {"result": await wake_loop.mute_mic()}
            elif cmd == "UNMUTE":
                result = {"result": await wake_loop.unmute_mic()}
            else:
                result = {"result": "UNKNOWN", "command": cmd}
            writer.write((_json.dumps(result) + "\n").encode("utf-8"))
            await writer.drain()
        except asyncio.TimeoutError:
            logger.warning("voice control socket: client read timed out")
        except Exception as e:  # noqa: BLE001
            logger.exception("voice control socket handler failed: %s", e)
            try:
                writer.write(b'{"result":"ERROR"}\n')
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # Unix-domain-socket: stale file from a crashed prior run blocks
    # bind(). Best-effort unlink first.
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    server = await asyncio.start_unix_server(handle, socket_path)
    try:
        os.chmod(socket_path, 0o660)
    except OSError as e:
        logger.warning("voice control socket chmod failed: %s", e)
    logger.info("voice control socket: %s", socket_path)
    return server


async def run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    active_model = _active_model(cfg)
    pricing = pricing_for_model(
        active_model, overrides=load_pricing_overrides(),
    )
    logger.info(
        "spend cap: provider=%s model=%s pricing=%s cap=$%.2f/day (safety x%.2f)",
        cfg.voice_provider, active_model, pricing.label,
        cfg.daily_spend_cap_usd, cfg.daily_spend_cap_safety_multiplier,
    )
    if pricing.label.startswith("unpriced:"):
        # No rate for the active model (not in the bundled dated defaults
        # nor the override). We do NOT invent one — cost will read $0 and
        # the spend cap can't bound it until a rate is entered at /voice.
        logger.warning(
            "event=pricing.unpriced model=%s — no rate available; cost "
            "estimates will be $0 and the spend cap cannot bound this "
            "model until you set a rate at http://%s/voice",
            active_model, cfg.hostname,
        )
    usage_store = UsageStore(cfg.usage_db, pricing=pricing)
    spend_cap = SpendCap(
        usage_store,
        cfg.daily_spend_cap_usd,
        cfg.daily_spend_cap_safety_multiplier,
    )

    camilla = CamillaController(cfg.camilla_host, cfg.camilla_port)
    renderer = RendererClient(
        librespot_state_path=cfg.librespot_state_path,
    )
    weather = WeatherClient(
        cfg.weather_default_location,
        cfg.weather_units,
        default_lat=cfg.weather_default_lat,
        default_lon=cfg.weather_default_lon,
        default_name=cfg.weather_default_display_name,
    )
    subway = (
        SubwayClient(
            cfg.subway_station_id,
            cfg.subway_default_direction,
        )
        if cfg.subway_enabled else None
    )
    # cfg.bus_stops is a list of (stop_id, label) pairs parsed from
    # the wizard's JASPER_BUS_STOPS env var. Split into the two
    # arguments BusClient expects: ids drive the SIRI fan-out;
    # labels drive `stop_label` on each returned arrival so the
    # voice model can name the stop in its answer.
    bus = (
        BusClient(
            stop_ids=[sid for sid, _ in cfg.bus_stops],
            api_key=cfg.mta_bustime_key,
            stop_labels={
                sid: label for sid, label in cfg.bus_stops if label
            },
        )
        if cfg.bus_enabled else None
    )
    # Citi Bike. None when no stations are saved; the tool factory
    # short-circuits to [] in that case so the model never sees an
    # always-empty tool. GBFS feeds are cached in-process (see
    # jasper.citibike) so the per-call cost is two short HTTP GETs
    # at worst, often zero.
    citibike = (
        CitiBikeClient(
            saved_stations=list(cfg.citibike_stations),
            ebike_only=cfg.citibike_ebike_only,
        )
        if cfg.citibike_enabled else None
    )
    if citibike is not None:
        logger.info(
            "citibike: enabled stations=%d ebike_only=%s",
            len(cfg.citibike_stations), cfg.citibike_ebike_only,
        )
    else:
        logger.info("citibike: disabled (no stations saved)")
    # Home Assistant client. None when JASPER_HA_URL or JASPER_HA_TOKEN
    # is unset; the tool factory short-circuits to [] in that case so
    # the model never sees a tool whose every call would fail. The
    # client owns a long-lived httpx.AsyncClient for the daemon's
    # lifetime — closed in the shutdown path below.
    ha = build_ha_client(cfg)
    if ha is not None:
        logger.info("home_assistant: enabled url=%s agent_id=%s",
                    ha.url, ha.agent_id or "(default)")
    else:
        logger.info(
            "home_assistant: disabled (set JASPER_HA_URL + JASPER_HA_TOKEN, "
            "or visit http://%s/ha to configure)",
            cfg.hostname,
        )
    # Volume coordinator: owns the canonical listening_level (0-100),
    # follows mux's effective source, and dispatches voice/dial-driven
    # changes to the right volume carrier (Camilla-master for
    # AirPlay/USB/idle, push-mode for Spotify/BT). Boot path applies
    # a safety regression to extreme stale values.
    volume_persistence = VolumePersistence(cfg.volume_state_path)
    # Build the multi-account Spotify router once; reused by both the
    # coordinator (for outbound volume control via Web API) and the
    # voice tool registry (transport / spotify_play). Same instance,
    # one OAuth refresh cycle per account.
    volume_spotify_router = _build_router(cfg)
    # Google Calendar + Gmail clients — built once, used by the tool
    # registry AND captured by the system-instruction lambda so the
    # model knows which household members have linked accounts. None
    # if Google's CLIENT_ID/SECRET aren't configured (the tools are
    # gated and never appear to the model in that case).
    google_clients = build_google_clients(cfg)
    if google_clients is not None:
        names = google_clients.list_account_names()
        if names:
            logger.info(
                "google: %d account(s) linked: %s (default: %s)",
                len(names), ", ".join(names),
                google_clients.default_account_name() or "(none)",
            )
        else:
            logger.info(
                "google: CLIENT_ID/SECRET configured but no accounts "
                "linked yet — visit %s to add one",
                cfg.google_setup_url,
            )
    volume_coordinator = VolumeCoordinator(
        camilla=camilla,
        persistence=volume_persistence,
        backend=renderer,
        spotify_router=volume_spotify_router,
        spotify_device_name=cfg.spotify_device_name,
    )
    # Ducker built after the coordinator so it can read the canonical
    # camilla target on restore (avoids additive-overshoot when other
    # writers — dial twists, voice tools — touch listening_level mid-
    # session).
    ducker = Ducker(
        camilla, cfg.duck_db,
        target_db_provider=volume_coordinator.get_camilla_target_db,
    )
    record = volume_persistence.load()
    # Loudness anchor: never expires. If the file has one, use it
    # exactly. Otherwise fall to DEFAULT_ANCHOR_DBFS (-30 dBFS = 40%
    # equivalent), which gives a conservative conversational-level TTS
    # output — neither blasting nor inaudible.
    if record is not None and record.loudness_anchor_dbfs is not None:
        initial_anchor = record.loudness_anchor_dbfs
        anchor_reason = "restored from disk"
    else:
        initial_anchor = DEFAULT_ANCHOR_DBFS
        anchor_reason = "first-boot default"
    logger.info(
        "tts loudness anchor: %s = %.1f dBFS",
        anchor_reason, initial_anchor,
    )
    try:
        target_level, restore_reason = await volume_coordinator.initialize(
            stale_after_sec=cfg.volume_regress_after_sec,
            safe_low_pct=cfg.volume_regress_safe_low_pct,
            safe_high_pct=cfg.volume_regress_safe_high_pct,
            first_boot_default_pct=cfg.volume_first_boot_default_pct,
        )
        logger.info(
            "volume coordinator: %s → listening_level=%d%%",
            restore_reason, target_level,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "volume coordinator: initialize failed (%s); proceeding with "
            "in-memory default", e,
        )

    # Inbound source-volume observers: poll shairport (DBus),
    # librespot (state file written by --onevent hook), and bluez-alsa
    # (DBus) once per second so iPhone slider movements / Spotify app
    # slider drags / BT volume button presses sync into the
    # coordinator's listening_level.
    volume_observer = VolumeObserver(
        volume_coordinator,
        librespot_state_path=cfg.librespot_state_path,
    )
    await volume_observer.start()

    # Timer scheduler — owns persistence + asyncio task lifecycle for
    # kitchen timers. Constructed BEFORE _build_registry so set_timer
    # / list_timers / cancel_timer are visible to the model from the
    # very first session.start. The on_fire announcement callback is
    # wired after WakeLoop exists (it can't fire before then anyway —
    # SQLite restore happens in scheduler.start() further down).
    timer_scheduler = TimerScheduler(db_path=cfg.timer_db_path)

    # Cue manager — built early so timer tools can pre-render their
    # fire announcements at set_timer time. The TtsPlayout isn't open
    # yet (that lives inside the async with block below); the manager
    # is constructed without it and `attach_tts` wires playback once
    # the playout is up. Pre-render and regen don't need playback.
    cues_manager = _build_cues_manager(cfg, tts=None)

    # Wake-event telemetry store (HANDOFF-wake-telemetry.md PR 3).
    # Opens the SQLite DB synchronously at startup so the daemon
    # is "ready" only after the schema migration is applied —
    # avoids racy "begin_event before CREATE TABLE" failures on
    # first-ever boot. Failure to open is logged + the daemon
    # continues with telemetry disabled (the wake / session path
    # is unaffected; only the flag_recent_issue tool is silently
    # withheld from the model in that mode).
    #
    # Created BEFORE `_build_registry` because make_diagnostic_tools
    # gates on the store and the LLM `session.update` is sent once
    # at WS handshake time — tools added to the registry after the
    # connection opens are invisible to the live session until the
    # next reconnect. Close lives in the outer finally below.
    wake_event_store: WakeEventStore | None = None
    try:
        wake_event_store = WakeEventStore(
            cfg.wake_events_dir,
            max_audio_bytes=cfg.wake_events_max_audio_bytes,
        )
        wake_event_store.open()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "wake_events: failed to open store at %s: %s "
            "(continuing with telemetry disabled)",
            cfg.wake_events_dir, e,
        )
        wake_event_store = None

    registry = _build_registry(
        cfg, camilla, renderer, weather, subway,
        volume_coordinator=volume_coordinator,
        volume_persistence=volume_persistence,
        spotify_router=volume_spotify_router,
        timer_scheduler=timer_scheduler,
        cues_manager=cues_manager,
        google_clients=google_clients,
        bus=bus,
        ha=ha,
        citibike=citibike,
        wake_event_store=wake_event_store,
    )

    # Wire the timer pre-render hook so set_timer (and start-time
    # restore for persisted timers) synthesises + caches the
    # fire-time announcement WAV ahead of time. Saves the user from
    # a 1–8 s gap between duck and audio at fire time.
    async def _prerender_timer(t: Timer) -> None:
        await cues_manager.prerender_text(announcement_text(t))
    timer_scheduler.set_pre_render(_prerender_timer)

    stop_event = asyncio.Event()

    def _shutdown(*_):
        logger.info("shutdown requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    logger.info(
        "jasper-voice ready: provider=%s model=%s wake=%s mic=%s %s",
        cfg.voice_provider, _active_model(cfg), cfg.wake_model,
        cfg.mic_device, _tts_ready_detail(cfg),
    )

    # Open the persistent live connection ONCE at daemon startup and
    # keep it open for the daemon's lifetime. Wake events acquire/release
    # turns against this connection — they don't open new WebSockets.
    # Pass a lambda (not the rendered string) so the time-injection
    # inside _build_system_instruction stays accurate across context
    # resets and reconnects — the connection re-renders on every
    # fresh open. The location is captured at startup; if you change
    # JASPER_DEFAULT_LOCATION you must restart jasper-voice.
    connection = _make_connection(cfg)
    # Time-billed providers (Grok: flat $/hour) price their per-turn token
    # rows to $0; their real cost is connection uptime. Wire a meter —
    # before start() so the initial connect's interval is captured — that
    # records connect/disconnect intervals the spend queries fold in. No
    # meter for token-billed providers (flat_per_hour_usd == 0).
    if pricing.flat_per_hour_usd > 0:
        set_meter = getattr(connection, "set_uptime_meter", None)
        if callable(set_meter):
            set_meter(ConnectionUptimeMeter(
                usage_store, cfg.voice_provider, pricing.flat_per_hour_usd,
            ))
            logger.info(
                "connection uptime meter: enabled for %s at $%.2f/hour",
                cfg.voice_provider, pricing.flat_per_hour_usd,
            )
    tts_volume_tracker: TtsVolumeTracker | None = None
    try:
        # Capture the linked-Google-accounts list at startup so the
        # system instruction tells the model which `account` values
        # are valid for the calendar/gmail tools. Wizard-driven account
        # changes trigger a daemon restart, so this snapshot stays
        # accurate for the daemon's lifetime.
        google_account_names = (
            google_clients.list_account_names() if google_clients else []
        )
        google_default_account = (
            google_clients.default_account_name() or ""
        ) if google_clients else ""
        # transit_configured is true when ANY transit tool is live —
        # the system prompt nudges the model toward /transit only when
        # ALL transit options are absent. Partial configurations
        # (e.g. subway set, bus/citibike not) don't need the nudge
        # because the available tool surface still answers the modes
        # the household has actually configured.
        transit_configured = (
            bool(subway)
            or bool(bus and bus.enabled)
            or bool(citibike and citibike.enabled)
        )
        # ha_configured drives the home_assistant nudge — when HA is
        # disabled, the model needs explicit guidance to redirect
        # smart-home requests to the wizard rather than misrouting to
        # unrelated tools (observed misroute: lights → get_current_time
        # + get_now_playing on May 22 2026).
        ha_configured = ha is not None
        await connection.start(
            registry,
            lambda: _build_system_instruction(
                cfg.weather_prompt_location,
                google_accounts=google_account_names,
                default_google_account=google_default_account,
                transit_configured=transit_configured,
                ha_configured=ha_configured,
                hostname=cfg.hostname,
            ),
        )
        # Open everything with an async lifecycle under one
        # AsyncExitStack — each configured wake leg's mic, plus the TTS
        # playout. `make_mic_capture` routes a `udp:PORT` device (the AEC
        # bridge's UDP transport) to UdpMicCapture and anything else
        # (`Array` chip-direct, a `hw:` USB mic) to the PortAudio
        # MicCapture. Which legs to build is data-driven from
        # jasper.wake_legs + cfg.mic_device* via _configured_wake_legs().
        #
        # Resilience asymmetry: the primary "on" (AEC3) leg is must-have
        # — it carries session audio + the Tier-1 heartbeat, so a
        # mic-open failure there is fatal (re-raised → systemd
        # Restart=on-watchdog + the AEC reconciler's mic-presence gate
        # recover us). Optional "off"/"dtln" legs are best-effort: a
        # mic-open failure is logged and that leg is skipped so the
        # speaker keeps waking on the healthy legs.
        async with contextlib.AsyncExitStack() as stack:
            legs: list[_LegRuntime] = []
            for spec, device in _configured_wake_legs(cfg):
                try:
                    leg_mic = await stack.enter_async_context(
                        make_mic_capture(
                            device,
                            capture_rate=cfg.mic_capture_rate,
                            capture_channels=cfg.mic_capture_channels,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    if spec.token == "on":
                        raise
                    logger.warning(
                        "event=wake.leg_skipped leg=%s device=%s "
                        "reason=mic_open_failed err=%s",
                        spec.token, device, exc,
                    )
                    continue
                # openWakeWord's Model carries per-instance prediction
                # state, so each leg gets its own detector — same model
                # file + threshold, only the input stream differs. The
                # "off" leg also gets a session shadow VAD (telemetry
                # only; see _shadow_vad_score_raw).
                legs.append(_LegRuntime(
                    spec,
                    leg_mic,
                    WakeWordDetector(
                        cfg.wake_model, threshold=cfg.wake_threshold,
                    ),
                    deque(maxlen=CAPTURE_RING_FRAMES),
                    shadow_vad=SpeechVAD() if spec.token == "off" else None,
                ))
            tts = await stack.enter_async_context(make_tts_playout(
                transport=cfg.tts_transport,
                device=cfg.tts_device,
                output_rate=cfg.tts_output_rate,
                # Constructor gain doesn't matter at runtime — TtsPlayout
                # initializes at its silent floor and the volume tracker's
                # first-tick read sets the real value before the first
                # turn can play. We pass cfg.tts_gain_db so a startup
                # before the tracker first applies (e.g. Camilla down at
                # boot) still has a sane fallback.
                gain_db=cfg.tts_gain_db,
                drain_tail_sec=cfg.tts_drain_tail_sec,
                outputd_socket=cfg.tts_outputd_socket,
            ))
            tts_volume_tracker = TtsVolumeTracker(
                camilla, tts,
                offset_db=cfg.tts_gain_db,
                music_headroom_db=cfg.tts_music_headroom_db,
                silence_threshold_dbfs=cfg.tts_silence_threshold_dbfs,
                music_window_sec=cfg.tts_music_window_sec,
                volume_persistence=volume_persistence,
                initial_anchor_dbfs=initial_anchor,
            )
            await tts_volume_tracker.start()

            # Wire the playout into the cue manager that was already
            # constructed up top so timer tools could register with a
            # working pre-render path. From here on cues.play() and
            # cues.speak_text() can write audio out.
            cues_manager.attach_tts(tts)
            # Kick off background regen for any missing/stale cues.
            # Doesn't block daemon "ready" — if regen fails (no
            # internet / bad API key), cues silently won't play; the
            # daemon's other voice paths still work.
            _schedule_cue_regen(cues_manager)

            # Tier 1 of the resilience ladder. Bumped on every mic
            # frame inside WakeLoop.run; pairs with `Type=notify` +
            # `WatchdogSec=30s` in jasper-voice.service. If the
            # async loop wedges or mic capture dies, the heartbeat
            # stops patting and systemd revives us cleanly via
            # `Restart=on-watchdog` before SIGKILL is needed. See
            # jasper/watchdog.py header.
            heartbeat = Heartbeat(stale_threshold_sec=5.0, interval_sec=10.0)
            heartbeat.start()
            # `wake_event_store` was opened at the top of run() —
            # see the comment block above `_build_registry` for the
            # timing rationale. We just hand it to WakeLoop here.
            wake_loop = WakeLoop(
                cfg, tts, connection, ducker,
                tts_volume_tracker, usage_store, spend_cap, stop_event,
                volume_coordinator=volume_coordinator,
                legs=legs,
                cues=cues_manager,
                camilla=camilla,
                heartbeat=heartbeat,
                wake_event_store=wake_event_store,
            )
            # Wire the supervisor's tight-retry-loop escalation cue to
            # the wake loop's session-aware cue play. Done here (after
            # both connection and wake loop exist) because the
            # connection is constructed first by _make_connection but
            # WakeLoop.play_supervisor_cue is the right callback target.
            if hasattr(connection, "set_failure_escalation_cb"):
                connection.set_failure_escalation_cb(
                    wake_loop.play_supervisor_cue,
                )
            # Wire timer announcements through the wake loop's
            # session-aware playback (duck + speak_text + restore,
            # with up-to-5s deferral if a voice turn is in flight).
            # set_on_fire BEFORE start() — start() restores persisted
            # timers and any whose fire_at has passed during downtime
            # are dropped before they'd hit on_fire anyway, but timers
            # whose fire_at is < 1s away could fire mid-restore.
            timer_scheduler.set_on_fire(wake_loop.announce_timer)
            await timer_scheduler.start()
            control_socket = await _start_control_socket(
                wake_loop, cfg.voice_control_socket,
            )
            try:
                await wake_loop.run()
            finally:
                heartbeat.stop()
                control_socket.close()
                try:
                    await control_socket.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        # Stop the scheduler FIRST so any in-flight `_run` tasks that
        # were about to fire get cancelled before we tear down the
        # cue manager / TtsPlayout they'd be calling into.
        await timer_scheduler.stop()
        # Wake-event store close — moved out of the inner async-with
        # block when the open was hoisted up so the diagnostic tools
        # could land in the registry before the LLM session opened.
        if wake_event_store is not None:
            try:
                wake_event_store.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("wake_events store close: %s", e)
        if tts_volume_tracker is not None:
            await tts_volume_tracker.stop()
        if volume_observer is not None:
            await volume_observer.stop()
        await volume_coordinator.aclose()
        await connection.stop()
        await weather.aclose()
        if ha is not None:
            await ha.aclose()
        if bus is not None:
            # BusClient owns an httpx.AsyncClient with a connection
            # pool; without aclose() the pool's idle connections +
            # FDs leak across daemon restart cycles. Mirror the
            # weather/ha pattern.
            try:
                await bus.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("bus.aclose failed during shutdown")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
