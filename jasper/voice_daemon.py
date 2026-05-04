from __future__ import annotations

import asyncio
import logging
import signal
import sys
from enum import Enum

from .audio_io import MicCapture, TtsPlayout
from .vad import SpeechVAD
from .camilla import CamillaController, Ducker
from .config import Config
from .moode import MoodeClient
from .subway import SubwayClient
from .tools import ToolRegistry
from .tools.audio import make_audio_tools
from .tools.spotify import build_spotify, make_spotify_tools
from .tools.subway import make_subway_tools
from .tools.transport import make_transport_tools
from .tools.weather import make_weather_tools
from .usage import SpendCap, UsageStore
from .voice.gemini_session import GeminiLiveSession
from .voice.session import VoiceSession
from .wake import WakeWordDetector
from .weather import WeatherClient

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are Jasper, a concise voice assistant living in a smart speaker. "
    "Speak briefly. When the user asks to control music or volume, call the "
    "appropriate tool — don't ask for confirmation first. Use get_now_playing "
    "before answering questions about the current track. Use get_weather for "
    "any weather, temperature, or rain question; if the user doesn't name a "
    "city, pass an empty location string and the tool will use the default. "
    "The weather response has now/today/tomorrow plus hourly_next_24h plus "
    "daily_next_14d — pick the right scope for the question. For 'this "
    "evening' / 'tonight' / 'tomorrow morning', filter hourly_next_24h by "
    "the hour part of each entry's 'time' (vs current_local_time). For "
    "'this week' use daily_next_14d[0:7], for 'next week' daily_next_14d[7:14] "
    "— summarise as a high/low range with any rainy days called out, e.g. "
    "'Highs in the low 70s, lows around 55. Mostly sunny except Thursday "
    "with a 60% chance of rain.' For rain questions, lead with the "
    "precipitation_probability percentage rather than just yes/no. If "
    "probability is null, fall back to the will_rain boolean. "
    "For subway questions ('when's the next train', 'when's the next D', "
    "'next train toward Coney'), call get_subway_arrivals. Both line and "
    "direction are optional — at a single-line station the line defaults "
    "to that line and direction defaults to the speaker's home direction, "
    "so a bare 'when's the next train' should pass empty strings. Voice "
    "answer style: 'Next uptown D trains at 9 Av in 5, 12, and 19 "
    "minutes.' or, when the station/line are obvious from context, just "
    "'Next train in 4 minutes, then 11 and 17.'"
)

# Refractory after a session ends before the wake detector is re-armed.
# Covers two transients that easily false-fire the wake-word model:
#   1. TTS tail still in the playback buffer for a few hundred ms
#   2. Music ramping back from ducked level (-40 dB → 0 dB) — the
#      instant "loudness wave" looks speech-like to openWakeWord
# Without proper hardware AEC reference wired into the XVF3800, music
# itself can also false-fire wake at higher levels (vocals especially).
# 5 sec is a defensive setting: every false-fire opens a Gemini Live
# session and burns a slot in the project's concurrent-session quota
# (Tier 1 = 50; lower if not yet billing-propagated), so reducing
# false-fire frequency directly reduces 409 errors on the API side.
# Real fix: hardware AEC reference signal (TODO).
WAKE_REFRACTORY_SEC = 10.0


class State(Enum):
    WAKE = "wake"
    SESSION = "session"


def _make_session(cfg: Config) -> VoiceSession:
    if cfg.voice_provider == "gemini":
        return GeminiLiveSession(
            api_key=cfg.gemini_api_key,
            model=cfg.gemini_model,
            voice=cfg.gemini_voice,
        )
    raise RuntimeError(f"unsupported voice provider: {cfg.voice_provider}")


def _build_registry(
    cfg: Config,
    camilla: CamillaController,
    moode: MoodeClient,
    weather: WeatherClient,
    subway: SubwayClient | None,
) -> ToolRegistry:
    registry = ToolRegistry()
    for fn in make_audio_tools(camilla):
        registry.register(fn)
    for fn in make_transport_tools(moode):
        registry.register(fn)
    sp = build_spotify(cfg)
    for fn in make_spotify_tools(sp, moode, cfg.spotify_device_name):
        registry.register(fn)
    for fn in make_weather_tools(weather):
        registry.register(fn)
    for fn in make_subway_tools(subway):
        registry.register(fn)
    return registry


async def _play_responses(session: VoiceSession, tts: TtsPlayout) -> None:
    """Drain session.audio_out() to the speaker. Barge-in handling: race
    each write against an interrupt signal so a user-interrupted-the-model
    event immediately cancels in-flight playback and flushes the audio
    buffer. Without this, ALSA/sounddevice buffering causes 100-300ms of
    overrun where the model talks over the user."""
    interrupt_task: asyncio.Task | None = None
    async for chunk in session.audio_out():
        if interrupt_task is None or interrupt_task.done():
            interrupt_task = asyncio.create_task(session.wait_for_interrupt())
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
            session.clear_interrupted()
            interrupt_task = None
    if interrupt_task is not None:
        interrupt_task.cancel()


async def _idle_watchdog(session: VoiceSession, timeout: int) -> None:
    """Close the session after `timeout` seconds of model silence.

    'Activity' is any audio chunk, tool call, or turn_complete from the
    server (see GeminiLiveSession._dispatch). This means the watchdog
    won't kill a session while the model is mid-TTS — only after the
    model goes silent AND no new chunks arrive for `timeout` seconds.
    Use a short timeout (5s) for snappy session-close after one-shot
    questions; longer (15s+) preserves multi-turn follow-up windows."""
    while True:
        await asyncio.sleep(0.5)
        idle_for = asyncio.get_event_loop().time() - session.last_activity_at()
        if idle_for > timeout:
            logger.info("idle timeout, closing session")
            return


class WakeLoop:
    """Single mic consumer. Dispatches each frame to either the wake-word
    detector (WAKE state) or the live session (SESSION state). No second
    consumer iterating over mic.frames() — eliminates the implicit
    frame-ownership coupling between wake-listen and active-session paths.
    """

    def __init__(
        self,
        cfg: Config,
        mic: MicCapture,
        tts: TtsPlayout,
        detector: WakeWordDetector,
        registry: ToolRegistry,
        ducker: Ducker,
        usage_store: UsageStore,
        spend_cap: SpendCap,
        stop_event: asyncio.Event,
    ) -> None:
        self._cfg = cfg
        self._mic = mic
        self._tts = tts
        self._detector = detector
        self._registry = registry
        self._ducker = ducker
        self._usage_store = usage_store
        self._spend_cap = spend_cap
        self._stop_event = stop_event

        # Local Silero VAD for in-session barge-in gating. While the
        # model is producing TTS, mic frames are forwarded to Gemini
        # ONLY if the local VAD detects user speech — TTS bleed-through
        # is filtered out, real interrupts pass through. See
        # _handle_session_frame for the gate logic.
        self._vad = SpeechVAD()

        self._state = State.WAKE
        self._session: VoiceSession | None = None
        self._session_id: int | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._refractory_until: float = 0.0

    async def run(self) -> None:
        async for frame in self._mic.frames():
            if self._stop_event.is_set():
                if self._state is State.SESSION:
                    await self._end_session()
                return

            if self._state is State.WAKE:
                await self._handle_wake_frame(frame)
            else:
                await self._handle_session_frame(frame)

    async def _handle_wake_frame(self, frame) -> None:
        # During refractory, swallow frames so TTS bleed doesn't self-trigger.
        if asyncio.get_event_loop().time() < self._refractory_until:
            return
        if not self._detector.feed(frame):
            return

        logger.info("wake detected")
        if not self._spend_cap.allowed():
            logger.warning("daily spend cap reached; voice disabled until rollover")
            return

        try:
            await self._begin_session()
        except Exception as e:  # noqa: BLE001
            logger.exception("session begin failed: %s", e)
            await self._cleanup_after_failed_begin()

    async def _handle_session_frame(self, frame) -> None:
        # If any background task ended, the session is over. Cleanup, then
        # this frame is silently consumed (no double-dispatch into detector).
        if any(t.done() for t in self._bg_tasks):
            await self._end_session()
            return

        assert self._session is not None

        # Mic frames are forwarded unconditionally during a session.
        # Server-side `NO_INTERRUPTION` (set in gemini_session.py) means
        # the server ignores user activity while the model is speaking,
        # so bleed-through can't truncate replies. Real barge-in is
        # disabled until we wire up hardware AEC (XVF3800 USB-IN as
        # reference signal via CamillaDSP-routed playback).
        try:
            await self._session.send_audio(frame.tobytes())
        except Exception as e:  # noqa: BLE001
            logger.warning("send_audio failed (will end session): %s", e)
            await self._end_session()

    async def _begin_session(self) -> None:
        import time as _time
        t_wake = _time.monotonic()
        self._session = _make_session(self._cfg)
        self._session_id = self._usage_store.open_session()
        # Reset Silero VAD's internal LSTM state at session start so
        # state from a previous session doesn't leak into this one.
        self._vad.reset()
        await self._ducker.duck()
        # Inject current local time into the system instruction. Without
        # this, Gemini either guesses time from training data (badly) or
        # asks the user for it. There's no get_time tool because per-
        # session injection is fine for our <60s session lengths and
        # doesn't burn a tool-call round-trip.
        from datetime import datetime
        now_local = datetime.now().astimezone()
        time_addendum = (
            f" Right now it is {now_local.strftime('%A, %B %-d %Y, %-I:%M %p %Z')}"
            f" ({now_local.tzname()}). Use this directly for time/date "
            "questions — do not ask the user."
        )
        await self._session.connect(
            self._registry, SYSTEM_INSTRUCTION + time_addendum,
        )
        connect_ms = (_time.monotonic() - t_wake) * 1000
        logger.info(
            "session connect done in %.0fms (wake→setupComplete)",
            connect_ms,
        )
        playback = asyncio.create_task(_play_responses(self._session, self._tts))
        idle = asyncio.create_task(_idle_watchdog(self._session, self._cfg.idle_timeout_sec))
        self._bg_tasks = {playback, idle}
        self._state = State.SESSION

    async def _cleanup_after_failed_begin(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
        await self._ducker.restore()
        if self._session_id is not None:
            self._usage_store.close_session(self._session_id, 0, 0)
        self._session = None
        self._session_id = None
        self._bg_tasks = set()
        self._state = State.WAKE
        self._refractory_until = asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC

    async def _end_session(self) -> None:
        for t in self._bg_tasks:
            t.cancel()
        for t in self._bg_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._bg_tasks = set()

        if self._session is not None:
            try:
                await asyncio.wait_for(self._session.end_input(), timeout=2.0)
            except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                logger.debug("end_input ignored: %s", e)
            try:
                await self._session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("session close error (ignored): %s", e)

            tokens = self._session.usage_tokens()
            assert self._session_id is not None
            cost = self._usage_store.close_session(
                self._session_id, tokens["input_tokens"], tokens["output_tokens"]
            )
            # Detect "Gemini Live accepted our connection but produced
            # nothing" — symptomatic of quota exhaustion, billing not
            # propagated, service degradation, or a service-side outage
            # of the specific model. Server doesn't surface a clean
            # error in any of these cases — closes the WebSocket fine,
            # accepts our audio, just never responds. Surface it loudly
            # so the operator notices in journalctl.
            bytes_sent = self._session.bytes_sent()
            chunks_received = self._session.chunks_received()
            if bytes_sent > 0 and chunks_received == 0:
                logger.warning(
                    "SILENT FAILURE: sent %d bytes of audio to %s but "
                    "received 0 audio chunks back. Likely causes: quota "
                    "exhausted (check Google Cloud Console → Quotas), "
                    "billing not yet propagated to this model, or "
                    "service-side outage of %s. Non-Live API may still "
                    "work (separate quota bucket).",
                    bytes_sent, self._cfg.gemini_model, self._cfg.gemini_model,
                )
            logger.info(
                "session ended: %s tokens, est $%.4f (sent=%dB, recv=%d chunks)",
                tokens, cost, bytes_sent, chunks_received,
            )

        await self._ducker.restore()
        self._session = None
        self._session_id = None
        self._state = State.WAKE
        self._refractory_until = asyncio.get_event_loop().time() + WAKE_REFRACTORY_SEC


async def run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    usage_store = UsageStore(cfg.usage_db)
    spend_cap = SpendCap(usage_store, cfg.daily_spend_cap_usd)

    camilla = CamillaController(cfg.camilla_host, cfg.camilla_port)
    moode = MoodeClient(cfg.moode_base_url, cfg.mpd_host, cfg.mpd_port)
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

    registry = _build_registry(cfg, camilla, moode, weather, subway)
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

    try:
        async with MicCapture(
            cfg.mic_device,
            capture_rate=cfg.mic_capture_rate,
            capture_channels=cfg.mic_capture_channels,
        ) as mic, TtsPlayout(
            cfg.tts_device,
            output_rate=cfg.tts_output_rate,
            gain_db=cfg.tts_gain_db,
        ) as tts:
            wake_loop = WakeLoop(
                cfg, mic, tts, detector, registry, ducker,
                usage_store, spend_cap, stop_event,
            )
            await wake_loop.run()
    finally:
        await moode.aclose()
        await weather.aclose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
