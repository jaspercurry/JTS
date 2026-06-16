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

from .tool_state import DEFAULT_PATH as STATE_PATH
from .tool_state import read_disabled_tools

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = "/run/jasper/tools.json"

# Statuses that mean "backend configured" — the user can turn these on/off,
# and the wizard CAN re-derive their on/off from the disabled-set.
# "needs_setup" is config-derived (it needs the live registry to compute),
# so it is never overlaid — the wizard trusts voice's baked value for it.
_CONFIGURED = ("active", "off")


def _unavailable() -> dict[str, Any]:
    return {"schema_version": 1, "tools": [], "unavailable": True}


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


def overlay(catalog: dict[str, Any], disabled: frozenset[str]) -> dict[str, Any]:
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
    tools_out: list[Any] = []
    pending = False
    for t in catalog.get("tools", []):
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        orig = t.get("status")
        if orig in _CONFIGURED and isinstance(name, str):
            desired = "off" if name in disabled else "active"
            if desired != orig:
                pending = True
            t = {**t, "status": desired}
        tools_out.append(t)
    return {**catalog, "tools": tools_out, "pending": pending}


def catalog_view(
    catalog_path: str = DEFAULT_CATALOG_PATH,
    state_path: str = STATE_PATH,
) -> dict[str, Any]:
    """The /tools/ wizard's /catalog.json payload: voice's catalog metadata
    with the fresh disabled-set overlaid + a `pending` flag. Convergent the
    instant a toggle writes tool_state.env, independent of the restart."""
    return overlay(read_catalog_json(catalog_path), read_disabled_tools(state_path))


def summary(
    catalog_path: str = DEFAULT_CATALOG_PATH,
    state_path: str = STATE_PATH,
) -> dict[str, Any]:
    """Compact catalog state for /state + jasper-doctor: presence, counts,
    the disabled-set, and whether a voice restart is pending."""
    disabled = read_disabled_tools(state_path)
    view = overlay(read_catalog_json(catalog_path), disabled)
    return {
        "catalog_present": not view.get("unavailable", False),
        "count": len(view.get("tools", [])),
        "disabled": sorted(disabled),
        "disabled_count": len(disabled),
        "pending": bool(view.get("pending", False)),
    }
