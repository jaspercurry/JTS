"""Light read-side view of the tool catalog: overlay the user's desired
disabled-set onto the catalog JSON jasper-voice wrote.

jasper-voice writes /run/jasper/tools.json at startup (jasper.tools.catalog),
baking each tool's status from the LIVE registry. But the /tools/ wizard
writes the disabled-set to tool_state.env WITHOUT restarting voice — an
explicit Apply does that — so the baked `status` goes stale the instant a
tool is toggled. This module re-derives each CONFIGURED tool's on/off status
from the FRESH disabled-set, so the page (and /state) reflect the user's
choice immediately, decoupled from the restart. It also reports whether a
restart is PENDING (the desired set differs from what voice baked).

Why a separate module from jasper.tools.catalog: that one BUILDS the JSON by
enumerating the full registry (heavy — imports every tool factory). This is
the READ side — it imports only `json` + jasper.tool_state, so the
socket-activated wizard and jasper-control can both use it without pulling in
jasper.tools (the transit lazy-import lesson).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .tool_prompt_overrides import DEFAULT_PATH as PROMPT_OVERRIDES_PATH
from .tool_prompt_overrides import read_prompt_overrides
from .tool_state import DEFAULT_PATH as STATE_PATH
from .tool_state import ToolState, read_tool_state

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = "/run/jasper/tools.json"

# Statuses that mean "backend configured" — the user can turn these on/off,
# and the wizard CAN re-derive their on/off from the disabled-set.
# "needs_setup" is config-derived (it needs the live registry to compute),
# so it is never overlaid — the wizard trusts voice's baked value for it.
_CONFIGURED = ("active", "off")


def _pack_id_for_tool(t: dict[str, Any]) -> str | None:
    pack = t.get("pack")
    pack_id = pack.get("id") if isinstance(pack, dict) else None
    if isinstance(pack_id, str) and pack_id:
        return pack_id
    name = t.get("name")
    return f"tool:{name}" if isinstance(name, str) and name else None


def _unavailable() -> dict[str, Any]:
    return {"schema_version": 2, "tools": [], "packs": [], "unavailable": True}


def read_catalog_json(path: str = DEFAULT_CATALOG_PATH) -> dict[str, Any]:
    """Read the /run catalog jasper-voice wrote. A missing / unreadable /
    malformed / wrong-shape file resolves to an explicit `unavailable` empty
    catalog so callers render an honest "not ready" state rather than
    erroring (e.g. during the ~seconds-long window a voice restart wipes and
    rewrites /run/jasper)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _unavailable()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        # UnicodeDecodeError (a ValueError, not an OSError) covers a non-UTF-8
        # / corrupt file — the FS-corruption class the fail-safe exists for;
        # without it /state, the doctor, and /catalog.json would crash.
        logger.warning("tool catalog read %s failed: %s", path, e)
        return _unavailable()
    if not isinstance(data, dict) or not isinstance(data.get("tools"), list):
        logger.warning("tool catalog %s has unexpected shape", path)
        return _unavailable()
    return data


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
    by_name = {t.get("name"): t for t in tools if isinstance(t.get("name"), str)}
    out: list[dict[str, Any]] = []
    for pack in packs.values():
        members = [
            by_name[name]
            for name in pack["tool_names"]
            if isinstance(name, str) and name in by_name
        ]
        out.append({
            **pack,
            "tool_names": [t["name"] for t in members],
            "status": _pack_status(members),
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


def overlay(
    catalog: dict[str, Any],
    state: ToolState | frozenset[str],
    prompt_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a copy of `catalog` with each CONFIGURED tool's status
    re-derived from `disabled`, plus a top-level `pending` flag — True iff
    applying `disabled` would change the live registry (some configured
    tool's desired on/off differs from what voice baked).

    `pending` is computed only over tools present in the catalog. A tool
    hidden from the catalog (jasper.tools.catalog._CATALOG_HIDDEN, today just
    home_assistant_confirm) that somehow appears in `disabled` won't be
    counted — but that's unreachable through the wizard (POST /toggle rejects
    names absent from the catalog) and benign even if hand-edited (a stranded
    confirm tool is harmless), so it's intentionally not special-cased here."""
    if isinstance(state, frozenset):
        state = ToolState(disabled_tools=state)
    prompt_overrides = prompt_overrides or {}
    tools_out: list[Any] = []
    pending = False
    for t in catalog.get("tools", []):
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        orig = t.get("status")
        pack_id = _pack_id_for_tool(t)
        disabled_by_pack = (
            isinstance(pack_id, str) and pack_id in state.disabled_packs
        )
        if orig in _CONFIGURED and isinstance(name, str):
            desired = (
                "off"
                if disabled_by_pack or name in state.disabled_tools
                else "active"
            )
            if desired != orig:
                pending = True
                t = {**t, "status": desired}
            if disabled_by_pack:
                t = {**t, "disabled_by_pack": True}
        elif (
            orig == "needs_setup"
            and isinstance(pack_id, str)
            and t.get("setup_url")
        ):
            setup_enabled = pack_id in state.setup_enabled_packs
            t = {
                **t,
                "requires_setup": True,
                "setup_enabled": setup_enabled,
            }
            if not setup_enabled:
                t = {**t, "status": "off", "disabled_by_pack": True}
        if isinstance(name, str):
            has_prompt_surface = (
                "description" in t
                or "default_description" in t
                or name in prompt_overrides
            )
            if has_prompt_surface:
                default_description = (
                    t.get("default_description") or t.get("description") or ""
                )
                desired_prompt = prompt_overrides.get(name, default_description)
                if desired_prompt != t.get("description"):
                    pending = True
                t = {
                    **t,
                    "description": desired_prompt,
                    "summary": t.get("summary") or desired_prompt,
                    "prompt_customized": name in prompt_overrides,
                }
        tools_out.append(t)
    return {
        **catalog,
        "tools": tools_out,
        "packs": _build_pack_payloads([t for t in tools_out if isinstance(t, dict)]),
        "pending": pending,
    }


def catalog_view(
    catalog_path: str = DEFAULT_CATALOG_PATH,
    state_path: str = STATE_PATH,
    prompt_overrides_path: str = PROMPT_OVERRIDES_PATH,
) -> dict[str, Any]:
    """The /tools/ wizard's /catalog.json payload: voice's catalog metadata
    with the fresh disabled-set overlaid + a `pending` flag. Convergent the
    instant a toggle writes tool_state.env, independent of the restart."""
    return overlay(
        read_catalog_json(catalog_path),
        read_tool_state(state_path),
        read_prompt_overrides(prompt_overrides_path),
    )


def summary(
    catalog_path: str = DEFAULT_CATALOG_PATH,
    state_path: str = STATE_PATH,
    prompt_overrides_path: str = PROMPT_OVERRIDES_PATH,
) -> dict[str, Any]:
    """Compact catalog state for /state + jasper-doctor: presence, counts,
    the disabled-set, and whether a voice restart is pending."""
    state = read_tool_state(state_path)
    overrides = read_prompt_overrides(prompt_overrides_path)
    view = overlay(read_catalog_json(catalog_path), state, overrides)
    return {
        "catalog_present": not view.get("unavailable", False),
        "count": len(view.get("tools", [])),
        "pack_count": len(view.get("packs", [])),
        "disabled": sorted(state.disabled_tools),
        "disabled_packs": sorted(state.disabled_packs),
        "disabled_count": len(state.disabled_tools),
        "disabled_pack_count": len(state.disabled_packs),
        "setup_enabled_packs": sorted(state.setup_enabled_packs),
        "setup_enabled_pack_count": len(state.setup_enabled_packs),
        "prompt_overrides": sorted(overrides),
        "prompt_override_count": len(overrides),
        "pending": bool(view.get("pending", False)),
    }
