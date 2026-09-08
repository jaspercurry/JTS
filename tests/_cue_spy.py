# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Recording stand-in for `AudioCueManager`, so the REAL `_play_cue` path
runs end to end in daemon tests.

Covers the whole surface `WakeLoop` reaches: `play()` (wake path),
`attach_tts()` / `prerender_text()` (the hooks `run()` wires), and
`snapshot()` (the `/state` cue-delivery block in `session_status()`).
"""
from __future__ import annotations

from typing import Any

from jasper.cues.manager import _OUTCOMES


class SpyCues:
    def __init__(self) -> None:
        self.played: list[str] = []

    def attach_tts(self, _tts: Any) -> None:
        return None

    async def prerender_text(self, _text: str) -> bool:
        return True

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"counts": {outcome: 0 for outcome in _OUTCOMES}, "last": None}
