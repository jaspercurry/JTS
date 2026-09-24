# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The playback route's STATUS surfaces, fan-in and outputd, snapshotted and diffed."""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .status_socket import FANIN_STATUS_SOCKET, OUTPUTD_STATUS_SOCKET, read_status_socket_or_none

ROUTE_SURFACES = ("fanin", "outputd")

# Numeric leaves that change between any two snapshots by design, not counters.
_IGNORED_DELTA_LEAF_KEYS = frozenset({"captured_at_monotonic_ns", "uptime_seconds"})


def snapshot_route_health() -> dict[str, Any]:
    """Fan-in's and outputd's STATUS; a surface whose daemon is unreachable is ``None``."""
    return {
        "captured_at_monotonic_ns": time.monotonic_ns(),
        "fanin": read_status_socket_or_none(FANIN_STATUS_SOCKET, event="route_health.snapshot_unavailable"),
        "outputd": read_status_socket_or_none(OUTPUTD_STATUS_SOCKET, event="route_health.snapshot_unavailable"),
    }


def numeric_deltas(before: Any, after: Any, *, prefix: tuple[str, ...] = ()) -> dict[str, float]:
    """``{"a.b.c": after - before}`` for every numeric leaf both trees hold that changed.

    Generic on purpose: the daemons add counters independently, and a fixed
    list would stop reporting a new one. Lists recurse by index
    (``fanin.inputs.0.xrun_count``), because fan-in keeps its per-lane
    counters in its ``inputs`` array. A key or list element on one side only
    is skipped: a schema or topology change is not a counter tick.
    """
    deltas: dict[str, float] = {}
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) & set(after)):
            deltas.update(numeric_deltas(before[key], after[key], prefix=(*prefix, str(key))))
        return deltas
    if isinstance(before, list) and isinstance(after, list):
        for i in range(min(len(before), len(after))):
            deltas.update(numeric_deltas(before[i], after[i], prefix=(*prefix, str(i))))
        return deltas
    if prefix and prefix[-1] in _IGNORED_DELTA_LEAF_KEYS:
        return deltas
    numeric = (int, float)
    if (isinstance(before, numeric) and not isinstance(before, bool)
            and isinstance(after, numeric) and not isinstance(after, bool) and after != before):
        deltas[".".join(prefix)] = float(after) - float(before)
    return deltas
