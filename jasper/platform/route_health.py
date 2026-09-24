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

# Counters whose nonzero change means the route glitched during a window: stable
# paths, and fan-in lane counters matched under any lane index since lanes
# reorder with the topology. Every name is cross-checked against the Rust STATUS
# serializers by tests/test_usbsink_impulse_tap_contract.py.
# The USB latency harness's set: outputd's content and DAC xruns, and each lane's
# xruns and USB-resampler unlock/silence/overrun.
KNOWN_HEALTH_COUNTER_PATHS: tuple[tuple[str, ...], ...] = (
    ("outputd", "content", "xrun_count"),
    ("outputd", "dac", "xrun_count"),
)
KNOWN_HEALTH_COUNTER_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("fanin", "inputs", "xrun_count"),
    ("fanin", "inputs", "resampler", "unlock_count"),
    ("fanin", "inputs", "resampler", "silence_frames"),
    ("fanin", "inputs", "resampler", "overrun_frames"),
)
# A measurement take's set: only real playback faults. Not the resampler's
# silence_frames, which grows on an idle USB direct lane.
TAKE_FAULT_COUNTER_PATHS: tuple[tuple[str, ...], ...] = (
    ("outputd", "dac", "xrun_count"),
    ("outputd", "shm_ring", "empty_reads"),
    ("outputd", "shm_ring", "reader_resyncs"),
)
TAKE_FAULT_COUNTER_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("fanin", "inputs", "xrun_count"),
    ("fanin", "inputs", "catchup_events"),
    ("fanin", "inputs", "resampler", "unlock_count"),
    ("fanin", "inputs", "resampler", "overrun_frames"),
)


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


def known_counter_deltas(
    deltas: Mapping[str, float], *,
    paths: tuple[tuple[str, ...], ...] = TAKE_FAULT_COUNTER_PATHS,
    suffixes: tuple[tuple[str, ...], ...] = TAKE_FAULT_COUNTER_SUFFIXES,
) -> dict[str, float]:
    """The health counters among :func:`numeric_deltas`' output, a take's by
    default: each stable path, zero when it did not move, and every lane
    counter that moved."""
    known = {".".join(path): deltas.get(".".join(path), 0.0) for path in paths}
    for key, delta in deltas.items():
        parts = key.split(".")
        if len(parts) > 3 and parts[2].isdigit() and (*parts[:2], *parts[3:]) in suffixes:
            known[key] = delta
    return known
