# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import struct

from jasper.music_sources import Source
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
