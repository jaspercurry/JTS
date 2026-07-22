# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import struct
import sys
from types import ModuleType, SimpleNamespace

import pytest

from jasper.music_sources import Source
from jasper import source_events
from jasper.source_events import classify_source_signal, inotify_changed_names


def test_airplay_signal_is_only_a_wake_hint_for_relevant_properties():
    assert classify_source_signal(
        interface="org.freedesktop.DBus.Properties",
        member="PropertiesChanged",
        path="/org/mpris/MediaPlayer2",
        body=[
            "org.mpris.MediaPlayer2.Player",
            {"PlaybackStatus": object()},
            [],
        ],
    ) == (Source.AIRPLAY,)
    assert classify_source_signal(
        interface="org.freedesktop.DBus.Properties",
        member="PropertiesChanged",
        path="/org/mpris/MediaPlayer2",
        body=["org.mpris.MediaPlayer2.Player", {"Volume": object()}, []],
    ) == ()


def test_bluetooth_transport_changes_wake_bluetooth_reconcile():
    assert classify_source_signal(
        interface="org.freedesktop.DBus.Properties",
        member="PropertiesChanged",
        path="/org/bluealsa/hci0/dev_x/a2dpsnk/source",
        body=["org.bluealsa.PCM1", {"State": object()}, []],
    ) == (Source.BLUETOOTH,)
    assert classify_source_signal(
        interface="org.freedesktop.DBus.ObjectManager",
        member="InterfacesAdded",
        path="/",
        body=["/org/bluez/hci0/dev_x/fd0", {"org.bluez.MediaTransport1": {}}],
    ) == (Source.BLUETOOTH,)


def test_inotify_parser_handles_atomic_rename_record():
    name = b"state.json\0"
    padded = name + b"\0" * ((4 - len(name) % 4) % 4)
    record = struct.pack("iIII", 1, 0x80, 7, len(padded)) + padded
    assert inotify_changed_names(record) == ("state.json",)


@pytest.mark.asyncio
async def test_spotify_adapter_retries_until_state_directory_returns(
    monkeypatch, tmp_path,
):
    attempts = 0
    recovered = asyncio.Event()
    notifications = []

    async def watch_once(path, notify):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileNotFoundError(path.parent)
        notify(Source.SPOTIFY, "spotify_inotify")
        recovered.set()
        await asyncio.Future()

    monkeypatch.setattr(source_events, "_watch_spotify_state_once", watch_once)
    monkeypatch.setattr(source_events, "_RETRY_INITIAL_SEC", 0.001)
    task = asyncio.create_task(source_events.watch_spotify_state(
        str(tmp_path / "state.json"),
        lambda source, via: notifications.append((source, via)),
    ))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert attempts == 2
    assert notifications == [(Source.SPOTIFY, "spotify_inotify")]


@pytest.mark.asyncio
async def test_dbus_adapter_reconnects_and_delivers_signal(monkeypatch):
    attempts = 0
    recovered = asyncio.Event()
    notifications = []

    class FakeMessage:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeMessageBus:
        def __init__(self, *, bus_type):
            assert bus_type == "system"
            self.handler = None

        async def connect(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("system bus temporarily unavailable")
            return self

        async def call(self, message):
            return SimpleNamespace(message_type="reply", error_name=None)

        def add_message_handler(self, handler):
            self.handler = handler

        async def wait_for_disconnect(self):
            assert self.handler is not None
            self.handler(SimpleNamespace(
                message_type="signal",
                interface="org.freedesktop.DBus.Properties",
                member="PropertiesChanged",
                path="/org/mpris/MediaPlayer2",
                body=[
                    "org.mpris.MediaPlayer2.Player",
                    {"PlaybackStatus": "Playing"},
                    [],
                ],
            ))
            recovered.set()
            await asyncio.Future()

        def disconnect(self):
            pass

    dbus_next = ModuleType("dbus_next")
    dbus_next.__path__ = []
    dbus_next.BusType = SimpleNamespace(SYSTEM="system")
    dbus_next.Message = FakeMessage
    dbus_next.MessageType = SimpleNamespace(ERROR="error", SIGNAL="signal")
    dbus_aio = ModuleType("dbus_next.aio")
    dbus_aio.MessageBus = FakeMessageBus
    dbus_errors = ModuleType("dbus_next.errors")
    dbus_errors.AuthError = type("AuthError", (Exception,), {})
    dbus_errors.DBusError = type("DBusError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "dbus_next", dbus_next)
    monkeypatch.setitem(sys.modules, "dbus_next.aio", dbus_aio)
    monkeypatch.setitem(sys.modules, "dbus_next.errors", dbus_errors)
    monkeypatch.setattr(source_events, "_RETRY_INITIAL_SEC", 0.001)

    task = asyncio.create_task(source_events.watch_dbus_sources(
        lambda source, via: notifications.append((source, via)),
    ))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert attempts == 2
    assert notifications == [(Source.AIRPLAY, "dbus")]


@pytest.mark.asyncio
async def test_unexpected_adapter_exit_is_observable(monkeypatch, caplog):
    async def crash(*args, **kwargs):
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(source_events, "watch_spotify_state", crash)
    monkeypatch.setattr(source_events, "watch_dbus_sources", crash)
    with caplog.at_level(logging.ERROR, logger="jasper.source_events"):
        tasks = source_events.start_source_event_tasks(
            lambda source, via: None,
            spotify_state_path="/run/librespot/state.json",
        )
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

    assert caplog.text.count("event=source_event.adapter_stopped") == 2
    assert "adapter=mux-spotify-events" in caplog.text
    assert "adapter=mux-dbus-events" in caplog.text
