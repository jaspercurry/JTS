# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Task ownership and cancellation-safe cleanup for jasper-voice."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger("jasper.voice_daemon")


async def await_cleanup_owned(
    operation: Coroutine,
    *,
    task_name: str,
) -> None:
    """Defer repeated caller cancellation until one cleanup completes."""
    cleanup = asyncio.create_task(operation, name=task_name)
    deferred_cancel = False
    current = asyncio.current_task()
    while not cleanup.done():
        try:
            await asyncio.wait({cleanup})
        except asyncio.CancelledError:
            if current is None or current.cancelling() == 0:
                break
            deferred_cancel = True
            current.uncancel()
    if cleanup.cancelled():
        raise asyncio.CancelledError
    error = cleanup.exception()
    if error is not None:
        if deferred_cancel:
            raise asyncio.CancelledError from None
        raise error
    if deferred_cancel:
        raise asyncio.CancelledError


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
