# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, TypeVar

from jasper.log_event import log_event

from .. import flight_recorder, transit
from ..mic_capture import InputDeviceUnavailable, make_mic_capture
from ..tts_playout import TtsPlayout
from ..assistant_loudness import active_voice_identity, ensure_seed_profile
from ..camilla import (
    CamillaController,
    set_canonical_target_db_provider,
)
from ..config import Config, VoiceConfigError, VoiceProviderNotConfigured
from ..conversation_history import (
    ConversationStore,
    read_settings as read_conversation_settings,
)
from ..cues import (
    AudioCueManager,
    build_cue_tts_backend,
)
from ..cues.park import play_park_cue
from ..cues.registry import (
    NO_ROOM_MIC_CUE_SLUG,
    VOICE_ASSETS_MISSING_CUE_SLUG,
    VOICE_NOT_SET_UP_CUE_SLUG,
)
from ..google_creds import GoogleClients, build_google_clients
from ..google_routes import build_google_routes_client
from ..home_assistant import HAClient, build_ha_client
from ..install_profile import (
    install_profile_supports_wake_detection,
    read_install_profile,
)
from ..mic_presence import voice_park_is_transient
from ..renderer import RendererClient
from ..spotify_router import Router, build_router
from ..timers import Timer, TimerScheduler, announcement_text
from ..tools import ToolRegistry, UntrustedContentMonitor
from .conversation import register_conversation_tools
from ..tools.packs import ToolDeps, outcomes_to_state, register_packs
from ..usage import (
    BillableActivityMeter,
    Pricing,
    SpendCap,
    UsageStore,
    load_pricing_overrides,
    pricing_for_model,
)
from ..usage_writer import VoiceUsageStore
from ..vad import SpeechVAD, SpeechVADSetupError
from ..voice import control_socket as control_socket_mod
from ..voice.assistant_output import FanInDucker
from ..voice.input_policy import (
    EffectiveSpeechInputPolicy,
    build_effective_speech_input_policy,
)
from ..voice.input_presence import voice_parked_no_mic
from ..voice.prompt import _build_system_instruction
from ..voice.session import LiveConnection
from ..volume_coordinator import VolumeCoordinator
from ..volume_observers import VolumeObserver
from ..volume_owner import install_volume_owner
from ..volume_persistence import VolumePersistence
from ..wake import WakeWordDetector
from ..wake_events import WakeEventStore
from ..watchdog import Heartbeat
from ..weather import WeatherClient
from ..voice_daemon import (
    CAPTURE_RING_FRAMES,
    VOICE_MIC_UNAVAILABLE_EXIT,
    VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
    VOICE_STARTUP_CONFIG_ERROR_EXIT,
    WakeLoop,
    LegRuntime,
    cancel_tracked_tasks,
    configured_wake_legs,
    track_task,
)
from .content_activity import ContentActivityTracker
from .push_to_talk import ManualMicRuntime
from ..logging_setup import configure_logging

logger = logging.getLogger("jasper.voice_daemon")

_T = TypeVar("_T")


def _wire_billable_activity_meter(
    *,
    connection: LiveConnection,
    usage_store: UsageStore,
    provider: str,
    flat_per_hour_usd: float,
) -> bool:
    """Wire flat-rate realtime billing into a provider connection.

    The adapter owns what "billable activity" means, by exposing
    ``set_billable_activity_meter``. A missing hook is logged because the
    spend cap would otherwise under-count a priced provider.
    """
    if flat_per_hour_usd <= 0:
        return False

    set_meter = getattr(connection, "set_billable_activity_meter", None)
    if not callable(set_meter):
        log_event(
            logger,
            "pricing.flat_rate_meter_unavailable",
            provider=provider,
            flat_per_hour_usd=f"{flat_per_hour_usd:.6f}",
            note=(
                "active model has a flat realtime rate but its adapter does "
                "not expose set_billable_activity_meter; spend cap will "
                "not count that provider's realtime activity"
            ),
            level=logging.WARNING,
        )
        return False

    set_meter(BillableActivityMeter(
        usage_store, provider, flat_per_hour_usd,
    ))
    logger.info(
        "realtime activity meter: enabled for %s at $%.2f/hour",
        provider, flat_per_hour_usd,
    )
    return True


def _active_voice(cfg: Config) -> str:
    """Return the voice id for the currently selected provider."""
    provider, _model, voice = active_voice_identity(cfg)
    return voice or f"<unknown:{provider}>"


def _require_usable_input(
    legs: list[LegRuntime],
    manual_mics: list[ManualMicRuntime],
    declared_manual_devices: Iterable[str],
) -> None:
    """Refuse to run a daemon that can never hear anything (NN-6).

    A planned primary leg that fails to open already raises in
    `_open_wake_legs`, so this is the backstop for the *other* shape: a
    speaker with no room mic (no leg planned at all — issue #2205) whose
    accessory sources then all failed to open. That loop SKIPS a bad source
    rather than raising, so without this the daemon comes up, logs "ready",
    keeps patting its watchdog, and is permanently deaf.

    Fails the same fatal-but-CLEAN way a primary mic-open failure does:
    `main()` exits VOICE_MIC_UNAVAILABLE_EXIT and systemd parks the unit.
    """
    if legs or manual_mics:
        return
    raise InputDeviceUnavailable(
        ",".join(declared_manual_devices) or "<none>",
        RuntimeError("no usable mic source: no wake leg, no manual mic"),
    )


# Floor for jasper-voice.service TimeoutStopSec: the 4.65 s cue plus drain
# and two 1 s-timeout duck legs is 8.7 s worst case, then the untimed
# teardown. See ADR-0239.
MIC_LOSS_CUE_STOP_FLOOR_SEC = 14.0


async def _announce_mic_loss_at_shutdown(wake_loop: WakeLoop) -> str:
    """Say out loud that this speaker just lost its microphone. See ADR-0239.

    Returns the result code it logged — ``not_parked``, ``transient_park``,
    ``ok`` or ``play_error``. Never raises.
    """
    if not voice_parked_no_mic():
        return "not_parked"
    if voice_park_is_transient():
        result = "transient_park"
    else:
        try:
            result = await wake_loop.play_cue(NO_ROOM_MIC_CUE_SLUG)
        except Exception:  # noqa: BLE001
            logger.exception("mic-loss cue play failed")
            result = "play_error"
    log_event(
        logger,
        "voice.mic_loss_cue",
        slug=NO_ROOM_MIC_CUE_SLUG,
        result=result,
        level=logging.INFO if result in ("ok", "transient_park") else logging.WARNING,
    )
    return result


def _announce_park_at_boot(slug: str) -> str:
    """Say out loud why this daemon is parking, then let the caller exit.

    The boot checks that raise 66/78 all run before the daemon's own cue
    manager and TtsPlayout exist, so the largest deaf window on the box is a
    park nobody hears (NN-6). Called after `asyncio.run(run())` returns, so
    no loop is running and `play_park_cue` can own one. Never raises, and
    never changes the exit code the caller hands systemd.
    """
    result = play_park_cue(slug, logger=logger)
    log_event(
        logger,
        "voice.park_cue",
        slug=slug,
        result=result,
        level=logging.INFO if result == "ok" else logging.WARNING,
    )
    return result


def _wake_detection_supported() -> bool:
    """Whether the install profile grants always-on wake inference.

    ``read_install_profile()`` raises ``ValueError`` on an unparseable marker
    token, which ``main()`` does not special-case: it would traceback out,
    exit 1, and climb ``Restart=on-failure`` to ``StartLimitAction=reboot``.
    So fail OPEN rather than reboot a speaker over a corrupt marker file.
    ``jasper.control.server._control_install_profile`` mirrors this and fails
    the opposite way — its stakes are a route allowlist, not a daemon crash.
    """
    try:
        profile = read_install_profile()
    except ValueError as e:
        log_event(
            logger,
            "voice.install_profile_unreadable",
            detail=str(e),
            level=logging.WARNING,
        )
        return True
    return install_profile_supports_wake_detection(profile)


def _wake_ready_detail(cfg: Config, planned_wake_legs: list) -> str:
    """The startup line's ``wake=`` field — the operator's evidence that the
    #2205 hardware verification greps for in the journal.

    Keyed on the RESOLVED leg plan, never on ``cfg.wake_model`` alone: on a
    speaker with no room mic the plan is empty and no detector is built, and
    naming the model there would claim wake detection on a box that will
    never wake.
    """
    return cfg.wake_model if planned_wake_legs else "disabled(no wake leg)"


def _tts_ready_detail(cfg: Config) -> str:
    """The startup line's ``tts_socket=`` field: where assistant audio
    enters (fan-in solo, outputd when a bonded member overrides it)."""
    return f"tts_socket={cfg.tts_outputd_socket}"


def _make_connection(
    cfg: Config,
    *,
    speech_policy: EffectiveSpeechInputPolicy | None = None,
) -> LiveConnection:
    """Construct the long-lived voice connection for the active provider.

    The single switch point — `JASPER_VOICE_PROVIDER` selects the adapter,
    and every other daemon path talks only to the `LiveConnection` /
    `LiveTurn` Protocols.

    Adapter modules are imported lazily inside each branch: loading
    `gemini_session` pulls in `google.genai` (~49 MB resident), and the
    OpenAI/Grok branches skip that cost symmetrically."""
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
    if cfg.voice_provider == "openai_live":
        from .openai_live_session import OpenAILiveConnection  # lazy — optional provider SDK
        return OpenAILiveConnection(
            api_key=cfg.openai_api_key, model=cfg.openai_live_model,
            voice=cfg.openai_live_voice, backend_model=cfg.openai_live_backend_model,
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
    """Construct the audio-cue manager.

    The template hostname is the host part of JASPER_MANAGEMENT_URL, so cues
    say "visit jts.local" rather than reading out a full URL. `tts` may be
    None when the daemon registers cue-aware tools (timer pre-render) before
    the TtsPlayout has opened; `attach_tts` wires it."""
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
    """Background task: bake any missing / stale cues. Failures are logged,
    never raised — the daemon comes up even if regeneration can't run."""
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

    track_task(
        asyncio.create_task(_run(), name="jasper-cues-regen"),
        task_set,
        label="jasper-cues-regen",
    )


def _schedule_assistant_loudness_seed(
    cfg: Config,
    task_set: set[asyncio.Task],
) -> None:
    """Opt-in background silent provider test that seeds the loudness profile.

    Spends a small provider TTS request, so it never runs by default —
    passive live-response measurement refines the profile for free.
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

    track_task(
        asyncio.create_task(_run(), name="assistant-loudness-seed"),
        task_set,
        label="assistant-loudness-seed",
    )


def _build_router(cfg: Config) -> Router | None:
    """The multi-account spotify router, or None when Spotify is unconfigured.

    Carries a `rebuild_fn` so a startup-time revocation (or a re-link via the
    web wizard) recovers without a daemon restart: with `router.clients`
    empty, the next tool call rebuilds via Router.refresh_if_empty()."""
    if not cfg.spotify_enabled:
        return None
    router = build_router(
        client_id=cfg.spotify_client_id,
        redirect_uri=cfg.spotify_redirect_uri,
        accounts_path=cfg.spotify_accounts_path,
        cache_path=cfg.spotify_cache_path,
        with_rebuild=True,
    )
    if not router.clients:
        # Surface the per-account reasons at startup so a "Spotify
        # tools are silent" report has a forensic trail.
        log_event(
            logger,
            "spotify.startup_empty",
            statuses=[(s.name, s.state) for s in router.statuses],
            setup_url=cfg.spotify_setup_url,
        )
    return router


def _build_registry(
    cfg: Config,
    renderer: RendererClient,
    weather: WeatherClient,
    transit_tools: list,
    volume_coordinator: "VolumeCoordinator",
    spotify_router: Router | None = None,
    timer_scheduler: TimerScheduler | None = None,
    google_clients: GoogleClients | None = None,
    google_routes=None,
    ha: HAClient | None = None,
    wake_event_store: "WakeEventStore | None" = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    # One shared monitor: the gmail/calendar packs stamp it when they return
    # third-party text, and the home_assistant pack reads it so only the
    # post-email window asks to confirm "unlock the door". See
    # jasper/tools/__init__.py UntrustedContentMonitor.
    untrusted_monitor = UntrustedContentMonitor()
    # Resolved once into the deps bundle so transport + spotify capture the
    # same Router as the volume coordinator.
    router = spotify_router if spotify_router is not None else _build_router(cfg)
    deps = ToolDeps(
        volume_coordinator=volume_coordinator,
        renderer=renderer,
        router=router,
        weather=weather,
        spotify_device_name=cfg.spotify_device_name,
        spotify_setup_url=cfg.spotify_setup_url,
        google_setup_url=cfg.google_setup_url,
        transit_tools=transit_tools,
        google_routes=google_routes,
        ha=ha,
        timer_scheduler=timer_scheduler,
        google_clients=google_clients,
        wake_event_store=wake_event_store,
        untrusted_monitor=untrusted_monitor,
    )
    # The outcomes ride back on the registry so a silently-missing tool
    # family is observable via STATUS -> /state.voice.tool_packs and
    # jasper-doctor, not just the journal.
    registry.pack_outcomes = register_packs(registry, deps)
    return registry


async def _serve_while_connecting(
    connect: Callable[[], Awaitable[None]],
    serve: Callable[[], Awaitable[None]],
) -> None:
    """Serve wake while the first provider connect is still dialling.

    Hearing must not wait on the WAN: mics, cues and ``READY=1`` are up
    before this is reached, so a boot with the link down answers a wake with
    a cue instead of silence. A connect that raises ends the run; whichever
    task finishes first, the other is cancelled on the way out.
    """
    connect_task = asyncio.create_task(connect())
    serve_task = asyncio.create_task(serve())
    try:
        done, _pending = await asyncio.wait(
            (connect_task, serve_task), return_when=asyncio.FIRST_COMPLETED,
        )
        if connect_task in done:
            # Both can land in one tick; the failure must not be left for
            # the suppressed await below to eat.
            connect_task.result()
        await serve_task
    finally:
        for task in (connect_task, serve_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _log_teardown_failed(name: str, exc: BaseException) -> None:
    log_event(
        logger,
        "voice.teardown_failed",
        resource=name,
        exc_type=type(exc).__name__,
        detail=str(exc),
        level=logging.WARNING,
    )


def _release(
    stack: contextlib.AsyncExitStack,
    name: str,
    fn: Callable[..., object],
    *args: Any,
) -> None:
    """Register `fn(*args)` as a teardown that cannot eat the park.

    `AsyncExitStack` REPLACES the body's exception with any callback's
    (demoting the original to `__context__`), so one unlucky teardown turns
    any park exception `main()` handles into a plain crash: no cue, exit 1,
    and a systemd restart loop instead of a park (NN-6; ADR-0239). Every
    release goes through here or `_arelease`. `CancelledError` is a
    `BaseException`, so cancellation still propagates.
    """
    def _tolerant() -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            _log_teardown_failed(name, exc)

    stack.callback(_tolerant)


def _arelease(
    stack: contextlib.AsyncExitStack,
    name: str,
    fn: Callable[..., Awaitable[object]],
    *args: Any,
) -> None:
    """`_release` for an awaitable teardown."""
    async def _tolerant() -> None:
        try:
            await fn(*args)
        except Exception as exc:  # noqa: BLE001
            _log_teardown_failed(name, exc)

    stack.push_async_callback(_tolerant)


async def _aenter(
    stack: contextlib.AsyncExitStack,
    name: str,
    cm: contextlib.AbstractAsyncContextManager[_T],
) -> _T:
    """`stack.enter_async_context`, with `_release`'s tolerant exit."""
    entered = await cm.__aenter__()
    _arelease(stack, name, cm.__aexit__, None, None, None)
    return entered


def _log_speech_input_policy(cfg: Config) -> EffectiveSpeechInputPolicy:
    policy = build_effective_speech_input_policy(cfg)
    log_event(
        logger,
        "voice.input_policy",
        provider=cfg.voice_provider,
        profile=policy.input_contract.profile,
        source=policy.input_contract.source,
        endpointing=policy.endpointing,
        openai_noise_reduction=policy.openai_noise_reduction_label,
        openai_noise_reduction_source=policy.openai_noise_reduction_source,
        contract=policy.input_contract.provenance,
    )
    for warning in policy.warnings:
        log_event(
            logger,
            "voice.input_policy.warning",
            warning=warning,
            level=logging.WARNING,
        )
    return policy


def _resolve_pricing(cfg: Config) -> tuple[Pricing, dict[str, dict]]:
    """The active model's rate card, and the overrides the usage store keeps."""
    overrides = load_pricing_overrides()
    model_name = cfg.active_voice_model
    pricing = pricing_for_model(model_name, overrides=overrides)
    logger.info(
        "spend cap: provider=%s model=%s pricing=%s cap=$%.2f/day (safety x%.2f)",
        cfg.voice_provider, model_name, pricing.label,
        cfg.daily_spend_cap_usd, cfg.daily_spend_cap_safety_multiplier,
    )
    if pricing.label.startswith("unpriced:"):
        # No rate for the active model (not in the bundled dated defaults
        # nor the override). We do NOT invent one — cost will read $0 and
        # the spend cap can't bound it until a rate is entered at
        # /assistant/voice/.
        log_event(
            logger,
            "pricing.unpriced",
            model=model_name,
            surface="voice",
            note=(
                "no rate available; cost estimates will be $0 and the "
                "spend cap cannot bound this model until you set a rate "
                f"at http://{cfg.hostname}/assistant/voice/"
            ),
            level=logging.WARNING,
        )
    return pricing, overrides


def _open_conversation_store() -> ConversationStore | None:
    settings = read_conversation_settings()
    if not settings.capture_enabled:
        return None
    return ConversationStore(settings.db_path)


async def _open_usage_store(
    stack: contextlib.AsyncExitStack,
    cfg: Config,
    pricing: Pricing,
    pricing_overrides: dict[str, dict],
) -> tuple[UsageStore, SpendCap]:
    usage_store = await VoiceUsageStore.start(
        cfg.usage_db, pricing=pricing, pricing_overrides=pricing_overrides,
    )
    _arelease(stack, "usage", usage_store.aclose)
    return usage_store, SpendCap(
        usage_store,
        cfg.daily_spend_cap_usd, cfg.daily_spend_cap_safety_multiplier,
    )


@dataclass(frozen=True, slots=True)
class _Integrations:
    """The third-party services the tool registry and system prompt see."""

    weather: WeatherClient
    transit_tools: list
    # True when ANY transit tool is live: the prompt nudges toward /transit
    # only when every option is absent, because a partial configuration still
    # answers the modes the household did set up.
    transit_configured: bool
    google_routes: Any
    google_clients: GoogleClients | None
    ha: HAClient | None


async def _open_integrations(
    stack: contextlib.AsyncExitStack, cfg: Config,
) -> _Integrations:
    """Every third-party service behind a tool pack or a prompt nudge."""
    weather = WeatherClient(
        cfg.weather_default_location,
        cfg.weather_units,
        default_lat=cfg.weather_default_lat,
        default_lon=cfg.weather_default_lon,
        default_name=cfg.weather_default_display_name,
        setup_url=f"{cfg.hostname}/assistant/weather/",
    )
    _arelease(stack, "weather", weather.aclose)
    transit_active = transit.active_transit(os.environ)
    _arelease(stack, "transit", transit_active.aclose)
    logger.info(
        "transit: packs=%s tools=%d",
        ",".join(transit.enabled_pack_ids(os.environ)) or "(none)",
        len(transit_active.tools),
    )
    google_routes = build_google_routes_client(os.environ)
    logger.info(
        "google_routes: %s",
        "enabled" if google_routes is not None else "disabled",
    )
    ha = build_ha_client(cfg)
    if ha is not None:
        _arelease(stack, "ha", ha.aclose)
        logger.info("home_assistant: enabled url=%s agent_id=%s",
                    ha.url, ha.agent_id or "(default)")
    else:
        logger.info(
            "home_assistant: disabled (set JASPER_HA_URL + JASPER_HA_TOKEN, "
            "or visit http://%s/assistant/ha/ to configure)",
            cfg.hostname,
        )
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
    return _Integrations(
        weather=weather,
        transit_tools=transit_active.tools,
        transit_configured=transit_active.configured,
        google_routes=google_routes,
        google_clients=google_clients,
        ha=ha,
    )


def _build_volume_coordinator(
    stack: contextlib.AsyncExitStack,
    cfg: Config,
    *,
    camilla: CamillaController,
    renderer: RendererClient,
) -> tuple[VolumeCoordinator, Router | None]:
    """The fader coordinator, plus the Spotify router it shares with the tools.

    One router for both, so the coordinator's outbound Web API volume and the
    transport / spotify packs share one OAuth refresh cycle per account.
    """
    from ..assistant_volume import volume_context_publisher_for_runtime

    persistence = VolumePersistence(cfg.volume_state_path)
    spotify_router = _build_router(cfg)
    coordinator = VolumeCoordinator(
        camilla=camilla,
        persistence=persistence,
        backend=renderer,
        spotify_router=spotify_router,
        spotify_device_name=cfg.spotify_device_name,
        volume_context_publisher=volume_context_publisher_for_runtime(os.environ),
    )
    _arelease(stack, "volume_coordinator", coordinator.aclose)
    return coordinator, spotify_router


async def _start_volume(
    stack: contextlib.AsyncExitStack,
    cfg: Config,
    coordinator: VolumeCoordinator,
) -> None:
    """Restore the listening level, then watch for out-of-band changes."""
    try:
        target_level, restore_reason = await coordinator.initialize(
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
    observer = VolumeObserver(
        coordinator, librespot_state_path=cfg.librespot_state_path,
    )
    await observer.start()
    _arelease(stack, "volume_observer", observer.stop)


def _open_wake_event_store(
    stack: contextlib.AsyncExitStack, cfg: Config,
) -> WakeEventStore | None:
    """Wake-event telemetry, or None when the DB will not open.

    Opened synchronously so a first-ever boot cannot race `begin_event`
    against `CREATE TABLE`. A failure disables telemetry alone: only
    `flag_recent_issue` is withheld from the model.
    """
    try:
        store = WakeEventStore(
            cfg.wake_events_dir,
            max_audio_bytes=cfg.wake_events_max_audio_bytes,
        )
        store.open()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "wake_events: failed to open store at %s: %s "
            "(continuing with telemetry disabled)",
            cfg.wake_events_dir, e,
        )
        return None
    _arelease(stack, "wake_events", store.aclose)
    return store


def _publish_tool_catalog(registry: ToolRegistry) -> None:
    """Apply the user's prompt overrides, then write the /run catalog.

    Runs before any provider serializes the registry. The catalog the
    /assistant/tools/ wizard reads includes EVERY tool (needs_setup ones via
    sentinel deps), not just the live ones.
    """
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


async def _prerender_timer(cues: AudioCueManager, timer: Timer) -> None:
    """Bake a timer's fire announcement at set_timer time, so firing it costs
    no 1-8 s gap between the duck and the audio."""
    await cues.prerender_text(announcement_text(timer))


def _request_shutdown(stop_event: asyncio.Event) -> None:
    logger.info("shutdown requested")
    stop_event.set()


def _install_shutdown_signals(stop_event: asyncio.Event) -> None:
    """Route SIGINT/SIGTERM to the stop event.

    Deliberately never unregistered: a second SIGTERM arriving during the
    unwind must still land here rather than terminate the process mid-cue
    (ADR-0239). `asyncio.run()` closes the loop, and these handlers with it.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, partial(_request_shutdown, stop_event))


def _open_provider_connection(
    cfg: Config,
    *,
    speech_policy: EffectiveSpeechInputPolicy,
    usage_store: UsageStore,
    pricing: Pricing,
) -> LiveConnection:
    """The one live connection, metered for the spend cap.

    Opened once and held for the daemon's lifetime: wake events acquire turns
    against it rather than opening new WebSockets. Its release is registered
    later, at the escalation-callback site (see `_wire_wake_loop`).
    """
    connection = _make_connection(cfg, speech_policy=speech_policy)
    record_backend = getattr(connection, "set_background_usage_recorder", None)
    if record_backend is not None:
        record_backend(usage_store.record_background_usage)
    # Time-billed providers (Grok: flat $/hour) price their per-turn token
    # rows to $0. Wire the meter before start() so the connection records the
    # active turn intervals spend queries fold in. Token-billed providers
    # have flat_per_hour_usd == 0 and get none.
    _wire_billable_activity_meter(
        connection=connection,
        usage_store=usage_store,
        provider=cfg.voice_provider,
        flat_per_hour_usd=pricing.flat_per_hour_usd,
    )
    return connection


def _connect_live_session(
    cfg: Config,
    connection: LiveConnection,
    registry: ToolRegistry,
    integrations: _Integrations,
) -> Callable[[], Awaitable[None]]:
    """The provider dial, with the system instruction rendered per open.

    The prompt is a callable, not a rendered string, so the time injection
    inside `_build_system_instruction` stays accurate across context resets
    and reconnects. The location and the linked Google accounts are
    snapshotted instead: changing either needs a jasper-voice restart, which
    the wizards trigger.
    """
    google = integrations.google_clients
    google_account_names = google.list_account_names() if google else []
    google_default_account = (
        google.default_account_name() or ""
    ) if google else ""
    return partial(
        connection.start,
        registry,
        lambda: _build_system_instruction(
            cfg.weather_prompt_location,
            google_accounts=google_account_names,
            default_google_account=google_default_account,
            transit_configured=integrations.transit_configured,
            travel_routes_configured=integrations.google_routes is not None,
            ha_configured=integrations.ha is not None,
            hostname=cfg.hostname,
            provider=cfg.voice_provider,
        ),
    )


async def _open_wake_legs(
    stack: contextlib.AsyncExitStack,
    cfg: Config,
    planned_wake_legs: list,
) -> list[LegRuntime]:
    """Open one mic + detector per planned wake leg.

    `make_mic_capture` routes a `udp:PORT` device (the AEC bridge's UDP
    transport) to UdpMicCapture and anything else (`Array` chip-direct, a
    `hw:` USB mic) to the PortAudio MicCapture.

    Resilience asymmetry: the primary "on" (AEC3) leg carries session audio
    plus the Tier-1 heartbeat, so a mic-open failure there is fatal-but-CLEAN
    — re-raised as `InputDeviceUnavailable` so `main()` parks the unit rather
    than crash-loop toward StartLimitAction=reboot. It is the backstop for
    what the AEC reconciler's marker gate cannot pre-empt (custom mic,
    present-but-unopenable, first boot); recovery is udev →
    jasper-aec-reconcile → restart_voice. Optional "off"/"dtln" legs are
    best-effort: the failure is logged and that leg skipped so the speaker
    keeps waking on the healthy ones.
    """
    legs: list[LegRuntime] = []
    for spec, device in planned_wake_legs:
        try:
            leg_mic = await _aenter(
                stack,
                f"wake_mic.{spec.token}",
                make_mic_capture(
                    device,
                    capture_rate=cfg.mic_capture_rate,
                    capture_channels=cfg.mic_capture_channels,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            if spec.token == "on":
                raise InputDeviceUnavailable(str(device), exc) from exc
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
        # openWakeWord's Model carries per-instance prediction state, so each
        # leg gets its own detector — same model file + threshold, only the
        # input stream differs. The "off" leg also gets a session shadow VAD
        # (telemetry only; see _shadow_vad_score_raw).
        legs.append(LegRuntime(
            spec,
            leg_mic,
            WakeWordDetector(
                cfg.wake_model, threshold=cfg.wake_threshold,
            ),
            deque(maxlen=CAPTURE_RING_FRAMES),
            shadow_vad=SpeechVAD() if spec.token == "off" else None,
        ))
    return legs


async def _open_manual_mics(
    stack: contextlib.AsyncExitStack, cfg: Config,
) -> list[ManualMicRuntime]:
    """Open every declared push-to-talk source, skipping the ones that fail.

    Best-effort by design — `_require_usable_input` refuses a daemon this
    loop has left with no input at all.
    """
    manual_mics: list[ManualMicRuntime] = []
    for source_id, device in cfg.manual_mic_sources.items():
        try:
            manual_mic = await _aenter(
                stack,
                f"manual_mic.{source_id}",
                make_mic_capture(
                    device,
                    capture_rate=cfg.mic_capture_rate,
                    capture_channels=cfg.mic_capture_channels,
                ),
            )
        except (
            InputDeviceUnavailable, OSError, RuntimeError,
            TimeoutError, TypeError, ValueError,
        ) as exc:
            log_event(
                logger,
                "manual_mic.source_skipped",
                source=source_id,
                device=device,
                reason="mic_open_failed",
                err=str(exc),
                level=logging.WARNING,
            )
            continue
        manual_mics.append(ManualMicRuntime(
            source_id,
            manual_mic,
            device,
        ))
    return manual_mics


async def _open_assistant_output(
    stack: contextlib.AsyncExitStack,
    cfg: Config,
    *,
    camilla: CamillaController,
    cues_manager: AudioCueManager,
    startup_tasks: set[asyncio.Task],
) -> tuple[TtsPlayout, FanInDucker, ContentActivityTracker]:
    """Everything assistant audio leaves through."""
    tts = await _aenter(stack, "tts", TtsPlayout(
        socket_path=cfg.tts_outputd_socket,
        gain_db=0.0,
        drain_tail_sec=cfg.tts_drain_tail_sec,
        provider=cfg.voice_provider,
        model=cfg.active_voice_model,
        voice=_active_voice(cfg),
        profile_path=cfg.assistant_loudness_profile_path,
    ))
    content_activity = ContentActivityTracker(camilla)
    # Registered BEFORE start(): stop() is a no-op on a tracker with no
    # task yet, so a start() that raises still gets torn down.
    _arelease(stack, "content_activity", content_activity.stop)
    await content_activity.start()

    # After the playout: the duck verb rides that connection.
    ducker = FanInDucker(tts)
    cues_manager.attach_tts(tts)
    _schedule_assistant_loudness_seed(cfg, startup_tasks)
    # Registered after the playout: the loudness seed in this set writes
    # through it, so the set must be cancelled before it closes.
    _arelease(stack, "startup_tasks", cancel_tracked_tasks, startup_tasks)
    return tts, ducker, content_activity


def _wire_wake_loop(
    stack: contextlib.AsyncExitStack,
    registry: ToolRegistry,
    connection: LiveConnection,
    wake_loop: WakeLoop,
) -> None:
    """Hand the loop to the registry and the connection, releases included."""
    _release(stack, "wake_loop", wake_loop.close_conversation_store)
    register_conversation_tools(registry, wake_loop.request_conversation_end)
    registry.set_dispatch_observer(
        wake_loop.bind_tool_dispatch,
    )
    _release(
        stack, "dispatch_observer", registry.set_dispatch_observer, None,
    )
    connection.set_failure_escalation_cb(
        wake_loop.play_supervisor_cue,
    )
    # Registered here rather than at construction: the escalation cue
    # above speaks through the wake loop's TtsPlayout, so the connection
    # must stop before that playout and the mics unwind.
    _arelease(stack, "connection", connection.stop)


async def _serve_until_stopped(
    stack: contextlib.AsyncExitStack,
    cfg: Config,
    *,
    wake_loop: WakeLoop,
    timer_scheduler: TimerScheduler,
    connect_live_session: Callable[[], Awaitable[None]],
) -> None:
    """Start the schedulers and the control socket, then serve wake."""
    # Wire timer announcements through the wake loop's session-aware playback
    # (duck + speak_text + restore, deferred up to 5 s if a voice turn is in
    # flight). set_on_fire BEFORE start(): start() restores persisted timers
    # and one whose fire_at is < 1 s away could fire mid-restore.
    timer_scheduler.set_on_fire(wake_loop.announce_timer)
    await timer_scheduler.start()
    # Registered after the TtsPlayout so the unwind cancels in-flight
    # announcements before the playout they speak through closes.
    _arelease(stack, "timer_scheduler", timer_scheduler.stop)
    control_socket = await control_socket_mod.serve(
        wake_loop, cfg.voice_control_socket,
    )
    # Registered last, so it unwinds first: the socket dispatches into the
    # wake loop and must not take a command after that teardown began.
    _arelease(
        stack, "control_socket", control_socket_mod.close, control_socket,
    )
    # Before the first mic frame: a restart mid-sweep must come up with
    # wake already off, not listen until the coordinator's next renewal.
    await wake_loop.measurement_hold.adopt_live_window()
    await _serve_while_connecting(
        connect_live_session, wake_loop.run,
    )
    # Still inside the exit stack, so the cue manager and its TtsPlayout are
    # open and the fan-in socket is live. Only on the clean stop: a crash is
    # not a park.
    await _announce_mic_loss_at_shutdown(wake_loop)


async def run() -> None:
    cfg = Config.from_env()
    configure_logging()
    # DEBUG for the in-RAM ring, INFO for the journal, plus the /system
    # Debug card toggle. See jasper/flight_recorder.py.
    flight_recorder.install("voice")
    speech_policy = _log_speech_input_policy(cfg)
    pricing, pricing_overrides = _resolve_pricing(cfg)
    conversation_store = _open_conversation_store()

    async with contextlib.AsyncExitStack() as stack:
        usage_store, spend_cap = await _open_usage_store(
            stack, cfg, pricing, pricing_overrides,
        )
        # No release registered: the controller caches its websocket for the
        # process lifetime by design, and close() can spend
        # CAMILLA_ATTEMPT_BUDGET_S on a wedged socket inside the 14 s stop
        # budget that already carries the mic-loss cue (ADR-0239).
        camilla = CamillaController(cfg.camilla_host, cfg.camilla_port)
        renderer = RendererClient(
            librespot_state_path=cfg.librespot_state_path,
        )
        integrations = await _open_integrations(stack, cfg)
        volume_coordinator, spotify_router = _build_volume_coordinator(
            stack, cfg, camilla=camilla, renderer=renderer,
        )
        # Every duck holder in this process releases against the coordinator's
        # canonical target so their interleavings cannot strand the fader at a
        # value one of them had ducked.
        set_canonical_target_db_provider(
            volume_coordinator.get_camilla_target_db,
        )
        # This daemon INJECTS its owner, so it needs no registration to work.
        # It registers the SAME instance anyway: leaving `volume_owner()`
        # answering None in a process that has one is how a later caller ends
        # up minting the second.
        install_volume_owner(volume_coordinator.volume_owner)
        await _start_volume(stack, cfg, volume_coordinator)

        # Constructed here because `_build_registry` below needs it for the
        # timer tools; started once the wake loop it announces through exists.
        timer_scheduler = TimerScheduler(db_path=cfg.timer_db_path)
        # Built before the playout so timer tools can pre-render at set_timer
        # time; `attach_tts` wires playback once the playout is up.
        cues_manager = _build_cues_manager(cfg, tts=None)
        # Opened BEFORE `_build_registry`: make_diagnostic_tools gates on the
        # store, and `session.update` is sent once at WS handshake — a tool
        # added later is invisible until the next reconnect.
        wake_event_store = _open_wake_event_store(stack, cfg)
        registry = _build_registry(
            cfg, renderer, integrations.weather, integrations.transit_tools,
            volume_coordinator=volume_coordinator,
            spotify_router=spotify_router,
            timer_scheduler=timer_scheduler,
            google_clients=integrations.google_clients,
            google_routes=integrations.google_routes,
            ha=integrations.ha,
            wake_event_store=wake_event_store,
        )
        _publish_tool_catalog(registry)
        timer_scheduler.set_pre_render(partial(_prerender_timer, cues_manager))

        startup_fire_and_forget: set[asyncio.Task] = set()
        # Scheduled HERE, above the mic open and the SpeechVAD construction
        # below: those raise the 66/78 parks, and `_announce_park_at_boot`
        # can only play a cue that already has a baked WAV. `regenerate`
        # needs the TTS backend only, and writes each WAV atomically, so a
        # park that overtakes it reads a whole file or none.
        _schedule_cue_regen(cues_manager, startup_fire_and_forget)
        stop_event = asyncio.Event()
        _install_shutdown_signals(stop_event)

        # Resolved once: the mics below are opened from this same list, and
        # `wake=` must report what the daemon DOES rather than what the config
        # names (see `_wake_ready_detail`). The install marker is static for
        # the process, so it is read once here too. See ADR-0217.
        planned_wake_legs = configured_wake_legs(
            cfg,
            wake_detection_supported=_wake_detection_supported(),
        )
        logger.info(
            "jasper-voice ready: provider=%s model=%s wake=%s mic=%s %s",
            cfg.voice_provider, cfg.active_voice_model,
            _wake_ready_detail(cfg, planned_wake_legs),
            cfg.mic_device or "(none)", _tts_ready_detail(cfg),
        )
        connection = _open_provider_connection(
            cfg,
            speech_policy=speech_policy,
            usage_store=usage_store,
            pricing=pricing,
        )
        connect_live_session = _connect_live_session(
            cfg, connection, registry, integrations,
        )

        legs = await _open_wake_legs(stack, cfg, planned_wake_legs)
        manual_mics = await _open_manual_mics(stack, cfg)
        _require_usable_input(
            legs, manual_mics, cfg.manual_mic_sources.values(),
        )
        tts, ducker, content_activity = await _open_assistant_output(
            stack, cfg,
            camilla=camilla,
            cues_manager=cues_manager,
            startup_tasks=startup_fire_and_forget,
        )

        # Tier 1 of the resilience ladder: bumped on every mic frame inside
        # WakeLoop.run, paired with `Type=notify` + `WatchdogSec=30s` in
        # jasper-voice.service. See jasper/watchdog.py.
        heartbeat = Heartbeat(stale_threshold_sec=5.0, interval_sec=10.0)
        heartbeat.start()
        _release(stack, "heartbeat", heartbeat.stop)
        wake_loop = WakeLoop(
            cfg, tts, connection, ducker,
            content_activity, usage_store, spend_cap, stop_event,
            volume_coordinator=volume_coordinator,
            legs=legs,
            cues=cues_manager,
            heartbeat=heartbeat,
            wake_event_store=wake_event_store,
            tool_packs=outcomes_to_state(registry.pack_outcomes),
            conversation_store=conversation_store,
            manual_mics=manual_mics,
        )
        _wire_wake_loop(stack, registry, connection, wake_loop)
        await _serve_until_stopped(
            stack, cfg,
            wake_loop=wake_loop,
            timer_scheduler=timer_scheduler,
            connect_live_session=connect_live_session,
        )


def main() -> None:
    try:
        asyncio.run(run())
    except InputDeviceUnavailable as e:
        configure_logging()
        # Intentionally idle, not a crash: jasper-voice.service parks the
        # unit on this code rather than restart-looping into
        # StartLimitAction=reboot. The udev-triggered AEC reconciler restarts
        # us when a mic reappears. See `_open_wake_legs`.
        log_event(
            logger,
            "voice.mic_unavailable",
            device=e.device,
            detail=str(e),
            level=logging.WARNING,
        )
        print(str(e), file=sys.stderr)
        _announce_park_at_boot(NO_ROOM_MIC_CUE_SLUG)
        sys.exit(VOICE_MIC_UNAVAILABLE_EXIT)
    except VoiceProviderNotConfigured as e:
        configure_logging()
        log_event(
            logger,
            "voice.unconfigured",
            reason=str(e),
            level=logging.WARNING,
        )
        print(str(e), file=sys.stderr)
        _announce_park_at_boot(VOICE_NOT_SET_UP_CUE_SLUG)
        sys.exit(VOICE_PROVIDER_NOT_CONFIGURED_EXIT)
    except VoiceConfigError as e:
        configure_logging()
        log_event(
            logger,
            "voice.config_invalid",
            reason=str(e),
            # Nothing restarts a parked unit on its own (jasper-voice.service
            # RestartPreventExitStatus=78) — a structured field, not prose
            # glued onto reason=, so a reader/log-shipper can act on it
            # without parsing English.
            remedy="restart_unit",
            level=logging.ERROR,
        )
        print(str(e), file=sys.stderr)
        _announce_park_at_boot(VOICE_ASSETS_MISSING_CUE_SLUG)
        sys.exit(VOICE_STARTUP_CONFIG_ERROR_EXIT)
    except SpeechVADSetupError as e:
        configure_logging()
        log_event(
            logger,
            "voice.vad_setup_failed",
            reason=str(e),
            level=logging.ERROR,
        )
        print(str(e), file=sys.stderr)
        _announce_park_at_boot(VOICE_ASSETS_MISSING_CUE_SLUG)
        sys.exit(VOICE_STARTUP_CONFIG_ERROR_EXIT)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
