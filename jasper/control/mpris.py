# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared shairport-sync MPRIS PlaybackStatus probe with subprocess hygiene.

Both `/state`'s AirPlay row and the Tier 3 ShairportSupervisor's
session gate ask the same question — "does shairport-sync's MPRIS
surface report Playing right now?" — via the same `busctl` call. This
module owns that subprocess so the hygiene rules live in one place:

- **Kill-on-timeout.** `asyncio.wait_for(proc.communicate(), ...)`
  cancels the *await*, not the child. Under a DBus stall every probe
  used to leak one live `busctl` process — and `/state` is polled
  every 5-7 s by the dashboard, so a sustained stall compounded into
  an unbounded process pile on a 1 GB Pi. We now SIGKILL the child and
  reap it before reporting "unknown".
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

import asyncio
import contextlib

_BUSCTL_PLAYBACK_STATUS_ARGV = (
    "busctl", "--system", "call",
    "org.mpris.MediaPlayer2.ShairportSync",
    "/org/mpris/MediaPlayer2",
    "org.freedesktop.DBus.Properties", "Get", "ss",
    "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
)


async def shairport_playing(timeout: float = 2.0) -> bool | None:
    """True/False when MPRIS answered; None when the answer is unknown
    (busctl missing, spawn failure, DBus stall, non-zero exit)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_BUSCTL_PLAYBACK_STATUS_ARGV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        # Covers FileNotFoundError (busctl not installed) plus the
        # transient spawn errnos (EAGAIN/ENOMEM) a loaded Pi can hit.
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        # DBus stall. wait_for cancelled our *await* but the child is
        # still alive — kill and reap it so a stuck system bus can't
        # accumulate one busctl per poll.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), 1.0)
        return None
    if proc.returncode != 0:
        return None
    return b'"Playing"' in stdout
