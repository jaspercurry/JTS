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
from collections.abc import Awaitable, Sequence
from typing import Any, Callable, Coroutine, TypeVar

__all__ = ["await_restore_task_resilient", "give_back", "resilient_restore"]

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


async def give_back(
    steps: Sequence[Callable[[], Awaitable[Any]]],
    *,
    body_error: BaseException | None = None,
) -> None:
    """Run every give-back step, in reverse order of taking.

    Every step runs even when an earlier one raises: a graph that will not come
    back must not strand the fader at measurement level. Each step is expected
    to be idempotent and safe against nothing-held.

    ``body_error`` is the exception already in flight, if any. A cleanup failure
    is ATTACHED to it rather than raised over it, because an ``__aexit__`` that
    raised would demote the real cause to ``__context__`` and report the
    symptom. With nothing in flight a give-back failure IS the failure and
    propagates.
    """
    first: BaseException | None = None
    for step in steps:
        try:
            await step()
        except BaseException as failure:  # noqa: BLE001 - see the docstring
            # EVERY step still runs: a graph that will not come back must not
            # stop the fader coming down. The FIRST failure is the one kept, as
            # it is the one nearest the cause.
            if first is None:
                first = failure
    if first is None:
        return
    if body_error is None:
        raise first
    if body_error.__context__ is None:
        body_error.__context__ = first
