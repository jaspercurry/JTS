from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections import deque

from jasper.log_event import log_event

from .. import flight_recorder, transit
from ..accounts import Registry, maybe_migrate_legacy
from ..audio_io import TtsPlayout, make_mic_capture, make_tts_playout
from ..assistant_loudness import active_voice_identity, ensure_seed_profile
from ..camilla import CamillaController, Ducker
from ..config import Config, VoiceProviderNotConfigured
from ..cues import AudioCueManager, build_cue_tts_backend
from ..google_creds import GoogleClients, build_google_clients
from ..home_assistant import HAClient, build_ha_client
from ..renderer import RendererClient
from ..research import ResearchScheduler, active_research_provider
from ..spotify_router import BuildResult, Router, build_clients
from ..timers import Timer, TimerScheduler, announcement_text
from ..tools import ToolRegistry, UntrustedContentMonitor
from ..tools.packs import ToolDeps, outcomes_to_state, register_packs
from ..usage import (
    ConnectionUptimeMeter,
    SpendCap,
    UsageStore,
    load_pricing_overrides,
    pricing_for_model,
)
from ..vad import SpeechVAD, SpeechVADSetupError
from ..voice.input_policy import (
    EffectiveSpeechInputPolicy,
    build_effective_speech_input_policy,
)
from ..voice.prompt import _build_system_instruction
from ..voice.session import LiveConnection
from ..volume_coordinator import VolumeCoordinator
from ..volume_observers import VolumeObserver
from ..volume_persistence import VolumePersistence
from ..wake import WakeWordDetector
from ..wake_events import WakeEventStore
from ..watchdog import Heartbeat
from ..weather import WeatherClient
from ..voice_daemon import (
    CAPTURE_RING_FRAMES,
    VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
    VOICE_STARTUP_CONFIG_ERROR_EXIT,
    ContentActivityTracker,
    FanInDucker,
    WakeLoop,
    _LegRuntime,
    _cancel_tracked_tasks,
    _configured_wake_legs,
    _track_task,
)

logger = logging.getLogger("jasper.voice_daemon")


def _active_model(cfg: Config) -> str:
    """Return the model name for the currently selected provider — used
    by startup-readiness logging and the silent-failure heuristic in
    `_end_turn` so journalctl shows the actual model in flight. Resolution
    lives on `Config.active_voice_model` (shared with jasper-doctor); the
    `<unknown:…>` sentinel keeps log lines legible for an unset provider."""
    return cfg.active_voice_model or f"<unknown:{cfg.voice_provider}>"


def _active_voice(cfg: Config) -> str:
    """Return the voice id for the currently selected provider."""
    provider, _model, voice = active_voice_identity(cfg)
    return voice or f"<unknown:{provider}>"


def _tts_ready_detail(cfg: Config) -> str:
    """Return the startup-log fields for the selected TTS transport."""
    if cfg.tts_transport == "outputd":
        return (
            f"tts_transport=outputd tts_owner=fanin "
            f"tts_socket={cfg.tts_outputd_socket}"
        )
    return f"tts_transport={cfg.tts_transport} unsupported=true"


def _make_connection(
    cfg: Config,
    *,
    speech_policy: EffectiveSpeechInputPolicy | None = None,
) -> LiveConnection:
    """Construct the long-lived voice connection for the active provider.

    Single switch point — `JASPER_VOICE_PROVIDER` selects which adapter
    runs. Daemon code above this function is provider-agnostic; daemon
    code below it talks only to the `LiveConnection` / `LiveTurn`
    Protocols and works equally for any provider that implements them.

    Adapter modules are imported lazily inside each branch. Loading
    `gemini_session` pulls in `google.genai` (~49 MB resident); loading
    `openai_session`/`grok_session` skips that cost when the active
    provider isn't Gemini. Symmetric for the OpenAI/Grok branches."""
    if speech_policy is None:
        speech_policy = build_effective_speech_input_policy(cfg)
    if cfg.voice_provider == "gemini":
        from .gemini_session import GeminiLiveConnection
        return GeminiLiveConnection(
            api_key=cfg.gemini_api_key,
            model=cfg.gemini_model,
            voice=cfg.gemini_voice,
            context_reset_sec=float(cfg.gemini_context_reset_sec),
        )
    if cfg.voice_provider == "openai":
        from .openai_session import OpenAIRealtimeConnection
        return OpenAIRealtimeConnection(
            api_key=cfg.openai_api_key,
            model=cfg.openai_model,
            voice=cfg.openai_voice,
            reasoning_effort=cfg.openai_reasoning_effort,
            noise_reduction=speech_policy.openai_noise_reduction,
            context_reset_sec=float(cfg.openai_context_reset_sec),
            session_max_sec=float(cfg.openai_session_max_sec),
            proactive_buffer_sec=float(cfg.openai_proactive_buffer_sec),
        )
    if cfg.voice_provider == "grok":
        from .grok_session import GrokRealtimeConnection
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


def _schedule_cue_regen(
    manager: AudioCueManager,
    task_set: set[asyncio.Task],
) -> None:
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

    _track_task(
        asyncio.create_task(_run(), name="jasper-cues-regen"),
        task_set,
        label="jasper-cues-regen",
    )


def _schedule_assistant_loudness_seed(
    cfg: Config,
    task_set: set[asyncio.Task],
) -> None:
    """Opt-in background silent provider test that seeds the loudness profile.

    This can spend a small provider TTS request, so it never runs by
    default. Passive live-response measurement still refines the profile
    after real replies without extra API calls.
    """
    if not cfg.assistant_loudness_auto_seed:
        return

    async def _run() -> None:
        await asyncio.sleep(2.0)
        try:
            profile = await asyncio.to_thread(
                ensure_seed_profile,
                cfg,
                path=cfg.assistant_loudness_profile_path,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("assistant loudness seed failed: %s", e)
            return
        if profile is not None:
            logger.info(
                "assistant loudness seed ready: provider=%s model=%s "
                "voice=%s source_lufs=%.1f confidence=%.2f",
                profile.provider, profile.model, profile.voice,
                profile.source_lufs, profile.confidence,
            )

    _track_task(
        asyncio.create_task(_run(), name="assistant-loudness-seed"),
        task_set,
        label="assistant-loudness-seed",
    )


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
        log_event(
            logger,
            "spotify.startup_empty",
            statuses=[(s.name, s.state) for s in result.statuses],
            setup_url=cfg.spotify_setup_url,
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
    transit_tools: list,
    volume_coordinator: "VolumeCoordinator",
    volume_persistence: VolumePersistence | None = None,
    spotify_router: Router | None = None,
    timer_scheduler: TimerScheduler | None = None,
    research_scheduler: ResearchScheduler | None = None,
    spend_cap: SpendCap | None = None,
    cues_manager: AudioCueManager | None = None,
    google_clients: GoogleClients | None = None,
    ha: HAClient | None = None,
    wake_event_store: "WakeEventStore | None" = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    # One shared "did we read untrusted content recently?" monitor: the
    # gmail/calendar packs stamp it when they return third-party text; the
    # home_assistant pack reads it so a clean voice session runs "unlock the
    # door" directly and only the post-email window asks to confirm. Threaded
    # to the relevant packs via ToolDeps below. See
    # jasper/tools/__init__.py UntrustedContentMonitor.
    untrusted_monitor = UntrustedContentMonitor()
    # Reuse the router built once for the coordinator; if not passed,
    # build it here for backward-compat with any caller that doesn't
    # plumb the shared instance through. Resolved once into the deps
    # bundle so transport + spotify capture the same Router.
    router = spotify_router if spotify_router is not None else _build_router(cfg)
    # Tool registration is data-driven: the ordered TOOL_PACKS registry
    # in jasper.tools.packs replaces the old hardcoded per-subsystem
    # block. The inline gates that used to live here (timer's
    # `is not None`, calendar/gmail's `list_account_names()`) are lifted
    # into each pack's `gate` predicate; the rest self-gate inside their
    # factory. The walk is fault-isolated per pack — see register_packs.
    # `camilla`, `volume_persistence`, and `cues_manager` are
    # accepted-but-unused here (kept in the signature for the re-export
    # shim / call site); they are deliberately NOT in ToolDeps.
    deps = ToolDeps(
        volume_coordinator=volume_coordinator,
        renderer=renderer,
        router=router,
        weather=weather,
        spotify_device_name=cfg.spotify_device_name,
        spotify_setup_url=cfg.spotify_setup_url,
        transit_tools=transit_tools,
        ha=ha,
        timer_scheduler=timer_scheduler,
        research_scheduler=research_scheduler,
        google_clients=google_clients,
        wake_event_store=wake_event_store,
        untrusted_monitor=untrusted_monitor,
        spend_cap=spend_cap,
    )
    # Stash the per-pack registration outcomes on the registry (the object
    # that crosses back to run()) so a silently-missing tool family is
    # observable via STATUS -> /state.voice.tool_packs + jasper-doctor,
    # not just the journal. register_packs already mutates `registry.tools`;
    # the outcome record rides alongside it.
    registry.pack_outcomes = register_packs(registry, deps)
    return registry


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
                              daemon's fan-in-backed TtsPlayout. Routed
                              here so a standalone CLI doesn't have to
                              recreate the output path or gain policy.
        MEASURE_PAUSE       → open a room-correction measurement
                              window. Drops mic frames, pauses the
                              outputd content meter. Refuses (BUSY) if a
                              session is active. Auto-clears in 2 min
                              if RESUME is never sent.
        MEASURE_RESUME      → close the measurement window.
                              Idempotent.
        MUTE                → user-driven mic mute. Drops mic frames
                              at the wake-loop gate, ends any active
                              session, plays a low-pitch click. Runtime
                              state is persisted. Idempotent.
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
    # Log flight recorder + runtime debug toggle (/system Debug card).
    # install() holds the jasper logger at DEBUG for the in-RAM ring,
    # keeps the journal at INFO, and applies the debug toggle. See
    # jasper/flight_recorder.py / docs/HANDOFF-observability.md.
    flight_recorder.install("voice")

    active_model = _active_model(cfg)
    pricing_overrides = load_pricing_overrides()
    pricing = pricing_for_model(active_model, overrides=pricing_overrides)
    speech_policy = build_effective_speech_input_policy(cfg)
    log_event(
        logger,
        "voice.input_policy",
        provider=cfg.voice_provider,
        profile=speech_policy.input_contract.profile,
        source=speech_policy.input_contract.source,
        endpointing=speech_policy.endpointing,
        openai_noise_reduction=speech_policy.openai_noise_reduction_label,
        openai_noise_reduction_source=speech_policy.openai_noise_reduction_source,
        contract=speech_policy.input_contract.provenance,
    )
    for warning in speech_policy.warnings:
        log_event(
            logger,
            "voice.input_policy.warning",
            warning=warning,
            level=logging.WARNING,
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
        log_event(
            logger,
            "pricing.unpriced",
            model=active_model,
            note=(
                "no rate available; cost estimates will be $0 and the "
                "spend cap cannot bound this model until you set a rate "
                f"at http://{cfg.hostname}/voice"
            ),
            level=logging.WARNING,
        )
    usage_store = UsageStore(
        cfg.usage_db,
        pricing=pricing,
        pricing_overrides=pricing_overrides,
    )
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
    # Transit (subway / bus / Citi Bike today; future city packs add more).
    # One call builds every provider in the household's ENABLED city packs
    # (JASPER_TRANSIT_CITIES; unset = all packs, non-breaking) and returns a
    # managed ActiveTransit: the flat tool list, a `configured` flag for the
    # system-prompt nudge, and an `aclose()` that releases any client owning a
    # pool (closed in shutdown below). Each provider self-gates on its own
    # config, so an enabled-but-unconfigured mode produces no tool —
    # `transit_configured` is exactly "at least one transit tool registered",
    # the same gate as before. Adding a city needs no edit here; see
    # jasper.transit.active_transit. os.environ carries
    # JASPER_TRANSIT_CITIES via transit.env, sourced by jasper-voice.service.
    transit_active = transit.active_transit(os.environ)
    transit_tools = transit_active.tools
    transit_configured = transit_active.configured
    logger.info(
        "transit: packs=%s tools=%d",
        ",".join(transit.enabled_pack_ids(os.environ)) or "(none)",
        len(transit_tools),
    )
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
    # Ducker built after the coordinator so restore follows the active
    # output topology. Current production routes TTS/cues into fan-in
    # before CamillaDSP, so ducking must also happen in fan-in; otherwise
    # Camilla main_volume would attenuate assistant audio along with
    # renderer/program audio.
    if cfg.duck_transport == "fanin":
        ducker = FanInDucker(cfg.tts_outputd_socket, cfg.duck_db)
    else:
        ducker = Ducker(
            camilla, cfg.duck_db,
            target_db_provider=volume_coordinator.get_camilla_target_db,
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

    # Research scheduler — same lifecycle shape as timers. Constructed
    # before tool registration so research(query) is visible from the first
    # model session when a text provider key is configured; the WakeLoop
    # announcement callback is wired after WakeLoop exists.
    active_research = active_research_provider(os.environ)
    research_scheduler: ResearchScheduler | None = None
    if active_research is not None:
        research_scheduler = ResearchScheduler(
            active_research.client,
            db_path=cfg.research_db_path,
            max_runtime_sec=cfg.research_max_runtime_sec,
            concurrency=cfg.research_concurrency,
            max_result_chars=cfg.research_max_result_chars,
            usage_store=usage_store,
            usage_provider=active_research.provider_id,
            usage_model=str(getattr(active_research.client, "model", "")),
        )

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
        cfg, camilla, renderer, weather, transit_tools,
        volume_coordinator=volume_coordinator,
        volume_persistence=volume_persistence,
        spotify_router=volume_spotify_router,
        timer_scheduler=timer_scheduler,
        research_scheduler=research_scheduler,
        spend_cap=spend_cap,
        cues_manager=cues_manager,
        google_clients=google_clients,
        ha=ha,
        wake_event_store=wake_event_store,
    )

    # Apply user-edited prompt overrides before any provider serializes the
    # registry, then write the /run catalog the /tools/ wizard reads. Includes
    # EVERY tool (needs_setup ones via sentinel deps), with status from the
    # live registry + the user's disabled pack/tool sets. Fail-soft.
    from ..tool_prompt_overrides import read_prompt_overrides
    from ..tool_state import read_tool_state
    from ..tools.catalog import DEFAULT_CATALOG_PATH, write_catalog
    tool_state = read_tool_state()
    prompt_overrides = read_prompt_overrides()
    registry.apply_prompt_overrides(prompt_overrides)
    write_catalog(
        registry,
        tool_state.disabled_tools,
        disabled_packs=tool_state.disabled_packs,
        prompt_overrides=prompt_overrides,
        path=DEFAULT_CATALOG_PATH,
    )

    # Wire the timer pre-render hook so set_timer (and start-time
    # restore for persisted timers) synthesises + caches the
    # fire-time announcement WAV ahead of time. Saves the user from
    # a 1–8 s gap between duck and audio at fire time.
    async def _prerender_timer(t: Timer) -> None:
        await cues_manager.prerender_text(announcement_text(t))
    timer_scheduler.set_pre_render(_prerender_timer)

    startup_fire_and_forget: set[asyncio.Task] = set()
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
    connection = _make_connection(cfg, speech_policy=speech_policy)
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
    content_activity: ContentActivityTracker | None = None
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
        # transit_configured (computed at construction above) is true when
        # ANY transit tool is live — the system prompt nudges the model
        # toward /transit only when ALL transit options are absent. Partial
        # configurations (e.g. subway set, bus/citibike not) don't need the
        # nudge because the available tool surface still answers the modes
        # the household has actually configured.
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
                provider=cfg.voice_provider,
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
                    log_event(
                        logger,
                        "wake.leg_skipped",
                        leg=spec.token,
                        device=device,
                        reason="mic_open_failed",
                        err=str(exc),
                        level=logging.WARNING,
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
                # outputd owns the final gain decision. This fallback is
                # used only by chirps/legacy sounddevice paths.
                gain_db=0.0,
                drain_tail_sec=cfg.tts_drain_tail_sec,
                outputd_socket=cfg.tts_outputd_socket,
                provider=cfg.voice_provider,
                model=_active_model(cfg),
                voice=_active_voice(cfg),
                assistant_loudness_profile_path=(
                    cfg.assistant_loudness_profile_path
                ),
            ))
            content_activity = ContentActivityTracker(camilla)
            await content_activity.start()

            # Wire the playout into the cue manager that was already
            # constructed up top so timer tools could register with a
            # working pre-render path. From here on cues.play() and
            # cues.speak_text() can write audio out.
            cues_manager.attach_tts(tts)
            # Kick off background regen for any missing/stale cues.
            # Doesn't block daemon "ready" — if regen fails (no
            # internet / bad API key), cues silently won't play; the
            # daemon's other voice paths still work.
            _schedule_cue_regen(cues_manager, startup_fire_and_forget)
            _schedule_assistant_loudness_seed(cfg, startup_fire_and_forget)

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
                content_activity, usage_store, spend_cap, stop_event,
                volume_coordinator=volume_coordinator,
                legs=legs,
                cues=cues_manager,
                camilla=camilla,
                heartbeat=heartbeat,
                wake_event_store=wake_event_store,
                tool_packs=outcomes_to_state(registry.pack_outcomes),
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
            if research_scheduler is not None:
                wake_loop.set_research_scheduler(research_scheduler)
                research_scheduler.set_on_done(wake_loop.announce_research_ready)
                await research_scheduler.start()
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
        await _cancel_tracked_tasks(startup_fire_and_forget)
        # Stop schedulers FIRST so any in-flight `_run` tasks that were
        # about to announce get cancelled before we tear down the cue
        # manager / TtsPlayout they'd be calling into.
        await timer_scheduler.stop()
        if research_scheduler is not None:
            await research_scheduler.stop()
        if active_research is not None:
            await active_research.aclose()
        # Wake-event store close — moved out of the inner async-with
        # block when the open was hoisted up so the diagnostic tools
        # could land in the registry before the LLM session opened.
        if wake_event_store is not None:
            try:
                wake_event_store.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("wake_events store close: %s", e)
        if content_activity is not None:
            await content_activity.stop()
        if volume_observer is not None:
            await volume_observer.stop()
        await volume_coordinator.aclose()
        await connection.stop()
        await weather.aclose()
        if ha is not None:
            await ha.aclose()
        # Release any transit client that owns a resource (today only
        # BusClient's httpx.AsyncClient pool, whose idle connections + FDs
        # would otherwise leak across daemon restart cycles). The managed
        # ActiveTransit result owns that cleanup — the daemon just closes the
        # subsystem, knowing nothing about which clients are closeable.
        await transit_active.aclose()


def main() -> None:
    try:
        asyncio.run(run())
    except VoiceProviderNotConfigured as e:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        log_event(
            logger,
            "voice.unconfigured",
            reason=str(e),
            level=logging.WARNING,
        )
        print(str(e), file=sys.stderr)
        sys.exit(VOICE_PROVIDER_NOT_CONFIGURED_EXIT)
    except SpeechVADSetupError as e:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        log_event(
            logger,
            "voice.vad_setup_failed",
            reason=str(e),
            level=logging.ERROR,
        )
        print(str(e), file=sys.stderr)
        sys.exit(VOICE_STARTUP_CONFIG_ERROR_EXIT)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
