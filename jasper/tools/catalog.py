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
from .packs import TOOL_PACKS, CatalogPack, ToolDeps, ToolPack, register_packs

logger = logging.getLogger(__name__)

CATALOG_SCHEMA_VERSION = 2
DEFAULT_CATALOG_PATH = "/run/jasper/tools.json"

# Tools that are REAL registry/manifest entries but are NOT independently
# user-toggleable, so they get no /tools/ card. home_assistant_confirm is the
# confirmation half of the Home Assistant consequential-action safety flow —
# an internal companion of home_assistant, not a browsable capability.
# Listing it let a user disable confirm alone and strand the confirm flow
# (and toggling it independently makes no sense); it follows home_assistant.
# It stays in the registry + manifest (the model uses it); it's only hidden
# from the catalog UI.
_CATALOG_HIDDEN: frozenset[str] = frozenset({"home_assistant_confirm"})

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
    # home assistant (factory returns [] when unconfigured). Only the
    # user-facing home_assistant tool gets a card; home_assistant_confirm is
    # hidden (_CATALOG_HIDDEN), so it needs no setup link.
    "home_assistant": "/ha/",
    # google (gated on ≥1 linked account)
    "calendar_today_summary": "/google/",
    "calendar_upcoming": "/google/",
    "gmail_unread_summary": "/google/",
    "gmail_read_thread": "/google/",
}


def _catalog_pack_payload(pack: CatalogPack | None) -> dict[str, Any] | None:
    if pack is None:
        return None
    return {
        "id": pack.id,
        "title": pack.title,
        "summary": pack.summary,
        "setup_url": pack.setup_url,
    }


def _pack_status(tools: list[dict[str, Any]]) -> str:
    statuses = {t.get("status") for t in tools}
    if statuses == {"active"}:
        return "active"
    if statuses == {"off"}:
        return "off"
    if statuses == {"needs_setup"}:
        return "needs_setup"
    return "partial"


def _build_pack_payloads(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    packs: dict[str, dict[str, Any]] = {}
    for tool in tools:
        pack = tool.get("pack")
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        if isinstance(pack, dict) and isinstance(pack.get("id"), str):
            pid = pack["id"]
            seed = {
                "id": pid,
                "title": pack.get("title") or pid,
                "summary": pack.get("summary") or "",
                "setup_url": pack.get("setup_url"),
                "category": tool.get("category") or "Utilities",
                "tool_names": [],
            }
        else:
            pid = f"tool:{name}"
            seed = {
                "id": pid,
                "title": name,
                "summary": tool.get("summary") or "",
                "setup_url": tool.get("setup_url"),
                "category": tool.get("category") or "Utilities",
                "tool_names": [],
                "singleton_tool_name": name,
            }
        if pid not in packs:
            packs[pid] = seed
        packs[pid]["tool_names"].append(name)
    by_name = {t["name"]: t for t in tools}
    out = []
    for pack in packs.values():
        members = [by_name[name] for name in pack["tool_names"] if name in by_name]
        status = _pack_status(members)
        out.append({
            **pack,
            "status": status,
            "tool_count": len(members),
            "active_count": sum(1 for t in members if t.get("status") == "active"),
            "off_count": sum(1 for t in members if t.get("status") == "off"),
            "needs_setup_count": sum(
                1 for t in members if t.get("status") == "needs_setup"
            ),
            "setup_required_count": sum(
                1 for t in members if t.get("requires_setup")
            ),
            "customized_count": sum(
                1 for t in members if t.get("prompt_customized")
            ),
        })
    return out


def _summary_from_description(text: str, *, max_chars: int = 180) -> str:
    """Short card copy derived from the model-facing description.

    The full tool docstring can be long because it teaches the model call
    boundaries and response style. Cards need a scan-friendly summary, but a
    separate hand-written copy layer would be busywork today. Use the first
    sentence when it fits; otherwise truncate at a word boundary.
    """
    compact = " ".join((text or "").strip().split())
    if len(compact) <= max_chars:
        return compact
    first_sentence, sep, _rest = compact.partition(". ")
    if sep and 24 <= len(first_sentence) <= max_chars:
        return first_sentence + "."
    clipped = compact[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (clipped or compact[:max_chars]).rstrip() + "..."


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


def _tool_pack_index(registry: ToolRegistry) -> dict[str, ToolPack]:
    packs_by_id = {p.name: p for p in TOOL_PACKS}
    return {
        name: packs_by_id[pack_id]
        for name, pack_id in registry.tool_packs.items()
        if pack_id in packs_by_id
    }


def build_catalog(
    live_registry: ToolRegistry,
    disabled: frozenset[str],
    *,
    disabled_packs: frozenset[str] = frozenset(),
    prompt_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute the catalog payload. `live_registry` is the daemon's REAL
    registry (configured + enabled). `disabled` is the user's set."""
    prompt_overrides = prompt_overrides or {}
    full = _full_catalog_registry()
    full.apply_prompt_overrides(prompt_overrides)
    pack_by_tool = _tool_pack_index(full)
    live_names = set(live_registry.tools.keys())
    tools = []
    for name, t in full.tools.items():
        if name in _CATALOG_HIDDEN:
            continue  # internal companion tool — no browse/toggle card
        pack = pack_by_tool.get(name)
        pack_id = pack.catalog_pack.id if pack and pack.catalog_pack else None
        disabled_by_pack = pack_id in disabled_packs if pack_id else False
        configured = name in live_names or name in disabled or disabled_by_pack
        if not configured:
            status = "needs_setup"
        elif disabled_by_pack or name in disabled:
            status = "off"
        else:
            status = "active"
        description = t.model_facing_description()
        default_description = t.default_model_facing_description()
        requires_setup = status == "needs_setup" and _SETUP_URLS.get(name) is not None
        tools.append({
            "name": t.name,
            "summary": _summary_from_description(description),
            "description": description,
            "default_description": default_description,
            "details": t.description,
            "labels": list(t.labels),
            "providers": sorted(t.providers) if t.providers else None,
            "category": pack.category if pack else "Utilities",
            "pack": _catalog_pack_payload(pack.catalog_pack if pack else None),
            "disabled_by_pack": disabled_by_pack,
            "prompt_customized": t.prompt_customized(),
            "status": status,
            "setup_url": _SETUP_URLS.get(name),
            "requires_setup": requires_setup,
            "parameters": t.parameters,
            "timeout": t.timeout,
            "untrusted_output": t.untrusted_output,
            "consequential": t.consequential,
        })
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "tools": tools,
        "packs": _build_pack_payloads(tools),
    }


def write_catalog(
    live_registry: ToolRegistry,
    disabled: frozenset[str],
    *,
    disabled_packs: frozenset[str] = frozenset(),
    prompt_overrides: dict[str, str] | None = None,
    path: str = DEFAULT_CATALOG_PATH,
) -> None:
    """Atomically write the catalog to `path` (world-readable 0644).
    Fail-soft: a write error logs and never raises — the daemon must
    boot even if /run isn't writable in a dev environment."""
    import json

    from ..atomic_io import atomic_write_text
    try:
        catalog = build_catalog(
            live_registry,
            disabled,
            disabled_packs=disabled_packs,
            prompt_overrides=prompt_overrides,
        )
        atomic_write_text(path, json.dumps(catalog, indent=2) + "\n", mode=0o644)
        # Count the tools actually WRITTEN to the catalog (the full
        # enumeration), not just the live registry — needs_setup tools are in
        # the file too, so len(live_registry.tools) under-reported the catalog.
        log_event(
            logger, "tool_catalog.written",
            path=path, tools=len(catalog["tools"]),
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger, "tool_catalog.write_failed",
            level=logging.WARNING, path=path, err=str(e),
        )
