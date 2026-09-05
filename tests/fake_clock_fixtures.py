# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared monotonic-clock test double for modules that read a module-level
``time`` reference's ``monotonic()`` (and, for retry-budget callers,
``asyncio.sleep()`` through the same reference)."""

from __future__ import annotations


class FakeClock:
    """Stand-in for a module's ``time`` reference. Only ``monotonic()`` is
    read by callers that advance the clock directly via ``.now``; callers
    driving a bounded retry loop also await ``sleep()``, which advances the
    clock by the requested delay instead of pausing wall time."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.now += secs
