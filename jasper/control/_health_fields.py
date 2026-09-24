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
a dashboard field that may simply be absent needs. ``read_text_file`` and
``read_int_file`` apply the same rule to a small /proc or /sys file.

Also the shared home for ``MONITOR_ERRORS``, the fail-soft exception tuple
every observability probe across the audio-health split degrades on, and for
``RESTART_REMEDY``/``DIAGNOSTICS_REMEDY``, the two household remedy sentences
several leaves splice into their own text -- for the same downward-only
reason: two leaves (e.g. the composer and a source/timing card) must share
the constant without importing each other.
"""

from __future__ import annotations

from typing import Any

from jasper.json_fields import as_mapping

# Expected failures at optional/cached observability boundaries. Programming
# errors outside this set should not be hidden; a dead sampler is surfaced as
# stale by snapshot() instead of silently retrying a broken implementation.
MONITOR_ERRORS = (
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


def finite_number(value: Any) -> int | float | None:
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


mapping = as_mapping


def as_int(value: Any, default: int = 0) -> int:
    """``value`` as an ``int``, or ``default`` when it is not one."""
    parsed = as_int_or_none(value)
    return default if parsed is None else parsed


def as_int_or_none(value: Any) -> int | None:
    """``value`` as an ``int``, or ``None`` when it is not one — ``0`` would
    misread as "confirmed zero" rather than "couldn't tell".

    ``bool`` is an ``int`` in Python, so it is rejected here too — a stray
    ``True``/``False`` in untyped JSON must not silently become 1 or 0.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def nonneg_delta(curr: Any, prev: Any) -> int | None:
    """``curr - prev`` when both are ``int`` and non-decreasing, else ``None``."""
    if not isinstance(curr, int) or not isinstance(prev, int) or curr < prev:
        return None
    return curr - prev


def nonneg_rate(curr: Any, prev: Any, dt: float) -> float | None:
    """A monotonic counter's per-second delta, or ``None`` on wrap/reset/absence."""
    delta = nonneg_delta(curr, prev)
    return delta / dt if delta is not None else None


def read_int_file(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_text_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def detail_row(label: str, value: Any) -> dict[str, str]:
    """One dashboard detail row: a fixed label paired with a stringified value."""
    return {"label": label, "value": str(value)}


def duration_label(seconds: float) -> str:
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
