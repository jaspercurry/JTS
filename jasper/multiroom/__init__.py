# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multiroom speaker grouping ("bonds") for JTS.

A bond has one *leader* (runs a snapserver, the timing master) and one or
more *followers* (run snapclients pointed at the leader); each speaker plays
a single assigned *channel* of the bond's stream. Off by default: with
grouping ``off`` no snapserver, snapclient, channel split or socket exists.
The wizard (`jasper/web/rooms_setup.py`) POSTs to jasper-control's
``/grouping/set``; `jasper/control/handlers/grouping.py` is the sole writer
of ``/var/lib/jasper/grouping.env`` -- ABSENT means off.

Fail-safe vs fail-loud (mirrors peering):
  - Missing / unreadable / malformed file => grouping OFF, no error.
  - Explicitly ON but internally inconsistent => stays ON with a specific
    ``error`` string the doctor surfaces.

Resolve config *callables* through the ``config`` module at call time
(``config.load_config(...)``), never via ``from .config import load_config``:
a from-import binds the value at import time, so a test monkeypatching
``jasper.multiroom.config.load_config`` neither reaches the captured binding
nor undoes it at teardown (#1270, #1678).
"""
