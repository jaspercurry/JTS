# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fresh SSOT read of the transit city-pack toggle for /state surfaces.

Mirrors ``jasper.multiroom.state.read_grouping_state``: the wizard owns
``JASPER_TRANSIT_CITIES`` in ``/var/lib/jasper/transit.env``, and any daemon
that *displays* it (chiefly ``jasper-control``'s ``/state`` and the
``/system/`` dashboard) but is NOT ``jasper-voice`` must re-read that file
fresh on every call — never ``os.environ``. Those long-lived daemons load
the env file once at start and are not restarted on a ``/transit/`` save, so
``os.environ`` goes stale (the same class of bug as the voice-provider reader
in :mod:`jasper.voice.provider_state`).

Kept out of :mod:`jasper.transit` proper so that package stays pure (no file
I/O, no path knowledge); this is the thin SSOT-reading edge.
"""
from __future__ import annotations

from typing import Any

from .. import location_state
from ..env_load import parse_env_file
from . import CITY_PACKS, enabled_pack_ids


def read_state(path: str = location_state.TRANSIT_FILE) -> dict[str, Any]:
    """Which transit city packs are enabled, as a JSON-able ``/state`` block.

    Re-reads ``path`` on every call (the fresh-read contract — never
    ``os.environ``). Total: never raises. A missing / unreadable file
    resolves to the absent-key default (all packs enabled, the
    non-breaking legacy behaviour); a present-but-empty
    ``JASPER_TRANSIT_CITIES`` resolves to none enabled — exactly matching
    what the voice daemon computes from its sourced environment, so the
    surface never disagrees with the running truth.

    Returns ``{"packs": [{"id", "label", "enabled"}, ...]}`` in registry
    order — one row per available city pack with its on/off state, so the
    dashboard can render the toggle state without cross-referencing two
    lists.
    """
    try:
        env = parse_env_file(path)
    except Exception:  # noqa: BLE001
        # parse_env_file swallows OSError but not e.g. UnicodeDecodeError on a
        # corrupt/non-UTF-8 file. transit.env is always ASCII in practice, but
        # honour the "never raises" contract: a bad file reads as absent ->
        # the legacy all-enabled default, never a crash.
        env = {}
    enabled = set(enabled_pack_ids(env))
    return {
        "packs": [
            {"id": pack.id, "label": pack.label, "enabled": pack.id in enabled}
            for pack in CITY_PACKS
        ],
    }
