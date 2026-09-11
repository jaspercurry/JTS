# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A configurable LiveTurn and an 80 ms silent mic frame for voice-daemon tests."""

from __future__ import annotations

from typing import AsyncIterator

import numpy as np

from jasper.voice.session import AudioOutChunk, TurnCapture, TurnUsage

#: One mic frame as the wake loop sees it — `MicCapture.OUTPUT_FRAME_SAMPLES`
#: at 16 kHz mono int16, i.e. 80 ms.
_FRAME_SAMPLES = 1280


def silent_frame() -> np.ndarray:
    return np.zeros(_FRAME_SAMPLES, dtype=np.int16)


async def drain_audio_chunks(turn) -> list[AudioOutChunk]:
    """Every chunk a turn plays out before its terminal sentinel."""
    return [chunk async for chunk in turn.audio_out_chunks()]


class FakeLiveTurn:
    owns_interruption = False
    continuous_input = False

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

    def end_input_at(self) -> float:
        return 0.0

    async def send_audio(self, _pcm_16khz_int16: bytes) -> None:
        self.send_audio_calls += 1

    def discard_input(self) -> None:
        return None

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

    def audio_dropped_bytes(self) -> int:
        # No playout queue here, so nothing can overflow one.
        return 0

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

    def server_turn_complete(self) -> bool:
        return False

    async def wait_for_interrupt(self) -> None:
        return None

    def clear_interrupted(self) -> None:
        return None

    def _on_connection_lost(self) -> None:
        return None

    async def send_text_context(self, text: str) -> None:
        return None

    async def audio_out(self) -> AsyncIterator[bytes]:
        return
        yield  # pragma: no cover

    async def audio_out_chunks(self) -> AsyncIterator[AudioOutChunk]:
        return
        yield  # pragma: no cover

    def request_local_interrupt(self) -> None:
        return None

    def drop_pending_audio(self) -> int:
        return 0

    def audio_chunks_pending(self) -> int:
        return 0

    async def cancel_response(self, reason: str) -> None:
        return None

    async def truncate_assistant_audio(
        self, provider_item_id: str | None, audio_played_ms: int,
    ) -> None:
        return None
