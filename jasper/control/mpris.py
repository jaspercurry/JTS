# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared shairport-sync MPRIS PlaybackStatus probe with subprocess hygiene.

Both `/state`'s AirPlay row and the Tier 3 ShairportSupervisor's
session gate ask the same question — "does shairport-sync's MPRIS
surface report Playing right now?" — via the same `busctl` call. The shared
system-bus subprocess boundary owns the hygiene rules in one place:

- **Kill-on-timeout.** `asyncio.wait_for(proc.communicate(), ...)`
  cancels the *await*, not the child, so under a DBus stall each probe
  would leak one live `busctl` process and a repeatedly-polled caller
  would compound that into a process pile on a 1 GB Pi. The child is
  SIGKILLed and reaped before reporting "unknown".
- **Spawn errors are "unknown", not a crash.** `FileNotFoundError` is
  just one member of the OSError family a spawn can raise (EAGAIN /
  ENOMEM under memory pressure are the realistic siblings on a loaded
  Pi). A spawn failure here must never propagate — `/state` is a
  fail-soft aggregate and one sick probe must not 500 the whole call.

Returns tri-state so each caller keeps its own unknown-handling:
`/state` maps None → null (section fails soft); the supervisor gate
cross-checks systemd and only maps None → "assume active" while the
shairport unit itself still appears live or unknown.
"""
from __future__ import annotations

from ..busctl import system_busctl

_BUSCTL_PLAYBACK_STATUS_ARGS = (
    "call",
    "org.mpris.MediaPlayer2.ShairportSync",
    "/org/mpris/MediaPlayer2",
    "org.freedesktop.DBus.Properties", "Get", "ss",
    "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
)


async def shairport_playing(timeout: float = 2.0) -> bool | None:
    """True/False when MPRIS answered; None when the answer is unknown
    (busctl missing, spawn failure, DBus stall, non-zero exit)."""
    stdout = await system_busctl(
        *_BUSCTL_PLAYBACK_STATUS_ARGS,
        timeout=timeout,
    )
    if stdout is None:
        return None
    return b'"Playing"' in stdout
