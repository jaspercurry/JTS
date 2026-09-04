# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared ``fanin.env`` staging for tests exercising ``jasper.fanin.ring_health``."""

from __future__ import annotations

from pathlib import Path


def declare_fanin_env(monkeypatch, tmp_path: Path, text: str) -> Path:
    """Write ``text`` to a tmp ``fanin.env`` and point ring_health at it."""
    path = tmp_path / "fanin.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr("jasper.fanin.ring_health.FANIN_ENV_PATH", str(path))
    return path
