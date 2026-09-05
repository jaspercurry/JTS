# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared stand-ins for voice-daemon lifecycle tests: a configurable
LiveTurn, and the 80 ms silent mic frame those tests feed the wake loop."""

from __future__ import annotations

import numpy as np

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

    def usage_tokens(self) -> dict[str, int]:
        return {"input_tokens": 0, "output_tokens": 0}

    def usage_breakdown(self) -> None:
        return None

    def bytes_sent(self) -> int:
        return self._bytes_sent

    def chunks_received(self) -> int:
        return self._chunks_received

    def turn_lost(self) -> bool:
        return False

    def user_transcript(self) -> str | None:
        return self._user_text

    def assistant_transcript(self) -> str | None:
        return self._assistant_text

    def conversation_metadata(self) -> dict | str | None:
        return self._metadata
