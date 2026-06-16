"""Data-driven tool-pack registry (Pattern 2 — registry, not typed Config).

`_build_registry` in jasper/voice/daemon_main.py used to hardcode one
`for fn in make_X_tools(...): registry.register(fn)` per subsystem, with
inline `if`-gating interleaved. This module lifts that into a flat,
ordered tuple of ToolPack records the daemon WALKS — mirroring
jasper.transit.active_transit's per-provider guard so one broken pack
contributes no tools instead of crashing the daemon.

The order of TOOL_PACKS is load-bearing: models over-rely on tool
ordering, so it MUST match the legacy _build_registry registration
order byte-for-byte. test_tool_packs_registry.py pins that invariant.

This is NOT a DI container. `deps` is exactly the bundle
_build_registry already received, frozen into one typed object. Ordinary
tools own no connection pool, so there is deliberately NO managed-result
/ aclose lifecycle here (that lives in jasper.transit.ActiveTransit for
the one subsystem that needs it).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..log_event import log_event
from .audio import make_audio_tools
from .calendar import make_calendar_tools
from .diagnostic import make_diagnostic_tools
from .gmail import make_gmail_tools
from .home_assistant import make_home_assistant_tools
from .spotify import make_spotify_tools
from .time import make_time_tools
from .timer import make_timer_tools
from .transport import make_transport_tools
from .weather import make_weather_tools

if TYPE_CHECKING:
    from ..google_creds import GoogleClients
    from ..home_assistant import HAClient
    from ..renderer import RendererClient
    from ..spotify_router import Router
    from ..timers import TimerScheduler
    from ..volume_coordinator import VolumeCoordinator
    from ..wake_events import WakeEventStore
    from ..weather import WeatherClient
    from . import ToolRegistry, UntrustedContentMonitor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDeps:
    """Everything a tool pack's build/gate needs — the exact args
    _build_registry already received, as one typed bundle.

    Fields carry the real collaborator types as forward refs (resolved
    only under `from __future__ import annotations`, so there is no
    runtime import cost and no import cycle). Typing them means the
    daemon's construction of this bundle is type-checked, guarding
    against a field-swap (e.g. weather <-> renderer). Tests pass
    None/sentinel deps: each factory captures its dep lazily in a
    closure (no dep is touched at build time), so a None/stub builds the
    same schema a live dep would. Only the two gate predicates below
    ever read a dep here."""
    volume_coordinator: VolumeCoordinator
    renderer: RendererClient
    router: Router | None  # Spotify Router, already resolved by the daemon
    weather: WeatherClient
    spotify_device_name: str
    spotify_setup_url: str
    # Pre-built by transit.active_transit in run() (it owns the aclose
    # lifecycle); here just the flat list of decorated tool callables.
    transit_tools: Iterable[Callable[..., Any]]
    ha: HAClient | None
    timer_scheduler: TimerScheduler | None
    google_clients: GoogleClients | None
    wake_event_store: WakeEventStore | None
    # Shared untrusted-content taint monitor: the gmail/calendar packs stamp
    # it (they return third-party text); the home_assistant pack reads it to
    # gate consequential actions. Optional/last so existing ToolDeps(...)
    # construction (tests) is unaffected; None is fail-safe (gmail no-ops its
    # mark, home_assistant always confirms). The daemon passes a real one.
    untrusted_monitor: UntrustedContentMonitor | None = None


@dataclass(frozen=True)
class CatalogPack:
    """Optional user-facing grouping for the /tools/ catalog.

    This is deliberately separate from ToolPack itself: ToolPack is the
    internal registration/fault-isolation unit, while a CatalogPack is a
    display affordance. Multiple internal packs may share one catalog pack
    (calendar + gmail -> Google), and some internal packs may expose their
    tools as standalone rows by leaving this unset.
    """
    id: str
    title: str
    summary: str
    setup_url: str | None = None


@dataclass(frozen=True)
class ToolPack:
    """One subsystem's tools. `build(deps)` returns the decorated
    callables to register (in order); `gate(deps)` lifts the inline
    `if` that used to wrap the call in _build_registry. Default gate is
    always-on — the common case where the factory self-gates on a None
    dep (home_assistant, diagnostic, transit's per-provider build)."""
    name: str
    build: Callable[[ToolDeps], Iterable[Callable[..., Any]]]
    gate: Callable[[ToolDeps], bool] = lambda _d: True
    category: str = "Utilities"
    catalog_pack: CatalogPack | None = None


@dataclass(frozen=True)
class PackOutcome:
    """The observability record for one pack's registration — what makes
    a silently-missing tool family visible WITHOUT grepping the journal.

    `status` is one of:
      - "registered": the pack's gate passed and `build` returned without
        raising. `tool_count` is how many tools it contributed (0 is
        legitimate — a factory that self-gates on a None dep, e.g.
        home_assistant unconfigured, builds successfully but empty).
      - "skipped": the pack's `gate` predicate returned False (timer with
        no scheduler, calendar/gmail with no linked account). Expected,
        not a fault.
      - "failed": `build` RAISED (ImportError in a tool module, a factory
        that throws). The tool family is silently missing from voice;
        this is the alarm condition `check_tool_packs` fails on. `error`
        carries the exception repr.

    Surfaced via jasper-voice STATUS -> /state.voice.tool_packs and
    cross-checked by jasper-doctor's check_tool_packs. Mirrors and
    slightly improves on transit.active_transit, which today logs a
    failed provider only to the journal."""
    name: str
    status: str  # "registered" | "skipped" | "failed"
    tool_count: int = 0
    error: str | None = None


def outcomes_to_state(outcomes: Iterable[PackOutcome]) -> list[dict[str, Any]]:
    """JSON-serializable view of pack outcomes for /state.voice.tool_packs.

    The single home for the wire shape, so the daemon emitter
    (WakeLoop.session_status) and the doctor consumer (_assess_tool_packs)
    can't drift."""
    return [
        {
            "name": o.name,
            "status": o.status,
            "tool_count": o.tool_count,
            "error": o.error,
        }
        for o in outcomes
    ]


def _google_ready(d: ToolDeps) -> bool:
    # Stricter than make_calendar_tools' own `clients is None` self-gate:
    # the daemon also required ≥1 linked account so the model never sees a
    # tool whose every call fails with "no accounts linked". Lifted
    # verbatim from _build_registry.
    return d.google_clients is not None and bool(d.google_clients.list_account_names())


PLAYBACK_PACK = CatalogPack(
    "playback",
    "Playback",
    "General volume and transport controls for the active source.",
)
SPOTIFY_PACK = CatalogPack(
    "spotify",
    "Spotify",
    "Search, play, and queue music through configured Spotify accounts.",
    setup_url="/spotify/",
)
NYC_TRANSIT_PACK = CatalogPack(
    "nyc-transit",
    "NYC Transit",
    "Subway, bus, and Citi Bike arrivals from the configured NYC stops.",
    setup_url="/transit/",
)
HOME_ASSISTANT_PACK = CatalogPack(
    "home-assistant",
    "Home Assistant",
    "Relay household device, scene, script, and state requests.",
    setup_url="/ha/",
)
TIMERS_PACK = CatalogPack(
    "timers",
    "Timers",
    "Set, list, update, and cancel household timers.",
)
GOOGLE_PACK = CatalogPack(
    "google",
    "Google",
    "Read calendar and Gmail data from linked Google accounts.",
    setup_url="/google/",
)


# Order is load-bearing — see module docstring. Mirrors the legacy
# _build_registry sequence exactly.
TOOL_PACKS: tuple[ToolPack, ...] = (
    ToolPack(
        "audio", lambda d: make_audio_tools(d.volume_coordinator),
        category="Music", catalog_pack=PLAYBACK_PACK,
    ),
    ToolPack(
        "transport", lambda d: make_transport_tools(d.renderer, d.router),
        category="Music", catalog_pack=PLAYBACK_PACK,
    ),
    ToolPack(
        "spotify",
        lambda d: make_spotify_tools(
            d.router,
            d.renderer,
            d.spotify_device_name,
            d.spotify_setup_url,
        ),
        category="Music",
        catalog_pack=SPOTIFY_PACK,
    ),
    ToolPack(
        "weather",
        lambda d: make_weather_tools(d.weather),
        category="Utilities",
    ),
    # Transit is pre-built by transit.active_transit in run() (it owns an
    # aclose lifecycle the daemon needs); here we only register the flat
    # list. Each provider already self-gated, so an empty list is correct.
    ToolPack(
        "transit", lambda d: d.transit_tools,
        category="Transit", catalog_pack=NYC_TRANSIT_PACK,
    ),
    # home_assistant + diagnostic self-gate inside the factory (return []
    # on a None dep), so no pack gate is needed — default always-on
    # reproduces today's behavior exactly. home_assistant reads the shared
    # taint monitor to gate consequential actions.
    ToolPack(
        "home_assistant",
        lambda d: make_home_assistant_tools(d.ha, monitor=d.untrusted_monitor),
        category="Smart Home",
        catalog_pack=HOME_ASSISTANT_PACK,
    ),
    ToolPack(
        "time",
        lambda _d: make_time_tools(),
        category="Utilities",
    ),
    # timer's factory does NOT self-gate on None, so the gate is load-bearing.
    ToolPack(
        "timer",
        lambda d: make_timer_tools(d.timer_scheduler),
        gate=lambda d: d.timer_scheduler is not None,
        category="Productivity",
        catalog_pack=TIMERS_PACK,
    ),
    # calendar + gmail stamp the shared taint monitor when they return
    # third-party text (arming home_assistant's confirmation window).
    ToolPack(
        "calendar",
        lambda d: make_calendar_tools(d.google_clients, monitor=d.untrusted_monitor),
        gate=_google_ready,
        category="Productivity",
        catalog_pack=GOOGLE_PACK,
    ),
    ToolPack(
        "gmail",
        lambda d: make_gmail_tools(d.google_clients, monitor=d.untrusted_monitor),
        gate=_google_ready,
        category="Productivity",
        catalog_pack=GOOGLE_PACK,
    ),
    ToolPack(
        "diagnostic",
        lambda d: make_diagnostic_tools(d.wake_event_store),
        category="System",
    ),
)


def register_packs(
    registry: "ToolRegistry",
    deps: ToolDeps,
    *,
    disabled: "frozenset[str] | None" = None,
) -> list[PackOutcome]:
    """Walk TOOL_PACKS in order; gate, build, and register each pack's
    tools onto `registry`. Each pack's build runs behind try/except for
    fault isolation — a broken pack (ImportError in a tool module, a
    factory that raises) contributes no tools and is logged, never
    crashing the daemon. Mirrors transit.active_transit's per-provider
    guard.

    `disabled` is the wizard-owned set of tool NAMES the household turned
    off (jasper.tool_state). A disabled tool is not registered, so the
    model never sees it — the user's explicit choice, NOT a failure (no
    cue). None (default) reads the SSOT file fail-safe; pass an explicit
    set in tests.

    Returns one PackOutcome per pack (in TOOL_PACKS order) so the
    registration result is observable beyond the journal — the daemon
    stashes it on the registry and surfaces it via STATUS ->
    /state.voice.tool_packs, and jasper-doctor cross-checks it. `tool_count`
    is the number of tools the pack actually CONTRIBUTED to the registry
    (after user-disabled removals), so sum(tool_count) == len(registry.tools).
    The return is additive: existing callers that ignore it are unaffected."""
    if disabled is None:
        from ..tool_state import read_disabled_tools
        disabled = read_disabled_tools()
    outcomes: list[PackOutcome] = []
    for pack in TOOL_PACKS:
        if not pack.gate(deps):
            outcomes.append(PackOutcome(pack.name, "skipped"))
            continue
        try:
            # Materialize inside the guard so a factory returning a lazy
            # generator that raises mid-iteration is still fault-isolated.
            fns = list(pack.build(deps))
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "event=tool_pack.build_failed pack=%s", pack.name,
            )
            outcomes.append(PackOutcome(pack.name, "failed", error=repr(e)))
            continue
        registered = 0
        for fn in fns:
            t = registry.register(fn)
            registry.tool_packs[t.name] = pack.name
            if t.name in disabled:
                # Registered, then removed by user choice — keeps the
                # filter at the single registration point and works
                # regardless of declared @tool name vs fn.__name__.
                del registry.tools[t.name]
                registry.tool_packs.pop(t.name, None)
                log_event(logger, "tool.disabled", name=t.name, pack=pack.name)
                continue
            registered += 1
        outcomes.append(
            PackOutcome(pack.name, "registered", tool_count=registered),
        )
    return outcomes
