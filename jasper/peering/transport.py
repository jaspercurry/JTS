"""Multicast UDP transport for peering messages.

Five message types, one JSON object per UDP datagram, max ~300 bytes.
Wire schema is documented inline. Malformed packets are silently
dropped (a buggy neighbor on the same multicast group shouldn't take
down the fleet).

Socket setup follows RFC 6762 (mDNS) / RFC 2365 (admin-local scope)
plus the standard Linux IP_MULTICAST_* knobs:

  - TTL = 1 (single subnet — packet dies at first router hop)
  - LOOP = 1 (we receive our own multicast — useful for self-test
    of the local multicast path, and natural for the gossip protocol
    where the sender is also a participant)
  - REUSEPORT (allow multiple processes on the host to join the same
    group; matches python-zeroconf's pattern)
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .config import MULTICAST_GROUP, MULTICAST_PORT, MULTICAST_TTL
from .rank import WakeReport

logger = logging.getLogger(__name__)


# Message type identifiers (kept short on the wire). The `proto` field
# is bumped when we make a breaking change; receivers reject anything
# with a higher proto than they understand.
PROTO_VERSION = 1


# Max size we'll accept from recvfrom. Real messages are <300 bytes;
# this cap protects against deliberately-large packets eating memory.
MAX_DATAGRAM_BYTES = 4096


# Parsed message types — receivers get one of these out of recv().
# Frozen dataclasses with slots: ~25% smaller than __dict__-backed
# instances on Pi 5, immutable so the state machine can stash them
# without defensive copying, generated __init__/__repr__/__eq__.

class IncomingMessage:
    """Marker base. Use isinstance() to dispatch.

    Empty __slots__ here lets subclasses' `@dataclass(slots=True)` take
    full effect — otherwise the inherited __dict__ defeats the memory
    win we're after.
    """
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class IncomingHello(IncomingMessage):
    peer_id: str
    room: str
    primary: bool
    ts_ns: int


@dataclass(frozen=True, slots=True)
class IncomingWake(IncomingMessage):
    epoch: str
    report: WakeReport
    ts_ns: int


@dataclass(frozen=True, slots=True)
class IncomingClaim(IncomingMessage):
    epoch: str
    peer_id: str
    ts_ns: int


@dataclass(frozen=True, slots=True)
class IncomingHeartbeat(IncomingMessage):
    epoch: str
    peer_id: str
    ts_ns: int


@dataclass(frozen=True, slots=True)
class IncomingEnd(IncomingMessage):
    epoch: str
    peer_id: str
    reason: str
    ts_ns: int


# ---------- Encoding ----------


def _envelope(t: str, peer_id: str, ts_ns: int, **extra) -> bytes:
    """Build a compact JSON datagram with the common envelope fields.

    Every message carries `t` (type), `proto` (version), `peer`
    (sender id), `ts` (monotonic ns — local to sender, for tracing
    only). Encoders pass any type-specific fields as kwargs."""
    return _encode({
        "t": t,
        "proto": PROTO_VERSION,
        "peer": peer_id,
        "ts": ts_ns,
        **extra,
    })


def encode_hello(peer_id: str, room: str, primary: bool, ts_ns: int) -> bytes:
    return _envelope(
        "HELLO", peer_id, ts_ns,
        room=room, primary=int(bool(primary)),
    )


def encode_wake(epoch: str, report: WakeReport, ts_ns: int) -> bytes:
    return _envelope(
        "WAKE", report.peer_id, ts_ns,
        epoch=epoch,
        score=round(report.score, 4),
        snr_db=round(report.snr_db, 2) if report.snr_db is not None else None,
        rms_dbfs=round(report.rms_dbfs, 2) if report.rms_dbfs is not None else None,
        primary=int(bool(report.primary)),
        can_serve=int(bool(report.can_serve)),
    )


def encode_claim(epoch: str, peer_id: str, ts_ns: int) -> bytes:
    return _envelope("CLAIM", peer_id, ts_ns, epoch=epoch)


def encode_heartbeat(epoch: str, peer_id: str, ts_ns: int) -> bytes:
    return _envelope("HEART", peer_id, ts_ns, epoch=epoch)


def encode_end(epoch: str, peer_id: str, reason: str, ts_ns: int) -> bytes:
    return _envelope(
        "END", peer_id, ts_ns,
        epoch=epoch,
        reason=reason[:64],  # cap to keep datagram small
    )


def _encode(obj: dict) -> bytes:
    """Compact JSON encoding (no whitespace) — keeps datagrams small."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


# ---------- Decoding ----------


def decode(raw: bytes) -> Optional[IncomingMessage]:
    """Parse a raw datagram. Returns None for any malformed input —
    silently dropping a bad packet is correct behavior here. We log at
    DEBUG so a misbehaving neighbor doesn't fill the journal.
    """
    try:
        msg = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.debug("peering: dropped malformed datagram: %s", e)
        return None
    if not isinstance(msg, dict):
        logger.debug("peering: dropped non-object datagram")
        return None
    proto = msg.get("proto")
    if proto != PROTO_VERSION:
        # Future-proof: if a peer running newer software arrives, drop
        # politely rather than crash. Once we have a v2, the receiver
        # will need to handle multiple protos explicitly.
        logger.debug("peering: dropped proto=%r (expected %d)", proto, PROTO_VERSION)
        return None
    t = msg.get("t")
    try:
        if t == "HELLO":
            return IncomingHello(
                peer_id=str(msg["peer"]),
                room=str(msg.get("room", "")),
                primary=bool(msg.get("primary", 0)),
                ts_ns=int(msg.get("ts", 0)),
            )
        if t == "WAKE":
            return IncomingWake(
                epoch=str(msg["epoch"]),
                ts_ns=int(msg.get("ts", 0)),
                report=WakeReport(
                    peer_id=str(msg["peer"]),
                    score=float(msg["score"]),
                    snr_db=_maybe_float(msg.get("snr_db")),
                    rms_dbfs=_maybe_float(msg.get("rms_dbfs")),
                    primary=bool(msg.get("primary", 0)),
                    can_serve=bool(msg.get("can_serve", 1)),
                ),
            )
        if t == "CLAIM":
            return IncomingClaim(
                epoch=str(msg["epoch"]),
                peer_id=str(msg["peer"]),
                ts_ns=int(msg.get("ts", 0)),
            )
        if t == "HEART":
            return IncomingHeartbeat(
                epoch=str(msg["epoch"]),
                peer_id=str(msg["peer"]),
                ts_ns=int(msg.get("ts", 0)),
            )
        if t == "END":
            return IncomingEnd(
                epoch=str(msg["epoch"]),
                peer_id=str(msg["peer"]),
                reason=str(msg.get("reason", ""))[:64],
                ts_ns=int(msg.get("ts", 0)),
            )
        logger.debug("peering: dropped unknown t=%r", t)
        return None
    except (KeyError, ValueError, TypeError) as e:
        logger.debug("peering: dropped bad %s payload: %s", t, e)
        return None


def _maybe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- Socket plumbing ----------


def open_multicast_socket(
    *,
    group: str = MULTICAST_GROUP,
    port: int = MULTICAST_PORT,
    ttl: int = MULTICAST_TTL,
    bind_addr: str = "0.0.0.0",
) -> socket.socket:
    """Open a UDP socket configured for our peering multicast group.

    Returns a non-blocking socket suitable for use with asyncio's
    loop.add_reader() / sock_sendto() helpers. Caller owns close().

    Idiomatic Linux multicast socket setup — same options
    python-zeroconf uses for the mDNS group.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setblocking(False)

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # SO_REUSEPORT is the Linux-correct version for multicast. On
    # macOS in tests it also exists (BSD-origin), so this is portable
    # for our developer workflow. Wrap in try in case we're on a
    # platform that doesn't expose it (Windows).
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    sock.bind((bind_addr, port))

    # Outbound TTL: 1 = single subnet, dies at first router hop.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

    # We receive our own multicast (LOOP=1). The state machine ignores
    # our own messages by peer_id, but LOOP=1 is useful for a
    # multicast-health self-test ("did my HELLO come back to me?").
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    # Join the multicast group on the default outbound interface.
    # struct mreq: 4 bytes group + 4 bytes interface; INADDR_ANY for
    # interface = "let the kernel pick the default outbound iface".
    mreq = struct.pack(
        "4sl", socket.inet_aton(group), socket.INADDR_ANY,
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    return sock


class MulticastTransport:
    """Async wrapper around a multicast UDP socket.

    Lifecycle:
      t = MulticastTransport()
      await t.start(loop, on_message=callback)  # spawns recv task
      await t.send(encode_hello(...))
      await t.stop()
    """

    def __init__(
        self,
        *,
        group: str = MULTICAST_GROUP,
        port: int = MULTICAST_PORT,
        ttl: int = MULTICAST_TTL,
    ) -> None:
        self._group = group
        self._port = port
        self._ttl = ttl
        self._sock: Optional[socket.socket] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._on_message: Optional[Callable[[IncomingMessage, str], Awaitable[None] | None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = asyncio.Event()

    async def start(
        self,
        on_message: Callable[[IncomingMessage, str], Awaitable[None] | None],
    ) -> None:
        if self._sock is not None:
            raise RuntimeError("MulticastTransport.start() called twice")
        self._sock = open_multicast_socket(
            group=self._group, port=self._port, ttl=self._ttl,
        )
        self._on_message = on_message
        self._loop = asyncio.get_running_loop()
        self._stopped.clear()
        self._recv_task = self._loop.create_task(
            self._recv_loop(), name="peering-mcast-recv",
        )
        logger.info(
            "event=peering.transport.started group=%s port=%d ttl=%d",
            self._group, self._port, self._ttl,
        )

    async def stop(self) -> None:
        self._stopped.set()
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._recv_task = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        logger.info("event=peering.transport.stopped")

    async def send(self, payload: bytes) -> None:
        """Send one datagram to the multicast group. Best-effort —
        OS-level errors are logged and swallowed (we don't want a
        transient send failure to crash arbitration)."""
        if self._sock is None:
            logger.debug("peering: send before start; dropped")
            return
        try:
            await self._loop.sock_sendto(  # type: ignore[union-attr]
                self._sock, payload, (self._group, self._port),
            )
        except OSError as e:
            logger.warning("peering: send failed: %s", e)

    async def _recv_loop(self) -> None:
        assert self._sock is not None and self._loop is not None
        while not self._stopped.is_set():
            try:
                data, addr = await self._loop.sock_recvfrom(
                    self._sock, MAX_DATAGRAM_BYTES,
                )
            except asyncio.CancelledError:
                raise
            except OSError as e:
                # Transient socket errors (kernel buffer fill, etc.) —
                # log at WARNING and keep going.
                logger.warning("peering: recv failed: %s", e)
                await asyncio.sleep(0.1)
                continue
            msg = decode(data)
            if msg is None:
                continue
            try:
                result = self._on_message(msg, addr[0]) if self._on_message else None
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("peering: on_message callback raised")
