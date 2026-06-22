# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wizard-owned SSOT for voice tool pack/tool UI state.

Keys in /var/lib/jasper/tool_state.env:

    JASPER_DISABLED_TOOL_PACKS=spotify,google
    JASPER_DISABLED_TOOLS=spotify_play,gmail_unread_summary
    JASPER_ENABLED_SETUP_TOOL_PACKS=home-assistant

FAIL-SAFE toward MORE configured runtime functionality, mirroring
mic_mute_persistence: a missing, unreadable, or malformed file resolves to
"nothing disabled" for tools/packs that can already run. A disabled tool simply
does not register — the model never sees it. This is the user's explicit
choice, NOT a failure, so no audible cue (contrast mic mute / wake-blocking
paths).

The setup-enabled pack set is UI-only intent for optional integrations that
are not configured yet. Unconfigured packs default to off/no nag; when a user
turns one on, the wizard can surface "Needs setup" and its setup page without
registering any runtime tool until configuration actually exists.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/var/lib/jasper/tool_state.env"
_TOOLS_KEY = "JASPER_DISABLED_TOOLS"
_PACKS_KEY = "JASPER_DISABLED_TOOL_PACKS"
_SETUP_PACKS_KEY = "JASPER_ENABLED_SETUP_TOOL_PACKS"


@dataclass(frozen=True)
class ToolState:
    disabled_tools: frozenset[str] = frozenset()
    disabled_packs: frozenset[str] = frozenset()
    setup_enabled_packs: frozenset[str] = frozenset()


def _parse_csv(value: str) -> frozenset[str]:
    v = value.strip().strip('"').strip("'")
    return frozenset(n.strip() for n in v.split(",") if n.strip())


def read_tool_state(path: str | os.PathLike = DEFAULT_PATH) -> ToolState:
    """Return the disabled pack/tool sets. Empty sets (= nothing disabled)
    on missing/unreadable/malformed file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ToolState()
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError (not an OSError) covers a non-UTF-8/corrupt
        # file — exactly the FS-corruption class the fail-safe exists for.
        logger.warning(
            "tool_state: read %s failed (%s) — treating as none disabled", p, e,
        )
        return ToolState()
    tools = frozenset()
    packs = frozenset()
    setup_packs = frozenset()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key == _TOOLS_KEY:
            tools = _parse_csv(value)
        elif key == _PACKS_KEY:
            packs = _parse_csv(value)
        elif key == _SETUP_PACKS_KEY:
            setup_packs = _parse_csv(value)
    return ToolState(
        disabled_tools=tools,
        disabled_packs=packs,
        setup_enabled_packs=setup_packs,
    )


def read_disabled_tools(path: str | os.PathLike = DEFAULT_PATH) -> frozenset[str]:
    """Return the set of tool names the user turned OFF."""
    return read_tool_state(path).disabled_tools


def read_disabled_packs(path: str | os.PathLike = DEFAULT_PATH) -> frozenset[str]:
    """Return the set of catalog pack ids the user turned OFF."""
    return read_tool_state(path).disabled_packs


def read_setup_enabled_packs(
    path: str | os.PathLike = DEFAULT_PATH,
) -> frozenset[str]:
    """Return unconfigured pack ids the user intentionally turned ON."""
    return read_tool_state(path).setup_enabled_packs


def write_tool_state(path: str | os.PathLike, state: ToolState) -> None:
    """Atomically write the disabled pack/tool sets. Mode 0644 (no secret;
    jasper-doctor + non-root readers inspect it)."""
    tools = ",".join(sorted(n.strip() for n in state.disabled_tools if n.strip()))
    packs = ",".join(sorted(n.strip() for n in state.disabled_packs if n.strip()))
    setup_packs = ",".join(
        sorted(n.strip() for n in state.setup_enabled_packs if n.strip())
    )
    lines = []
    if packs:
        lines.append(f"{_PACKS_KEY}={packs}")
    if setup_packs:
        lines.append(f"{_SETUP_PACKS_KEY}={setup_packs}")
    lines.append(f"{_TOOLS_KEY}={tools}")
    atomic_write_text(Path(path), "\n".join(lines) + "\n", mode=0o644)


def write_disabled_tools(
    path: str | os.PathLike,
    names: "frozenset[str] | set[str] | list[str]",
) -> None:
    """Atomically write the disabled-set as a comma-joined sorted list.
    Mode 0644 (no secret; jasper-doctor + non-root readers inspect it).
    Best-effort: raises OSError on hard failure (caller decides policy)."""
    current = read_tool_state(path)
    write_tool_state(
        path,
        ToolState(
            disabled_tools=frozenset(n.strip() for n in names if n.strip()),
            disabled_packs=current.disabled_packs,
            setup_enabled_packs=current.setup_enabled_packs,
        ),
    )


def write_disabled_packs(
    path: str | os.PathLike,
    names: "frozenset[str] | set[str] | list[str]",
) -> None:
    """Atomically write the disabled pack set while preserving tool state."""
    current = read_tool_state(path)
    write_tool_state(
        path,
        ToolState(
            disabled_tools=current.disabled_tools,
            disabled_packs=frozenset(n.strip() for n in names if n.strip()),
            setup_enabled_packs=current.setup_enabled_packs,
        ),
    )
