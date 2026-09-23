# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Attempt a graph restore, and wait for one to FINISH before a caller's
cancellation propagates.

The house idiom for putting a graph back: :func:`attempt_graph_restore` runs
the restore and never raises, and :func:`resilient_restore` /
:func:`await_restore_task_resilient` start it as a TASK, so a cancel aimed at
the awaiter lands on the shield rather than on the restore, and keep waiting
through a repeat cancel. A bare ``await asyncio.shield(coro)`` is a different
and weaker thing — it detaches the restore and lets the cancellation past it,
which is how a fader ends up stranded at measurement level (ADR-0179).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Coroutine, TypeVar

from jasper.camilla import CamillaUnavailable

__all__ = [
    "attempt_graph_restore",
    "await_restore_task_resilient",
    "resilient_restore",
]

#: What the shielded operation answers with. Generic because the idiom is about
#: CANCELLATION, not a payload: pinning it to one type forces the next caller to
#: copy the loop below.
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

    :func:`await_restore_task_resilient` takes a Task, so without this every
    caller spells its own ``create_task`` and an unshielded restore reads as if
    it had a shield.
    """
    return await await_restore_task_resilient(asyncio.create_task(operation))


_COMMISSION_OPERATION_ERRORS = (
    CamillaUnavailable,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)


async def attempt_graph_restore(
    restore: Callable[[], Awaitable[Any]],
) -> tuple[bool, str | None]:
    """Run one graph restore and never raise: ``(took_effect, raise_message)``.

    The one verdict the swap transaction reaches, here and on
    ``program_playback``'s measurement path: it TOOK, it RAISED (message
    present), or CamillaDSP REJECTED it (``False``, no message). Both failures
    are returned rather than collapsed to a bool because they are different
    failures at the same call site — #2198 is what an absent distinction costs.
    Callers own the consequence, which is the half that legitimately differs:
    a restore inside a ``finally`` reports, one inside an ``except`` raises.
    """
    try:
        restored = await restore()
    except _COMMISSION_OPERATION_ERRORS as exc:
        return False, str(exc)
    return restored is True, None
