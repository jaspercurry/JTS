"""The single speaker-identity reader.

One place that assembles "who is this speaker" — display name, room,
mDNS hostname, and stable peer_id — so consumers (`/rooms/`,
`control_advert`, future bond/grouping code) stop reconstructing identity
ad-hoc and drifting from each other.

Room precedence is the point: the room now lives in the *identity home*
(`jasper.speaker_name`), so that wins. A legacy fallback to peering's own
`JASPER_PEER_ROOM` / `peering.config.default_room()` keeps `/rooms/`
consistent on installs that still carry a pre-identity peering room but
haven't moved it into the identity home yet.

``read_identity()`` is TOTAL: every field has a safe fallback and the
function never raises, so an unreadable file or missing env degrades to a
sensible default rather than breaking a caller's render path.

Scope split with :mod:`jasper.identity_state`: this module reads the
*intended* identity (display name, room, configured hostname, stable
peer_id). ``identity_state`` reads the *observed* network identity —
what Avahi actually advertises after RFC 6762 collision renames, as
snapshotted by ``jasper-identity-reconcile`` into
``/var/lib/jasper/identity.env``. Intended vs observed disagreeing is
exactly the drift the reconciler surfaces; consumers pick the side
they mean.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from . import speaker_name
from .peering import config as peering_config

logger = logging.getLogger(__name__)

# Stable per-install peer identifier, written once by the peering layer
# (jasper/peering/config.py:_ensure_peer_id). We only ever READ it here —
# generating it is peering's job; identity is a reader, not a writer.
PEER_ID_FILE = peering_config.PEER_ID_FILE

# Default mDNS hostname when JASPER_HOSTNAME is unset. Matches every other
# surface (the wizards, control_advert) so identity agrees with them.
DEFAULT_HOSTNAME = "jts.local"

# Legacy env var from the pre-identity peering room. Read here only as a
# fallback so an older install still surfaces a room.
LEGACY_PEER_ROOM_ENV = "JASPER_PEER_ROOM"


@dataclass(frozen=True)
class SpeakerIdentity:
    name: str
    room: str
    hostname: str
    peer_id: str


def _read_peer_id(path: str | None = None) -> str:
    """Stable peer_id contents, stripped; "" on any failure (never raises).

    Resolves the module-level ``PEER_ID_FILE`` at call time when ``path`` is
    None so tests (and any future re-point) can override the constant.
    """
    target = PEER_ID_FILE if path is None else path
    try:
        with open(target, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""
    except Exception:  # noqa: BLE001 — identity reads must never raise
        logger.debug("identity: unexpected error reading %s", target, exc_info=True)
        return ""


def _resolve_room() -> str:
    """Room with identity-home-wins precedence (never raises).

      1. identity home — jasper.speaker_name.runtime_room()
      2. legacy peering env — JASPER_PEER_ROOM
      3. peering's hostname-derived default — default_room()
    """
    try:
        room = speaker_name.runtime_room()
        if room:
            return room
    except Exception:  # noqa: BLE001
        logger.debug("identity: runtime_room failed", exc_info=True)

    legacy = os.environ.get(LEGACY_PEER_ROOM_ENV, "").strip()
    if legacy:
        return legacy

    try:
        return peering_config.default_room()
    except Exception:  # noqa: BLE001
        logger.debug("identity: default_room failed", exc_info=True)
        return ""


def read_identity() -> SpeakerIdentity:
    """Resolve this speaker's identity. TOTAL — never raises.

    name     — jasper.speaker_name.runtime_name() (env → state → "JTS")
    room     — identity home wins, then legacy JASPER_PEER_ROOM, then
               peering.config.default_room() (see _resolve_room)
    hostname — JASPER_HOSTNAME or "jts.local"
    peer_id  — /var/lib/jasper/peer_id stripped, "" on any failure
    """
    try:
        name = speaker_name.runtime_name()
    except Exception:  # noqa: BLE001 — a bad name read must not break callers
        logger.debug("identity: runtime_name failed", exc_info=True)
        name = speaker_name.DEFAULT_SPEAKER_NAME

    room = _resolve_room()
    hostname = (os.environ.get("JASPER_HOSTNAME", DEFAULT_HOSTNAME).strip()
                or DEFAULT_HOSTNAME)
    peer_id = _read_peer_id()

    return SpeakerIdentity(name=name, room=room, hostname=hostname, peer_id=peer_id)
