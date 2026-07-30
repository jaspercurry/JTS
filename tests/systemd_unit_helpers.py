# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small parsers shared by tests that inspect checked-in systemd units."""

from __future__ import annotations

import re
import shlex


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
