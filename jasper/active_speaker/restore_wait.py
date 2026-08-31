# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wait for a graph restore to FINISH before a caller's cancellation propagates.

The house idiom, factored out of :mod:`jasper.active_speaker.web_commissioning`
so a caller does not have to import that module's whole commissioning stack to
put a graph back. Start the restore as a TASK, so a cancel aimed at the awaiter
lands on the shield rather than on the restore, and keep waiting through a
repeat cancel. A bare ``await asyncio.shield(coro)`` is a different and weaker
thing — it detaches the restore and lets the cancellation past it, which is how
a fader ends up stranded at measurement level (ADR-0179).
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

__all__ = ["await_restore_task_resilient", "resilient_restore"]

#: What the shielded operation answers with. Generic because the idiom is about
#: CANCELLATION, not about a payload — the graph restores that named this module
#: answer a report dict, and a give-back that answers nothing needs the same
#: shield for the same reason. Pinning it to one payload type would have made
#: the second caller write a fourth copy of the loop below.
_Restored = TypeVar("_Restored")


async def await_restore_task_resilient(
    restore_task: "asyncio.Task[_Restored]",
) -> _Restored:
    """Await one graph restoration before propagating caller cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(restore_task)
            break
        except asyncio.CancelledError as exc:
            if restore_task.cancelled():
                raise
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return result


async def resilient_restore(
    operation: "Coroutine[Any, Any, _Restored]",
) -> _Restored:
    """Run one restore coroutine to completion before propagating cancellation.

    :func:`await_restore_task_resilient` takes a Task, so every caller was
    spelling its own ``create_task`` line first — a restore could be written
    without the shield and still read as if it had one. One runner instead.
    """
    return await await_restore_task_resilient(asyncio.create_task(operation))
