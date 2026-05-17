"""Multi-device wake-word arbitration ("peering") for JTS.

When a household runs multiple JTS speakers on the same LAN, all of
them hear the same "Hey Jarvis" — without coordination, all of them
would answer at once. This package adds the coordination: peers
discover each other via mDNS-SD (`_jasper-peer._udp`), exchange a
small per-wake JSON message over multicast UDP, deterministically
pick one winner, and suppress the rest for the duration of that
turn.

**Off by default.** A single-Pi household pays nothing: with mode
`off`, no Avahi service is advertised, no zeroconf import happens,
no multicast socket is opened, no thread is spawned. The user
explicitly turns peering on via the `/peers/` web wizard, which
writes `/var/lib/jasper/peering.env` and restarts `jasper-control`.

Architecture (see `docs/satellites.md` "Microphone arbitration"
section for the design rationale that pre-dates this module):

  jasper-control hosts the peering daemon. It listens on multicast
  239.192.0.1:5354 (TTL=1, RFC 2365 admin-local scope) and serves a
  Unix socket at /run/jasper/peering.sock that jasper-voice queries
  on every wake event ("did I win? — WIN | LOSE"). Arbitration is
  P2P: every peer applies the same pure ranking function to the
  same set of WAKE messages and reaches the same conclusion.

Public API:
  - PeeringConfig / load_config  — configuration in /var/lib/jasper/peering.env
  - WakeReport, rank            — pure ranking function
  - PeeringStateMachine          — pure event-driven state machine
  - PeeringDaemon                — asyncio orchestrator (transport + discovery + state)
  - start_uds_server / send_arbitrate_request  — UDS RPC voice↔control

Module layout — separated by I/O profile so each piece is
independently testable:

  config.py     pure  — dataclass + env-file loader
  rank.py       pure  — deterministic per-utterance winner pick
  state.py      pure  — state machine driven by timestamped events
  transport.py  I/O   — multicast UDP socket + JSON encode/decode
  discovery.py  I/O   — AsyncZeroconf browse wrapper
  uds.py        I/O   — Unix-socket RPC server (voice→peering)
  avahi.py      I/O   — render template into /etc/avahi/services/jasper-peer.service
  daemon.py     I/O   — asyncio orchestrator (no logic, just plumbing)
"""
from __future__ import annotations

from .config import PeeringConfig, PeeringMode, load_config
from .rank import WakeReport, rank
from .state import (
    Action,
    PeeringStateMachine,
    PeerState,
)

__all__ = [
    "Action",
    "PeeringConfig",
    "PeeringMode",
    "PeeringStateMachine",
    "PeerState",
    "WakeReport",
    "load_config",
    "rank",
]
