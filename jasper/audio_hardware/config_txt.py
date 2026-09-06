# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``config.txt`` parse/render primitives shared by the USB data-role and I2S
HAT managed blocks (ADR-0235).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Literal


DEFAULT_BOOT_CONFIG_PATH = "/boot/firmware/config.txt"

BoardUsbTopology = Literal[
    "shared_otg_port",
    "separate_host_ports",
    "unsupported",
]
UsbDataRole = Literal["host", "peripheral", "unknown"]

MANAGED_BLOCK_BEGIN = "# BEGIN JTS USB DATA ROLE"
MANAGED_BLOCK_END = "# END JTS USB DATA ROLE"

_OVERLAY_LINE_RE = re.compile(
    r"^\s*dtoverlay\s*=\s*([^,\s#]+)",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
_ROLE_LINE_RE = re.compile(
    r"^\s*dtoverlay\s*=\s*dwc2\s*,\s*dr_mode\s*=\s*(host|peripheral)\s*(?:#.*)?$",
    re.IGNORECASE,
)
_LEGACY_COMMENT_START = "# JTS install — required for the composite USB gadget"


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


def configured_usb_role(content: str) -> UsbDataRole:
    role: UsbDataRole = "unknown"
    for line in _global_or_all_lines(content):
        match = _ROLE_LINE_RE.match(line)
        if match:
            role = match.group(1).lower()  # type: ignore[assignment]
    return role


def _dwc2_parameters(line: str) -> tuple[str, ...] | None:
    directive = line.split("#", 1)[0].strip()
    key, separator, value = directive.partition("=")
    if not separator or key.strip().lower() != "dtoverlay":
        return None
    parts = tuple(part.strip() for part in value.split(","))
    if not parts or parts[0].lower() != "dwc2":
        return None
    return parts[1:]


def _removable_dwc2_line(line: str) -> bool:
    parameters = _dwc2_parameters(line)
    if parameters is None:
        return False
    if not parameters:
        return True
    if len(parameters) == 1:
        key, separator, value = parameters[0].partition("=")
        if (
            separator
            and key.strip().lower() == "dr_mode"
            and value.strip().lower() in {"host", "peripheral"}
        ):
            return True
    raise ValueError(
        "ambiguous global/[all] dwc2 overlay; remove unsupported parameters "
        "before JTS reconciles the USB data role"
    )


def _without_legacy_comment(content: str) -> str:
    """Remove only the contiguous comment paragraph from the old installer.

    The old role directive itself is removed by the ordinary DWC2 pass.  This
    deliberately refuses a DOTALL pattern: intervening hardware directives
    can never become part of a migration match.
    """

    lines = content.splitlines(keepends=True)
    remove: set[int] = set()
    for index, line in enumerate(lines):
        if not line.strip().startswith(_LEGACY_COMMENT_START):
            continue
        cursor = index
        while cursor < len(lines) and lines[cursor].lstrip().startswith("#"):
            cursor += 1
        if cursor >= len(lines) or _SECTION_RE.match(lines[cursor]) is None:
            continue
        section = _SECTION_RE.match(lines[cursor])
        if section is None or section.group(1).strip().lower() != "all":
            continue
        role_index = cursor + 1
        while role_index < len(lines) and not lines[role_index].strip():
            role_index += 1
        if role_index < len(lines) and _ROLE_LINE_RE.match(lines[role_index]):
            remove.update(range(index, cursor))
    return "".join(line for index, line in enumerate(lines) if index not in remove)


def _without_managed_role_lines(content: str) -> str:
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    section = "global"
    in_managed_block = False
    for line in lines:
        if line.strip() == MANAGED_BLOCK_BEGIN:
            if in_managed_block:
                raise ValueError("nested JTS USB data-role block")
            in_managed_block = True
            continue
        if in_managed_block:
            if line.strip() == MANAGED_BLOCK_END:
                in_managed_block = False
            elif (
                line.strip()
                and not line.lstrip().startswith("#")
                and not _removable_dwc2_line(line)
            ):
                raise ValueError(
                    "unexpected directive inside JTS USB data-role block"
                )
            continue
        if line.strip() == MANAGED_BLOCK_END:
            raise ValueError("JTS USB data-role block ends without a beginning")
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1).strip().lower()
            output.append(line)
            continue
        if section in {"global", "all"} and _removable_dwc2_line(line):
            continue
        output.append(line)
    if in_managed_block:
        raise ValueError("JTS USB data-role block is missing its end marker")
    return _collapse_empty_all_sections("".join(output))


def render_boot_config(content: str, desired_role: UsbDataRole) -> str:
    if desired_role == "unknown":
        return content
    cleaned = _without_legacy_comment(content)
    cleaned = _without_managed_role_lines(cleaned).rstrip()
    purpose = (
        "reserve the shared OTG port for output-DAC host mode"
        if desired_role == "host"
        else "enable the composite USB gadget on the OTG-capable port"
    )
    last_line = cleaned.splitlines()[-1].strip().lower() if cleaned else ""
    section_prefix = "" if last_line == "[all]" else "[all]\n"
    separator = "\n" if last_line == "[all]" else "\n\n"
    block = section_prefix + (
        f"{MANAGED_BLOCK_BEGIN}\n"
        f"# JTS hardware reconciliation: {purpose}.\n"
        "# Generated from board topology + registered DAC overlay; do not edit.\n"
        f"dtoverlay=dwc2,dr_mode={desired_role}\n"
        f"{MANAGED_BLOCK_END}\n"
    )
    return f"{cleaned}{separator}{block}" if cleaned else block
