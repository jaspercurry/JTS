from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .audio_io import MicCapture, TtsPlayout
from .camilla import CamillaController, Ducker
from .config import Config
from .moode import MoodeClient
from .tools import ToolRegistry
from .tools.audio import make_audio_tools
from .tools.spotify import build_spotify, make_spotify_tools
from .tools.transport import make_transport_tools
from .usage import SpendCap, UsageStore
from .voice.gemini_session import GeminiLiveSession
from .voice.session import VoiceSession
from .wake import WakeWordDetector

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are Jasper, a concise voice assistant living in a smart speaker. "
    "Speak briefly. When the user asks to control music or volume, call the "
    "appropriate tool — don't ask for confirmation first. Use get_now_playing "
    "before answering questions about the current track."
)


def _make_session(cfg: Config) -> VoiceSession:
    if cfg.voice_provider == "gemini":
        return GeminiLiveSession(api_key=cfg.gemini_api_key, model=cfg.gemini_model)
    raise RuntimeError(f"unsupported voice provider: {cfg.voice_provider}")


def _build_registry(cfg: Config, camilla: CamillaController, moode: MoodeClient) -> ToolRegistry:
    registry = ToolRegistry()
    for fn in make_audio_tools(camilla):
        registry.register(fn)
    for fn in make_transport_tools(moode):
        registry.register(fn)
    sp = build_spotify(cfg)
    for fn in make_spotify_tools(sp):
        registry.register(fn)
    return registry


async def _run_session(
    cfg: Config,
    registry: ToolRegistry,
    mic: MicCapture,
    tts: TtsPlayout,
    ducker: Ducker,
    usage_store: UsageStore,
) -> None:
    """One wake → talk → respond → idle cycle."""
    session = _make_session(cfg)
    session_id = usage_store.open_session()
    await ducker.duck()

    try:
        await session.connect(registry, SYSTEM_INSTRUCTION)
        playback_task = asyncio.create_task(_play_responses(session, tts))
        idle_task = asyncio.create_task(_idle_watchdog(session, cfg.idle_timeout_sec))
        capture_task = asyncio.create_task(_pump_mic(session, mic, idle_task))
        done, pending = await asyncio.wait(
            {playback_task, idle_task, capture_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        await session.close()
        await ducker.restore()
        tokens = session.usage_tokens()
        cost = usage_store.close_session(
            session_id, tokens["input_tokens"], tokens["output_tokens"]
        )
        logger.info("session ended: %s tokens, est $%.4f", tokens, cost)


async def _play_responses(session: VoiceSession, tts: TtsPlayout) -> None:
    async for chunk in session.audio_out():
        await tts.write(chunk)


async def _pump_mic(session: VoiceSession, mic: MicCapture, idle_task: asyncio.Task) -> None:
    """Send mic frames to the session until the idle watchdog fires."""
    async for frame in mic.frames():
        if idle_task.done():
            return
        await session.send_audio(frame.tobytes())


async def _idle_watchdog(session: VoiceSession, timeout: int) -> None:
    """End the session after `timeout` seconds without a model turn."""
    last_turn = asyncio.get_event_loop().time()
    while True:
        await asyncio.sleep(1.0)
        if session.turn_complete():
            last_turn = asyncio.get_event_loop().time()
        elif asyncio.get_event_loop().time() - last_turn > timeout:
            logger.info("idle timeout, closing session")
            return


async def run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    usage_store = UsageStore(cfg.usage_db)
    spend_cap = SpendCap(usage_store, cfg.daily_spend_cap_usd)

    camilla = CamillaController(cfg.camilla_host, cfg.camilla_port)
    moode = MoodeClient(cfg.moode_base_url, cfg.mpd_host, cfg.mpd_port)
    ducker = Ducker(camilla, cfg.duck_db)

    registry = _build_registry(cfg, camilla, moode)
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
        async with MicCapture(cfg.mic_device) as mic, TtsPlayout(cfg.tts_device) as tts:
            await _wake_loop(cfg, mic, tts, detector, registry, ducker, usage_store, spend_cap, stop_event)
    finally:
        await moode.aclose()


async def _wake_loop(
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
    async for frame in mic.frames():
        if stop_event.is_set():
            return
        if not detector.feed(frame):
            continue
        logger.info("wake detected")
        if not spend_cap.allowed():
            logger.warning("daily spend cap reached; voice disabled until rollover")
            continue
        try:
            await _run_session(cfg, registry, mic, tts, ducker, usage_store)
        except Exception as e:  # noqa: BLE001
            logger.exception("session crashed: %s", e)
        detector.reset()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
