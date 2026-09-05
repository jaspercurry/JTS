# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared pyalsaaudio PCM double for the wired-capture and wired-level-meter
suites."""

from __future__ import annotations

import struct
import time


def frames_bytes(values):
    """Interleaved S32_LE frames: ``values`` is [(ch0, ch1), ...]."""
    return b"".join(struct.pack("<ii", a, b) for a, b in values)


class FakePcm:
    """Deterministic capture PCM: a scripted sequence of read results.

    Each script step is ``(frames, values)`` for a good read, the string
    ``"overrun"`` for a −EPIPE read, or ``"empty"`` — pyalsaaudio semantics.
    After the script, reads block briefly and return silence so the reader
    keeps running until stopped.
    """

    def __init__(self, script, *, idle_frames=64):
        self._script = list(script)
        self._idle_frames = idle_frames
        self.closed = False

    def read(self):
        if self._script:
            step = self._script.pop(0)
            if step == "overrun":
                return -32, b""
            if step == "empty":
                return 0, b""
            frames, values = step
            return frames, frames_bytes(values)
        # Idle: keep delivering silence at a real-ish cadence.
        time.sleep(0.001)
        return self._idle_frames, frames_bytes([(0, 0)] * self._idle_frames)

    def close(self):
        self.closed = True
