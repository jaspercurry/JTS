# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fire-and-forget task bookkeeping shared by jasper-voice's owners.

A tracked task removes itself from its set when it finishes and logs an
unhandled exception instead of dropping it on the floor; cancelling a set
awaits every member so a teardown cannot outrun the work it cancelled.
Both the daemon's startup tasks and `WakeLoop`'s per-turn background work
use the same pair.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("jasper.voice_daemon")


def track_task(
    task: asyncio.Task,
    task_set: set[asyncio.Task],
    *,
    label: str,
) -> asyncio.Task:
    task_set.add(task)

    def _discard(done: asyncio.Task) -> None:
        task_set.discard(done)
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.warning(
                "fire-and-forget task %s failed: %s",
                label,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_discard)
    return task


async def cancel_tracked_tasks(task_set: set[asyncio.Task]) -> None:
    tasks = list(task_set)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    task_set.difference_update(tasks)
