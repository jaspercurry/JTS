# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Text surface for voice daemon structural guards."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
VOICE_RUNTIME_PATHS = (
    REPO / "jasper" / "voice_daemon.py",
    REPO / "jasper" / "voice" / "prompt.py",
    REPO / "jasper" / "voice" / "earcons.py",
    REPO / "jasper" / "voice" / "turn_playback.py",
    REPO / "jasper" / "voice" / "daemon_main.py",
)


def voice_runtime_text() -> str:
    return "\n".join(path.read_text() for path in VOICE_RUNTIME_PATHS)
