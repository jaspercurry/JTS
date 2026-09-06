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
from functools import partial
from typing import Any, TypeVar

from jasper.log_event import log_event

from .. import flight_recorder, transit
from ..audio_io import (
    InputDeviceUnavailable,
    TtsPlayout,
    make_mic_capture,
)
from ..assistant_loudness import active_voice_identity, ensure_seed_profile
from ..camilla import (
    CamillaController,
    Ducker,
    set_canonical_target_db_provider,
)
from ..config import Config, VoiceProviderNotConfigured
from ..conversation_history import (
    ConversationStore,
    read_settings as read_conversation_settings,
)
from ..cues import (
    AudioCueManager,
    build_cue_tts_backend,
    build_env_cue_manager,
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
from ..research import ResearchScheduler, active_research_provider
from ..spotify_router import Router, build_router
from ..timers import Timer, TimerScheduler, announcement_text
from ..tts_routing import FANIN_TTS_SOCKET, VOICE_TTS_SOCKET_ENV
from ..tools import ToolRegistry, UntrustedContentMonitor
from ..tools.packs import ToolDeps, outcomes_to_state, register_packs
from ..usage import (
    BillableActivityMeter,
    SpendCap,
    UsageStore,
    household_usage_reader,
    load_pricing_overrides,
    pricing_for_model,
)
from ..vad import SpeechVAD, SpeechVADSetupError
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
    NO_ROOM_MIC_CUE_SLUG,
    VOICE_MIC_UNAVAILABLE_EXIT,
    VOICE_NOT_SET_UP_CUE_SLUG,
    VOICE_PROVIDER_NOT_CONFIGURED_EXIT,
    VOICE_STARTUP_CONFIG_ERROR_EXIT,
    ContentActivityTracker,
    FanInDucker,
    WakeLoop,
    _LegRuntime,
    _ManualMicRuntime,
    _cancel_tracked_tasks,
    _configured_wake_legs,
    _track_task,
)
from ..logging_setup import configure_logging

logger = logging.getLogger("jasper.voice_daemon")

_T = TypeVar("_T")


def _active_model(cfg: Config) -> str:
    """Return the model name for the currently selected provider — used
    by startup-readiness logging and the silent-failure heuristic in
    `_end_turn` so journalctl shows the actual model in flight. Resolution
    lives on `Config.active_voice_model`; the `<unknown:…>` sentinel keeps
    log lines legible for an unset provider."""
    return cfg.active_voice_model or f"<unknown:{cfg.voice_provider}>"


def _wire_billable_activity_meter(
    *,
    connection: LiveConnection,
    usage_store: UsageStore,
    provider: str,
    flat_per_hour_usd: float,
) -> bool:
    """Wire flat-rate realtime billing into a provider connection.

    Token-billed providers skip this entirely. For flat-rate realtime
    providers, the provider adapter owns what "billable activity" means
    by exposing ``set_billable_activity_meter`` and marking the meter at
    the right lifecycle points. A missing hook is observable because it
    means the spend cap would otherwise under-count a priced provider.
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


def _warn_if_research_model_unpriced(
    research_model: str,
    *,
    pricing_overrides,
) -> bool:
    """Warn (and report) when the research model has no rate.

    Mirrors the voice-model unpriced guard in ``run`` and the
    ``_wire_billable_activity_meter`` shape: if ``JASPER_RESEARCH_OPENAI_MODEL``
    is overridden to a model with no rate, research cost records $0 and the
    daily spend cap silently under-counts. Make it observable. Returns ``True``
    when the warning fired (the model is unpriced), ``False`` otherwise — so the
    decision is unit-testable without standing up the daemon.
    """
    research_pricing = pricing_for_model(research_model, overrides=pricing_overrides)
    if not research_pricing.label.startswith("unpriced:"):
        return False
    log_event(
        logger,
        "pricing.unpriced",
        model=research_model,
        surface="research",
        note=(
            "no rate for the research model; research cost will read "
            "$0 and the daily spend cap cannot bound it until you add a "
            "jasper/data/model_pricing.json row (or a "
            "/var/lib/jasper/pricing.json override)"
        ),
        level=logging.WARNING,
    )
    return True


def _active_voice(cfg: Config) -> str:
    """Return the voice id for the currently selected provider."""
    provider, _model, voice = active_voice_identity(cfg)
    return voice or f"<unknown:{provider}>"


def _require_usable_input(
    legs: list[_LegRuntime],
    manual_mics: list[_ManualMicRuntime],
    declared_manual_devices: Iterable[str],
) -> None:
    """Refuse to run a daemon that can never hear anything.

    No wake leg AND no manual mic means every input this daemon could have
    opened is gone. A planned primary leg that fails to open already raises
    in the leg factory, so this is the backstop for the *other* shape: a
    speaker with no room mic (so no leg was planned at all — issue #2205)
    whose accessory sources then all failed to open. That loop deliberately
    SKIPS a bad source rather than raising, so without this the daemon would
    come up, log "ready", pat its watchdog on the keepalive tick, and be
    permanently deaf — the exact silent failure the house rule forbids.

    Fails the same fatal-but-CLEAN way a primary mic-open failure does:
    `InputDeviceUnavailable` → `main()` exits VOICE_MIC_UNAVAILABLE_EXIT →
    systemd parks the unit instead of crash-looping toward a reboot.
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


# Bound on the boot-park cue: the same cue + drain the floor above budgets,
# plus TtsPlayout's own 1.0 s connect timeout. Past this the daemon is holding
# systemd's start timeout (READY=1 was never sent) for a cue nobody will hear.
PARK_CUE_TIMEOUT_SEC = 12.0


def _announce_park_at_boot(slug: str) -> str:
    """Say out loud why this daemon is parking, then let the caller exit.

    The boot checks that raise 66/78 all run before the daemon's own cue
    manager and TtsPlayout exist, so the largest deaf window on the box is a
    park nobody hears (AGENTS.md non-negotiable 6). This rebuilds the minimum
    to speak: a Config-less manager from the environment — `Config.from_env()`
    is exactly what raised on the 78 path — over a TtsPlayout on the same
    socket `main()`'s Config would have resolved.

    Called from `main()` after `asyncio.run(run())` has returned, so no loop
    is running and this owns its own. Returns the result code it logged:
    ``ok``, ``play_failed``, ``play_error``, ``timeout`` or ``interrupted``.

    Never raises — not even a ``BaseException`` — and never changes the exit
    code. It is called from inside `main()`'s ``except`` handlers, so anything
    escaping here skips the caller's ``sys.exit()`` and the process exits 1,
    which is neither a park nor a success code for systemd.
    """
    # The cap is held out here so the classifier below can ask which bound
    # fired: TtsPlayout's own 1.0 s connect timeout raises TimeoutError too,
    # and `asyncio.TimeoutError is TimeoutError` on 3.11+, so the exception
    # type alone cannot tell the two apart. `asyncio.timeout()` reads the
    # running loop's clock, so it can only be built inside the coroutine.
    cap: asyncio.Timeout | None = None

    async def _play() -> str:
        nonlocal cap
        socket_path = os.environ.get(VOICE_TTS_SOCKET_ENV, FANIN_TTS_SOCKET)
        async with (
            asyncio.timeout(PARK_CUE_TIMEOUT_SEC) as cap,
            TtsPlayout(socket_path=socket_path) as tts,
        ):
            manager = build_env_cue_manager(tts_playout=tts)
            return "ok" if await manager.play(slug) else "play_failed"

    try:
        result = asyncio.run(_play())
    except TimeoutError:
        if cap is not None and cap.expired():
            result = "timeout"
        else:
            logger.exception("park cue play failed")
            result = "play_error"
    except Exception:  # noqa: BLE001
        logger.exception("park cue play failed")
        result = "play_error"
    except BaseException:  # noqa: BLE001
        result = "interrupted"
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

    ``read_install_profile()`` raises ``ValueError`` on an unparseable
    marker token. ``main()`` special-cases only ``InputDeviceUnavailable``,
    ``VoiceProviderNotConfigured`` and ``SpeechVADSetupError`` — anything
    else would traceback out, exit 1, and climb ``Restart=on-failure`` to
    ``StartLimitAction=reboot``. Fail OPEN (today's pre-ADR-0217 behaviour:
    wake detection supported, legs planned as always) rather than reboot a
    speaker over a corrupt marker file. Mirrors
    ``jasper.control.server._control_install_profile``, which fails the
    opposite way because its stakes are a route allowlist, not a daemon
    crash.
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
    """The startup line's ``wake=`` field.

    Keyed on the RESOLVED leg plan, never on ``cfg.wake_model`` alone: on a
    speaker with no room mic the plan is empty and no detector is built, and
    naming the model there would tell an operator wake detection is live on a
    box that will never wake.

    Extracted for the same reason as ``_tts_ready_detail`` — the string is
    the operator's evidence (the #2205 hardware verification greps for it in
    the journal), so it gets a test rather than living unreachable inside a
    ~350-line ``run()``.
    """
    return cfg.wake_model if planned_wake_legs else "disabled(no wake leg)"


def _tts_ready_detail(cfg: Config) -> str:
    """The startup line's ``tts_socket=`` field: where assistant audio
    enters (fan-in solo, outputd when a bonded member overrides it)."""
    return f"tts_socket={cfg.tts_outputd_socket}"


def build_ducker(
    cfg: Config,
    *,
    volume_owner: Any,
    target_db_provider: Callable[[], Awaitable[float]],
) -> Ducker | FanInDucker:
    """Pick the duck transport that matches where TTS enters the mix.

    Production routes TTS/cues into fan-in ahead of CamillaDSP, so the duck
    has to happen in fan-in too; Camilla main_volume would otherwise attenuate
    the assistant along with the renderer program.
    """
    if cfg.duck_transport == "fanin":
        return FanInDucker(cfg.tts_outputd_socket, cfg.duck_db)
    return Ducker(
        volume_owner, cfg.duck_db, target_db_provider=target_db_provider,
    )


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
    tool call triggers a rebuild via Router.refresh_if_empty()."""
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
    research_scheduler: ResearchScheduler | None = None,
    spend_cap: SpendCap | None = None,
    research_delivery_recorder=None,
    google_clients: GoogleClients | None = None,
    google_routes=None,
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
    # in jasper.tools.packs decides what's included. Per-tool gates
    # (timer's `is not None`, calendar/gmail's `list_account_names()`)
    # live in each pack's `gate` predicate; the rest self-gate inside
    # their factory. The walk is fault-isolated per pack — see
    # register_packs.
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
        research_scheduler=research_scheduler,
        google_clients=google_clients,
        wake_event_store=wake_event_store,
        untrusted_monitor=untrusted_monitor,
        spend_cap=spend_cap,
        research_delivery_recorder=research_delivery_recorder,
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
        START [source]      → manual_session_start  (long-press begin)
        END                 → manual_session_end    (long-press release)
        STATUS              → session_status        (diagnostic snapshot)
        CUE_PLAY <slug>     → play a registered audio cue through the
                              daemon's fan-in-backed TtsPlayout. Routed
                              here so a standalone CLI doesn't have to
                              recreate the output path or gain policy.
        MEASURE_PAUSE       → open a room-correction measurement
                              window. Drops mic frames, pauses the
                              outputd content meter, and reports additive
                              `drained` evidence while keeping the compatible
                              `result=ok` whenever cleanup is owned. Refuses
                              (BUSY) if a session is active. Auto-clears after
                              measurement_hold.MEASUREMENT_AUTOCLEAR_SEC
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
    via systemd's RuntimeDirectory=jasper."""
    import json as _json

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("voice control socket: client read timed out")
                return
            line = raw.decode("ascii", errors="replace").strip()
            parts = line.split(maxsplit=1)
            cmd = parts[0].upper() if parts else ""
            arg = parts[1] if len(parts) > 1 else ""
            if cmd == "START":
                result = {
                    "result": await wake_loop.manual_session_start(arg or None),
                }
            elif cmd == "END":
                result = {"result": await wake_loop.manual_session_end()}
            elif cmd == "STATUS":
                result = wake_loop.session_status()
            elif cmd == "CUE_PLAY":
                result = {"result": await wake_loop.play_cue(arg)}
            elif cmd == "MEASURE_PAUSE":
                result = await wake_loop.measurement_hold.pause_response()
            elif cmd == "MEASURE_RESUME":
                result = {"result": await wake_loop.measurement_hold.resume()}
            elif cmd == "MUTE":
                result = {"result": await wake_loop.mute_mic()}
            elif cmd == "UNMUTE":
                result = {"result": await wake_loop.unmute_mic()}
            else:
                result = {"result": "UNKNOWN", "command": cmd}
            writer.write((_json.dumps(result) + "\n").encode("utf-8"))
            await writer.drain()
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


async def _serve_while_connecting(
    connect: Callable[[], Awaitable[None]],
    serve: Callable[[], Awaitable[None]],
) -> None:
    """Serve wake while the first provider connect is still dialling.

    Hearing must not wait on the WAN: mics, cues and ``READY=1`` are up
    before this is reached, so a boot with the link down answers a wake
    with a cue instead of silence. A connect that raises ends the run; a
    connect that returns leaves the daemon serving with the supervisor
    retrying. Whichever finishes first, the other is cancelled on the
    way out.
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
    exception (demoting the original to `__context__`), so one unlucky
    teardown turns the `InputDeviceUnavailable` / `VoiceProviderNotConfigured`
    park raised inside the body into a plain crash: no cue, exit 1, and a
    systemd restart loop instead of a park (NN-6; ADR-0239). Every release
    in `run()` goes through here or `_arelease`. `CancelledError` is a
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


async def run() -> None:
    cfg = Config.from_env()
    configure_logging()
    # Log flight recorder + runtime debug toggle (/system Debug card).
    # install() holds the jasper logger at DEBUG for the in-RAM ring,
    # keeps the journal at INFO, and applies the debug toggle. See
    # jasper/flight_recorder.py.
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
            surface="voice",
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
    # The cap reads HOUSEHOLD spend: this daemon's own voice ledger plus the
    # tuning-surface sibling ledger (jasper-correction-web's paid tuning
    # calls). Passing the live writer store as main_store means spend this
    # daemon just recorded is visible without a read-only reopen; the tuning
    # sibling is summed as a path, picked up lazily even if created later. So
    # voice sessions and hold-to-talk refuse once tuning spend has exhausted
    # the shared cap.
    spend_cap = SpendCap(
        household_usage_reader(cfg.usage_db, main_store=usage_store),
        cfg.daily_spend_cap_usd,
        cfg.daily_spend_cap_safety_multiplier,
    )
    conversation_settings = read_conversation_settings()
    conversation_store: ConversationStore | None = None
    if conversation_settings.capture_enabled:
        conversation_store = ConversationStore(conversation_settings.db_path)

    # One exit stack owns every teardown here: each resource registers its
    # release (through _release / _arelease / _aenter) at the site that
    # creates or starts it, so the unwind is the exact reverse of
    # construction — which is dependency order, since each resource is
    # built from the ones above it.
    async with contextlib.AsyncExitStack() as stack:
        # No release registered for the controller: it caches its websocket
        # for the process lifetime by design, and close() can spend
        # CAMILLA_ATTEMPT_BUDGET_S on a wedged socket inside the 14 s stop
        # budget that already carries the mic-loss cue (ADR-0239).
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
            setup_url=f"{cfg.hostname}/weather",
        )
        _arelease(stack, "weather", weather.aclose)
        # Transit (subway / bus / Citi Bike today; future city packs add more).
        # One call builds every provider in the household's ENABLED city packs
        # (JASPER_TRANSIT_CITIES; unset = all packs, non-breaking) and returns a
        # managed ActiveTransit: the flat tool list, a `configured` flag for the
        # system-prompt nudge, and an `aclose()` that releases any client owning a
        # pool. Each provider self-gates on its own config, so an
        # enabled-but-unconfigured mode produces no tool — `transit_configured`
        # is exactly "at least one transit tool registered", the same gate as
        # before. Adding a city needs no edit here; see
        # jasper.transit.active_transit. os.environ carries
        # JASPER_TRANSIT_CITIES via transit.env, sourced by jasper-voice.service.
        transit_active = transit.active_transit(os.environ)
        _arelease(stack, "transit", transit_active.aclose)
        transit_tools = transit_active.tools
        transit_configured = transit_active.configured
        logger.info(
            "transit: packs=%s tools=%d",
            ",".join(transit.enabled_pack_ids(os.environ)) or "(none)",
            len(transit_tools),
        )
        google_routes = build_google_routes_client(os.environ)
        travel_routes_configured = google_routes is not None
        logger.info(
            "google_routes: %s",
            "enabled" if travel_routes_configured else "disabled",
        )
        # Home Assistant client. None when JASPER_HA_URL or JASPER_HA_TOKEN
        # is unset; the tool factory short-circuits to [] in that case so
        # the model never sees a tool whose every call would fail. The
        # client owns a long-lived httpx.AsyncClient for the daemon's lifetime.
        ha = build_ha_client(cfg)
        if ha is not None:
            _arelease(stack, "ha", ha.aclose)
            logger.info("home_assistant: enabled url=%s agent_id=%s",
                        ha.url, ha.agent_id or "(default)")
        else:
            logger.info(
                "home_assistant: disabled (set JASPER_HA_URL + JASPER_HA_TOKEN, "
                "or visit http://%s/ha to configure)",
                cfg.hostname,
            )
        # Volume coordinator: owns the canonical listening_level (0-100),
        # follows mux's effective source, and dispatches voice/accessory-driven
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
        from ..assistant_volume import volume_context_publisher_for_runtime

        volume_coordinator = VolumeCoordinator(
            camilla=camilla,
            persistence=volume_persistence,
            backend=renderer,
            spotify_router=volume_spotify_router,
            spotify_device_name=cfg.spotify_device_name,
            volume_context_publisher=volume_context_publisher_for_runtime(os.environ),
        )
        _arelease(stack, "volume_coordinator", volume_coordinator.aclose)
        # Every duck holder in this process — Ducker, CueDuck, and the graph-swap
        # bracket — releases against the coordinator's canonical target so their
        # interleavings cannot strand the fader at a value one of them had ducked.
        set_canonical_target_db_provider(volume_coordinator.get_camilla_target_db)
        # This daemon INJECTS its owner (Ducker and CueDuck take it as a
        # constructor argument), so it needs no registration to work. It registers
        # anyway, and registers the SAME instance: leaving `volume_owner()`
        # answering None in a process that has an owner is precisely how a later
        # caller ends up minting the second one.
        install_volume_owner(volume_coordinator.volume_owner)
        # Built after the coordinator so restore follows the active output topology,
        # and so the Camilla duck shares the coordinator's fader owner rather than
        # writing beside it.
        ducker = build_ducker(
            cfg,
            volume_owner=volume_coordinator.volume_owner,
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
        _arelease(stack, "volume_observer", volume_observer.stop)

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
            _arelease(stack, "active_research", active_research.aclose)
            research_scheduler = ResearchScheduler(
                active_research.client,
                db_path=cfg.research_db_path,
                max_runtime_sec=cfg.research_max_runtime_sec,
                concurrency=cfg.research_concurrency,
                max_result_chars=cfg.research_max_result_chars,
                retention=cfg.research_retention,
                usage_store=usage_store,
                usage_provider=active_research.provider_id,
                usage_model=str(getattr(active_research.client, "model", "")),
            )
            # The store opens in __init__; stop() is registered at the start()
            # site below, so the unwind cancels jobs before closing the store.
            _release(stack, "research_store", research_scheduler.close)
            _warn_if_research_model_unpriced(
                str(getattr(active_research.client, "model", "")),
                pricing_overrides=pricing_overrides,
            )
        research_configured = research_scheduler is not None

        # Cue manager — built early so timer tools can pre-render their
        # fire announcements at set_timer time. The TtsPlayout isn't open
        # yet (that lives inside the async with block below); the manager
        # is constructed without it and `attach_tts` wires playback once
        # the playout is up. Pre-render and regen don't need playback.
        cues_manager = _build_cues_manager(cfg, tts=None)

        # Wake-event telemetry store.
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
        # next reconnect.
        wake_event_store: WakeEventStore | None = None
        try:
            wake_event_store = WakeEventStore(
                cfg.wake_events_dir,
                max_audio_bytes=cfg.wake_events_max_audio_bytes,
            )
            wake_event_store.open()
            _release(stack, "wake_events", wake_event_store.close)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "wake_events: failed to open store at %s: %s "
                "(continuing with telemetry disabled)",
                cfg.wake_events_dir, e,
            )
            wake_event_store = None

        research_delivery_recorder_ref = {"fn": None}

        def _record_research_delivery(job, assistant_text, decision) -> None:
            fn = research_delivery_recorder_ref["fn"]
            if fn is not None:
                fn(job, assistant_text, decision)

        registry = _build_registry(
            cfg, renderer, weather, transit_tools,
            volume_coordinator=volume_coordinator,
            spotify_router=volume_spotify_router,
            timer_scheduler=timer_scheduler,
            research_scheduler=research_scheduler,
            spend_cap=spend_cap,
            research_delivery_recorder=_record_research_delivery,
            google_clients=google_clients,
            google_routes=google_routes,
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
        # Deliberately never unregistered: a second SIGTERM arriving during
        # the unwind must still land on _shutdown rather than terminate the
        # process mid-cue (ADR-0239). asyncio.run() closes the loop — and
        # with it these handlers — as soon as run() returns.

        # `wake=` must report what this daemon actually DOES, not what the config
        # happens to name — see `_wake_ready_detail`. Resolved once here because
        # the mics below are opened from this same list.
        #
        # The install marker is static for the process, so it is read once here
        # and passed down rather than re-read per decision. See ADR-0217.
        planned_wake_legs = _configured_wake_legs(
            cfg,
            wake_detection_supported=_wake_detection_supported(),
        )
        logger.info(
            "jasper-voice ready: provider=%s model=%s wake=%s mic=%s %s",
            cfg.voice_provider, _active_model(cfg),
            _wake_ready_detail(cfg, planned_wake_legs),
            cfg.mic_device or "(none)", _tts_ready_detail(cfg),
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
        # Its release is registered further down, at the escalation-callback
        # site: the connection speaks its failure cue through the wake loop,
        # so it has to stop before the playout and mics that cue uses.
        # Time-billed providers (Grok: flat $/hour) price their per-turn token
        # rows to $0. Wire a meter before start(); the connection will record
        # active turn intervals that spend queries fold in. No meter for
        # token-billed providers (flat_per_hour_usd == 0).
        _wire_billable_activity_meter(
            connection=connection,
            usage_store=usage_store,
            provider=cfg.voice_provider,
            flat_per_hour_usd=pricing.flat_per_hour_usd,
        )
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
        connect_live_session = partial(
            connection.start,
            registry,
            lambda: _build_system_instruction(
                cfg.weather_prompt_location,
                google_accounts=google_account_names,
                default_google_account=google_default_account,
                transit_configured=transit_configured,
                travel_routes_configured=travel_routes_configured,
                research_configured=research_configured,
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
        # — it carries session audio + the Tier-1 heartbeat. A mic-open
        # failure there is fatal-but-CLEAN: re-raised as
        # InputDeviceUnavailable so main() exits VOICE_MIC_UNAVAILABLE_EXIT
        # and systemd PARKS the unit (SuccessExitStatus +
        # RestartPreventExitStatus) instead of crash-looping toward
        # StartLimitAction=reboot. The AEC reconciler's marker gate
        # (ConditionPathExists) keeps us from even starting when it knows
        # the mic is absent; this exit is the backstop for the cases the
        # marker can't pre-empt (custom mic, present-but-unopenable, first
        # boot before any reconcile). Plug-in recovery: udev →
        # jasper-aec-reconcile → restart_voice. Optional "off"/"dtln" legs
        # are best-effort: a mic-open failure is logged and that leg is
        # skipped so the speaker keeps waking on the healthy legs.
        legs: list[_LegRuntime] = []
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
        manual_mics: list[_ManualMicRuntime] = []
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
                InputDeviceUnavailable,
                OSError,
                RuntimeError,
                TimeoutError,
                TypeError,
                ValueError,
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
            manual_mics.append(_ManualMicRuntime(
                source_id,
                manual_mic,
                device,
            ))
        _require_usable_input(
            legs, manual_mics, cfg.manual_mic_sources.values(),
        )
        tts = await _aenter(stack, "tts", TtsPlayout(
            socket_path=cfg.tts_outputd_socket,
            # outputd owns the final gain decision; this initial value
            # only matters for chirps that play before the first real
            # gain update lands.
            gain_db=0.0,
            drain_tail_sec=cfg.tts_drain_tail_sec,
            provider=cfg.voice_provider,
            model=_active_model(cfg),
            voice=_active_voice(cfg),
            profile_path=cfg.assistant_loudness_profile_path,
        ))
        content_activity = ContentActivityTracker(camilla)
        # Registered BEFORE start(): stop() is a no-op on a tracker with no
        # task yet, so a start() that raises still gets torn down.
        _arelease(stack, "content_activity", content_activity.stop)
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
        _arelease(
            stack, "startup_tasks", _cancel_tracked_tasks,
            startup_fire_and_forget,
        )

        # Tier 1 of the resilience ladder. Bumped on every mic
        # frame inside WakeLoop.run; pairs with `Type=notify` +
        # `WatchdogSec=30s` in jasper-voice.service. If the
        # async loop wedges or mic capture dies, the heartbeat
        # stops patting and systemd revives us cleanly via
        # `Restart=on-watchdog` before SIGKILL is needed. See
        # jasper/watchdog.py header.
        heartbeat = Heartbeat(stale_threshold_sec=5.0, interval_sec=10.0)
        heartbeat.start()
        _release(stack, "heartbeat", heartbeat.stop)
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
            conversation_store=conversation_store,
            manual_mics=manual_mics,
        )
        _release(stack, "wake_loop", wake_loop.close_conversation_store)
        # Host-compose the wake funnel at the single cross-provider
        # dispatch seam. Tool implementations and provider adapters stay
        # unaware of WakeLoop / SQLite; the narrow observer records only
        # registered call start/completion while a wake event is active.
        registry.set_dispatch_observer(
            wake_loop.record_tool_dispatch_stage,
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
        research_delivery_recorder_ref["fn"] = (
            wake_loop.record_research_delivery
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
        # Registered after the TtsPlayout so the unwind cancels in-flight
        # announcements before the playout they speak through closes.
        _arelease(stack, "timer_scheduler", timer_scheduler.stop)
        if research_scheduler is not None:
            wake_loop.set_research_scheduler(
                research_scheduler,
                provider_id=active_research.provider_id,
                model=str(getattr(active_research.client, "model", "")),
            )
            research_scheduler.set_on_done(wake_loop.announce_research_ready)
            await research_scheduler.start()
            _arelease(stack, "research_scheduler", research_scheduler.stop)
        control_socket = await _start_control_socket(
            wake_loop, cfg.voice_control_socket,
        )

        # Registered last, so it unwinds first: the socket dispatches into
        # the wake loop and must not take a command after that teardown began.
        await _aenter(stack, "control_socket", control_socket)
        await _serve_while_connecting(
            connect_live_session, wake_loop.run,
        )
        # Still inside the exit stack, so the cue manager and its
        # TtsPlayout are open and the fan-in socket is live. Only on
        # the clean stop: a crash is not a park.
        await _announce_mic_loss_at_shutdown(wake_loop)


def main() -> None:
    try:
        asyncio.run(run())
    except InputDeviceUnavailable as e:
        configure_logging()
        # Intentionally idle, not a crash: the primary mic could not be
        # opened. Exit VOICE_MIC_UNAVAILABLE_EXIT so jasper-voice.service
        # parks the unit cleanly (SuccessExitStatus + RestartPreventExitStatus)
        # rather than restart-looping into StartLimitAction=reboot. The AEC
        # reconciler (udev-triggered) restarts us when a mic reappears.
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
    except SpeechVADSetupError as e:
        configure_logging()
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
