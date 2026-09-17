# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fail-soft field readers shared by the audio-health composer and the
incident store.

A leaf on purpose: ``audio_incidents`` must not import the composer, so the
two ends of one dashboard payload read its untyped daemon JSON through this
module instead of through each other. Not
:mod:`jasper.json_fields` — that one raises on a bad field and coerces to
``float``; these return ``None`` and keep an ``int`` an ``int``, which is what
a dashboard field that may simply be absent needs.

Also the shared home for ``_MONITOR_ERRORS``, the fail-soft exception tuple
every observability probe across the audio-health split degrades on, and for
``RESTART_REMEDY``/``DIAGNOSTICS_REMEDY``, the two household remedy sentences
several leaves splice into their own text -- for the same downward-only
reason: two leaves (e.g. the composer and a source/timing card) must share
the constant without importing each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Expected failures at optional/cached observability boundaries. Programming
# errors outside this set should not be hidden; a dead sampler is surfaced as
# stale by snapshot() instead of silently retrying a broken implementation.
_MONITOR_ERRORS = (
    AttributeError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)

# Household register for every sentence the audio-health card writes: what is
# wrong with the household's sound and what they can do about it, never a
# daemon name, a unit, a systemd state, or a command (#2472) -- that half
# lives in `jasper-doctor` and `/state.audio_health.technical`. Both remedies
# name buttons on the same /system/ page as the card.
RESTART_REMEDY = "Try Restart audio."
DIAGNOSTICS_REMEDY = "Run diagnostics if sound doesn't come back."


def _finite_number(value: Any) -> int | float | None:
    """One real number out of untyped JSON, unwidened, or ``None``.

    ``bool`` is an ``int`` and a numeric string is something ``float``
    accepts, so both are rejected; an arbitrary-precision ``int`` is legal
    JSON and raises ``OverflowError`` rather than returning ``inf``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    """``value`` when it is an object, else an empty one — so a chain of
    ``.get()`` hops over an absent branch stays a lookup, not a crash."""
    return value if isinstance(value, Mapping) else {}


def _as_int(value: Any, default: int = 0) -> int:
    """``value`` as an ``int``, or ``default`` when it is not one.

    ``bool`` is an ``int`` in Python, so it is rejected here too — a stray
    ``True``/``False`` in untyped JSON must not silently become 1 or 0.
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    """``value`` as a ``float``, or ``None`` when it is not one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int_or_none(value: Any) -> int | None:
    """Like ``_as_int``, but a missing/unparseable value stays ``None``, not
    ``0`` -- ``0`` would misread as "confirmed zero" rather than "couldn't
    tell".
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nonneg_delta(curr: Any, prev: Any) -> int | None:
    """``curr - prev`` when both are ``int`` and non-decreasing, else ``None``."""
    if not isinstance(curr, int) or not isinstance(prev, int) or curr < prev:
        return None
    return curr - prev


def _nonneg_rate(curr: Any, prev: Any, dt: float) -> float | None:
    """A monotonic counter's per-second delta, or ``None`` on wrap/reset/absence."""
    delta = _nonneg_delta(curr, prev)
    return delta / dt if delta is not None else None


def _sum_or_none(block: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    """Sum of the named counters, or ``None`` unless every one of them is present."""
    total = 0
    for key in keys:
        value = _as_int_or_none(block.get(key))
        if value is None:
            return None
        total += value
    return total


def _nonnegative_counter(value: Any) -> int | None:
    """A monotonic counter's current reading, or ``None`` when unreadable.

    A negative value cannot be a counter (they only go up between resets);
    a bare ``float``/``str`` is rejected rather than coerced, since a counter
    field that is not already an ``int`` in the daemon's JSON is corrupt.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _detail(label: str, value: Any) -> dict[str, str]:
    """One dashboard detail row: a fixed label paired with a stringified value."""
    return {"label": label, "value": str(value)}


def _duration_label(seconds: float) -> str:
    """A duration as the dashboard prints it, coarsening as it grows."""
    seconds = max(0.0, seconds)
    if seconds < 1.0:
        return f"{round(seconds * 1000):d} ms"
    if seconds < 60.0:
        return f"{round(seconds):d} sec"
    minutes = int(seconds // 60)
    remainder = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s" if remainder else f"{minutes} min"
    hours = int(minutes // 60)
    return f"{hours}h {minutes % 60}m"
