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
import logging

import dbus_next.aio
import pytest

import jasper.identity.speaker_name_discovery as speaker_name_discovery
from jasper.identity.speaker_name_discovery import find_bluetooth_conflicts
from tests._log_events import event_fields


class _HangingMessageBus:
    """A dbus_next MessageBus stand-in that hangs at a chosen call point."""

    def __init__(self, *, hang: str, **_kwargs) -> None:
        self._hang = hang

    async def connect(self) -> "_HangingMessageBus":
        if self._hang == "connect":
            await asyncio.Event().wait()  # never set: simulates a wedged connect
        return self

    async def introspect(self, *_args, **_kwargs):
        if self._hang == "introspect":
            await asyncio.Event().wait()  # never set: simulates a wedged introspect
        raise AssertionError("introspect should not be reached in this case")

    def disconnect(self) -> None:
        pass


@pytest.mark.parametrize("hang", ["connect", "introspect"])
async def test_find_bluetooth_conflicts_bounds_a_wedged_bus(
    monkeypatch, caplog, hang: str,
) -> None:
    monkeypatch.setattr(speaker_name_discovery, "_BLUEZ_SCAN_MARGIN_SEC", 0.05)
    monkeypatch.setattr(
        dbus_next.aio, "MessageBus", lambda **kw: _HangingMessageBus(hang=hang, **kw),
    )

    with caplog.at_level(logging.WARNING, logger=speaker_name_discovery.__name__):
        result = await find_bluetooth_conflicts("Kitchen", timeout=0.05)

    assert result == []  # fail-open, same contract as every other scan failure here
    fields = event_fields(caplog, "speaker_name.bluetooth_scan_timeout")
    assert fields["timeout_sec"] == "0.05"
