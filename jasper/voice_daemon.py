from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections import deque
from enum import Enum

from .accounts import Registry, maybe_migrate_legacy
from .audio_buffer import (
    ACQUIRE_BUFFER_MAX_FRAMES,
    drain_acquire_buffer,
)
from .audio_io import MicCapture, TtsPlayout, make_mic_capture
from .cues import AudioCueManager, build_cue_tts_backend
from .vad import SpeechVAD
from .camilla import CamillaController, CueDuck, Ducker
from .config import Config
from .watchdog import Heartbeat
from .google_creds import GoogleClients, build_google_clients
from .renderer import RendererClient
from .spotify_router import Router, build_clients
from .subway import SubwayClient
from .timers import Timer, TimerScheduler, announcement_text
from .tools import ToolRegistry
from .tools.audio import make_audio_tools
from .tools.calendar import make_calendar_tools
from .tools.gmail import make_gmail_tools
from .tools.spotify import make_spotify_tools
from .tools.subway import make_subway_tools
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

SYSTEM_INSTRUCTION = (
    "You are Jarvis, a voice assistant in a smart speaker. The user's name "
    "is Jasper. "
    # Brevity rules — these are the highest-priority constraint. Voice
    # output is ~3 words/second; long replies feel laggy and over-eager.
    "Answer style: terse, factual, like Alexa or Siri. One sentence is "
    "ideal; two is the maximum. After your answer, STOP. Do NOT ask "
    "follow-up questions. Do NOT offer related actions ('would you like "
    "me to...', 'do you want me to also...'). Do NOT invite further "
    "conversation ('anything else?', 'let me know if...'). Do NOT "
    "restate the question. Only ask a clarifying question "
    "when the user's request is genuinely ambiguous and you literally "
    "cannot proceed without more information; in that case ask exactly "
    "one specific question and nothing else. "
    # Preambles — conditional language deliberately mirrors OpenAI's
    # Realtime Prompting Guide (cookbook.openai.com/examples/realtime_
    # prompting_guide). The model was RLHF-trained to evaluate the
    # *conditional* "when to / when not to" rules below; an absolute
    # prohibition ("never preamble") gets partially ignored because it
    # conflicts with the conditional pattern the model knows. The
    # "tool call is lightweight" bullet is the load-bearing one for
    # this assistant — every tool in the toolset (volume, transport,
    # weather, subway, spotify, timers, calendar, gmail, get_now_
    # playing) returns in under 2 seconds, so the user never benefits
    # from a "checking the weather" / "getting subway arrivals" /
    # "let me look that up" preamble.
    "Preambles: do NOT use a preamble in any of these cases, and every "
    "situation in this assistant falls into one of them — so in "
    "practice you should never produce a preamble:\n"
    "  - the answer is direct and can be given immediately;\n"
    "  - the user is only confirming, correcting, or declining "
    "something;\n"
    "  - the tool call is lightweight and the user would not benefit "
    "from an update (every tool here returns in well under 2 seconds);\n"
    "  - the latest audio is silence, background noise, hold music, "
    "TV audio, or side conversation.\n"
    "Call tools silently. Do not announce, narrate, or preface a tool "
    "call. Speak only the result after the tool returns. "
    # Few-shot examples to anchor the style.
    "Examples of correct style:\n"
    "  User: 'What time is it?'      → 'It's 9:47.'\n"
    "  User: 'What's the weather?'   → '62 and partly cloudy. Rain by Thursday.'\n"
    "  User: 'Pause.' / 'Stop.'      → [pause] 'Paused.'\n"
    "  User: 'Skip.' / 'Next song.'  → [next_track] 'Skipping.'\n"
    "  User: 'Go back.'              → [previous_track] 'Going back.'\n"
    "  User: 'Resume.' / 'Play.'     → [resume] 'Resuming.'\n"
    "  User: 'Play some jazz.'       → [spotify_play 'jazz'] (speak the response's `confirm` field, e.g. 'Playing Jazz Vibes.')\n"
    "  User: 'Play my Workout playlist.' → [spotify_play 'Workout' kind=playlist] (speak `confirm`, e.g. 'Now playing your Workout Mix playlist.')\n"
    "  User: 'Shuffle my Workout playlist.' / 'Play my Workout playlist on shuffle.' / 'Play Workout shuffled.' → [spotify_play 'Workout' kind=playlist shuffle=true] (speak `confirm`, e.g. 'Shuffling your Workout Mix playlist.')\n"
    "  User: 'Volume up.'            → [adjust_volume +10] (speak the new `percent` from the tool result, e.g. 'Volume seventy.')\n"
    "  User: 'Turn it down a lot.'   → [adjust_volume -25] (speak the new `percent`, e.g. 'Volume forty-five.')\n"
    "  User: 'Set volume to 30.'     → [set_volume 30] (speak the new `percent`, e.g. 'Volume thirty.')\n"
    "  User: 'What's the volume?'    → [get_volume] 'Volume is at 70%.'\n"
    "  User: 'Mute.'                 → [mute] 'Muted.'\n"
    "  User: 'Set a timer for 5 minutes.' → [set_timer 300] (speak `confirm`, e.g. 'Set a timer for 5 minutes.')\n"
    "  User: 'Set a pasta timer for 10 minutes.' → [set_timer 600 label='pasta'] (speak `confirm`, e.g. 'Set a pasta timer for 10 minutes.')\n"
    "  User: 'How much time left on my timer?' / 'What timers do I have?' → [list_timers] 'Three minutes and twenty seconds left.' (or summarise multiple)\n"
    "  User: 'Cancel the pasta timer.' / 'Stop the 5-minute timer.' → [cancel_timer 'pasta'] (speak `confirm`, e.g. 'Cancelled the pasta timer.')\n"
    "  User: 'What's on my calendar today?'         → [calendar_today_summary] (read out events with start times)\n"
    "  User: 'What's on Brittany's calendar today?' → [calendar_today_summary account='brittany']\n"
    "  User: 'What's coming up this afternoon?'     → [calendar_upcoming hours=6]\n"
    "  User: 'What's on this week?'                 → [calendar_upcoming hours=168]\n"
    "  User: 'Any new emails?' / 'What's in my inbox?' → [gmail_unread_summary] (read sender + subject for each)\n"
    "  User: 'Did Brittany get any emails?'         → [gmail_unread_summary account='brittany']\n"
    "  User: 'Read me the first one.' / 'Open that email.' → [gmail_read_thread thread_id='<id-from-prior-summary>'] (read sender, then body)\n"
    "  User: 'Who won the game?'     → 'Sorry, I don't have sports scores.'\n"
    "Examples of INCORRECT style (do not produce these):\n"
    "  'Sure! It's 9:47. Anything else I can help you with?'\n"
    "  'The weather is 62 and partly cloudy. Would you like the full forecast?'\n"
    "  'Pausing now. Let me know when you'd like me to resume!'\n"
    "  'Let me check the weather. It's 62 and partly cloudy.'\n"
    "  'Checking the live arrivals now. Next D in 5, 12, and 19 minutes.'\n"
    "  'Getting subway arrivals. Next D in 5, 12, and 19 minutes.'\n"
    "  'Let me check tomorrow's forecast. Tomorrow will be...'\n"
    "  'I'll pull that up. Volume is at 70%.'\n"
    "  'Looking that up... Now playing your Release Radar playlist.'\n"
    "  'One moment. Volume is at 70%.'\n"
    "  'Okay, here's the weather: 62 and partly cloudy.'\n"
    # Tool-use rules. (Tool-call silence is enforced by the Preambles
    # section above; the rules here cover the per-tool RESULT phrasing
    # after the tool returns.)
    "When the user asks to control music or volume, call the appropriate "
    "tool — don't ask for confirmation first. After set_volume / "
    "adjust_volume, restate the new `percent` from the tool result "
    "('Volume sixty.'). After mute / unmute, say 'Muted.' / 'Unmuted.' "
    "For transport tools, restate the action: 'Paused.' / 'Skipping.' "
    "/ 'Going back.' / 'Resuming.' For get_volume, speak the level "
    "('Volume is at 70%.'). When the user asks what the volume is, "
    "call get_volume — don't change it. Use the default step of 10% "
    "for 'volume up'/'volume down'; pass a larger delta (±20-30) for "
    "'a lot louder/quieter'. "
    "For bare 'play' / 'resume' / 'keep playing' (no song or artist named), "
    "call resume — that un-pauses paused music. ONLY call spotify_play when "
    "the user names a song, artist, album, or playlist (e.g. 'play Kanye', "
    "'play Bohemian Rhapsody', 'play my workout playlist'). "
    "When spotify_play returns a `confirm` field on success, speak that "
    "exact sentence — do NOT say 'Done.' instead. The `confirm` field is "
    "ground truth for what was just queued; do NOT call get_now_playing "
    "to verify a play action — Spotify's current_playback lags the "
    "start_playback call by several seconds and may report the previous "
    "track. The user needs to hear which artist/song/playlist was "
    "selected because voice-to-text often mishears playlist names. On "
    "error, speak the `error` field verbatim. Use get_now_playing only "
    "when the user asks about the current track ('what's playing?', "
    "'who is this?'), not as a chaser to spotify_play. "
    "Use get_weather for any weather, temperature, or rain question; if "
    "the user doesn't name a city, pass an empty location string and the "
    "tool will use the default. The weather response has now/today/tomorrow "
    "plus hourly_forecast (next 7 days, hourly granularity) plus "
    "daily_next_14d — pick the right scope. For specific times within "
    "the next 7 days ('this evening' / 'tonight' / 'tomorrow morning' / "
    "'Saturday afternoon' / 'what time will it rain on Friday'), filter "
    "hourly_forecast by the entry's 'time' field — match the date "
    "(YYYY-MM-DD) and hour against the user's reference. For 'this "
    "week' use daily_next_14d[0:7], for 'next week' daily_next_14d[7:14] — "
    "summarise as a high/low range with any rainy days called out, e.g. "
    "'Highs in the low 70s, lows around 55. Mostly sunny except Thursday "
    "with a 60% chance of rain.' For rain questions lead with the "
    "precipitation_probability percentage; if it's null, fall back to "
    "will_rain. "
    "For subway questions ('when's the next train', 'when's the next D', "
    "'next train toward Coney'), call get_subway_arrivals. ALWAYS call "
    "the tool fresh on every train question — never reuse a prior "
    "result, even if the user just asked seconds ago. Train arrivals "
    "are real-time; minutes counted down since the last call, and "
    "trains have come and gone. Repeating a stale answer is wrong. "
    "Both line and direction are optional — at a single-line station "
    "the line defaults to that line and direction defaults to the "
    "speaker's home direction, so a bare 'when's the next train' "
    "passes empty strings. Voice answer style: 'Next uptown D trains "
    "in 5, 12, and 19 minutes.' or, when station/line are obvious, "
    "just 'Next train in 4 minutes, then 11 and 17.' "
    # Timer rules.
    "For timer requests, call set_timer with the duration in seconds — "
    "convert the user's spoken duration ('5 minutes' → 300, '1 hour' "
    "→ 3600, '90 seconds' → 90). When the user names the timer "
    "('pasta timer', 'laundry'), pass that as the label arg. Speak "
    "the response's `confirm` field verbatim. Multiple timers run in "
    "parallel — setting a new one does NOT cancel existing ones. The "
    "speaker plays the announcement when the timer fires; do NOT "
    "promise to remind or follow up — the system handles it. For "
    "'how much time left' / 'what timers do I have' / 'list my "
    "timers', call list_timers and speak a brief summary using the "
    "remaining field of each entry. For 'cancel the X timer', call "
    "cancel_timer with the label or duration as the query; on "
    "success speak `confirm`. If cancel_timer returns "
    "reason='ambiguous', read out the matches' durations and ask "
    "the user which one to cancel. "
    # Calendar / Gmail rules.
    "For calendar questions, call calendar_today_summary (today's events) "
    "or calendar_upcoming (next N hours; pass the hours arg). For email "
    "questions, call gmail_unread_summary; if the user then asks to read "
    "or open one, call gmail_read_thread with the thread_id from the "
    "prior summary's response. The Google tools route per household "
    "member: when the user names a person ('Brittany's calendar', "
    "'Jasper's email'), pass that name as the `account` arg; when no "
    "person is named, OMIT the account arg and the default account is "
    "used. If the user names someone who isn't in the linked-accounts "
    "list (provided in this prompt's addendum), ask which of the "
    "linked accounts to use — list them by name. On a 'Google access "
    "for X can't be refreshed' error, speak it verbatim — the user "
    "needs to re-link at the wizard. Voice answer style: read events "
    "as 'You have <N> things today: <summary> at <time>, <summary> at "
    "<time>...'; for emails read 'You have <N> unread: <sender> about "
    "<subject>, <sender> about <subject>...' — keep it scannable, the "
    "user can ask for full details on any one."
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
) -> str:
    """Return the system instruction with current local time, the
    user's home location, and the linked Google account names
    injected.

    Called at every connection (re)open — the persistent connection
    lives across the 5-min context-reset window, so calling this on
    every fresh open keeps the time accurate to within that window.

    `location` should be the user's home location (a city/neighborhood
    string the geocoder can resolve). When set, Gemini stops asking
    "what city are you in?" for location-sensitive questions outside
    the weather tool's scope (sunset times, nearby places, traffic).

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
    addendum = (
        f" Right now it is {now_local.strftime('%A, %B %-d %Y, %-I:%M %p %Z')}"
        f" ({now_local.tzname()}). Use this directly for time/date "
        "questions — do not ask the user."
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
    isn't configured at the env level."""
    if not cfg.spotify_enabled:
        return None
    accounts = Registry.load(cfg.spotify_accounts_path)
    # First-run migration: if there's a legacy single-user OAuth cache
    # and no accounts registered yet, wrap that cache as a "default"
    # account so existing installs keep working without re-auth.
    maybe_migrate_legacy(accounts, cfg.spotify_cache_path, default_name="default")
    clients = build_clients(
        accounts,
        client_id=cfg.spotify_client_id,
        redirect_uri=cfg.spotify_redirect_uri,
    )
    if not clients:
        logger.info(
            "spotify: no accounts have OAuth tokens; tools disabled until "
            "someone visits %s",
            cfg.spotify_setup_url,
        )
    return Router(clients=clients, default_name=accounts.default_name)


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
    """Single mic consumer. Dispatches each frame to either the wake-word
    detector (WAKE state) or the active live turn (SESSION state). No
    second consumer iterating over mic.frames() — eliminates the implicit
    frame-ownership coupling between wake-listen and active-turn paths.
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
    ) -> None:
        self._cfg = cfg
        self._mic = mic
        self._tts = tts
        self._detector = detector
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
                await self._handle_wake_frame(frame)
            else:
                await self._handle_session_frame(frame)

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

    async def _handle_wake_frame(self, frame) -> None:
        # During refractory, swallow frames so TTS bleed doesn't self-trigger.
        if asyncio.get_event_loop().time() < self._refractory_until:
            return
        score = self._detector.feed(frame)
        if score is None:
            return

        # Reset openWakeWord's internal smoothing/state right after a
        # wake fires. Without this, the model stays primed for several
        # seconds — its baseline activation is elevated, so music
        # vocals or TTS-tail bleed can easily push past the threshold
        # and false-fire on the next listening window. Symptom: clean
        # wake on the first turn, then unprompted ducking dips while
        # music plays after the response. Resetting here zeroes the
        # bias so the next WAKE pass is judged fresh.
        self._detector.reset()

        import time as _time
        self._wake_event_at_monotonic = _time.monotonic()
        logger.info(
            "event=wake.detected score=%.2f threshold=%.2f",
            score, self._detector.threshold,
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
            # session after them is bad UX.
            if self._mic_muted:
                logger.info("event=wake.late_cancel reason=mic_muted")
                return  # finally clears _acquiring + buffer
            if self._measurement_active.is_set():
                logger.info("event=wake.late_cancel reason=measurement_active")
                return

            # Step 1: peer arbitration.
            decision = await self._peer_arbitrate(
                score=score, snr_db=None, rms_dbfs=rms_dbfs,
                can_serve=can_serve,
            )
            if decision == "LOSE":
                # Another peer is handling it. Stay silent — losers
                # don't play chirps or cues; they just back off.
                logger.info("event=peering.wake.lost score=%.2f", score)
                return  # finally clears _acquiring + buffer

            # Re-check the late-cancel gates: arbitration could have
            # taken up to 500 ms, during which the user could have
            # muted (e.g. via dial). Same reasoning as Step 0.
            if self._mic_muted:
                logger.info("event=wake.late_cancel reason=mic_muted_post_arb")
                return
            if self._measurement_active.is_set():
                logger.info("event=wake.late_cancel reason=measurement_active_post_arb")
                return

            # Step 2: gate cues — only the winner pays this cost.
            if not spend_allowed:
                logger.warning("daily spend cap reached; voice disabled until rollover")
                await self._play_cue("spend_cap_reached")
                return
            if conn_paused:
                logger.warning(
                    "wake detected but live connection is paused (reconnect/backoff); "
                    "ignoring this wake event",
                )
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
        except Exception as e:  # noqa: BLE001
            logger.exception("turn acquire failed: %s", e)
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
        installs pay zero observable cost — the disabled check
        short-circuits before any I/O.
        """
        self._peering_current_epoch = ""
        if not self._cfg.peering_enabled:
            return "WIN"
        try:
            from .peering.uds import send_request
        except ImportError:
            # peering package not installed (shouldn't happen in
            # production, but defensive — keep wake working).
            return "WIN"
        import json as _json  # noqa: PLC0415
        payload = _json.dumps({
            "score": float(score),
            "snr_db": snr_db,
            "rms_dbfs": rms_dbfs,
            "can_serve": bool(can_serve),
        })
        try:
            resp = await send_request(
                self._cfg.peering_uds_socket,
                f"ARBITRATE {payload}",
                timeout=0.5,
            )
        except FileNotFoundError:
            # Peering daemon isn't running (mode=on in voice config but
            # mode=off / failed in jasper-control). Fall back to solo.
            return "WIN"
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning(
                "peer arbitrate failed (%s); falling back to WIN", e,
            )
            return "WIN"
        except Exception as e:  # noqa: BLE001
            logger.exception("peer arbitrate raised: %s", e)
            return "WIN"
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
        if not self._cfg.peering_enabled or self._turn is None:
            return
        try:
            from .peering.uds import send_request
        except ImportError:
            return
        epoch = getattr(self, "_peering_current_epoch", "")
        # Best-effort: even if we don't know the epoch, the peering
        # state machine will infer from its own current epoch state.
        try:
            await send_request(
                self._cfg.peering_uds_socket,
                f"SESSION_STARTED {epoch}",
                timeout=0.5,
            )
        except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
            logger.debug("peering session_started notify failed: %s", e)
        except Exception:  # noqa: BLE001
            logger.exception("peering session_started raised")

    async def _notify_peering_session_ended(self, reason: str) -> None:
        """Fire-and-forget notice to the peering daemon. Same shape as
        _notify_peering_session_started — see that docstring."""
        if not self._cfg.peering_enabled:
            return
        try:
            from .peering.uds import send_request
        except ImportError:
            return
        epoch = getattr(self, "_peering_current_epoch", "")
        try:
            await send_request(
                self._cfg.peering_uds_socket,
                f"SESSION_ENDED {epoch} {reason}",
                timeout=0.5,
            )
        except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
            logger.debug("peering session_ended notify failed: %s", e)
        except Exception:  # noqa: BLE001
            logger.exception("peering session_ended raised")

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
            list(cfg.subway_lines) or None,
        )
        if cfg.subway_enabled else None
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
        await connection.start(
            registry,
            lambda: _build_system_instruction(
                cfg.weather_default_location,
                google_accounts=google_account_names,
                default_google_account=google_default_account,
            ),
        )
        # `make_mic_capture` routes to UdpMicCapture for
        # `JASPER_MIC_DEVICE=udp:PORT` (the AEC bridge's UDP transport
        # under the resilience-ladder PR 2 architecture) or back to
        # the PortAudio MicCapture for anything else (`Array` for
        # chip-direct, a `hw:` substring for any other USB mic).
        async with make_mic_capture(
            cfg.mic_device,
            capture_rate=cfg.mic_capture_rate,
            capture_channels=cfg.mic_capture_channels,
        ) as mic, TtsPlayout(
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
            wake_loop = WakeLoop(
                cfg, mic, tts, detector, connection, ducker,
                tts_volume_tracker, usage_store, spend_cap, stop_event,
                volume_coordinator=volume_coordinator,
                cues=cues_manager,
                camilla=camilla,
                heartbeat=heartbeat,
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
