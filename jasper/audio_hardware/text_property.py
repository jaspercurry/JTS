# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read one small board-identity text file, fail-soft.

Devicetree publishes the board model and the fitted HAT's ID EEPROM as
NUL-terminated string files under ``/proc/device-tree``; ``config.txt`` is
ordinary text. Every caller here wants the same thing: absence and
unreadability are ordinary answers about the hardware, not errors to raise.
"""
from __future__ import annotations

from pathlib import Path


def read_text_property(path: str | Path) -> str:
    """Return the file's text, NULs stripped, or "" when it cannot be read."""

    try:
        return Path(path).read_text(encoding="utf-8").replace("\x00", "").strip()
    except OSError:
        return ""
