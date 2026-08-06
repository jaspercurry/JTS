# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the WiiM Remote 2 BLE connection-event reservation.

The measured promise this pins (jts4, identical interval 11.25 ms, latency 0,
mic packets over a fixed window):

    CE 0x0008/0x000c ->  794 / 805 packets  (~100 % of realtime)
    CE 0x0000/0x0000 ->  196 / 190 packets  (~26 %)

Between-condition ratio 4.2x; within-condition spread 1.4 % / 3.2 %;
``bad_packets=0`` throughout, so this is delivery, not decode. If someone
reverts the CE fields to the BlueZ default of zero, the remote's microphone
goes back to a quarter of realtime — so the constants and the exact encoded
bytes are asserted here rather than left to prose.
"""
from __future__ import annotations

import inspect
import struct

import pytest

from jasper.accessories.constants import WIIM_REMOTE_2_NAME_RE
from jasper.cli import wiim_remote_ce as ce

WIIM_BDADDR = "AA:BB:CC:DD:EE:01"
OTHER_BDADDR = "11:22:33:44:55:66"


def _conn(
    handle: int = 0x0040,
    bdaddr: str = WIIM_BDADDR,
    link_type: int = ce.LE_LINK,
) -> ce.HciConnection:
    return ce.HciConnection(
        handle=handle, bdaddr=bdaddr, link_type=link_type, state=1,
    )


def _event(code: int, body: bytes) -> bytes:
    return bytes((ce.HCI_EVENT_PKT, code, len(body))) + body


def _command_status(status: int, opcode: int) -> bytes:
    return _event(ce.EVT_COMMAND_STATUS, struct.pack("<BBH", status, 1, opcode))


def _update_complete(
    status: int,
    handle: int,
    *,
    interval: int = 0x0009,
    latency: int = 0,
    timeout: int = 0x0258,
) -> bytes:
    body = struct.pack(
        "<BBHHHH",
        ce.LE_CONNECTION_UPDATE_COMPLETE,
        status,
        handle,
        interval,
        latency,
        timeout,
    )
    return _event(ce.EVT_LE_META, body)


# ---------------------------------------------------------------- constants


def test_connection_event_length_stays_reserved():
    """The whole point of the helper. 0x0000 is the BlueZ default that
    measured at ~26 % of realtime; these are the values that measured at
    ~100 %."""
    assert ce.CE_LENGTH_MIN == 0x0008, "5.0 ms minimum connection-event length"
    assert ce.CE_LENGTH_MAX == 0x000C, "7.5 ms maximum connection-event length"
    assert ce.CE_LENGTH_MIN > 0 and ce.CE_LENGTH_MAX > 0, (
        "a zero CE length is the BlueZ default this helper exists to override"
    )
    assert ce.CE_LENGTH_MIN <= ce.CE_LENGTH_MAX


def test_latency_is_not_the_lever():
    """Latency 0 with the default CE length still measured 196 packets, so
    latency must stay at 0 — a future 'fix' that raises it is not addressing
    the measured cause."""
    assert ce.CONN_LATENCY == 0


def test_connection_update_command_bytes_are_exact():
    """Pin the wire bytes, not just the constants: an endianness slip or a
    reordered parameter would still 'look right' in the constants above."""
    #   01        HCI command packet
    #   13 20     opcode 0x2013 (OGF 0x08 LE, OCF 0x0013), little-endian
    #   0e        14 parameter bytes
    #   40 00     handle 0x0040
    #   09 00     interval min 11.25 ms
    #   09 00     interval max 11.25 ms
    #   00 00     latency 0
    #   58 02     supervision timeout 6000 ms
    #   08 00     min CE 5.0 ms
    #   0c 00     max CE 7.5 ms
    expected = bytes.fromhex("0113200e4000090009000000580208000c00")
    packet = ce.encode_connection_update(0x0040)
    assert packet.hex() == expected.hex()
    assert len(packet) == 4 + 14


def test_hci_filter_option_is_sixteen_bytes():
    """The two trailing pad bytes are load-bearing: newer kernels'
    copy_safe_from_sockptr rejects a 14-byte struct hci_ufilter with EINVAL."""
    flt = ce.hci_event_filter()
    assert len(flt) == 16
    type_mask, mask_lo, mask_hi, opcode = struct.unpack("<IIIH", flt[:14])
    assert type_mask == 1 << ce.HCI_EVENT_PKT
    assert mask_lo == mask_hi == 0xFFFFFFFF
    assert opcode == 0


# ------------------------------------------------------- connection listing


def test_parse_connection_list_reverses_bdaddr_and_bounds_count():
    raw_addr = bytes((0x01, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA))  # little-endian
    buf = ce._CONN_LIST_HEADER.pack(0, 2) + b"".join((
        ce._CONN_INFO.pack(0x0040, raw_addr, ce.LE_LINK, 0, 1, 0),
        ce._CONN_INFO.pack(0x0041, bytes(6), 0x00, 1, 1, 0),
    ))
    connections = ce.parse_connection_list(buf)
    assert [c.handle for c in connections] == [0x0040, 0x0041]
    assert connections[0].bdaddr == WIIM_BDADDR
    assert connections[0].link_type == ce.LE_LINK
    assert connections[1].link_type == 0x00


def test_parse_connection_list_ignores_a_truncated_tail():
    """A short/garbled ioctl result must not raise inside a root helper."""
    buf = ce._CONN_LIST_HEADER.pack(0, 4) + ce._CONN_INFO.pack(
        0x0040, bytes(6), ce.LE_LINK, 0, 1, 0,
    )
    assert len(ce.parse_connection_list(buf)) == 1
    assert ce.parse_connection_list(b"") == []


# -------------------------------------------------------- target selection


def test_select_target_picks_the_single_connected_wiim_le_link():
    selection = ce.select_target(
        [_conn(), _conn(handle=0x0041, bdaddr=OTHER_BDADDR)],
        {WIIM_BDADDR: "WiiM Remote 2", OTHER_BDADDR: "Someone's Pixel"},
    )
    assert selection.reason == "ok"
    assert selection.connection is not None
    assert selection.connection.handle == 0x0040
    assert selection.matches == 1


def test_select_target_ignores_non_le_links():
    """A classic-Bluetooth link with a matching name is not the mic link, and
    HCI_LE_Connection_Update is meaningless on it."""
    selection = ce.select_target(
        [_conn(link_type=0x00)],
        {WIIM_BDADDR: "WiiM Remote 2"},
    )
    assert selection.connection is None
    assert selection.reason == "no_le_links"


def test_select_target_applies_nothing_when_no_name_matches():
    selection = ce.select_target([_conn()], {WIIM_BDADDR: "Living Room TV"})
    assert selection.connection is None
    assert selection.reason == "no_match"
    assert selection.le_links == 1


def test_select_target_applies_nothing_when_two_remotes_match():
    """Ambiguity is a refusal: retuning the wrong household link is worse than
    doing nothing."""
    selection = ce.select_target(
        [_conn(), _conn(handle=0x0041, bdaddr=OTHER_BDADDR)],
        {WIIM_BDADDR: "WiiM Remote 2", OTHER_BDADDR: "wiim remote 2"},
    )
    assert selection.connection is None
    assert selection.reason == "ambiguous"
    assert selection.matches == 2


def test_select_target_uses_the_shared_name_pattern():
    """The name regex is the one in jasper.accessories.constants — a second
    copy is the documented drift failure mode (the reconciler activates the
    adapter while the helper's stale regex matches nothing)."""
    default = inspect.signature(ce.select_target).parameters["name_regex"].default
    assert default is WIIM_REMOTE_2_NAME_RE
    # And it really is the pattern the remote's advertised name satisfies.
    assert ce.select_target([_conn()], {WIIM_BDADDR: "WiiM Remote 2"}).reason == "ok"


def test_connected_bluez_names_skips_disconnected_devices():
    managed = {
        "/org/bluez/hci0/dev_A": {
            ce.BLUEZ_DEVICE_IFACE: {
                "Connected": True, "Address": "aa:bb:cc:dd:ee:01",
                "Alias": "WiiM Remote 2",
            },
        },
        "/org/bluez/hci0/dev_B": {
            ce.BLUEZ_DEVICE_IFACE: {
                "Connected": False, "Address": OTHER_BDADDR, "Alias": "WiiM Remote 2",
            },
        },
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": "00:00:00:00:00:00"}},
    }
    assert ce.connected_bluez_names(managed) == {WIIM_BDADDR: "WiiM Remote 2"}


# ------------------------------------------------------------ verification


def test_interpret_event_accepts_our_completed_update():
    outcome = ce.interpret_event(_update_complete(0x00, 0x0040), handle=0x0040)
    assert outcome is not None
    assert outcome.applied is True
    assert outcome.reason == "applied"
    assert outcome.handle == 0x0040
    assert outcome.interval_ms == pytest.approx(11.25)
    assert outcome.supervision_timeout_ms == 6000


def test_interpret_event_ignores_a_completion_for_another_handle():
    """Another LE link updating at the same moment must not be read as our
    success."""
    assert ce.interpret_event(_update_complete(0x00, 0x0041), handle=0x0040) is None


def test_interpret_event_reports_a_failed_update():
    outcome = ce.interpret_event(_update_complete(0x3E, 0x0040), handle=0x0040)
    assert outcome is not None
    assert outcome.applied is False
    assert outcome.reason == "update_rejected"
    assert outcome.status == 0x3E


def test_interpret_event_reports_an_immediate_command_rejection():
    packet = _command_status(0x0C, ce.LE_CONNECTION_UPDATE_OPCODE)
    outcome = ce.interpret_event(packet, handle=0x0040)
    assert outcome is not None
    assert outcome.applied is False
    assert outcome.reason == "command_rejected"
    assert outcome.status == 0x0C


def test_interpret_event_keeps_waiting_after_a_successful_command_status():
    """Status 0 means 'accepted, completion to follow' — not success."""
    packet = _command_status(0x00, ce.LE_CONNECTION_UPDATE_OPCODE)
    assert ce.interpret_event(packet, handle=0x0040) is None


def test_interpret_event_ignores_command_status_for_another_opcode():
    assert ce.interpret_event(_command_status(0x0C, 0x2006), handle=0x0040) is None


@pytest.mark.parametrize("packet", [
    b"",
    b"\x04\x3e",                       # truncated header
    b"\x01\x13\x20\x00",               # a command packet, not an event
    _event(ce.EVT_LE_META, b"\x01"),   # another LE subevent
    _event(0x05, b"\x00\x40\x00\x16"),  # Disconnection Complete
])
def test_interpret_event_ignores_noise(packet):
    assert ce.interpret_event(packet, handle=0x0040) is None


class _FakeSocket:
    """Minimal stand-in for the bound raw HCI socket."""

    def __init__(self, packets: list[bytes | type[TimeoutError]]) -> None:
        self._packets = list(packets)
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, _size: int) -> bytes:
        if not self._packets:
            raise TimeoutError("no more packets")
        nxt = self._packets.pop(0)
        if nxt is TimeoutError:
            raise TimeoutError("controller went quiet")
        assert isinstance(nxt, bytes)
        return nxt


def test_read_update_outcome_skips_noise_then_confirms():
    sock = _FakeSocket([
        _command_status(0x00, ce.LE_CONNECTION_UPDATE_OPCODE),
        _event(0x05, b"\x00\x41\x00\x16"),
        _update_complete(0x00, 0x0040),
    ])
    outcome = ce.read_update_outcome(sock, handle=0x0040, timeout=5.0)
    assert outcome.applied is True
    assert all(t > 0 for t in sock.timeouts)


def test_read_update_outcome_times_out_without_a_completion():
    """No completion event within the bound is 'not applied', never success."""
    outcome = ce.read_update_outcome(_FakeSocket([TimeoutError]), handle=0x0040, timeout=1.0)
    assert outcome.applied is False
    assert outcome.reason == "timeout"


def test_read_update_outcome_handles_a_closed_socket():
    outcome = ce.read_update_outcome(_FakeSocket([b""]), handle=0x0040, timeout=1.0)
    assert outcome.applied is False
    assert outcome.reason == "socket_closed"


# ------------------------------------------------------------- run() policy


def _async_return(value):
    async def _coro(*_args, **_kwargs):
        return value
    return _coro


def _device(address: str, alias: str = "WiiM Remote 2") -> dict:
    return {ce.BLUEZ_DEVICE_IFACE: {
        "Connected": True, "Address": address, "Alias": alias,
    }}


def _unreachable(*_args, **_kwargs):
    raise AssertionError("apply_reservation must not run without a single match")


@pytest.fixture
def bluez_sees_one_wiim(monkeypatch):
    monkeypatch.setattr(
        ce, "_bluez_managed_objects", _async_return({"/d/a": _device(WIIM_BDADDR)}),
    )


def test_run_applies_the_reservation_to_the_single_match(
    monkeypatch, bluez_sees_one_wiim,
):
    calls: list[int] = []

    def fake_apply(handle, **_kwargs):
        calls.append(handle)
        return ce.UpdateOutcome(
            applied=True, reason="applied", status=0, handle=handle,
        )

    monkeypatch.setattr(ce, "live_connections", lambda *a, **k: [_conn()])
    monkeypatch.setattr(ce, "apply_reservation", fake_apply)
    assert ce.run() == 0
    assert calls == [0x0040]


def test_run_applies_nothing_when_no_link_matches(monkeypatch):
    monkeypatch.setattr(ce, "live_connections", lambda *a, **k: [_conn()])
    monkeypatch.setattr(ce, "_bluez_managed_objects", _async_return({}))
    monkeypatch.setattr(ce, "apply_reservation", _unreachable)
    assert ce.run() == 0


def test_run_applies_nothing_when_two_links_match(monkeypatch):
    monkeypatch.setattr(
        ce, "live_connections",
        lambda *a, **k: [_conn(), _conn(handle=0x0041, bdaddr=OTHER_BDADDR)],
    )
    monkeypatch.setattr(ce, "_bluez_managed_objects", _async_return({
        "/d/a": _device(WIIM_BDADDR),
        "/d/b": _device(OTHER_BDADDR),
    }))
    monkeypatch.setattr(ce, "apply_reservation", _unreachable)
    assert ce.run() == 0


def test_run_applies_nothing_when_bluez_is_unreachable(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise TimeoutError("bus wedged")

    monkeypatch.setattr(ce, "live_connections", lambda *a, **k: [_conn()])
    monkeypatch.setattr(ce, "_bluez_managed_objects", _boom)
    monkeypatch.setattr(ce, "apply_reservation", _unreachable)
    # Exit 0: nothing was changed and nothing is actionable, so the unit must
    # not park in `failed` over a transient bus condition.
    assert ce.run() == 0


def test_run_reports_failure_when_the_controller_refuses(
    monkeypatch, bluez_sees_one_wiim,
):
    monkeypatch.setattr(ce, "live_connections", lambda *a, **k: [_conn()])
    monkeypatch.setattr(
        ce, "apply_reservation",
        lambda *a, **k: ce.UpdateOutcome(
            applied=False, reason="command_rejected", status=0x0C,
        ),
    )
    # The one actionable case: right link found, controller said no.
    assert ce.run() == 1


def test_run_survives_a_failing_connection_list(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("no adapter")

    monkeypatch.setattr(ce, "live_connections", boom)
    monkeypatch.setattr(ce, "apply_reservation", _unreachable)
    assert ce.run() == 0
