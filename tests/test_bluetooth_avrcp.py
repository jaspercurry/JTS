# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""bluetooth.avrcp — the A2DP-sink probe and AVRCP control over BlueZ D-Bus.

One ObjectManager read answers both "does a phone have an A2DP transport to
us" (the mux source-state probe) and "which MediaPlayer1 to drive". The
probe must fail soft: an unreachable bus is None, never a raise.
"""
from __future__ import annotations

import sys

import pytest
from dbus_next import Message, Variant  # type: ignore
from dbus_next.errors import DBusError  # type: ignore

from jasper.bluetooth import adapter, avrcp

DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
OTHER_DEVICE = "/org/bluez/hci0/dev_11_22_33_44_55_66"
TRANSPORT = f"{DEVICE}/fd0"


A2DP_SINK_UUID = "0000110B-0000-1000-8000-00805F9B34FB"
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"


def _transport(state: str, device: str = DEVICE, uuid: str = A2DP_SINK_UUID) -> dict:
    return {
        avrcp.BLUEZ_TRANSPORT_IFACE: {
            "State": Variant("s", state),
            "Device": Variant("o", device),
            "UUID": Variant("s", uuid),
        },
    }


def _player(status: str) -> dict:
    return {avrcp.BLUEZ_PLAYER_IFACE: {"Status": Variant("s", status)}}


def _install_objects(monkeypatch, objects=None, error: Exception | None = None):
    async def fake_managed_objects(session=None):
        if error is not None:
            raise error
        return objects

    monkeypatch.setattr(adapter, "managed_objects", fake_managed_objects)


class _Session:
    """A BluezSession stand-in recording the method calls it is asked for."""

    def __init__(self, error: DBusError | None = None) -> None:
        self.calls: list[Message] = []
        self._error = error

    async def call(self, msg: Message):
        self.calls.append(msg)
        if self._error is not None:
            raise self._error
        return []


@pytest.mark.parametrize(
    ("objects", "playing"),
    [
        ({TRANSPORT: _transport("active")}, True),
        ({TRANSPORT: _transport("idle")}, True),
        # Only the A2DP SINK role is "a phone playing to us": bluez-alsa also
        # runs an a2dp-source endpoint and HFP/SCO uses the same interface.
        ({TRANSPORT: _transport("active", uuid=A2DP_SOURCE_UUID)}, False),
        ({TRANSPORT: _transport("active", uuid=HFP_AG_UUID)}, False),
        ({TRANSPORT: {avrcp.BLUEZ_TRANSPORT_IFACE: {"Device": Variant("o", DEVICE)}}}, False),
        ({DEVICE: {"org.bluez.Device1": {}}}, False),
        ({}, False),
    ],
)
async def test_probe_reports_a_connected_a2dp_transport(monkeypatch, objects, playing):
    _install_objects(monkeypatch, objects)
    assert await avrcp.a2dp_sink_playing() is playing


@pytest.mark.parametrize(
    "error",
    [OSError("no bus"), EOFError(), TimeoutError(), AttributeError(), DBusError("org.bluez.Error.Failed", "y")],
)
async def test_unreachable_bus_is_none_not_a_raise(monkeypatch, error):
    _install_objects(monkeypatch, error=error)
    assert await avrcp.a2dp_sink_playing() is None
    assert await avrcp.bluetooth_player_path() is None


async def test_a_missing_dbus_next_fails_soft_like_an_unreachable_bus(monkeypatch):
    """The probe is gathered with return_exceptions=False (mux arbitration),
    so an ImportError from the lazy import must not escape the module."""
    monkeypatch.setitem(sys.modules, "jasper.bluetooth.adapter", None)
    assert await avrcp.a2dp_sink_playing() is None
    with pytest.raises(RuntimeError):
        await avrcp.bluetooth_avrcp_call("Pause", _Session())


@pytest.mark.parametrize(
    ("objects", "expected"),
    [
        # The active A2DP device's player wins over an earlier-sorted one.
        (
            {
                TRANSPORT: _transport("active"),
                f"{OTHER_DEVICE}/player0": _player("playing"),
                f"{DEVICE}/player0": _player("paused"),
            },
            f"{DEVICE}/player0",
        ),
        # No transport: the first player.
        ({f"{OTHER_DEVICE}/player0": _player("playing")}, f"{OTHER_DEVICE}/player0"),
        ({TRANSPORT: _transport("active")}, None),
    ],
)
async def test_player_path_prefers_the_active_a2dp_device(monkeypatch, objects, expected):
    _install_objects(monkeypatch, objects)
    assert await avrcp.bluetooth_player_path() == expected


@pytest.mark.parametrize(
    ("status", "member"), [("playing", "Pause"), ("paused", "Play"), (None, "Play")],
)
async def test_playpause_resolves_from_player_status(monkeypatch, status, member):
    player = {} if status is None else _player(status)
    _install_objects(monkeypatch, {f"{DEVICE}/player0": player or {avrcp.BLUEZ_PLAYER_IFACE: {}}})
    session = _Session()
    await avrcp.bluetooth_avrcp_call("PlayPause", session)
    assert [(m.path, m.interface, m.member) for m in session.calls] == [
        (f"{DEVICE}/player0", avrcp.BLUEZ_PLAYER_IFACE, member),
    ]


@pytest.mark.parametrize(
    ("objects", "error"),
    [
        (None, None),
        ({}, None),
        ({f"{DEVICE}/player0": _player("playing")}, DBusError("org.bluez.Error.Failed", "nope")),
    ],
)
async def test_avrcp_call_raises_runtime_error_when_it_cannot_drive_a_player(
    monkeypatch, objects, error,
):
    _install_objects(monkeypatch, objects, error=OSError() if objects is None else None)
    with pytest.raises(RuntimeError):
        await avrcp.bluetooth_avrcp_call("Next", _Session(error))
