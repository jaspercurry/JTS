# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Recording stand-in for `AudioCueManager`, so the REAL `_play_cue` path
runs end to end in daemon tests.

Covers every method `WakeLoop` / `AssistantOutput` reach: `play()` (wake
path), `speak_text_guarded()` (dynamic text), `attach_tts()` /
`prerender_text()` (the hooks `run()` wires), `note_skipped()` (the output
gates' refusals) and `snapshot()` (the `/state` cue-delivery block in
`session_status()`). The health record is a real manager, so the snapshot
this spy publishes is the shape and the arithmetic production uses.
"""
from __future__ import annotations

from typing import Any, Callable

from jasper.cues.manager import AudioCueManager, OUTCOME_DELIVERED, REASON_OK


class SpyCues:
    def __init__(self) -> None:
        self.played: list[str] = []
        self.spoken: list[str] = []
        self.skipped: list[tuple[str, str]] = []
        self._health = AudioCueManager(
            sounds_dir="/nonexistent", hostname="jts.local", voice="spy",
        )

    def attach_tts(self, _tts: Any) -> None:
        return None

    async def prerender_text(self, _text: str) -> bool:
        return True

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        self._health._record(OUTCOME_DELIVERED, REASON_OK, slug)
        return True

    async def speak_text_guarded(
        self, text: str, should_play: Callable[[], bool],
    ) -> bool:
        if not should_play():
            return False
        self.spoken.append(text)
        self._health._record(OUTCOME_DELIVERED, REASON_OK, "text")
        return True

    def note_skipped(self, reason: str, slug: str) -> None:
        self.skipped.append((reason, slug))
        self._health.note_skipped(reason, slug)

    def snapshot(self) -> dict[str, Any]:
        return self._health.snapshot()
