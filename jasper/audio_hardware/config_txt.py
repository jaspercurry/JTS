# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``config.txt`` parse/render primitives shared by the boot-config owners.

Pulled out of ``usb_port_role.py`` (ADR-0235 PR 6): the section-scoping and
``[all]``-header healing rules apply equally to the USB data-role's managed
block and the I2S HAT's managed block, so both import from here rather than
duplicating the parser.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


DEFAULT_BOOT_CONFIG_PATH = "/boot/firmware/config.txt"

_OVERLAY_LINE_RE = re.compile(
    r"^\s*dtoverlay\s*=\s*([^,\s#]+)",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")


def _global_or_all_lines(content: str) -> tuple[str, ...]:
    """Return directives that apply globally or under the final ``[all]``.

    JTS owns its role and registered DAC overlays in that portable scope.  A
    carrier-specific block such as ``[cm5]`` must not affect a Zero merely
    because it appears in the same config file.
    """

    section = "global"
    out: list[str] = []
    for line in content.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1).strip().lower()
            continue
        if section in {"global", "all"}:
            out.append(line)
    return tuple(out)


def _overlay_values(lines: Iterable[str]) -> set[str]:
    overlays: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _OVERLAY_LINE_RE.match(line)
        if match:
            overlays.add(match.group(1).lower())
    return overlays


def overlay_declared_anywhere(content: str, overlay: str) -> bool:
    """Whether ``dtoverlay=<overlay>`` appears under ANY section, unlike
    :func:`configured_i2s_overlays`'s global/``[all]``-only view.

    The boot-config doctor check's fallback: a model-scoped line (say
    ``[pi5]``) must not false-FAIL a check whose only job is catching the
    line vanishing from the file entirely (#2575).
    """
    return overlay.lower() in _overlay_values(content.splitlines())


def boot_config_path() -> Path:
    """``config.txt``'s resolved path, honoring ``JTS_BOOT_CONFIG_FILE``."""

    return Path(os.environ.get("JTS_BOOT_CONFIG_FILE", DEFAULT_BOOT_CONFIG_PATH))


def read_boot_config_or_none(path: str | Path | None = None) -> str | None:
    """``config.txt``'s content, or ``None`` if it could not be read.

    Unlike :func:`~.text_property.read_text_property` (whose callers treat a
    transient USB-port-role read as equivalent to "not configured"), a caller
    confirming a saved line is genuinely GONE — not merely unreadable right
    now — needs that distinction kept, so this neither collapses the error to
    "" nor strips surrounding whitespace from the file it returns.
    """
    target = Path(path) if path is not None else boot_config_path()
    try:
        return target.read_text(encoding="utf-8").replace("\x00", "")
    except OSError:
        return None


def _collapse_empty_all_sections(content: str) -> str:
    """Collapse adjacent bare ``[all]`` headers separated only by blank lines.

    ``render_boot_config`` and ``render_i2s_hat_boot_config`` each append a
    fresh ``[all]`` header when the file doesn't already end in one, then
    strip only their own block's directives on the next pass -- leaving the
    now-empty header behind. Two (or more) adjacent ``[all]`` headers with
    nothing but blank lines between them are equivalent to one, so this
    heals both new growth and an already-bloated file in one pass.

    Only a header with no trailing comment is treated as droppable: a line
    like ``[all]  # keep me`` is never our own emission (the writer always
    emits a bare ``[all]``), so it and any comment it carries are left
    untouched rather than silently merged away.
    """
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().lower() == "[all]":
            cursor = index + 1
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            if cursor < len(lines) and lines[cursor].strip().lower() == "[all]":
                index = cursor
                continue
        output.append(line)
        index += 1
    return "".join(output)
