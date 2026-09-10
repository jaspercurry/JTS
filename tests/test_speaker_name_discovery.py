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
import time

import dbus_next.aio
import pytest

import jasper.identity.speaker_name_discovery as speaker_name_discovery
from jasper.identity.speaker_name_discovery import find_bluetooth_conflicts
from tests._log_events import event_fields

class _HangingInterface:
    def __init__(self, *, hang: str) -> None:
        self._hang = hang
        self._collects = 0

    async def call_get_managed_objects(self) -> dict:
        self._collects += 1
        if self._hang == "cleanup" and self._collects > 1:
            await asyncio.Event().wait()  # never set: wedges the re-read
        return {}

    async def call_start_discovery(self) -> None:
        return None

    async def call_stop_discovery(self) -> None:
        if self._hang == "cleanup":
            await asyncio.Event().wait()  # never set: wedges the cleanup call


class _HangingProxyObject:
    def __init__(self, hang: str) -> None:
        self._hang = hang

    def get_interface(self, _name: str) -> _HangingInterface:
        return _HangingInterface(hang=self._hang)


class _HangingMessageBus:
    """A dbus_next MessageBus stand-in that hangs at a chosen call point."""

    def __init__(self, *, hang: str, **_kwargs) -> None:
        self._hang = hang
        self.disconnects = 0

    async def connect(self) -> "_HangingMessageBus":
        if self._hang == "connect":
            await asyncio.Event().wait()  # never set: simulates a wedged connect
        return self

    async def introspect(self, *_args, **_kwargs) -> None:
        if self._hang == "introspect":
            await asyncio.Event().wait()  # never set: simulates a wedged introspect
        return None

    def get_proxy_object(self, *_args, **_kwargs) -> _HangingProxyObject:
        return _HangingProxyObject(self._hang)

    def disconnect(self) -> None:
        self.disconnects += 1


def _install(monkeypatch, hang: str) -> list[_HangingMessageBus]:
    monkeypatch.setattr(speaker_name_discovery, "_BLUEZ_SCAN_MARGIN_SEC", 0.05)
    buses: list[_HangingMessageBus] = []

    def _factory(**kwargs) -> _HangingMessageBus:
        bus = _HangingMessageBus(hang=hang, **kwargs)
        buses.append(bus)
        return bus

    monkeypatch.setattr(dbus_next.aio, "MessageBus", _factory)
    return buses


@pytest.mark.parametrize("hang", ["connect", "introspect", "cleanup"])
async def test_find_bluetooth_conflicts_bounds_a_wedged_bus(
    monkeypatch, caplog, hang: str,
) -> None:
    buses = _install(monkeypatch, hang)

    with caplog.at_level(logging.WARNING, logger=speaker_name_discovery.__name__):
        start = time.monotonic()
        async with asyncio.timeout(5.0):  # a regression hangs; fail fast instead
            result = await find_bluetooth_conflicts("Kitchen", timeout=0.05)
        elapsed = time.monotonic() - start

    assert result == []  # fail-open, same contract as every other scan failure here
    assert elapsed < 1.0
    fields = event_fields(caplog, "speaker_name.bluetooth_scan_timeout")
    assert fields["timeout_sec"] == "0.05"
    # Even a connect() cancelled mid-flight must not leak the opened transport.
    assert [bus.disconnects for bus in buses] == [1]
