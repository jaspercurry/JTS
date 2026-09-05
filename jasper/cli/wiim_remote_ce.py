# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Reserve BLE connection-event time for the WiiM Remote 2 microphone link.

Why this exists
---------------
The remote's mic arrives as GATT notifications carrying 16 ms of audio each,
so realtime needs 62.5 notifications/second. One notification is 131 B of
value + 3 B ATT + 4 B L2CAP = 138 B, and the link's negotiated ``Max RX time``
of 328 us on the 1M PHY is the legacy ~27-octet packet duration — so each
notification takes about **6 Link Layer PDUs**.

BlueZ hardcodes ``min_ce_len = max_ce_len = 0`` (``net/bluetooth/hci_sync.c``).
On the Pi Zero 2 W's BCM43436 controller that default admits roughly *one* PDU
per connection event: 6 events x 11.25 ms = 67.5 ms per notification, about
15 notifications/second — a quarter of realtime. Reserving connection-event
length lets one event carry ~4 PDUs.

Measured back-to-back on jts4, same interval (11.25 ms), same latency (0),
counting mic packets over a fixed window:

===========================  ======  ======  ===========
CE length                    run 1   run 2   ~ realtime
===========================  ======  ======  ===========
0x0008 / 0x000c (5.0-7.5ms)     794     805       ~100 %
0x0000 / 0x0000 (BlueZ dflt)    196     190        ~24 %
===========================  ======  ======  ===========

Within-condition spread was 1.4 % / 3.2 %; between conditions 4.1x. Decoding
was never the problem (``bad_packets=0`` throughout) — only delivery was.

Connection *latency* is not the lever: latency 0 with the default CE length
still gave 196 packets. Do not "fix" throughput by changing latency.

The Pi 5 always worked because its controller's *default* event scheduling
already admits the ~4 PDUs/event realtime needs (62.5 notifications/s x 6 PDUs
= 375 PDUs/s, over 88.9 connection events/s at 11.25 ms). Same lever,
different default.

What the reservation costs the rest of the radio — **INFERRED, not measured.**
5.0-7.5 ms of CE against an 11.25 ms interval is nominally 44-67 % of the
interval budget, on a BCM43436 where Bluetooth and WiFi share one antenna by
time-division coexistence, and JTS also runs A2DP and the voice websocket over
that radio. This is believed benign because Min/Max_CE_Length are *scheduler
hints*, not a fixed allocation: a connection event ends as soon as either side
sends More Data = 0, so the reservation is only genuinely consumed while the
remote is streaming — during push-to-talk holds, which is exactly when the mic
should win the radio. The jts4 end-to-end (owner spoke, speaker answered over
WiFi across several turns) is functional evidence the concurrent path survived,
but no coexistence measurement was taken. If A2DP or the websocket is ever seen
to stutter *only* while the remote's button is held, this is the first place to
look.

What it does
------------
This is a short-lived root oneshot. It takes **no argument that identifies a
connection** — it discovers and validates the target itself, so the caller
(the adapter task in jasper-input, via the broker's start-only allowlist) hands
over nothing this helper trusts:

1. ask BlueZ which connected devices are named like a WiiM Remote 2;
2. enumerate live connections with the ``HCIGETCONNLIST`` ioctl;
3. keep only LE links whose BD_ADDR is one of those devices;
4. exactly one match -> send ``HCI_LE_Connection_Update`` on that handle;
   zero or several -> log and change nothing;
5. read HCI events back and only report success on
   ``LE Connection Update Complete`` with status 0 **and** our handle.

The reservation lives on the connection, so it is lost on every disconnect —
and it can also be overwritten mid-connection, because ``hci_le_conn_update()``
zeroes the CE fields when BlueZ services a peripheral-initiated parameter
update. Nothing here re-arms it.

The BlueZ read comes first on purpose. It is the slow step (bounded at 5 s),
and **connection handles are recycled**: if the remote dropped while we were
waiting on the bus and another LE device inherited its handle, we would retune
a stranger's link. ``LE Connection Update Complete`` carries no address, so the
journal would happily log success against the WiiM's BD_ADDR. Enumerating last
shrinks that window to the microseconds inside :func:`select_target`, and
:func:`apply_reservation` re-reads the connection list one syscall before it
sends to confirm the handle still belongs to the same address.

What this is NOT: a guarantee that a compromised caller cannot get some other
device retuned. Anything in the ``bluetooth`` group can set a BlueZ device's
``Alias``, so a hostile ``jasper-input`` could rename a device into the regex.
That buys it nothing it does not already have — it can drive the whole adapter
over D-Bus — but the honest claim is "this helper adds no new authority",
not "this target can never be spoofed".

Deliberately not done here: no ``main.conf`` edit and no debugfs
``conn_min_interval``/``conn_max_interval`` write. Those are adapter-global;
an earlier attempt at the global route broke pairing for the whole adapter.
``hcitool`` is also avoided — it is deprecated and may not stay installed.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any, Mapping

from jasper.accessories.constants import WIIM_REMOTE_2_NAME_RE
from jasper.cli._logging import CLI_LOG_FORMAT
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# AF_BLUETOOTH / BTPROTO_HCI exist on Linux, not on a macOS dev box. Resolve
# them once so this module still imports (and its pure logic still tests) off
# the Pi; the socket helpers raise a clear error instead of AttributeError.
_AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", None)
_BTPROTO_HCI = getattr(socket, "BTPROTO_HCI", None)

# The Pi's on-board controller. Hardcoded rather than exposed as a flag: the
# unit's ExecStart takes no arguments at all, which is what makes "an
# unprivileged caller cannot aim this" true by construction.
HCI_DEVICE_ID = 0

SOL_HCI = 0
HCI_FILTER = 2
HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04
EVT_COMMAND_STATUS = 0x0F
EVT_LE_META = 0x3E
LE_CONNECTION_UPDATE_COMPLETE = 0x03

OGF_LE_CTL = 0x08
OCF_LE_CONNECTION_UPDATE = 0x0013
LE_CONNECTION_UPDATE_OPCODE = (OGF_LE_CTL << 10) | OCF_LE_CONNECTION_UPDATE  # 0x2013

# _IOR('H', 212, int) — see include/net/bluetooth/hci_sock.h.
HCIGETCONNLIST = 0x800448D4
# struct hci_conn_info: __u16 handle; bdaddr_t bdaddr; __u8 type; __u8 out;
#                       __u16 state; __u32 link_mode;
_CONN_INFO = struct.Struct("<H6sBBHI")
# struct hci_conn_list_req: __u16 dev_id; __u16 conn_num; conn_info[];
_CONN_LIST_HEADER = struct.Struct("<HH")
# The adapter carries a handful of links at most (a remote, a phone, maybe a
# speaker). 16 is generous and bounds the ioctl buffer at 260 bytes.
HCI_MAX_CONN_LIST = 16
LE_LINK = 0x80

# ---- HCI_LE_Connection_Update parameters ---------------------------------
# All seven fields go out every time: the command has no "leave this one
# alone" encoding. And HCI offers no way to READ a live connection's current
# parameters back — HCIGETCONNLIST returns handle/bdaddr/type/out/state/
# link_mode and nothing else — so the three below are FORCED to fixed values,
# not echoed. They are set to what this link is expected to already be using,
# so forcing them should be a no-op; only the two CE fields further down were
# measured to change throughput.
CONN_INTERVAL_MIN = 0x0009      # 11.25 ms (units of 1.25 ms)
CONN_INTERVAL_MAX = 0x0009      # 11.25 ms
CONN_LATENCY = 0x0000           # measured NOT to be the lever; keep it at 0
# 6000 ms (units of 10 ms). PENDING HARDWARE MEASUREMENT: Linux's default
# outgoing LE supervision timeout is 420 ms. If that is what this link
# actually negotiated, forcing 6000 ms makes link-loss detection ~14x slower
# and delays reconnect. The owner is reading the negotiated value off btmon;
# do not change this number until that measurement lands.
#
# Two residuals ride that one measurement, so retire them together:
#   1. the value itself is unverified (above);
#   2. the reservation is requested unconditionally, so Pi 4/5 — which never
#      had the CE starvation bug — also carry this forced timeout for no
#      corresponding benefit. Gating by hardware was considered and declined:
#      HCI_LE_Connection_Update has no partial-update encoding (all seven
#      parameters go out together, so CE cannot be sent without this), and a
#      mis-detecting gate would silently reinstate the original starvation bug
#      on a Zero. Measuring the negotiated value removes both at once.
SUPERVISION_TIMEOUT = 0x0258
# The whole point of this helper. 0x0000/0x0000 is the BlueZ default that
# starves the link to ~24 % of realtime (see the table in the module
# docstring). Reverting these two to zero re-breaks the microphone.
CE_LENGTH_MIN = 0x0008          # 5.0 ms (units of 0.625 ms)
CE_LENGTH_MAX = 0x000C          # 7.5 ms

# How long to wait for the controller's LE Connection Update Complete. The
# controller has to reach the next connection event on both sides to apply an
# update, so a couple of intervals plus slack; a timeout means "not applied".
HCI_EVENT_TIMEOUT_SEC = 5.0
# BlueZ ObjectManager read bound. The mic adapter just finished talking
# to the same bus, so this only has to cover a wedged bus, not a cold one.
BLUEZ_TIMEOUT_SEC = 5.0
# Largest HCI event packet: 1 B type + 2 B header + 255 B payload.
_HCI_EVENT_BUF_BYTES = 258

BLUEZ_DEVICE_IFACE = "org.bluez.Device1"


@dataclass(frozen=True)
class HciConnection:
    """One row of the controller's live connection list."""

    handle: int
    bdaddr: str
    link_type: int
    state: int


@dataclass(frozen=True)
class TargetSelection:
    """Which live link (if any) this helper is allowed to touch."""

    connection: HciConnection | None
    reason: str
    le_links: int
    matches: int


@dataclass(frozen=True)
class UpdateOutcome:
    """Verified result of one HCI_LE_Connection_Update."""

    applied: bool
    reason: str
    status: int | None = None
    handle: int | None = None
    interval_ms: float | None = None
    latency: int | None = None
    supervision_timeout_ms: int | None = None
    detail: str | None = None


# Outcomes where the command was never written to the controller: the link
# moved out from under us, or the confirm-time enumeration failed. Both are
# benign and self-healing — the adapter re-requests on the next reconnect —
# so they exit 0 rather than parking the unit in `failed`. Named once here
# because :func:`apply_reservation` produces them and :func:`run` routes them.
NOTHING_WRITTEN_REASONS = frozenset({"handle_reused", "conn_list_failed"})


def hci_event_filter() -> bytes:
    """The HCI_FILTER socket option: events only, all events, any opcode.

    ``struct hci_ufilter`` is 16 bytes — ``u32 type_mask; u32 event_mask[2];
    le16 opcode`` plus two bytes of trailing pad. The pad is load-bearing:
    newer kernels' ``copy_safe_from_sockptr`` rejects a short 14-byte option
    with EINVAL, so ``2x`` is not cosmetic. Verified on jts4.
    """
    return struct.pack(
        "<IIIH2x",
        1 << HCI_EVENT_PKT,   # type_mask: HCI event packets only
        0xFFFFFFFF,           # event_mask[0]
        0xFFFFFFFF,           # event_mask[1]
        0,                    # opcode: no per-opcode filtering
    )


def encode_connection_update(handle: int) -> bytes:
    """Build the full HCI command packet for one connection handle."""
    params = struct.pack(
        "<HHHHHHH",
        handle,
        CONN_INTERVAL_MIN,
        CONN_INTERVAL_MAX,
        CONN_LATENCY,
        SUPERVISION_TIMEOUT,
        CE_LENGTH_MIN,
        CE_LENGTH_MAX,
    )
    return (
        struct.pack("<BHB", HCI_COMMAND_PKT, LE_CONNECTION_UPDATE_OPCODE, len(params))
        + params
    )


def format_bdaddr(raw: bytes) -> str:
    """Render a little-endian ``bdaddr_t`` as the usual big-endian text form."""
    return ":".join(f"{byte:02X}" for byte in reversed(raw))


def parse_connection_list(buf: bytes) -> list[HciConnection]:
    """Decode a filled ``struct hci_conn_list_req`` buffer."""
    if len(buf) < _CONN_LIST_HEADER.size:
        return []
    _dev_id, count = _CONN_LIST_HEADER.unpack_from(buf, 0)
    connections: list[HciConnection] = []
    for index in range(min(count, HCI_MAX_CONN_LIST)):
        offset = _CONN_LIST_HEADER.size + index * _CONN_INFO.size
        if offset + _CONN_INFO.size > len(buf):
            break
        handle, addr, link_type, _outbound, state, _link_mode = _CONN_INFO.unpack_from(
            buf, offset,
        )
        connections.append(HciConnection(
            handle=handle,
            bdaddr=format_bdaddr(addr),
            link_type=link_type,
            state=state,
        ))
    return connections


def _hci_socket() -> socket.socket:
    if _AF_BLUETOOTH is None or _BTPROTO_HCI is None:
        raise OSError("AF_BLUETOOTH/BTPROTO_HCI unavailable on this platform")
    return socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)


def live_connections(device_id: int = HCI_DEVICE_ID) -> list[HciConnection]:
    """Ask the controller for its live connection list."""
    with _hci_socket() as sock:
        buf = bytearray(
            _CONN_LIST_HEADER.pack(device_id, HCI_MAX_CONN_LIST)
            + bytes(HCI_MAX_CONN_LIST * _CONN_INFO.size)
        )
        fcntl.ioctl(sock.fileno(), HCIGETCONNLIST, buf)
    return parse_connection_list(bytes(buf))


def connected_bluez_names(
    managed_objects: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, str]:
    """Map upper-case BD_ADDR -> advertised name, for *connected* devices only."""
    from jasper.accessories._dbus import variant_value

    names: dict[str, str] = {}
    for ifaces in managed_objects.values():
        props = ifaces.get(BLUEZ_DEVICE_IFACE)
        if not props or not bool(variant_value(props.get("Connected"))):
            continue
        address = str(variant_value(props.get("Address")) or "").upper()
        if not address:
            continue
        name = str(
            variant_value(props.get("Alias"))
            or variant_value(props.get("Name"))
            or ""
        )
        names[address] = name
    return names


def select_target(
    connections: list[HciConnection],
    bluez_names: Mapping[str, str],
    *,
    name_regex: str = WIIM_REMOTE_2_NAME_RE,
) -> TargetSelection:
    """Pick the one live LE link that is the WiiM remote, or pick nothing.

    Ambiguity is a refusal, not a coin flip: two connected remotes means we
    cannot tell which link carries the mic this helper was asked about, and
    retuning the wrong household device is worse than doing nothing.
    """
    pattern = re.compile(name_regex)
    le_links = [conn for conn in connections if conn.link_type == LE_LINK]
    if not le_links:
        return TargetSelection(None, "no_le_links", 0, 0)
    matches = [
        conn for conn in le_links
        if pattern.search(bluez_names.get(conn.bdaddr, ""))
    ]
    if not matches:
        return TargetSelection(None, "no_match", len(le_links), 0)
    if len(matches) > 1:
        return TargetSelection(None, "ambiguous", len(le_links), len(matches))
    return TargetSelection(matches[0], "ok", len(le_links), 1)


def confirm_target(
    connections: list[HciConnection], *, handle: int, bdaddr: str,
) -> bool:
    """True when ``handle`` is still an LE link to ``bdaddr``.

    The anti-TOCTOU check. Controllers recycle connection handles, so the
    handle we validated a moment ago can belong to a different device by the
    time we send — and nothing downstream would notice, because
    ``LE Connection Update Complete`` reports a handle and no address.
    """
    return any(
        conn.handle == handle
        and conn.bdaddr == bdaddr
        and conn.link_type == LE_LINK
        for conn in connections
    )


def interpret_event(packet: bytes, *, handle: int) -> UpdateOutcome | None:
    """Classify one HCI event packet. ``None`` means "keep waiting".

    Only two things end the wait: the controller refusing the command
    outright (a non-zero Command Status for our opcode), or an LE Connection
    Update Complete **for the handle we targeted**. A completion for another
    link is somebody else's update and is ignored.
    """
    if len(packet) < 3 or packet[0] != HCI_EVENT_PKT:
        return None
    code = packet[1]
    body = packet[3:]

    if code == EVT_COMMAND_STATUS:
        if len(body) < 4:
            return None
        status = body[0]
        opcode = struct.unpack_from("<H", body, 2)[0]
        if opcode != LE_CONNECTION_UPDATE_OPCODE:
            return None
        if status != 0:
            return UpdateOutcome(
                applied=False, reason="command_rejected", status=status,
            )
        return None  # accepted; the completion event is still to come

    if code == EVT_LE_META:
        if len(body) < 10 or body[0] != LE_CONNECTION_UPDATE_COMPLETE:
            return None
        status, event_handle, interval, latency, timeout = struct.unpack_from(
            "<BHHHH", body, 1,
        )
        if event_handle != handle:
            return None
        return UpdateOutcome(
            applied=status == 0,
            reason="applied" if status == 0 else "update_rejected",
            status=status,
            handle=event_handle,
            interval_ms=interval * 1.25,
            latency=latency,
            supervision_timeout_ms=timeout * 10,
        )

    return None


def read_update_outcome(
    sock: Any,
    *,
    handle: int,
    timeout: float = HCI_EVENT_TIMEOUT_SEC,
) -> UpdateOutcome:
    """Read HCI events until the update is confirmed, refused, or times out."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return UpdateOutcome(applied=False, reason="timeout")
        sock.settimeout(remaining)
        try:
            packet = sock.recv(_HCI_EVENT_BUF_BYTES)
        except TimeoutError:
            return UpdateOutcome(applied=False, reason="timeout")
        if not packet:
            return UpdateOutcome(applied=False, reason="socket_closed")
        outcome = interpret_event(packet, handle=handle)
        if outcome is not None:
            return outcome


def apply_reservation(
    handle: int,
    *,
    expect_bdaddr: str,
    device_id: int = HCI_DEVICE_ID,
    timeout: float = HCI_EVENT_TIMEOUT_SEC,
) -> UpdateOutcome:
    """Send the connection update on ``handle`` and verify the controller's answer.

    ``expect_bdaddr`` is required, not optional: re-confirming the handle
    still belongs to that address is the last thing done before ``send``, so
    a recycled handle cannot be retuned. Keeping the check in here rather
    than in :func:`run` means no caller can skip it.
    """
    with _hci_socket() as sock:
        # Order matters: the filter must be installed BEFORE bind(), or the
        # early events are dropped. bind() takes a 1-TUPLE on this CPython —
        # a 2-tuple raises "bind(): wrong format"; the channel defaults to
        # HCI_CHANNEL_RAW. Both verified on jts4.
        sock.setsockopt(SOL_HCI, HCI_FILTER, hci_event_filter())
        sock.bind((device_id,))
        # Last check before the write: one ioctl, then one send. Anything
        # slower than that in between is a window a recycled handle can fit
        # through.
        #
        # Its OSError is caught HERE rather than left to run(): by the time it
        # reached run() the only thing left to say was "hci_send_failed",
        # which is a lie — nothing was sent. bluetooth.service restarting
        # between the two enumerations is enough to produce it (ENODEV).
        try:
            live = live_connections(device_id)
        except OSError as exc:
            return UpdateOutcome(
                applied=False, reason="conn_list_failed", detail=str(exc),
            )
        if not confirm_target(live, handle=handle, bdaddr=expect_bdaddr):
            return UpdateOutcome(applied=False, reason="handle_reused")
        sock.send(encode_connection_update(handle))
        return read_update_outcome(sock, handle=handle, timeout=timeout)


async def _bluez_managed_objects(
    timeout: float,
) -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    # Lazy import: reuse the accessory reconciler's single BlueZ enumeration
    # rather than growing a second one, without paying its imports when this
    # helper never gets that far.
    from jasper.accessories.reconcile import bluez_managed_objects

    return await asyncio.wait_for(bluez_managed_objects(), timeout)


def run(
    *,
    name_regex: str = WIIM_REMOTE_2_NAME_RE,
    timeout: float = HCI_EVENT_TIMEOUT_SEC,
    bluez_timeout: float = BLUEZ_TIMEOUT_SEC,
) -> int:
    """Discover the WiiM link, apply the reservation, verify it. Returns an exit code.

    Exit 1 is reserved for failures where the command actually reached (or
    should have reached) the controller: it refused the update, never
    confirmed it, or the socket/send itself failed. Everything that wrote
    nothing — no LE links, no WiiM among them, two of them, an unreachable
    BlueZ, either enumeration failing, or a handle that changed owner
    mid-flight — is logged at WARNING and exits 0, so a transient condition
    does not park the unit in ``failed``. Every one of those is still an
    anomaly worth reading: in production this only runs *after* the adapter
    subscribed to a live, name-matched LE link.
    """
    from dbus_next.errors import DBusError  # type: ignore

    try:
        # TimeoutError covers asyncio.wait_for on 3.11+ (they are the same
        # class); DBusError is what a missing/wedged BlueZ raises.
        managed = asyncio.run(_bluez_managed_objects(bluez_timeout))
    except (DBusError, OSError, RuntimeError, TimeoutError) as exc:
        log_event(
            logger, "wiim_remote_ce.skipped", reason="bluez_unavailable",
            err=f"{type(exc).__name__}: {exc}", level=logging.WARNING,
        )
        return 0

    # Enumerate AFTER the bus read, never before: handles are recycled, and
    # this is the value we are about to write a command against. See the
    # module docstring.
    try:
        connections = live_connections()
    except OSError as exc:
        log_event(
            logger, "wiim_remote_ce.skipped", reason="conn_list_failed",
            err=str(exc), level=logging.WARNING,
        )
        return 0

    selection = select_target(
        connections, connected_bluez_names(managed), name_regex=name_regex,
    )
    if selection.connection is None:
        log_event(
            logger, "wiim_remote_ce.skipped", reason=selection.reason,
            le_links=selection.le_links, matches=selection.matches,
            level=logging.WARNING,
        )
        return 0

    target = selection.connection
    try:
        outcome = apply_reservation(
            target.handle, expect_bdaddr=target.bdaddr, timeout=timeout,
        )
    except OSError as exc:
        log_event(
            logger, "wiim_remote_ce.not_applied", reason="hci_send_failed",
            handle=f"0x{target.handle:04x}", err=str(exc), level=logging.WARNING,
        )
        return 1

    if outcome.reason in NOTHING_WRITTEN_REASONS:
        fields: dict[str, Any] = {
            "reason": outcome.reason,
            "handle": f"0x{target.handle:04x}",
            "bdaddr": target.bdaddr,
        }
        if outcome.detail:
            fields["err"] = outcome.detail
        # fields=, not **fields: log_event reserves `level`, and a dict that
        # ever gained a "level" key would blow up the splat with a duplicate
        # keyword argument. The explicit parameter exists for exactly this.
        log_event(
            logger, "wiim_remote_ce.skipped", level=logging.WARNING, fields=fields,
        )
        return 0

    if outcome.applied:
        log_event(
            logger, "wiim_remote_ce.applied",
            handle=f"0x{target.handle:04x}",
            bdaddr=target.bdaddr,
            # "requested", not "applied": LE Connection Update Complete
            # carries no CE fields — per spec they are hints the controller
            # may ignore. Only the interval/latency/timeout below are
            # confirmed values read back off the completion event.
            ce_min_ms_requested=CE_LENGTH_MIN * 0.625,
            ce_max_ms_requested=CE_LENGTH_MAX * 0.625,
            interval_ms=outcome.interval_ms,
            latency=outcome.latency,
            supervision_timeout_ms=outcome.supervision_timeout_ms,
        )
        return 0

    log_event(
        logger, "wiim_remote_ce.not_applied", reason=outcome.reason,
        handle=f"0x{target.handle:04x}", status=outcome.status,
        level=logging.WARNING,
    )
    return 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jasper-wiim-remote-ce",
        description=(
            "Reserve BLE connection-event time on the live WiiM Remote 2 link. "
            "Takes no arguments: the target is discovered and validated here."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    # INFO, not configure_verbose_logging's WARNING default: "applied" is
    # the only journal confirmation that a reservation engaged.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    return run()


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
