# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""find_bluetooth_conflicts must stay bounded even if BlueZ never answers.

R-012: MessageBus().connect(), bus.introspect, and call_get_managed_objects
carried no timeout, so a wedged system bus hung the synchronous /speaker/
POST handler (speaker_setup.py:_find_conflicts -> asyncio.run) indefinitely.
"""

from __future__ import annotations

import asyncio
import time

import dbus_next.aio

from jasper.identity.speaker_name_discovery import find_bluetooth_conflicts


class _HangingMessageBus:
    """A dbus_next MessageBus stand-in whose connect() never resolves."""

    def __init__(self, **_kwargs) -> None:
        pass

    async def connect(self) -> "_HangingMessageBus":
        await asyncio.Event().wait()  # never set: simulates a wedged system bus
        return self


async def test_find_bluetooth_conflicts_bounds_a_wedged_bus(monkeypatch) -> None:
    monkeypatch.setattr(dbus_next.aio, "MessageBus", lambda **kw: _HangingMessageBus(**kw))

    start = time.monotonic()
    result = await find_bluetooth_conflicts("Kitchen", timeout=0.05)
    elapsed = time.monotonic() - start

    assert result == []  # fail-open, same contract as every other scan failure here
    assert elapsed < 5.0  # bound is timeout + 2s (~2.05s here); a real hang never returns
