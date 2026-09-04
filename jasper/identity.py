# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
they mean. The hostname fallback below crosses to that file for the
one value it records on the *intended* side — the reconciler's
snapshot of ``JASPER_HOSTNAME`` — and never for the observed names,
so the split holds.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from . import identity_state, speaker_name
from .http_security import DEFAULT_MANAGEMENT_HOSTNAME
from .peering import config as peering_config

logger = logging.getLogger(__name__)

# Stable per-install peer identifier, written once by the peering layer
# (jasper/peering/config.py:_ensure_peer_id). We only ever READ it here —
# generating it is peering's job; identity is a reader, not a writer.
PEER_ID_FILE = peering_config.PEER_ID_FILE

# Default mDNS hostname when neither the environment nor identity.env names
# one. The same name the management-host allowlist defaults to, from its
# owner, so a bare speaker is reachable at the name identity prints.
DEFAULT_HOSTNAME = DEFAULT_MANAGEMENT_HOSTNAME

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


def resolve_hostname() -> str:
    """mDNS hostname with process-env-wins precedence (never raises).

      1. JASPER_HOSTNAME in the process environment
      2. the reconciler's recorded CONFIGURED hostname from identity.env
      3. DEFAULT_HOSTNAME

    Step 2 is what makes a CLI honest. ``JASPER_HOSTNAME`` reaches a daemon
    through its unit's ``EnvironmentFile=``; a command run over ssh gets no
    such file, so env-or-literal alone printed ``jts.local`` on every box.
    ``identity.env`` is the one file a bare shell can reach that records the
    same intent, because ``jasper-identity-reconcile`` snapshots
    ``JASPER_HOSTNAME`` into it (deploy/bin/jasper-identity-reconcile,
    ``CONFIGURED_HOSTNAME``).

    Read through :func:`jasper.identity_state.configured_hostname` rather
    than by spelling the file's key here: ``identity_state`` owns
    identity.env's vocabulary and its stat-keyed cache, and it is total,
    so this stays total and cheap enough to call per URL built.
    """
    configured = os.environ.get("JASPER_HOSTNAME", "").strip()
    if configured:
        return configured
    return identity_state.configured_hostname() or DEFAULT_HOSTNAME


#: Where a human goes to run or apply a crossover round.
CROSSOVER_PAGE_PATH = "/sound/crossover/"

#: Where a human DECLARES the speaker — drivers, their safety profile, the
#: corner. A second page rather than a second spelling of the first: the
#: per-driver bound comes from the design draft that page writes, and an
#: operator whose speaker has never been commissioned cannot satisfy
#: ``--drivers`` by pointing harder at a file that does not exist yet.
SOUND_SETUP_PAGE_PATH = "/sound/setup/"


def speaker_url(path: str) -> str:
    """A handoff URL for THIS speaker. TOTAL — never raises.

    Through :func:`resolve_hostname`, so ``jts3.local`` never prints as
    ``jts.local`` and sends its reader to a different box — silently, because
    that name usually resolves to something.

    Deliberately NOT ``Config.from_env``, whose ``hostname`` field says the
    same thing: that constructor refuses outright when no voice provider is
    configured, so an orientation verb built on it would fail on a bench
    speaker that has never been given an API key.
    """
    return f"http://{resolve_hostname()}{path}"


def read_identity() -> SpeakerIdentity:
    """Resolve this speaker's identity. TOTAL — never raises.

    name     — jasper.speaker_name.runtime_name() (env → state → "JTS")
    room     — see _resolve_room
    hostname — see resolve_hostname
    peer_id  — /var/lib/jasper/peer_id stripped, "" on any failure
    """
    try:
        name = speaker_name.runtime_name()
    except Exception:  # noqa: BLE001 — a bad name read must not break callers
        logger.debug("identity: runtime_name failed", exc_info=True)
        name = speaker_name.DEFAULT_SPEAKER_NAME

    room = _resolve_room()
    hostname = resolve_hostname()
    peer_id = _read_peer_id()

    return SpeakerIdentity(name=name, room=room, hostname=hostname, peer_id=peer_id)
