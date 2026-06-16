"""Wizard-owned SSOT for which voice tools the household has DISABLED.

Single key in /var/lib/jasper/tool_state.env:

    JASPER_DISABLED_TOOLS=spotify_play,gmail_unread_summary

FAIL-SAFE toward MORE functionality, mirroring mic_mute_persistence:
a missing, unreadable, or malformed file resolves to "nothing disabled"
(every tool ON). A disabled tool simply does not register — the model
never sees it. This is the user's explicit choice, NOT a failure, so no
audible cue (contrast mic mute / wake-blocking paths).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/var/lib/jasper/tool_state.env"
_KEY = "JASPER_DISABLED_TOOLS"


def read_disabled_tools(path: str | os.PathLike = DEFAULT_PATH) -> frozenset[str]:
    """Return the set of tool names the user turned OFF. Empty set
    (= nothing disabled) on missing/unreadable/malformed file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError (not an OSError) covers a non-UTF-8/corrupt
        # file — exactly the FS-corruption class the fail-safe exists for.
        logger.warning(
            "tool_state: read %s failed (%s) — treating as none disabled", p, e,
        )
        return frozenset()
    names: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != _KEY:
            continue
        v = value.strip().strip('"').strip("'")
        names = {n.strip() for n in v.split(",") if n.strip()}
        break
    return frozenset(names)


def write_disabled_tools(
    path: str | os.PathLike,
    names: "frozenset[str] | set[str] | list[str]",
) -> None:
    """Atomically write the disabled-set as a comma-joined sorted list.
    Mode 0644 (no secret; jasper-doctor + non-root readers inspect it).
    Best-effort: raises OSError on hard failure (caller decides policy)."""
    ordered = ",".join(sorted({n.strip() for n in names if n.strip()}))
    atomic_write_text(Path(path), f"{_KEY}={ordered}\n", mode=0o644)
