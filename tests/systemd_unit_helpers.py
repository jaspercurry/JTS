# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small parsers shared by tests that inspect checked-in systemd units."""

from __future__ import annotations

import math
import re
import shlex

# Longest suffix first: `min` must win over `m`, and `ms` over `s`.
_TIME_SPAN_SCALE_SEC: tuple[tuple[str, float], ...] = (
    ("min", 60.0),
    ("ms", 0.001),
    ("h", 3600.0),
    ("m", 60.0),
    ("s", 1.0),
)


def _assigned_values(unit_text: str, key: str) -> list[str]:
    """Return every logical value assigned to ``key`` in file order."""
    logical_text = re.sub(r"\\\s*\n\s*", " ", unit_text)
    values: list[str] = []
    for line in logical_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")):
            continue
        name, separator, candidate = stripped.partition("=")
        if separator and name.strip() == key:
            values.append(candidate.strip())
    return values


def value_for(unit_text: str, key: str) -> str | None:
    """Return the last value assigned to a scalar ``Key=Value`` directive."""
    values = _assigned_values(unit_text, key)
    return values[-1] if values else None


def _unit_label(unit_text: str) -> str:
    """Name the unit from its own text, so a parser fault says which one."""
    description = value_for(unit_text, "Description")
    return f"the unit {description!r}" if description else "an undescribed unit"


def seconds_for(unit_text: str, key: str, default: float | None = None) -> float:
    """Return a time-span directive in seconds.

    systemd reads both ``0`` and ``infinity`` as "no timeout", so either answers
    ``math.inf``. An empty assignment resets the directive to the manager
    default, which is also what an absent key leaves in force, so both answer
    ``default``. ``default=None`` marks the key required: arithmetic callers get
    a diagnostic naming the key and the unit rather than a ``None`` fault.
    """
    raw = value_for(unit_text, key)
    if not raw:
        if default is None:
            raise AssertionError(
                f"{key}= is unset in {_unit_label(unit_text)}; pass a default "
                "when the manager default is the intended value"
            )
        return default
    if raw == "infinity":
        return math.inf
    magnitude, scale = raw, 1.0
    for suffix, suffix_scale in _TIME_SPAN_SCALE_SEC:
        if raw.endswith(suffix):
            magnitude, scale = raw[: -len(suffix)], suffix_scale
            break
    try:
        seconds = float(magnitude) * scale
    except ValueError:
        raise AssertionError(
            f"{key}={raw} in {_unit_label(unit_text)} is not a time span this "
            "parser understands (a bare integer, or ms/s/m/min/h)"
        ) from None
    return math.inf if seconds == 0.0 else seconds


def assignments_for(unit_text: str, key: str) -> tuple[str, ...]:
    """Resolve an accumulating directive while preserving each raw value."""
    values: list[str] = []
    for assigned in _assigned_values(unit_text, key):
        if not assigned:
            values.clear()
        else:
            values.append(assigned)
    return tuple(values)


def values_for(unit_text: str, key: str) -> tuple[str, ...]:
    """Resolve an accumulating, whitespace-tokenized systemd directive.

    Repeated non-empty assignments append values. An empty assignment resets
    the list, matching systemd's list-directive semantics.
    """
    values: list[str] = []
    for assigned in assignments_for(unit_text, key):
        try:
            values.extend(shlex.split(assigned))
        except ValueError:
            values.extend(assigned.split())
    return tuple(values)


def pulled_ordered_dependencies(unit_text: str) -> set[str]:
    """Units a synchronous start of this unit must also drive to terminal.

    A requirement dependency joins the same job transaction, and the ordering
    edge makes PID 1 hold this unit's start job until that dependency reports
    terminal. Type is deliberately not filtered: a long-running dependency that
    happens to be inactive is waited on exactly like a oneshot that is inactive
    by design.
    """
    pulled: set[str] = set()
    for key in ("Wants", "Requires", "BindsTo"):
        pulled.update(values_for(unit_text, key))
    return set(values_for(unit_text, "After")) & pulled


def never_stays_complete(unit_text: str) -> bool:
    """True when a unit is inactive between runs, so every start re-runs it."""
    return (
        value_for(unit_text, "Type") == "oneshot"
        and (value_for(unit_text, "RemainAfterExit") or "no") == "no"
    )
