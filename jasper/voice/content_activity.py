# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cached playback activity for voice telemetry; owns its polling task."""

from __future__ import annotations

import asyncio

from ..camilla import CamillaController

CONTENT_ACTIVITY_POLL_SEC = 1.0
CONTENT_ACTIVITY_THRESHOLD_DBFS = -55.0


class ContentActivityTracker:
    """Cheap observer for music/activity telemetry.

    It never sets TTS gain. Outputd owns the final assistant loudness
    decision; this tracker only keeps a recent best-effort playback RMS
    value for ``/state`` and the wake-event columns.
    """

    def __init__(
        self,
        camilla: CamillaController,
        *,
        threshold_dbfs: float = CONTENT_ACTIVITY_THRESHOLD_DBFS,
    ) -> None:
        self._camilla = camilla
        self._threshold_dbfs = float(threshold_dbfs)
        self._last_dbfs: float | None = None
        self._paused = False
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def music_dbfs(self) -> float | None:
        return self._last_dbfs

    def music_is_playing(self) -> bool:
        return self._last_dbfs is not None and self._last_dbfs > self._threshold_dbfs

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def refresh_now(self) -> float | None:
        if self._paused:
            return self._last_dbfs
        rms_pair = await self._camilla.get_playback_rms(best_effort=True)
        if rms_pair is None:
            return self._last_dbfs
        self._last_dbfs = max(rms_pair)
        return self._last_dbfs

    async def start(self) -> None:
        await self.refresh_now()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(CONTENT_ACTIVITY_POLL_SEC)
            except asyncio.CancelledError:
                return
            if self._paused:
                continue
            await self.refresh_now()
