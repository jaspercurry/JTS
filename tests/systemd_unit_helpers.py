# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Small parsers shared by tests that inspect checked-in systemd units."""

from __future__ import annotations


def value_for(unit_text: str, key: str) -> str | None:
    """Return the last value assigned to a scalar ``Key=Value`` directive."""
    value: str | None = None
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")):
            continue
        name, separator, candidate = stripped.partition("=")
        if separator and name.strip() == key:
            value = candidate.strip()
    return value
