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
    from . import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDeps:
    """Everything a tool pack's build/gate needs — the exact args
    _build_registry already received, as one typed bundle.

    Loosely typed (object/Any) on purpose: the equality test passes
    None for every field, and each factory captures its dep lazily in a
    closure (no dep is touched at build time), so None builds the same
    schema a live dep would. The daemon constructs this with the real
    collaborators; nothing here reaches into a dep itself except the two
    gate predicates below."""
    volume_coordinator: Any
    renderer: Any
    router: Any            # Spotify Router (already resolved by the daemon)
    weather: Any
    spotify_device_name: str
    spotify_setup_url: str
    transit_tools: Iterable  # pre-built by transit.active_transit in run()
    ha: Any
    timer_scheduler: Any
    google_clients: Any
    wake_event_store: Any


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


def _google_ready(d: ToolDeps) -> bool:
    # Stricter than make_calendar_tools' own `clients is None` self-gate:
    # the daemon also required ≥1 linked account so the model never sees a
    # tool whose every call fails with "no accounts linked". Lifted
    # verbatim from _build_registry.
    return d.google_clients is not None and bool(d.google_clients.list_account_names())


# Order is load-bearing — see module docstring. Mirrors the legacy
# _build_registry sequence exactly.
TOOL_PACKS: tuple[ToolPack, ...] = (
    ToolPack("audio", lambda d: make_audio_tools(d.volume_coordinator)),
    ToolPack("transport", lambda d: make_transport_tools(d.renderer, d.router)),
    ToolPack("spotify", lambda d: make_spotify_tools(
        d.router, d.renderer, d.spotify_device_name, d.spotify_setup_url)),
    ToolPack("weather", lambda d: make_weather_tools(d.weather)),
    # Transit is pre-built by transit.active_transit in run() (it owns an
    # aclose lifecycle the daemon needs); here we only register the flat
    # list. Each provider already self-gated, so an empty list is correct.
    ToolPack("transit", lambda d: d.transit_tools),
    # home_assistant + diagnostic self-gate inside the factory (return []
    # on a None dep), so no pack gate is needed — default always-on
    # reproduces today's behavior exactly.
    ToolPack("home_assistant", lambda d: make_home_assistant_tools(d.ha)),
    ToolPack("time", lambda _d: make_time_tools()),
    # timer's factory does NOT self-gate on None, so the gate is load-bearing.
    ToolPack("timer", lambda d: make_timer_tools(d.timer_scheduler),
             gate=lambda d: d.timer_scheduler is not None),
    ToolPack("calendar", lambda d: make_calendar_tools(d.google_clients),
             gate=_google_ready),
    ToolPack("gmail", lambda d: make_gmail_tools(d.google_clients),
             gate=_google_ready),
    ToolPack("diagnostic", lambda d: make_diagnostic_tools(d.wake_event_store)),
)


def register_packs(registry: "ToolRegistry", deps: ToolDeps) -> None:
    """Walk TOOL_PACKS in order; gate, build, and register each pack's
    tools onto `registry`. Each pack's build runs behind try/except for
    fault isolation — a broken pack (ImportError in a tool module, a
    factory that raises) contributes no tools and is logged, never
    crashing the daemon. Mirrors transit.active_transit's per-provider
    guard."""
    for pack in TOOL_PACKS:
        if not pack.gate(deps):
            continue
        try:
            fns = pack.build(deps)
        except Exception:  # noqa: BLE001
            logger.exception(
                "tool pack %s build failed; skipping its tools", pack.name,
            )
            continue
        for fn in fns:
            registry.register(fn)
