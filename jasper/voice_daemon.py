from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections import deque
from enum import Enum


@contextlib.asynccontextmanager
async def _nullcontext_async(value):
    """Async equivalent of `contextlib.nullcontext`. Yields `value`
    without entering or exiting anything. Used at the WakeLoop
    construction site to make the optional second mic `async with`
    a single statement regardless of whether the second mic is
    configured (yields None) or is a real UdpMicCapture context."""
    yield value

from .accounts import Registry, maybe_migrate_legacy
from .audio_buffer import (
    ACQUIRE_BUFFER_MAX_FRAMES,
    drain_acquire_buffer,
)
from .audio_io import MicCapture, TtsPlayout, UdpMicCapture, make_mic_capture
from .wake_events import (
    WakeEventStore,
    make_event_id,
    CAPTURE_PRE_SEC,
    CAPTURE_POST_SEC,
)
from .cues import AudioCueManager, build_cue_tts_backend
from .vad import SpeechVAD
from .camilla import CamillaController, CueDuck, Ducker
from .config import Config
from .watchdog import Heartbeat
from .google_creds import GoogleClients, build_google_clients
from .renderer import RendererClient
from .spotify_router import BuildResult, Router, build_clients
from .bus import BusClient
from .subway import SubwayClient
from .timers import Timer, TimerScheduler, announcement_text
from .tools import ToolRegistry
from .tools.audio import make_audio_tools
from .tools.calendar import make_calendar_tools
from .tools.gmail import make_gmail_tools
from .tools.spotify import make_spotify_tools
from .tools.bus import make_bus_tools
from .tools.subway import make_subway_tools
from .tools.time import make_time_tools
from .tools.timer import make_timer_tools
from .tools.transport import make_transport_tools
from .tools.weather import make_weather_tools
from .usage import SpendCap, UsageStore, pricing_for_provider
from .voice.session import LiveConnection, LiveTurn
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

# Structured per OpenAI's Realtime Prompting Guide
# (cookbook.openai.com/examples/realtime_prompting_guide):
#   Role & Objective → Personality & Tone → Tools (when to call,
#   preambles, what to say after) → Out of scope.
#
# Two design principles from that guide and the official "Using
# realtime models" docs that we previously violated:
#
#   1. POSITIVE framing for tool calls — "Call X when Y", not "Don't
#      forget X". The earlier version of this prompt had ~15 "Do NOT"
#      clauses and zero positive "Call the tool when…" instructions,
#      which is exactly the pattern OpenAI says causes gpt-realtime to
#      drift from rules, skip phases, or misuse tools.
#
#   2. CONDITIONAL framing for preamble suppression — "Skip the
#      preamble when X, Y, Z" instead of "Never preamble". Absolute
#      prohibitions get partially ignored (the model has been RLHF-
#      trained on the conditional pattern) AND, worse, can teach the
#      model to skip the entire tool-call workflow rather than just
#      the preamble part. The previous version had both an absolute
#      "in practice you should never produce a preamble" AND a list
#      of "Examples of INCORRECT style" that showed tool calls + their
#      results as bad-style examples — exactly the over-suppression
#      footgun.
#
# Net effect (verified 2026-05-21 via the voice-eval harness on the Pi
# against gpt-realtime-2): the previous prompt caused the model to
# call ZERO tools across 5 consecutive read-only scenarios. That's
# the same hallucination pattern the user observed in production
# ("Jarvis tells me train times without ever calling the subway
# tool"). Restructuring per OpenAI's guidance is the fix.
SYSTEM_INSTRUCTION = (
    # ---- Role & Objective ------------------------------------------------
    "You are Jarvis, a voice assistant in a household smart speaker. "
    "The user's name is Jasper. Your job is to answer the user's "
    "questions and control music, volume, timers, calendar, and email "
    "by calling the available tools. "

    # ---- Personality & Tone ----------------------------------------------
    "Voice style is terse and factual — like Alexa or Siri. One "
    "sentence per response is ideal, two is the maximum. After "
    "answering, stop: don't ask follow-up questions, don't offer "
    "related actions, don't invite further conversation, don't "
    "restate the question. Ask a clarifying question only when the "
    "user's request is genuinely ambiguous and you cannot proceed "
    "otherwise — in that case ask one specific question and nothing "
    "else. "

    # ---- Tools — when to call them ---------------------------------------
    # POSITIVE framing. The tools have fresh data the model does not;
    # the model should call them, not guess.
    "Call a tool whenever the user's request matches its purpose. "
    "These tools have data and capabilities you do not — answering "
    "from memory or guessing is incorrect. Specifically:\n"
    "  - Any question about the current time, day of week, or date "
    "('what time is it', 'what day is it', 'what's today's date') "
    "→ call get_current_time. Your internal clock is the "
    "session-open timestamp from the system prompt; it goes stale "
    "within hours, so always call the tool for time queries.\n"
    "  - Any question about weather, temperature, rain, sunrise, or "
    "sunset → call get_weather. If the user doesn't name a city, "
    "pass an empty location string and the tool uses the speaker's "
    "default.\n"
    "  - Any question about the next train, subway arrivals, or "
    "which train is coming → call get_subway_arrivals. Call it fresh "
    "every time — train times are live and a prior result is stale. "
    "Pass empty strings for line and direction by default — the tool "
    "returns every line stopping at the home station in the "
    "configured direction(s), including trains rerouted from other "
    "lines during service changes (an N running on D tracks shows "
    "up alongside regular Ds). Pass a specific line ('D') only when "
    "the user names it; pass 'both' for direction when the user "
    "wants both directions in one answer.\n"
    "  - Any question about the next bus, bus arrivals, or which "
    "bus is coming → call get_bus_arrivals. Call it fresh every "
    "time — bus arrivals are real-time. Pass an empty `route` "
    "string for a bare 'when's the next bus'; pass a specific "
    "route like 'B35' only if the user names one. The speaker "
    "may have multiple configured bus stops near home (e.g. "
    "opposing-direction stops at the same intersection). The "
    "tool unions arrivals across all of them — each arrival "
    "carries its own `stop_label` so you can name which stop "
    "each bus is at.\n"
    "  - Music control ('play', 'pause', 'skip', 'previous', "
    "'resume', 'volume up', 'mute', etc.) → call the matching tool. "
    "Do not ask for confirmation.\n"
    "  - Bare 'play' / 'resume' / 'keep playing' with no song or "
    "artist named → call resume (un-pauses paused music). Call "
    "spotify_play only when the user names a song, artist, album, "
    "or playlist.\n"
    "  - 'What's playing?' / 'Who is this?' → call get_now_playing. "
    "Do NOT call get_now_playing as a chaser after spotify_play — "
    "Spotify's current_playback lags by several seconds and may "
    "report the previous track.\n"
    "  - Volume questions ('what's the volume?') → call get_volume; "
    "don't change it. Default step for 'volume up/down' is 10%; "
    "pass ±20–30 for 'a lot louder/quieter'.\n"
    "  - Timers ('set a timer for 5 minutes') → call set_timer with "
    "the duration in seconds ('5 minutes' → 300, '1 hour' → 3600). "
    "Pass `label` when the user names the timer ('pasta', "
    "'laundry'). Multiple timers run in parallel — a new one does "
    "not cancel existing ones. The speaker plays the announcement "
    "automatically when the timer fires; don't promise to remind.\n"
    "  - Timer status ('how much time left', 'list my timers') → "
    "call list_timers.\n"
    "  - 'Cancel the X timer' → call cancel_timer with the label or "
    "duration as the query.\n"
    "  - Calendar questions about today → call calendar_today_summary; "
    "questions about the next few hours/days → call calendar_upcoming "
    "with `hours` set appropriately (6 for 'this afternoon', 168 for "
    "'this week').\n"
    "  - Email questions ('any new emails?') → call "
    "gmail_unread_summary. If the user follows up 'read me the first "
    "one' / 'open that email', call gmail_read_thread with the "
    "thread_id from the prior summary.\n"
    "  - When the user names a household member ('Brittany's "
    "calendar', 'Jasper's email'), pass that name as the `account` "
    "arg to the calendar/gmail tools. When no person is named, "
    "omit `account` and the default is used. The linked-accounts "
    "list is in the addendum below; if the user names someone "
    "outside it, ask which linked name to use.\n"

    # ---- Tools — preambles -----------------------------------------------
    # CONDITIONAL framing. List when to skip; don't say "never".
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

    # ---- Tools — what to say after the tool returns ----------------------
    "After a tool returns, speak the result briefly:\n"
    "  - spotify_play: when the result has a `confirm` field, "
    "speak that sentence verbatim. Do not say 'Done.' instead. "
    "On error, speak the `error` field verbatim.\n"
    "  - set_volume / adjust_volume: speak the new `percent` from "
    "the tool result ('Volume sixty.').\n"
    "  - mute / unmute: 'Muted.' / 'Unmuted.'\n"
    "  - pause / resume / next_track / previous_track: 'Paused.' / "
    "'Resuming.' / 'Skipping.' / 'Going back.'\n"
    "  - get_volume: 'Volume is at 70%.'\n"
    "  - get_current_time: speak the local time naturally — "
    "'It's 3:47 PM.' or 'It's Thursday, May 21.' Round to natural "
    "phrasing ('a quarter past 7') when the user asks casually. "
    "Don't read out the timezone abbreviation unless asked.\n"
    "  - get_weather: pick the right scope from the response. "
    "now / today / tomorrow for current-and-near questions; "
    "hourly_forecast filtered by date+hour for 'this evening' / "
    "'tomorrow morning' / 'Saturday afternoon'; daily_next_14d for "
    "'this week' (slice [0:7]) and 'next week' (slice [7:14]). "
    "For rain questions, lead with precipitation_probability; if "
    "null, fall back to will_rain. For week-scope answers, "
    "summarise as a high/low range with any rainy days called "
    "out — e.g. 'Highs in the low 70s, lows around 55. Mostly "
    "sunny except Thursday with a 60% chance of rain.'\n"
    "  - get_subway_arrivals: walk the `arrivals` list and speak "
    "each train's line + direction + minutes. Examples: 'Next "
    "Manhattan-bound D in 3 minutes, then an N in 7.' / 'D in 3, "
    "N in 7 — both Manhattan-bound.' / 'Manhattan-bound D in 3, "
    "Coney-bound D in 6.' Name the line for each train when "
    "multiple lines are coming (rerouted train mixed in with "
    "regulars, or multi-line station). Name the direction when "
    "the query asked for both directions or when context would "
    "be ambiguous. Skip naming when the user's question already "
    "pinned it ('next D uptown?' → just 'in 3 and 7 minutes.').\n"
    "  - get_bus_arrivals: walk the `arrivals` list and speak "
    "each bus with route + minutes. Use `minutes_from_now`; "
    "ignore `presentable_distance` and `stops_from_call` — the "
    "user wants minutes, not stops or miles. Name `stop_label` "
    "when multiple stops are configured AND arrivals from "
    "different stops appear in one response — examples: 'B35 "
    "westbound at 4 Av/39 St in 4 minutes, B70 eastbound in 7.' "
    "/ 'B35 at the eastbound stop in 2, then a B35 at the "
    "westbound stop in 5.' Skip the stop label when arrivals "
    "all come from one stop OR the user's question already "
    "pinned it. For a bus at 0 minutes, say 'approaching' or "
    "'now' instead of '0 minutes'. NEVER say 'stops away' or "
    "'miles away'.\n"
    "  - set_timer / cancel_timer: speak the response's `confirm` "
    "field verbatim. If cancel_timer returns `reason='ambiguous'`, "
    "read the candidate durations and ask which to cancel.\n"
    "  - list_timers: brief summary of remaining time per timer.\n"
    "  - calendar tools: 'You have N things today: <summary> at "
    "<time>, <summary> at <time>…' — keep it scannable.\n"
    "  - gmail_unread_summary: 'You have N unread: <sender> about "
    "<subject>, <sender> about <subject>…' — scannable; the user "
    "can follow up for details.\n"
    "  - On a 'Google access for X can't be refreshed' error, "
    "speak it verbatim — the message tells the user how to fix it.\n"

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
# 0.7 s (May 2026). Even 0.7 s combined with POST_RESPONSE_IDLE_TIMEOUT_SEC
# (1.5 s tail) produced a ~2.2 s total deadzone after the model finished
# speaking — long enough that quick follow-ups got dropped silently.
# The tail already drains the ALSA buffer fully by the time turn-end
# fires, so the refractory was double-counting the safety margin.
# 0.2 s is ~2.5x the 85 ms dmix buffer — still a margin, but won't
# swallow conversational pacing.
WAKE_REFRACTORY_SEC = 0.2


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

# Shorter idle timeout after the model has started responding. The
# regular `cfg.idle_timeout_sec` (~10 s) is the time we wait for
# the FIRST chunk to come back; once any chunk has arrived (or
# turn_complete fired), we switch to this much shorter window so
# the music un-ducks promptly after Gemini finishes speaking,
# instead of holding the duck for ~10 s of dead air.
#
# Measured anchor is `last_chunk_played_at` — the consumer's dequeue
# timestamp, which advances at real-time playback rate. After that
# the only audio still in flight is whatever's in the 85 ms ALSA dmix
# buffer; 0.5 s gives ~6x margin on buffer drain. Was 1.5 s; the
# extra second was being eaten as deadzone after the model finished
# speaking, swallowing quick follow-up wakes.
POST_RESPONSE_IDLE_TIMEOUT_SEC = 0.5

# Pad after _play_responses drains, covering the ~60 ms ALSA dmix buffer that outlives the last tts.write.
TTS_ALSA_DRAIN_SEC = 0.3

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
# one primitive: wake-tail audio is too short (~100-200 ms of mic
# residual) to ever hit 200 ms continuous, so it can't false-arm; a
# real spoken command — fast or slow — easily clears it. Pattern
# borrowed from OpenVoiceOS's dinkum-listener (`speech_begin`
# parameter, default 0.3 s); see ovos-dinkum-listener voice_loop.py.
# We use 200 ms instead of 300 ms because our short single-word
# commands ("next", "pause") only span ~250 ms of audio, and 300 ms
# would miss them.
SUSTAINED_SPEECH_TO_ARM_SEC = 0.20


class State(Enum):
    WAKE = "wake"
    SESSION = "session"


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
    Gemini's source-peak ends up `music_headroom_db` above the windowed
    music RMS:

        tts_gain_db = (windowed_rms + headroom) - GEMINI_SOURCE_PEAK

    Always capped at the user's master_volume + offset (the legacy
    formula remains as a hard ceiling — playback_rms can only make TTS
    quieter, never louder). When playback_rms < silence_threshold the
    tracker falls back to the legacy formula directly.

    Hearing-safety belt is in TtsPlayout.set_gain_db (MIN/MAX clamp).
    This class is defense-in-depth on top of that.

    Pause/resume around voice sessions so duck-induced volume changes
    don't pull TTS down DURING the very turn TTS is playing.
    """

    POLL_INTERVAL_SEC = 0.25
    # Approximate peak of Gemini Live's TTS PCM output (dBFS). Voice
    # is dynamic but consistent across sessions/utterances per the
    # source library; observed peaks cluster around -3 dBFS. Used to
    # convert "where do we want TTS to sit" → "what gain to apply".
    GEMINI_SOURCE_PEAK_DBFS = -3.0

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

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def _record_rms(self, rms_dbfs: float) -> float:
        """Append latest RMS reading and return windowed peak."""
        now = asyncio.get_event_loop().time()
        self._peak_buffer.append((now, rms_dbfs))
        cutoff = now - self._window_sec
        while self._peak_buffer and self._peak_buffer[0][0] < cutoff:
            self._peak_buffer.popleft()
        return max(p for _, p in self._peak_buffer)

    def _compute_gain(self, vol_db: float, windowed_rms: float) -> float:
        """Pure: given current main_volume and windowed RMS peak,
        return target gain.

        Three branches:
          1. Music currently playing (windowed_rms above threshold) →
             match observed loudness directly.
          2. Silence, but we have a loudness anchor (the last-known
             music level, possibly from a previous session) → target
             that level. This is what fixes the "TTS too loud during
             silence when iPhone is at 20%" problem: anchor reflects
             actual perceived output regardless of upstream attenuators.
          3. Otherwise → main_volume + offset (legacy fallback). With
             initial_anchor_dbfs defaulting to DEFAULT_ANCHOR_DBFS
             (-30 dBFS = 40%), branch 3 is rarely hit in practice; it's
             a backstop.

        master_volume + offset is the ABSOLUTE CEILING in all branches.
        Anchor can only push gain DOWN from that ceiling, never up."""
        ceiling = vol_db + self._offset_db
        if windowed_rms > self._silence_threshold_dbfs:
            target = (
                windowed_rms + self._headroom_db - self.GEMINI_SOURCE_PEAK_DBFS
            )
        elif self._anchor_dbfs > -120.0:
            target = (
                self._anchor_dbfs + self._headroom_db
                - self.GEMINI_SOURCE_PEAK_DBFS
            )
        else:
            target = ceiling
        # Quantize to 1 dB to avoid log spam and rapid micro-adjustments
        # below human-perceivable change (~3 dB JND for loudness).
        return round(min(target, ceiling))

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
        self._tts.set_gain_db(self._compute_gain(vol_db, windowed))

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
            self._tts.set_gain_db(self._compute_gain(vol_db, windowed))


def _build_system_instruction(
    location: str = "",
    *,
    google_accounts: list[str] | None = None,
    default_google_account: str = "",
    transit_configured: bool = True,
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
        addendum += (
            " Transit tools (subway, bus arrivals) aren't set up on this "
            "speaker yet — no get_subway_arrivals or get_bus_arrivals tool "
            "is available. If the user asks about the next train or next "
            "bus, briefly say: 'Transit isn't set up yet — visit "
            "jts.local/transit to configure it.' Don't promise to check "
            "or look it up; the data source is genuinely absent."
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


def _active_model(cfg: Config) -> str:
    """Return the model name for the currently selected provider — used
    by startup-readiness logging and the silent-failure heuristic in
    `_end_turn` so journalctl shows the actual model in flight."""
    if cfg.voice_provider == "gemini":
        return cfg.gemini_model
    if cfg.voice_provider == "openai":
        return cfg.openai_model
    if cfg.voice_provider == "grok":
        return cfg.grok_model
    return f"<unknown:{cfg.voice_provider}>"


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
    return registry


async def _play_responses(turn: LiveTurn, tts: TtsPlayout) -> None:
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
        async for chunk in turn.audio_out():
            if interrupt_task is None or interrupt_task.done():
                interrupt_task = asyncio.create_task(turn.wait_for_interrupt())
            write_task = asyncio.create_task(tts.write(chunk))
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
                await tts.flush()
                turn.clear_interrupted()
                interrupt_task = None
            # write_task is either in `done` (completed normally) or
            # was cancelled+awaited above; either way no cleanup left.
            write_task = None
        # Drain ALSA buffer before the caller disengages the duck.
        await asyncio.sleep(TTS_ALSA_DRAIN_SEC)
    finally:
        for t in (interrupt_task, write_task):
            if t is None or t.done():
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _idle_watchdog(turn: LiveTurn, timeout: int) -> None:
    """Close the turn based on explicit server-side signals where
    possible, falling back to a timer when the server stays silent.

    Three cases:
      * `turn.server_turn_complete()` is True → the server has
        explicitly told us "model is done speaking". Wait a short
        TTS-tail window so the last chunks finish playing through
        the speaker, then close. This is the canonical clean close
        and the only reliable way to avoid cutting off the model
        mid-response.
      * No chunks received yet → the model hasn't started speaking;
        wait the full `timeout` for the first chunk to arrive (Live
        API can take 3-5 s, sometimes longer).
      * Chunks arriving but turn_complete hasn't fired → don't
        close; the model is mid-response. Mid-response chunk gaps
        can be > 1.5 s during normal speech pauses, so a timer
        here would race with real output. Wait until either
        turn_complete arrives (case 1) or the connection drops.

    Also exits early if the underlying connection drops mid-turn — the
    connection's reconnect supervisor will mark the turn as lost via
    `turn_lost()` and there's nothing more to do here."""
    while True:
        await asyncio.sleep(0.25)
        if turn.turn_lost():
            logger.warning("idle watchdog: connection lost mid-turn, ending turn")
            return
        now = asyncio.get_event_loop().time()
        idle_for = now - turn.last_activity_at()
        if turn.server_turn_complete():
            # Defer while chunks are still queued — a slow tts.write isn't "audio finished."
            pending_getter = getattr(turn, "audio_chunks_pending", None)
            if callable(pending_getter) and pending_getter() > 0:
                continue
            # `getattr` guard: protocol method is new; older implementations fall back to network-arrival anchor.
            played_anchor = 0.0
            getter = getattr(turn, "last_chunk_played_at", None)
            if callable(getter):
                played_anchor = getter() or 0.0
            tail_anchor = (
                played_anchor
                or turn.last_chunk_at()
                or turn.last_activity_at()
            )
            tail_idle = now - tail_anchor
            if tail_idle > POST_RESPONSE_IDLE_TIMEOUT_SEC:
                logger.info(
                    "turn_complete + tail (%.1fs since last %s), ending turn",
                    tail_idle,
                    "chunk played" if played_anchor else "chunk received",
                )
                return
            continue
        any_chunk_received = turn.last_chunk_at() > 0
        if not any_chunk_received and idle_for > timeout:
            logger.info(
                "idle timeout (pre-response phase, %.1fs); no chunks, ending turn",
                float(timeout),
            )
            return


class WakeLoop:
    """Mic consumer. Dispatches each primary-mic frame to either the
    wake-word detector (WAKE state) or the active live turn (SESSION
    state). One consumer iterating over the primary `mic.frames()` —
    eliminates implicit frame-ownership coupling between wake-listen
    and active-turn paths.

    Dual-stream wake detection (when `mic_off` + `detector_off` are
    set): a parallel secondary consumer reads a second mic source
    (typically the bridge's chip-direct UDP stream — see
    docs/HANDOFF-wake-telemetry.md PR 1) and runs an independent
    `WakeWordDetector` instance on every frame. Either leg crossing
    threshold fires the wake event (OR-gate). A shared refractory +
    asyncio lock guarantees one user attempt = one wake event
    regardless of which leg(s) crossed first. The secondary leg is
    wake-detection-only: its frames don't populate pre-roll or
    flow into sessions — the primary AEC ON stream remains the
    canonical session audio source.
    """

    def __init__(
        self,
        cfg: Config,
        mic: MicCapture,
        tts: TtsPlayout,
        detector: WakeWordDetector,
        connection: LiveConnection,
        ducker: Ducker,
        tts_volume_tracker: TtsVolumeTracker,
        usage_store: UsageStore,
        spend_cap: SpendCap,
        stop_event: asyncio.Event,
        volume_coordinator: "VolumeCoordinator",
        cues: AudioCueManager | None = None,
        camilla: CamillaController | None = None,
        heartbeat: "Heartbeat | None" = None,
        mic_off: "UdpMicCapture | None" = None,
        detector_off: WakeWordDetector | None = None,
        wake_event_store: WakeEventStore | None = None,
    ) -> None:
        self._cfg = cfg
        self._mic = mic
        self._tts = tts
        self._detector = detector
        # Secondary wake-detection leg. Both must be present to enable
        # dual-stream mode; if only one is set we treat as misconfigured
        # and stay single-stream (logs a warning at run() startup so the
        # operator notices).
        self._mic_off = mic_off
        self._detector_off = detector_off
        # Per-leg recent wake scores (raw, 0.0-1.0). Updated every frame
        # the corresponding leg scores (regardless of threshold). Read
        # at fire time so the wake event payload carries BOTH legs'
        # most-recent peaks — even when only one leg crossed threshold,
        # the other leg's score gives signal on whether AEC helped or
        # hurt for this specific utterance.
        self._recent_score_on: float = 0.0
        self._recent_score_off: float = 0.0
        # Wall-clock (asyncio loop time) at which each leg's recent
        # score was set. Used at fire time to confirm the other leg's
        # score is from the same time window (not a stale score from
        # 3 seconds ago when, say, AEC OFF hasn't been scored recently
        # because frames stopped arriving on 9877).
        self._recent_score_on_at: float = 0.0
        self._recent_score_off_at: float = 0.0
        # Shared OR-gate lock across the two leg loops. Held only for
        # the critical section that sets refractory_until + reads the
        # other leg's recent score. Without this, both legs could race
        # to fire the same wake event simultaneously.
        self._wake_fire_lock: asyncio.Lock = asyncio.Lock()
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
        # _speech_run_started_at >= SUSTAINED_SPEECH_TO_ARM_SEC`,
        # arm the silence detector.
        self._speech_run_started_at: float = 0.0
        # Rolling ring buffer of the most recent mic frames. Always
        # appended-to (regardless of WAKE/SESSION state); drained into
        # the new turn at _begin_turn so the first phoneme of the
        # command isn't clipped.
        self._pre_roll: deque = deque(maxlen=PRE_ROLL_FRAMES)

        # Wake-event telemetry (HANDOFF-wake-telemetry.md PR 3).
        # Separate from `_pre_roll` because the capture-ring sizing is
        # tuned for offline review (~6 s windows around each wake
        # event) and the pre-roll is tuned for first-phoneme
        # preservation in turn-open (~560 ms). Conflating them would
        # force one to compromise on the other's dimension.
        #
        # The store handles the SQLite writes + audio capture +
        # retention; this set of attributes is just the WakeLoop's
        # contribution: the ring + the in-flight event id.
        #
        # CAPTURE_RING_FRAMES sized to (pre + post) seconds with safety
        # margin: 4 + 2 = 6 s window, +2 s slack for the 2 s post-fire
        # collection window so we don't run off the end of the ring.
        self._wake_event_store: WakeEventStore | None = wake_event_store
        _capture_ring_frames = int(
            ((CAPTURE_PRE_SEC + CAPTURE_POST_SEC) * MicCapture.OUTPUT_RATE
             / MicCapture.OUTPUT_FRAME_SAMPLES) + 25
        )
        self._capture_ring_on: deque = deque(maxlen=_capture_ring_frames)
        # PR 2 (dual-stream) wakes will populate this; PR 3 only
        # references it via getattr-tolerant code so this PR is
        # independent of the merge order.
        self._capture_ring_off: deque = deque(maxlen=_capture_ring_frames)
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
        # Optional secondary wake-detection leg. Spawned only when both
        # `mic_off` and `detector_off` were passed at construction
        # time; logs a warning + stays single-stream if only one is
        # set (misconfiguration). See class docstring + the OR-gate
        # logic in `_handle_wake_frame` for the dual-stream design.
        secondary_task: asyncio.Task | None = None
        if self._mic_off is not None and self._detector_off is not None:
            secondary_task = asyncio.create_task(
                self._wake_secondary_loop(),
                name="wake-secondary-aec-off",
            )
        elif (self._mic_off is None) ^ (self._detector_off is None):
            logger.warning(
                "dual-stream wake misconfigured: mic_off=%s detector_off=%s "
                "(both must be set; staying single-stream)",
                self._mic_off, self._detector_off,
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
            # Cancel + join the secondary loop on any exit path. Without
            # this the task could outlive run() and keep scoring frames
            # against a stopped detector / closed mic.
            if secondary_task is not None:
                secondary_task.cancel()
                try:
                    await secondary_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _wake_secondary_loop(self) -> None:
        """Parallel wake-only consumer of the secondary (AEC OFF) mic.

        Scores every frame through `_detector_off` and dispatches to
        `_handle_wake_frame(frame, leg='off')`, which shares refractory
        + the OR-gate lock with the primary loop so one user attempt
        fires at most one wake event regardless of which leg(s) cross
        threshold first.

        This leg is **wake-detection-only**: frames are NOT appended
        to pre-roll, NOT routed to `_acquire_buffer` during the
        wake→turn-open window, and NOT forwarded to live sessions.
        The primary AEC ON stream remains the canonical session
        audio source — keeps session quality unchanged and avoids
        feeding the LLM mixed dual-stream audio.

        Mirrors the primary-loop gating (measurement window, mic
        mute, acquiring, state=WAKE) so the AEC OFF leg respects
        every "stop listening" signal the primary loop respects.
        """
        assert self._mic_off is not None
        # Wake-telemetry capture ring for the AEC OFF leg, parallel to
        # the primary loop's `_capture_ring_on`. `getattr` so this
        # stays safe in test setups that build WakeLoop via
        # `__new__` + manual init (the dual-stream wake-handler tests).
        capture_ring_off: deque | None = getattr(self, "_capture_ring_off", None)
        async for frame in self._mic_off.frames():
            if self._stop_event.is_set():
                return
            if self._measurement_active.is_set():
                continue
            # Mute is a privacy promise — do NOT record audio for the
            # wake-events corpus when the user has explicitly muted
            # the mic. Mirrors the primary loop where the capture
            # ring fills only AFTER the mute / measurement gates.
            if self._mic_muted:
                continue
            # Fill the capture ring while the user is "live" (gated
            # past mute + measurement). Done BEFORE the acquiring /
            # WAKE-state checks so a wake fire's 6 s window still has
            # pre-fire context even if it overlaps the wake→turn-open
            # buffering window.
            if capture_ring_off is not None:
                capture_ring_off.append(frame)
            if self._acquiring:
                continue
            if self._state is not State.WAKE:
                # Secondary leg ignores frames during a live session
                # — only the primary stream feeds the LLM. The leg
                # resumes scoring when the session ends (state →
                # WAKE) and the primary loop's `_acquiring` drops
                # back to False.
                continue
            await self._handle_wake_frame(frame, leg="off")

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
        """Score one frame on the named leg ('on' = post-AEC primary,
        'off' = chip-direct secondary). Always tracks the leg's
        recent peak. If the threshold is crossed AND we win the
        OR-gate race against the other leg, fires a single wake event
        with BOTH legs' recent scores attached.

        Refractory + acquiring checks ensure one user attempt = one
        wake event, regardless of which leg(s) fire first."""
        # Quick refractory check — both legs early-out without scoring
        # while the previous wake's TTS may still be bleeding into the
        # mic. Cheap to do per-leg per-frame.
        now_loop = asyncio.get_event_loop().time()
        if now_loop < self._refractory_until:
            return

        # Score the frame on this leg's detector. Always track the raw
        # score (regardless of threshold) so the OTHER leg, when it
        # fires, can pull our most-recent peak into the wake-event
        # payload — even if we never crossed threshold for this
        # utterance.
        detector = self._detector if leg == "on" else self._detector_off
        if detector is None:
            return
        score = detector.score_frame(frame)
        if leg == "on":
            self._recent_score_on = score
            self._recent_score_on_at = now_loop
        else:
            self._recent_score_off = score
            self._recent_score_off_at = now_loop

        if score < detector.threshold:
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
            peak_on = self._recent_score_on
            peak_off = self._recent_score_off
            # If a leg's most-recent score is older than the per-frame
            # cadence × a small safety factor, treat it as "no recent
            # frame from that leg" — surfaces an AEC OFF stream that
            # stopped feeding (bridge crash / PR 1 not yet deployed)
            # without lying about a stale score in the wake event.
            STALE_SEC = 0.32  # 4× MicCapture's 80 ms frame period
            if leg != "on" and (now_loop - self._recent_score_on_at) > STALE_SEC:
                peak_on = None  # type: ignore[assignment]
            if leg != "off" and (now_loop - self._recent_score_off_at) > STALE_SEC:
                peak_off = None  # type: ignore[assignment]

        # Reset BOTH detectors after a wake fires. openWakeWord's
        # prediction smoothing keeps recent-activation state across
        # calls; without resetting, the post-fire baseline stays
        # elevated and music vocals or TTS-tail bleed can false-fire
        # on the next listening window. Reset the loser as well as
        # the winner because the loser's score was also elevated by
        # the user's wake utterance.
        self._detector.reset()
        if self._detector_off is not None:
            self._detector_off.reset()

        import time as _time
        self._wake_event_at_monotonic = _time.monotonic()
        logger.info(
            "event=wake.detected leg=%s score_on=%s score_off=%s threshold=%.2f",
            leg,
            f"{peak_on:.2f}" if peak_on is not None else "none",
            f"{peak_off:.2f}" if peak_off is not None else "none",
            detector.threshold,
        )
        # Use the firing-leg's score for the existing event payload
        # field so downstream code (peering ranker, etc.) keeps
        # working without per-leg awareness.
        score = peak_on if leg == "on" and peak_on is not None else (
            peak_off if leg == "off" and peak_off is not None else score
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
            # Pull whichever leg's recent peak is current (PR 2 dual-
            # stream populates both; PR 3 alone only the firing leg).
            peak_on = getattr(self, "_recent_score_on", None)
            peak_off = getattr(self, "_recent_score_off", None)
            recent_on_at = getattr(self, "_recent_score_on_at", 0.0)
            recent_off_at = getattr(self, "_recent_score_off_at", 0.0)
            if leg == "on":
                trigger_kind = "fire_aec_on"
                peak_on = score
            else:  # leg == "off"
                trigger_kind = "fire_aec_off"
                peak_off = score
            # Compute per-leg peak offset relative to wake-fire time.
            # Use the SAME `now_loop` that `_handle_wake_frame`
            # captured at the top — that's the canonical fire-time
            # reference, and it's also what was just written to
            # `_recent_score_<leg>_at` when this frame scored. NOT
            # `asyncio.get_event_loop().time()` here — recomputing
            # would include the detector.reset() latency (~200ms x
            # 2 on Pi 5), making the firing leg's offset look like
            # ~-400 ms instead of ~0 ms.
            #
            # Semantics: 0 = leg's last score == fire frame (the
            # firing leg by definition). Negative N = leg's last
            # score was N ms before fire (the OTHER leg, when its
            # last score-bearing frame was earlier than the firing
            # leg's).
            wake_fire_time = now_loop
            peak_off_ms = (
                int((recent_off_at - wake_fire_time) * 1000)
                if recent_off_at else None
            )
            peak_on_ms = (
                int((recent_on_at - wake_fire_time) * 1000)
                if recent_on_at else None
            )
            # Per-leg instantaneous mic RMS at fire-time, in dBFS.
            # Sampled from the last frame in each capture ring so we
            # get "what was the mic seeing right now" without an
            # extra capture. Helps separate low-energy FPs from real
            # attempts in offline review.
            mic_rms_on = self._tail_frame_rms_dbfs(
                getattr(self, "_capture_ring_on", None)
            )
            mic_rms_off = self._tail_frame_rms_dbfs(
                getattr(self, "_capture_ring_off", None)
            )
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
            try:
                await store.begin_event(
                    event_id=event_id,
                    trigger_kind=trigger_kind,
                    peak_score_aec_on=peak_on,
                    peak_score_aec_off=peak_off,
                    peak_offset_ms_on=peak_on_ms,
                    peak_offset_ms_off=peak_off_ms,
                    threshold=self._detector.threshold,
                    wake_model=self._cfg.wake_model,
                    voice_provider=getattr(self._cfg, "voice_provider", None),
                    bridge_config=bridge_config,
                    music_active=music_active_proxy,
                    music_volume_db=music_volume_db,
                    mic_muted=getattr(self, "_mic_muted", None),
                    mic_rms_dbfs_on=mic_rms_on,
                    mic_rms_dbfs_off=mic_rms_off,
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
            await self._wake_event_store.attach_audio(
                event_id=event_id,
                audio_on=audio_on,
                audio_off=audio_off,
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

        # Once we've sent `audio_stream_end` we stop forwarding mic
        # frames for the rest of this turn — the model is generating
        # its response and any further audio would re-open an audio
        # stream the server has been told is finished.
        if self._input_ended:
            return

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
            sustained = now - self._speech_run_started_at
            if not self._user_speech_seen and sustained >= SUSTAINED_SPEECH_TO_ARM_SEC:
                logger.info(
                    "user speech detected (sustained=%.0fms, silero=%.2f) — silence detector armed",
                    sustained * 1000, speech_prob,
                )
                self._user_speech_seen = True
                await self._telemetry_stage("speech_detected")
            self._silence_started_at = 0.0
        else:
            # Sub-threshold frame breaks the run. Wake-tail residual
            # never reaches ~200 ms continuous, so this is what keeps
            # it from arming.
            self._speech_run_started_at = 0.0
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
        the spend-cap or connection state separately."""
        return {
            "state": self._state.name,
            "input_ended": self._input_ended,
            "spend_allowed": self._spend_cap.allowed(),
            "connection_paused": self._connection.is_paused(),
            "mic_muted": self._mic_muted,
        }

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
        self._input_ended = False
        self._turn_started_at_loop = asyncio.get_event_loop().time()
        self._max_silero_score_in_turn = 0.0
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
        # Breakdown so a slow wake→activity_start can be localized to
        # the actual offender. `sched_lag` is the event-loop scheduling
        # delay between wake firing and this coroutine starting;
        # everything else is one named await. Pair with the
        # `acquire-buffer drained: N frames` log to gauge how much
        # audio Silero misses while this runs.
        logger.info(
            "turn acquire done in %.0fms "
            "(sched_lag=%.0f state=%.0f tts_apply=%.0f duck=%.0f acquire=%.0f) "
            "(wake→activity_start)",
            (t_after_acquire - t_wake) * 1000,
            (t_begin - t_wake) * 1000,
            (t_after_state - t_begin) * 1000,
            (t_after_tts_apply - t_after_state) * 1000,
            (t_after_duck - t_after_tts_apply) * 1000,
            (t_after_acquire - t_after_duck) * 1000,
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
        playback = asyncio.create_task(_play_responses(self._turn, self._tts))
        idle = asyncio.create_task(
            _idle_watchdog(self._turn, self._cfg.idle_timeout_sec)
        )
        self._bg_tasks = {playback, idle}
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
        # Wake-event telemetry: record the terminal state of the
        # in-flight event. `_user_speech_seen` tells us whether the
        # session got real user input — if not, the wake was likely
        # a false positive (music transient, TTS bleed) or the user
        # changed their mind. Either way the outcome is 'no_speech',
        # which dual-stream FP analysis keys off.
        await self._telemetry_stage("turn_complete")
        terminal_outcome = (
            "completed" if self._user_speech_seen else "no_speech"
        )
        await self._telemetry_outcome(terminal_outcome, reason)

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
            logger.info(
                "turn ended: %s tokens, est $%.4f (sent=%dB, recv=%d chunks%s)",
                tokens, cost, bytes_sent, chunks_received,
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

    pricing = pricing_for_provider(
        cfg.voice_provider, model=_active_model(cfg),
    )
    logger.info(
        "spend cap: provider=%s pricing=%s cap=$%.2f/day",
        cfg.voice_provider, pricing.label, cfg.daily_spend_cap_usd,
    )
    if (
        cfg.voice_provider == "grok"
        and cfg.daily_spend_cap_usd > 0
        and pricing.flat_per_hour_usd > 0
    ):
        # Grok bills per hour, not per token; UsageStore tracks tokens
        # and will under-count. Document the gap so the user knows the
        # cap behaviour is advisory under Grok.
        logger.warning(
            "spend cap with Grok: token-based accounting under-counts "
            "Grok's flat $%.2f/hour rate. Spend cap is effectively a "
            "liveness nudge under this provider — use xAI's billing "
            "dashboard for real numbers.",
            pricing.flat_per_hour_usd,
        )
    usage_store = UsageStore(cfg.usage_db, pricing=pricing)
    spend_cap = SpendCap(usage_store, cfg.daily_spend_cap_usd)

    camilla = CamillaController(cfg.camilla_host, cfg.camilla_port)
    renderer = RendererClient(
        librespot_state_path=cfg.librespot_state_path,
    )
    weather = WeatherClient(cfg.weather_default_location, cfg.weather_units)
    subway = (
        SubwayClient(
            cfg.subway_station_id,
            cfg.subway_default_direction,
        )
        if cfg.subway_enabled else None
    )
    # cfg.bus_stops is a list of MonitoringRefs (v2 multi-stop). Empty
    # list → bus disabled, runtime tool not registered.
    bus = (
        BusClient(
            stop_ids=list(cfg.bus_stops),
            api_key=cfg.mta_bustime_key,
        )
        if cfg.bus_enabled else None
    )
    # Volume coordinator: owns the canonical listening_level (0-100),
    # dispatches voice/dial-driven changes to the active source's own
    # attenuator (AirPlay DBus / Spotify HTTP / BT DBus) instead of
    # only adjusting CamillaDSP main_volume. Boot path applies a
    # safety regression to extreme stale values.
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

    registry = _build_registry(
        cfg, camilla, renderer, weather, subway,
        volume_coordinator=volume_coordinator,
        volume_persistence=volume_persistence,
        spotify_router=volume_spotify_router,
        timer_scheduler=timer_scheduler,
        cues_manager=cues_manager,
        google_clients=google_clients,
        bus=bus,
    )

    # Wire the timer pre-render hook so set_timer (and start-time
    # restore for persisted timers) synthesises + caches the
    # fire-time announcement WAV ahead of time. Saves the user from
    # a 1–8 s gap between duck and audio at fire time.
    async def _prerender_timer(t: Timer) -> None:
        await cues_manager.prerender_text(announcement_text(t))
    timer_scheduler.set_pre_render(_prerender_timer)
    detector = WakeWordDetector(cfg.wake_model, cfg.wake_threshold)

    stop_event = asyncio.Event()

    def _shutdown(*_):
        logger.info("shutdown requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    logger.info(
        "jasper-voice ready: provider=%s model=%s wake=%s mic=%s tts=%s",
        cfg.voice_provider, _active_model(cfg), cfg.wake_model,
        cfg.mic_device, cfg.tts_device,
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
        # transit_configured is true when either subway or bus
        # client is live — the system prompt nudges the model toward
        # /transit only when BOTH are absent. Partial configurations
        # (e.g. subway set, bus not) don't need the nudge because
        # the available tool surface still answers train queries.
        transit_configured = bool(subway) or bool(bus and bus.enabled)
        await connection.start(
            registry,
            lambda: _build_system_instruction(
                cfg.weather_default_location,
                google_accounts=google_account_names,
                default_google_account=google_default_account,
                transit_configured=transit_configured,
            ),
        )
        # `make_mic_capture` routes to UdpMicCapture for
        # `JASPER_MIC_DEVICE=udp:PORT` (the AEC bridge's UDP transport
        # under the resilience-ladder PR 2 architecture) or back to
        # the PortAudio MicCapture for anything else (`Array` for
        # chip-direct, a `hw:` substring for any other USB mic).
        #
        # Optional secondary mic for dual-stream wake detection (the
        # OR-gate path documented in docs/HANDOFF-wake-telemetry.md).
        # When `cfg.mic_device_raw` is set (e.g. `udp:9877` paired
        # with the bridge's chip-direct stream), the WakeLoop reads
        # both streams in parallel and fires wake on either crossing
        # threshold. Empty `mic_device_raw` keeps the existing
        # single-stream behaviour.
        mic_off_cm = (
            make_mic_capture(
                cfg.mic_device_raw,
                capture_rate=cfg.mic_capture_rate,
                capture_channels=cfg.mic_capture_channels,
            )
            if cfg.mic_device_raw else _nullcontext_async(None)
        )
        async with make_mic_capture(
            cfg.mic_device,
            capture_rate=cfg.mic_capture_rate,
            capture_channels=cfg.mic_capture_channels,
        ) as mic, mic_off_cm as mic_off, TtsPlayout(
            cfg.tts_device,
            output_rate=cfg.tts_output_rate,
            # Constructor gain doesn't matter at runtime — TtsPlayout
            # initializes at its silent floor and the volume tracker's
            # first-tick read sets the real value before the first
            # turn can play. We pass cfg.tts_gain_db so a startup
            # before the tracker first applies (e.g. Camilla down at
            # boot) still has a sane fallback.
            gain_db=cfg.tts_gain_db,
        ) as tts:
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
            # Second WakeWordDetector instance for the AEC OFF leg.
            # openWakeWord's `Model` carries internal prediction-buffer
            # state per instance — two independent detectors so the
            # legs don't cross-contaminate. Same model file and same
            # threshold; only the input stream differs. Constructed
            # only when mic_off is present.
            detector_off = (
                WakeWordDetector(cfg.wake_model, threshold=cfg.wake_threshold)
                if mic_off is not None else None
            )
            # Wake-event telemetry store (HANDOFF-wake-telemetry.md PR 3).
            # Opens the SQLite DB synchronously at startup so the daemon
            # is "ready" only after the schema migration is applied —
            # avoids racy "begin_event before CREATE TABLE" failures on
            # first-ever boot. Failure to open is logged + the daemon
            # continues with telemetry disabled (the wake / session
            # path is unaffected).
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
            wake_loop = WakeLoop(
                cfg, mic, tts, detector, connection, ducker,
                tts_volume_tracker, usage_store, spend_cap, stop_event,
                volume_coordinator=volume_coordinator,
                cues=cues_manager,
                camilla=camilla,
                heartbeat=heartbeat,
                mic_off=mic_off,
                detector_off=detector_off,
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
                if wake_event_store is not None:
                    try:
                        wake_event_store.close()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("wake_events store close: %s", e)
    finally:
        # Stop the scheduler FIRST so any in-flight `_run` tasks that
        # were about to fire get cancelled before we tear down the
        # cue manager / TtsPlayout they'd be calling into.
        await timer_scheduler.stop()
        if tts_volume_tracker is not None:
            await tts_volume_tracker.stop()
        if volume_observer is not None:
            await volume_observer.stop()
        await volume_coordinator.aclose()
        await connection.stop()
        await weather.aclose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
