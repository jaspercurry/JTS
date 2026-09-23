# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-process echo detection for VolumeCoordinator's outbound writes.

Split out of ``jasper.volume_coordinator``: each function takes the
coordinator state it reads explicitly, so it carries no coordinator
reference of its own.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .music_sources import Source
from .volume_persistence import VolumePersistence
from .volume_state import OutboundStamp

# Window during which an observed source-side change is treated as
# the echo of our own write and ignored. Long enough that DBus
# round-trip + bus latency on a busy Pi 5 is well within it; short
# enough that a real user-touched slider movement that happens to
# land just after our write isn't swallowed.
ECHO_WINDOW_SEC = 0.5
PERSISTENCE_ECHO_WINDOW_SEC = 2.0


def stamp_outbound(last_outbound: dict[Source, OutboundStamp], source: Source) -> None:
    """Record that ``source`` was just written outbound by this coordinator."""
    last_outbound[source] = OutboundStamp(at_mono=time.monotonic())


def is_own_echo(
    last_outbound: dict[Source, OutboundStamp],
    source: Source,
    observed_level: int,
) -> bool:
    """Whether an inbound observation of ``source`` is this coordinator's own echo."""
    stamp = last_outbound.get(source)
    if stamp is None:
        return False
    if time.monotonic() - stamp.at_mono > ECHO_WINDOW_SEC:
        return False
    # Within the window, ignore even a different value. Polling can
    # race the source's own state update after an outbound write,
    # especially on source handoff. If the user really changed the
    # sender slider, the next 1 Hz poll will pick up the stable
    # value outside this short window.
    return True


def is_recent_cross_process_write(
    persistence: VolumePersistence,
    current_level: int,
    observed_level: int,
) -> bool:
    """Suppress stale polls after another process changed volume.

    jasper-control creates its own coordinator for LAN / hardware
    knob requests, so voice_daemon's observer does not see that
    coordinator's in-memory outbound stamp. `last_used_at` is the
    durable cross-process echo stamp: if disk moved ahead of this
    coordinator very recently and the source reports a different
    level, prefer the persisted knob/HTTP/voice truth for one short
    poll window.
    """
    record = persistence.load()
    if (
        record is None
        or record.last_used_at is None
        or record.listening_level is None
    ):
        return False
    if int(record.listening_level) == int(observed_level):
        return False
    if int(record.listening_level) == int(current_level):
        return False
    age = (datetime.now(timezone.utc) - record.last_used_at).total_seconds()
    # VolumePersistence writes timestamps at second precision, so
    # this needs to be wider than the in-memory monotonic window.
    return 0.0 <= age <= PERSISTENCE_ECHO_WINDOW_SEC
