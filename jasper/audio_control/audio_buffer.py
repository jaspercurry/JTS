# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered input with an explicit oldest-first drop boundary."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any


# 250 × 80 ms = 20 s, about 640 KB of mono int16 PCM. Acquisition may
# outlast a whole command; its frozen pre-roll is kept outside this buffer.
ACQUIRE_BUFFER_MAX_FRAMES = 250
ACQUIRE_BUFFER_MAX_AGE_SEC = 20.0


@dataclass(slots=True)
class InputFrame:
    pcm: Any
    captured_at: float
    discontinuity: bool = False


class AudioBuffer:
    def __init__(
        self, max_frames: int = ACQUIRE_BUFFER_MAX_FRAMES,
        max_age_sec: float = ACQUIRE_BUFFER_MAX_AGE_SEC,
    ) -> None:
        self._frames: deque[InputFrame] = deque()
        self._max_frames = max_frames
        self._max_age_sec = max_age_sec
        self.dropped_frames = 0
        self._gap_pending = False

    def __len__(self) -> int:
        return len(self._frames)

    def clear(self) -> None:
        self._frames.clear()
        self._gap_pending = False

    def append(
        self, pcm, captured_at: float | None = None, *, discontinuity: bool = False,
    ) -> None:
        if len(self._frames) == self._max_frames:
            self._drop()
        self._frames.append(InputFrame(
            pcm, time.monotonic() if captured_at is None else captured_at,
            discontinuity,
        ))

    def extend(self, frames) -> None:
        for frame in frames:
            self.append(frame)

    def _drop(self) -> None:
        self._frames.popleft()
        self.dropped_frames += 1
        self._gap_pending = True

    def pop(self) -> InputFrame | None:
        now = time.monotonic()
        while self._frames and now - self._frames[0].captured_at > self._max_age_sec:
            self._drop()
        if not self._frames:
            return None
        frame = self._frames.popleft()
        frame.discontinuity |= self._gap_pending
        self._gap_pending = False
        return frame
