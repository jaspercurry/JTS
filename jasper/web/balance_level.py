# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Browser-mic level evidence shared by calibration flows."""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass

DEFAULT_FLOOR_WINDOW_S = 1.5
DEFAULT_TARGET_ABOVE_FLOOR_DB = 15.0
DEFAULT_TARGET_MIN_DB = -55.0
DEFAULT_LOCK_FRAMES = 3
DEFAULT_MIN_FLOOR_FRAMES = 4


def target_db_for_floor(
    floor_dbfs: float,
    *,
    above_floor_db: float = DEFAULT_TARGET_ABOVE_FLOOR_DB,
    minimum_dbfs: float = DEFAULT_TARGET_MIN_DB,
) -> float:
    """Detection target for a measured room floor."""
    return max(float(floor_dbfs) + above_floor_db, minimum_dbfs)


@dataclass(frozen=True)
class FloorEstimate:
    floor_dbfs: float
    target_dbfs: float
    frame_count: int
    window_s: float


@dataclass(frozen=True)
class MicLiveness:
    live: bool
    frame_count: int
    latest_dbfs: float | None
    age_s: float | None


class MicLevelTracker:
    """Retain recent browser meter frames and derive floor/liveness.

    The browser is treated as a measurement peripheral: it reports dBFS frames,
    but the backend decides floors, targets, and locks from server time.
    """

    def __init__(
        self,
        *,
        retain_s: float = 10.0,
        floor_window_s: float = DEFAULT_FLOOR_WINDOW_S,
    ) -> None:
        self._retain_s = float(retain_s)
        self._floor_window_s = float(floor_window_s)
        self._frames: deque[tuple[float, float]] = deque()
        self.frames_seen = 0

    def add(self, dbfs: float, *, now: float | None = None) -> bool:
        """Add one finite dBFS frame. Returns False for invalid input."""
        try:
            value = float(dbfs)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value):
            return False
        t = time.monotonic() if now is None else float(now)
        # Bound pathological browser values without hiding ordinary silence.
        value = max(-160.0, min(20.0, value))
        self._frames.append((t, value))
        self.frames_seen += 1
        self._prune(t)
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self._retain_s
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()

    def floor_estimate(
        self,
        *,
        now: float | None = None,
        min_frames: int = DEFAULT_MIN_FLOOR_FRAMES,
    ) -> FloorEstimate | None:
        """Median floor over the recent floor window, or None if stale."""
        t = time.monotonic() if now is None else float(now)
        self._prune(t)
        cutoff = t - self._floor_window_s
        samples = [db for at, db in self._frames if at >= cutoff]
        if len(samples) < min_frames:
            return None
        floor = float(statistics.median(samples))
        return FloorEstimate(
            floor_dbfs=floor,
            target_dbfs=target_db_for_floor(floor),
            frame_count=len(samples),
            window_s=self._floor_window_s,
        )

    def liveness(
        self,
        *,
        now: float | None = None,
        max_age_s: float = 1.0,
    ) -> MicLiveness:
        t = time.monotonic() if now is None else float(now)
        self._prune(t)
        if not self._frames:
            return MicLiveness(
                live=False,
                frame_count=self.frames_seen,
                latest_dbfs=None,
                age_s=None,
            )
        at, db = self._frames[-1]
        age = max(0.0, t - at)
        return MicLiveness(
            live=age <= max_age_s,
            frame_count=self.frames_seen,
            latest_dbfs=db,
            age_s=age,
        )

