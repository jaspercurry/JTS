# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Offline output sink shared with paid evals. All acknowledgements are simulated."""
from __future__ import annotations

import asyncio

from tests._wake_loop import FakeTts


class RecordingPlayout(FakeTts):
    """An immediate virtual drain, never evidence of physical or acoustic output."""

    def __init__(self) -> None:
        self._audio = bytearray()
        self._events: list[dict] = []
        self.flush_ack: dict | None = None
        self.drained = False

    @property
    def audio(self) -> bytes:
        return bytes(self._audio)

    async def write_segment(
        self, pcm: bytes, *, provider_item_id=None, segment_kind="assistant", on_first_write=None,
    ) -> bool:
        self._audio.extend(pcm)
        # Each virtual write drains immediately: 24 kHz mono samples map to
        # twice as many 48 kHz ledger frames. This is a simulation assumption.
        frames = len(pcm)
        self._events.append({
            "segment": len(self._events) + 1, "provider_item_id": provider_item_id,
            "kind": segment_kind, "queued_frames": frames, "written_frames": frames,
            "drained_frames": frames, "flushed_frames": 0,
        })
        if on_first_write is not None:
            await on_first_write()
        await asyncio.sleep(0)
        return True

    async def wait_drained(self) -> None:
        self.drained = True

    async def flush(self) -> dict:
        self.flush_ack = {
            "ok": True, "requests": 1, "pending_frames": 0,
            "segments": len(self._events), "flushed_frames": 0,
            "max_audio_played_ms": sum(e["drained_frames"] for e in self._events) // 48,
            "events": self._events.copy(),
        }
        self._events.clear()
        return self.flush_ack
