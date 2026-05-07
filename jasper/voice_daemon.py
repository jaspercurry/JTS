from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections import deque
from enum import Enum

from .accounts import Registry, maybe_migrate_legacy
from .audio_io import MicCapture, TtsPlayout
from .vad import SpeechVAD
from .camilla import CamillaController, Ducker
from .config import Config
from .renderer import RendererBackend, make_backend
from .spotify_router import Router, build_clients
from .subway import SubwayClient
from .tools import ToolRegistry
from .tools.audio import make_audio_tools
from .tools.spotify import make_spotify_tools
from .tools.subway import make_subway_tools
from .tools.transport import make_transport_tools
from .tools.weather import make_weather_tools
from .usage import SpendCap, UsageStore
from .voice.gemini_session import GeminiLiveConnection
from .voice.session import LiveConnection, LiveTurn
from .volume_coordinator import VolumeCoordinator
from .volume_observers import VolumeObserver
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
    "restate the question. Do NOT preface ('sure!', 'of course!', 'let "
    "me check'). Just answer and stop. Only ask a clarifying question "
    "when the user's request is genuinely ambiguous and you literally "
    "cannot proceed without more information; in that case ask exactly "
    "one specific question and nothing else. "
    # Few-shot examples to anchor the style.
    "Examples of correct style:\n"
    "  User: 'What time is it?'      → 'It's 9:47.'\n"
    "  User: 'What's the weather?'   → '62 and partly cloudy. Rain by Thursday.'\n"
    "  User: 'Pause.' / 'Stop.'      → [pause] 'Done.'\n"
    "  User: 'Skip.' / 'Next song.'  → [next_track] 'Done.'\n"
    "  User: 'Go back.'              → [previous_track] 'Done.'\n"
    "  User: 'Resume.' / 'Play.'     → [resume] 'Done.'\n"
    "  User: 'Play some jazz.'       → [spotify_play 'jazz'] (speak the response's `confirm` field, e.g. 'Playing Jazz Vibes.')\n"
    "  User: 'Play my Workout playlist.' → [spotify_play 'Workout' kind=playlist] (speak `confirm`, e.g. 'Now playing your Workout Mix playlist.')\n"
    "  User: 'Shuffle my Workout playlist.' / 'Play my Workout playlist on shuffle.' / 'Play Workout shuffled.' → [spotify_play 'Workout' kind=playlist shuffle=true] (speak `confirm`, e.g. 'Shuffling your Workout Mix playlist.')\n"
    "  User: 'Volume up.'            → [adjust_volume +10] 'Done.'\n"
    "  User: 'Turn it down a lot.'   → [adjust_volume -25] 'Done.'\n"
    "  User: 'Set volume to 30.'     → [set_volume 30] 'Done.'\n"
    "  User: 'Mute.'                 → [mute] 'Done.'\n"
    "  User: 'Who won the game?'     → 'Sorry, I don't have sports scores.'\n"
    "Examples of INCORRECT style (do not produce these):\n"
    "  'Sure! It's 9:47. Anything else I can help you with?'\n"
    "  'The weather is 62 and partly cloudy. Would you like the full forecast?'\n"
    "  'Pausing now. Let me know when you'd like me to resume!'\n"
    # Tool-use rules (existing).
    "When the user asks to control music or volume, call the appropriate "
    "tool — don't ask for confirmation first. After a volume or transport "
    "tool call, reply with the single word 'Done.' and stop — "
    "never narrate the action ('Setting volume to 30…') and never "
    "ask a follow-up. Use the default step of 10% for 'volume up'/'volume "
    "down'; pass a larger delta (±20-30) for 'a lot louder/quieter'. "
    "For bare 'play' / 'resume' / 'keep playing' (no song or artist named), "
    "call resume — that un-pauses paused music. ONLY call spotify_play when "
    "the user names a song, artist, album, or playlist (e.g. 'play Kanye', "
    "'play Bohemian Rhapsody', 'play my workout playlist'). "
    "When spotify_play returns a `confirm` field on success, speak that "
    "exact sentence — do NOT say 'Done.' instead. The user needs to hear "
    "which artist/song/playlist was selected because voice-to-text often "
    "mishears playlist names. On error, speak the `error` field verbatim. "
    "Use get_now_playing before answering questions about the current track. "
    "Use get_weather for any weather, temperature, or rain question; if "
    "the user doesn't name a city, pass an empty location string and the "
    "tool will use the default. The weather response has now/today/tomorrow "
    "plus hourly_next_24h plus daily_next_14d — pick the right scope. For "
    "'this evening' / 'tonight' / 'tomorrow morning', filter hourly_next_24h "
    "by the hour of each entry's 'time' vs current_local_time. For 'this "
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
    "just 'Next train in 4 minutes, then 11 and 17.'"
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
# pre-persistent-connection era when each wake cost a Live slot).
# Both were over-correcting for a problem refractory can't solve.
WAKE_REFRACTORY_SEC = 0.7

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
POST_RESPONSE_IDLE_TIMEOUT_SEC = 1.5

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
            × mpd_vol × camilla_main_volume × room_correction → DAC

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
        # opportunistically debounce-write it so external changes (moOde
        # web UI volume slider, mpc, hardware knob) get captured even
        # though they don't go through our voice tools. The same path
        # also persists the loudness anchor as it updates.
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
        try:
            vol_db, muted = await self._camilla.get_volume_and_mute()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "tts volume tracker: read failed (%s); falling to silent gain",
                e,
            )
            self._tts.set_gain_db(self._tts.MIN_TTS_GAIN_DB)
            return
        if muted:
            self._tts.set_gain_db(self._tts.MIN_TTS_GAIN_DB)
            return
        try:
            l, r = await self._camilla.get_playback_rms()
            rms = max(l, r)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "playback_rms read failed (%s); using silence fallback",
                e,
            )
            rms = float("-inf")
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
            try:
                vol_db, muted = await self._camilla.get_volume_and_mute()
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "tts volume tracker poll: read failed (%s), holding last gain",
                    e,
                )
                continue
            if muted:
                self._tts.set_gain_db(self._tts.MIN_TTS_GAIN_DB)
                continue
            # Debounced persistence catches external main_volume changes
            # (moOde UI, mpc, hardware knob, anything that bypasses our
            # voice tools). Voice-tool-driven changes already persist
            # immediately via tools/audio.py; this is the catch-all.
            if self._volume_persistence is not None:
                self._volume_persistence.maybe_save(vol_db)
            try:
                l, r = await self._camilla.get_playback_rms()
                rms = max(l, r)
            except Exception:  # noqa: BLE001
                rms = float("-inf")
            windowed = self._record_rms(rms)
            self._maybe_update_anchor(windowed)
            self._tts.set_gain_db(self._compute_gain(vol_db, windowed))


def _build_system_instruction() -> str:
    """Return the system instruction with current local time injected.

    Called at every connection (re)open — the persistent connection
    lives across the 5-min context-reset window, so calling this on
    every fresh open keeps the time accurate to within that window."""
    from datetime import datetime
    now_local = datetime.now().astimezone()
    time_addendum = (
        f" Right now it is {now_local.strftime('%A, %B %-d %Y, %-I:%M %p %Z')}"
        f" ({now_local.tzname()}). Use this directly for time/date "
        "questions — do not ask the user."
    )
    return SYSTEM_INSTRUCTION + time_addendum


def _make_connection(cfg: Config) -> LiveConnection:
    if cfg.voice_provider == "gemini":
        return GeminiLiveConnection(
            api_key=cfg.gemini_api_key,
            model=cfg.gemini_model,
            voice=cfg.gemini_voice,
            context_reset_sec=float(cfg.live_context_reset_sec),
        )
    raise RuntimeError(f"unsupported voice provider: {cfg.voice_provider}")


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
        client_secret=cfg.spotify_client_secret,
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
    moode: RendererBackend,
    weather: WeatherClient,
    subway: SubwayClient | None,
    volume_coordinator: "VolumeCoordinator",
    volume_persistence: VolumePersistence | None = None,
    spotify_router: Router | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    for fn in make_audio_tools(volume_coordinator):
        registry.register(fn)
    # Reuse the router built once for the coordinator; if not passed,
    # build it here for backward-compat with any caller that doesn't
    # plumb the shared instance through.
    router = spotify_router if spotify_router is not None else _build_router(cfg)
    for fn in make_transport_tools(moode, router):
        registry.register(fn)
    for fn in make_spotify_tools(router, moode, cfg.spotify_device_name):
        registry.register(fn)
    for fn in make_weather_tools(weather):
        registry.register(fn)
    for fn in make_subway_tools(subway):
        registry.register(fn)
    return registry


async def _play_responses(turn: LiveTurn, tts: TtsPlayout) -> None:
    """Drain turn.audio_out() to the speaker. Barge-in handling: race
    each write against an interrupt signal so a user-interrupted-the-model
    event immediately cancels in-flight playback and flushes the audio
    buffer. Without this, ALSA/sounddevice buffering causes 100-300ms of
    overrun where the model talks over the user."""
    interrupt_task: asyncio.Task | None = None
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
    if interrupt_task is not None:
        interrupt_task.cancel()


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
            # Wait POST_RESPONSE_IDLE_TIMEOUT_SEC after the LAST audio
            # chunk so any tail chunks finish playing through the
            # speaker, then close. We use last_chunk_at if any audio
            # came through, else last_activity_at (no audio response).
            tail_anchor = turn.last_chunk_at() or turn.last_activity_at()
            tail_idle = now - tail_anchor
            if tail_idle > POST_RESPONSE_IDLE_TIMEOUT_SEC:
                logger.info(
                    "turn_complete + tail (%.1fs), ending turn",
                    tail_idle,
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
    ) -> None:
        self._cfg = cfg
        self._mic = mic
        self._tts = tts
        self._detector = detector
        self._connection = connection
        self._ducker = ducker
        self._tts_volume_tracker = tts_volume_tracker
        self._usage_store = usage_store
        self._spend_cap = spend_cap
        self._stop_event = stop_event
        self._volume_coordinator = volume_coordinator

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

    async def run(self) -> None:
        async for frame in self._mic.frames():
            if self._stop_event.is_set():
                if self._state is State.SESSION:
                    await self._end_turn()
                return

            # Continuously fill the pre-roll ring. When wake fires, the
            # last N frames already in this deque are what we replay
            # into the turn so the user's first phoneme isn't lost.
            self._pre_roll.append(frame)

            if self._state is State.WAKE:
                await self._handle_wake_frame(frame)
            else:
                await self._handle_session_frame(frame)

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

        logger.info("wake detected (score=%.2f, threshold=%.2f)", score, self._detector.threshold)
        if not self._spend_cap.allowed():
            logger.warning("daily spend cap reached; voice disabled until rollover")
            return

        # If the connection is in a backoff/failed window, don't bother
        # opening a turn — surface the situation in the log and skip.
        if self._connection.is_paused():
            logger.warning(
                "wake detected but live connection is paused (reconnect/backoff); "
                "ignoring this wake event"
            )
            return

        try:
            await self._begin_turn()
        except Exception as e:  # noqa: BLE001
            logger.exception("turn begin failed: %s", e)
            await self._cleanup_after_failed_begin()

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
        }

    async def _begin_turn(self) -> None:
        import time as _time
        t_wake = _time.monotonic()
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
        await self._ducker.duck()
        self._session_id = self._usage_store.open_session()
        self._turn = await self._connection.acquire_turn()
        acquire_ms = (_time.monotonic() - t_wake) * 1000
        logger.info(
            "turn acquire done in %.0fms (wake→activity_start)",
            acquire_ms,
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

    async def _end_turn(self) -> None:
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
            assert self._session_id is not None
            cost = self._usage_store.close_session(
                self._session_id, tokens["input_tokens"], tokens["output_tokens"]
            )
            # Per-turn silent-failure detection. With the persistent
            # connection, the original session-level signal ("sent N
            # bytes, recv 0 chunks") moves down to the turn level —
            # otherwise multi-turn conversations would mask one bad
            # turn under another's chunk count. Causes are unchanged:
            # quota exhaustion, billing not propagated, model outage.
            bytes_sent = self._turn.bytes_sent()
            chunks_received = self._turn.chunks_received()
            if bytes_sent > 0 and chunks_received == 0 and not self._turn.turn_lost():
                logger.warning(
                    "SILENT FAILURE: sent %d bytes of audio to %s on this "
                    "turn but received 0 audio chunks back. Likely causes: "
                    "quota exhausted (check Google Cloud Console → Quotas), "
                    "billing not yet propagated to this model, or service-"
                    "side outage of %s. Non-Live API may still work "
                    "(separate quota bucket).",
                    bytes_sent, self._cfg.gemini_model, self._cfg.gemini_model,
                )
            logger.info(
                "turn ended: %s tokens, est $%.4f (sent=%dB, recv=%d chunks%s)",
                tokens, cost, bytes_sent, chunks_received,
                ", turn_lost" if self._turn.turn_lost() else "",
            )

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
        START   → manual_session_start  (long-press begin)
        END     → manual_session_end    (long-press release)
        STATUS  → session_status        (diagnostic snapshot)

    The socket lives in /run (tmpfs) so it gets created fresh each boot
    via systemd's RuntimeDirectory=jasper. Both jasper-voice and
    jasper-control run as root, so default 0o600 perms are fine."""
    import json as _json

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            cmd = raw.decode("ascii", errors="replace").strip().upper()
            if cmd == "START":
                result = {"result": await wake_loop.manual_session_start()}
            elif cmd == "END":
                result = {"result": await wake_loop.manual_session_end()}
            elif cmd == "STATUS":
                result = wake_loop.session_status()
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

    usage_store = UsageStore(cfg.usage_db)
    spend_cap = SpendCap(usage_store, cfg.daily_spend_cap_usd)

    camilla = CamillaController(cfg.camilla_host, cfg.camilla_port)
    moode = make_backend(
        moode_base_url=cfg.moode_base_url,
        mpd_host=cfg.mpd_host,
        mpd_port=cfg.mpd_port,
        librespot_state_path=cfg.librespot_state_path,
        backend_name=cfg.renderer_backend,
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
    ducker = Ducker(camilla, cfg.duck_db)

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
    volume_coordinator = VolumeCoordinator(
        camilla=camilla,
        persistence=volume_persistence,
        backend=moode,
        spotify_router=volume_spotify_router,
        spotify_device_name=cfg.spotify_device_name,
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

    # Inbound source-volume observers (Phase 2). On the debian backend
    # we poll shairport (DBus), librespot (state file written by
    # --onevent hook), and bluez-alsa (DBus) once per second so
    # iPhone slider movements / Spotify app slider drags / BT volume
    # button presses sync into the coordinator's listening_level.
    # Skipped on moOde — that backend's worker.php owns coordination.
    volume_observer: VolumeObserver | None = None
    if cfg.renderer_backend == "debian":
        volume_observer = VolumeObserver(
            volume_coordinator,
            librespot_state_path=cfg.librespot_state_path,
        )
        await volume_observer.start()
    else:
        logger.info(
            "volume observer disabled (renderer_backend=%s; only enabled on debian)",
            cfg.renderer_backend,
        )

    registry = _build_registry(
        cfg, camilla, moode, weather, subway,
        volume_coordinator=volume_coordinator,
        volume_persistence=volume_persistence,
        spotify_router=volume_spotify_router,
    )
    detector = WakeWordDetector(cfg.wake_model, cfg.wake_threshold)

    stop_event = asyncio.Event()

    def _shutdown(*_):
        logger.info("shutdown requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    logger.info(
        "jasper-voice ready: model=%s wake=%s mic=%s tts=%s",
        cfg.gemini_model, cfg.wake_model, cfg.mic_device, cfg.tts_device,
    )

    # Open the persistent live connection ONCE at daemon startup and
    # keep it open for the daemon's lifetime. Wake events acquire/release
    # turns against this connection — they don't open new WebSockets.
    # Pass _build_system_instruction (not the rendered string) so the
    # time-injection inside it stays accurate across context resets and
    # reconnects — the connection re-renders it on every fresh open.
    connection = _make_connection(cfg)
    tts_volume_tracker: TtsVolumeTracker | None = None
    try:
        await connection.start(registry, _build_system_instruction)
        async with MicCapture(
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
            wake_loop = WakeLoop(
                cfg, mic, tts, detector, connection, ducker,
                tts_volume_tracker, usage_store, spend_cap, stop_event,
                volume_coordinator=volume_coordinator,
            )
            control_socket = await _start_control_socket(
                wake_loop, cfg.voice_control_socket,
            )
            try:
                await wake_loop.run()
            finally:
                control_socket.close()
                try:
                    await control_socket.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        if tts_volume_tracker is not None:
            await tts_volume_tracker.stop()
        if volume_observer is not None:
            await volume_observer.stop()
        await volume_coordinator.aclose()
        await connection.stop()
        await moode.aclose()
        await weather.aclose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
