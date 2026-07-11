# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared stereo-pair helpers for measurement flows.

Balance and acoustic-sync both need the same leader-gated pair context:
the local grouping state, the recorded bond sibling, and a left/right
member map. Keep that logic here so measurement pages can share it
without importing each other's private functions.
"""

from __future__ import annotations

import os


def resolve_pair() -> tuple[dict | None, dict | None, str]:
    """Return ``(self_grouping, peer, error)`` for the bonded leader.

    ``peer`` is ``{addr, label, hostname, grouping}``. Resolution is
    roster-first through the same rooms helper used by bond/swap/trim so a
    foreign bond claimant cannot poison pair measurement.
    """
    from .rooms_setup import (
        _discover_speakers_cached,
        _resolve_bond_peer,
        _self_addresses,
    )
    from ..multiroom.state import read_grouping_state

    own = read_grouping_state()
    bond_id = str(own.get("bond_id") or "").strip()
    if not own.get("enabled") or not bond_id:
        hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
        return None, None, f"bond a pair first ({hostname}/rooms)"
    if str(own.get("role") or "") != "leader":
        return None, None, "open this page on the pair leader"

    known = _self_addresses()
    addr, pg, perr = _resolve_bond_peer(own, known)
    if perr:
        return None, None, f"pair {perr}"
    directory_row = next(
        (
            r for r in _discover_speakers_cached()
            if str(r.get("address") or "").strip() == addr
        ),
        {},
    )
    peer_hostname = str(directory_row.get("hostname") or "").strip()
    label = str(own.get("peer_name") or "").strip()
    if not label:
        label = str(directory_row.get("name") or "").strip() or addr
    return own, {
        "addr": addr,
        "label": label,
        "hostname": peer_hostname,
        "grouping": pg,
    }, ""


def members_by_channel(own: dict, peer: dict, hostname: str) -> dict | None:
    """Map a bonded pair onto ``{left, right}`` member records."""
    self_ch = str(own.get("channel") or "")
    peer_ch = str(peer["grouping"].get("channel") or "")
    if {self_ch, peer_ch} != {"left", "right"}:
        return None
    mine = {
        "addr": "",
        "is_self": True,
        "label": f"this speaker ({hostname})",
        "snapcast_name": hostname.split(".")[0],
        "trim_db": float(own.get("trim_db") or 0.0),
        "grouping": own,
    }
    theirs = {
        "addr": peer["addr"],
        "is_self": False,
        "label": peer["label"],
        "snapcast_name": str(peer.get("hostname") or "").split(".")[0],
        "trim_db": float(peer["grouping"].get("trim_db") or 0.0),
        "grouping": peer["grouping"],
    }
    return {self_ch: mine, peer_ch: theirs}
