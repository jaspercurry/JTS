"""Wizard-owned SSOT for user-edited tool prompts.

Stored as JSON under /var/lib/jasper because prompts are multi-line text,
not env-style scalar config. Fail-safe: missing/unreadable/malformed files
resolve to no overrides, so code defaults remain authoritative.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/var/lib/jasper/tool_prompt_overrides.json"


def read_prompt_overrides(
    path: str | os.PathLike = DEFAULT_PATH,
) -> dict[str, str]:
    """Return tool name -> model-facing prompt override.

    Empty dict on missing/unreadable/malformed file. Blank override values are
    ignored; reset is represented by deleting the key.
    """
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning(
            "tool_prompt_overrides: read %s failed (%s) — using defaults", p, e,
        )
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "tool_prompt_overrides: %s has unexpected shape — using defaults", p,
        )
        return {}
    return {
        str(name): value
        for name, value in raw.items()
        if isinstance(name, str) and isinstance(value, str) and value.strip()
    }


def write_prompt_overrides(
    path: str | os.PathLike,
    overrides: dict[str, str],
) -> None:
    """Atomically write prompt overrides. Mode 0644: prompt text is not a
    secret, and doctor/control surfaces may inspect it."""
    cleaned = {
        str(name).strip(): str(value)
        for name, value in overrides.items()
        if str(name).strip() and isinstance(value, str) and value.strip()
    }
    atomic_write_text(
        Path(path),
        json.dumps(cleaned, indent=2, sort_keys=True) + "\n",
        mode=0o644,
    )
