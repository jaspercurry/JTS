# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared stand-ins for voice-daemon lifecycle tests: a configurable
LiveTurn, the 80 ms silent mic frame those tests feed the wake loop, and a
session_status() prep helper."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from jasper.voice.session import TurnCapture, TurnUsage

#: One mic frame as the wake loop sees it — `MicCapture.OUTPUT_FRAME_SAMPLES`
#: at 16 kHz mono int16, i.e. 80 ms.
_FRAME_SAMPLES = 1280


def silent_frame() -> np.ndarray:
    return np.zeros(_FRAME_SAMPLES, dtype=np.int16)


class FakeLiveTurn:
    def __init__(
        self,
        user_text: str | None = None,
        assistant_text: str | None = None,
        *,
        metadata: dict | str | None = None,
        bytes_sent: int = 0,
        chunks_received: int = 0,
    ) -> None:
        self.end_input_calls = 0
        self.release_calls = 0
        self.send_audio_calls = 0
        self._user_text = user_text
        self._assistant_text = assistant_text
        self._metadata = metadata
        self._bytes_sent = bytes_sent
        self._chunks_received = chunks_received

    def last_chunk_at(self) -> float:
        return 0.0

    def last_activity_at(self) -> float:
        return 0.0

    async def send_audio(self, _pcm_16khz_int16: bytes) -> None:
        self.send_audio_calls += 1

    async def end_input(self) -> None:
        self.end_input_calls += 1

    async def release(self) -> None:
        self.release_calls += 1

    def usage(self) -> TurnUsage:
        return TurnUsage()

    def bytes_sent(self) -> int:
        return self._bytes_sent

    def chunks_received(self) -> int:
        return self._chunks_received

    def turn_lost(self) -> bool:
        return False

    def capture(self) -> TurnCapture | None:
        if (
            self._user_text is None
            and self._assistant_text is None
            and self._metadata is None
        ):
            return None
        return TurnCapture(
            user_text=self._user_text,
            assistant_text=self._assistant_text,
            data=self._metadata,
        )


def _prep_session_status(wl) -> None:
    """Seed wl._state, _input_ended, _ducker, and _content_activity so session_status() can run."""
    from jasper.voice_daemon import State
    wl._state = State.WAKE
    wl._input_ended = False
    wl._ducker = MagicMock()
    wl._ducker.is_ducked = False
    wl._content_activity = MagicMock()
    wl._content_activity.music_dbfs = -32.0
