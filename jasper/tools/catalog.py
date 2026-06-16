"""Build + write the /run/jasper/tools.json catalog the /tools/ wizard reads.

jasper-voice owns this file: it enumerates EVERY first-party tool (via
gate-satisfying sentinel deps — the same pattern as
tests/test_tool_manifest.py::_full_registry), knows the LIVE registry
(configured + enabled) and the user's disabled-set, and computes each
tool's status by set membership. The socket-activated /tools/ wizard only
READS this JSON; it never imports jasper.tools (the transit lazy-import
lesson — keep the wizard light).

status:
  "active"      backend configured AND user-enabled (in the live registry)
  "off"         backend configured but user-DISABLED
  "needs_setup" exists in the codebase but backend not configured

Status subtlety: "configured" = name in the live registry OR in the
disabled-set. A tool whose backend isn't configured can't be in the live
registry; but a *disabled* tool also isn't in it. The disabled-set is
what separates "configured-but-off" from "needs_setup": if the user
explicitly disabled it, its backend must have been configurable, so
"off". Edge case — a tool BOTH unconfigured AND in the disabled-set (user
disabled it, then removed the backend config) renders "off"; re-enabling
surfaces "needs_setup" on the next catalog write after a restart.
Acceptable.
"""
from __future__ import annotations

import logging
import types
from typing import Any

from ..log_event import log_event
from . import ToolRegistry
from .packs import ToolDeps, register_packs

logger = logging.getLogger(__name__)

CATALOG_SCHEMA_VERSION = 1
DEFAULT_CATALOG_PATH = "/run/jasper/tools.json"

# Tool -> setup wizard, for tools that can be "needs_setup". Only tools
# whose backend SELF-GATES (so they're absent from the live registry until
# configured) ever surface a setup link — transit, Home Assistant, and
# Google. Tools that register unconditionally (spotify_*, get_weather,
# core time/volume/transport/timer/diagnostic) can never be needs_setup,
# so they get no entry. Keys are tool NAMES; a guard test pins every key
# against the full catalog so a rename can't leave a stale entry.
_SETUP_URLS: dict[str, str] = {
    # transit pack (subway/bus/citibike self-gate on config)
    "get_subway_arrivals": "/transit/",
    "get_bus_arrivals": "/transit/",
    "get_citibike_status": "/transit/",
    # home assistant (factory returns [] when unconfigured)
    "home_assistant": "/ha/",
    "home_assistant_confirm": "/ha/",
    # google (gated on ≥1 linked account)
    "calendar_today_summary": "/google/",
    "calendar_upcoming": "/google/",
    "gmail_unread_summary": "/google/",
    "gmail_read_thread": "/google/",
}


def _full_catalog_registry() -> ToolRegistry:
    """EVERY tool's schema, built with gate-satisfying sentinels.
    Mirrors tests/test_tool_manifest.py::_full_registry. Imports the
    transit factories directly so needs_setup transit tools enumerate
    even when no city is configured."""
    from .bus import make_bus_tools
    from .citibike import make_citibike_tools
    from .subway import make_subway_tools
    transit = []
    transit += list(make_subway_tools(object()))
    transit += list(make_bus_tools(types.SimpleNamespace(enabled=True)))
    transit += list(make_citibike_tools(types.SimpleNamespace(enabled=True)))
    deps = ToolDeps(
        volume_coordinator=None, renderer=None, router=None, weather=None,
        spotify_device_name="JTS", spotify_setup_url="",
        transit_tools=transit, ha=object(), timer_scheduler=object(),
        google_clients=types.SimpleNamespace(list_account_names=lambda: ["seed"]),
        wake_event_store=object(),
    )
    reg = ToolRegistry()
    # Pass disabled=frozenset() so the FULL catalog ignores the user's
    # disabled-set (status is computed separately below).
    register_packs(reg, deps, disabled=frozenset())
    return reg


def build_catalog(
    live_registry: ToolRegistry,
    disabled: frozenset[str],
) -> dict[str, Any]:
    """Compute the catalog payload. `live_registry` is the daemon's REAL
    registry (configured + enabled). `disabled` is the user's set."""
    full = _full_catalog_registry()
    live_names = set(live_registry.tools.keys())
    tools = []
    for name, t in full.tools.items():
        configured = name in live_names or name in disabled
        if not configured:
            status = "needs_setup"
        elif name in disabled:
            status = "off"
        else:
            status = "active"
        tools.append({
            "name": t.name,
            "description": t.model_facing_description(),
            "labels": list(t.labels),
            "providers": sorted(t.providers) if t.providers else None,
            "status": status,
            "setup_url": _SETUP_URLS.get(name),
        })
    return {"schema_version": CATALOG_SCHEMA_VERSION, "tools": tools}


def write_catalog(
    live_registry: ToolRegistry,
    disabled: frozenset[str],
    *,
    path: str = DEFAULT_CATALOG_PATH,
) -> None:
    """Atomically write the catalog to `path` (world-readable 0644).
    Fail-soft: a write error logs and never raises — the daemon must
    boot even if /run isn't writable in a dev environment."""
    import json

    from ..atomic_io import atomic_write_text
    try:
        payload = json.dumps(build_catalog(live_registry, disabled), indent=2)
        atomic_write_text(path, payload + "\n", mode=0o644)
        log_event(
            logger, "tool_catalog.written",
            path=path, tools=len(live_registry.tools),
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger, "tool_catalog.write_failed",
            level=logging.WARNING, path=path, err=str(e),
        )
