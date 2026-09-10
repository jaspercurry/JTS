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
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
