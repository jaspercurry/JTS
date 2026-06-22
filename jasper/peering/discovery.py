# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""mDNS-SD discovery of sibling JTS peers via python-zeroconf.

Browses for `_jasper-peer._udp.local.` and emits PeerSeen / PeerGone
events as siblings appear and disappear. Used only for the "is
anyone else on the network?" question — the arbitration messages
themselves go over our private multicast group, not via mDNS.

python-zeroconf coexists with the system Avahi daemon **in browse-
only mode** — we never publish from here. Advertising is handled by
the static XML file at /etc/avahi/services/jasper-peer.service (see
jasper.peering.avahi).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from jasper.log_event import log_event

logger = logging.getLogger(__name__)


SERVICE_TYPE = "_jasper-peer._udp.local."


@dataclass(frozen=True)
class PeerSeen:
    peer_id: str
    room: str
    primary: bool
    address: str  # IPv4 string of the sibling
    port: int


@dataclass(frozen=True)
class PeerGone:
    peer_id: str
    address: str


class PeerDiscovery:
    """Async wrapper around AsyncZeroconf + AsyncServiceBrowser.

    Lifecycle:
      d = PeerDiscovery(self_peer_id="our-uuid")
      await d.start(on_event=callback)
      ...
      await d.stop()

    on_event is called for both PeerSeen and PeerGone; it's
    responsible for routing events into the peering daemon's state.

    Self-discovery (we always see our own advertisement) is filtered
    out before the callback fires — `self_peer_id` is the UUID we
    advertise via Avahi, matched against the `peer_id` TXT record.
    """

    def __init__(self, *, self_peer_id: str) -> None:
        self._self_peer_id = self_peer_id
        self._on_event: Optional[
            Callable[[PeerSeen | PeerGone], Awaitable[None] | None]
        ] = None
        self._zc = None  # type: ignore[var-annotated]  # AsyncZeroconf, lazy-imported
        self._browser = None  # type: ignore[var-annotated]
        # Track current peers so we can emit clean PeerGone events on
        # zeroconf's REMOVED notifications (which only give us the
        # service name, not the TXT records). Keyed by service name.
        self._peers: dict[str, PeerSeen] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(
        self,
        on_event: Callable[[PeerSeen | PeerGone], Awaitable[None] | None],
    ) -> None:
        # Lazy import keeps the module import cheap when peering is off
        # — zeroconf only loads if we actually start a browser.
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        self._on_event = on_event
        self._loop = asyncio.get_running_loop()
        # IPv4-only — our multicast group is v4 and dual-stack adds
        # complexity for zero benefit on a home LAN.
        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)

        # Bound method captures self via closure. zeroconf calls this
        # from its own thread; we schedule the actual handling on our
        # asyncio loop so the state machine stays single-threaded.
        def on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
            self._loop.call_soon_threadsafe(  # type: ignore[union-attr]
                lambda: asyncio.ensure_future(
                    self._handle_change(zeroconf, service_type, name, state_change),
                )
            )

        # Capture the imported ServiceStateChange enum on the instance
        # so _handle_change doesn't need its own import. (Avoids
        # importing zeroconf from module scope, which would prevent
        # the module from loading when peering is off.)
        self._ServiceStateChange = ServiceStateChange  # type: ignore[attr-defined]

        self._browser = AsyncServiceBrowser(
            self._zc.zeroconf,
            [SERVICE_TYPE],
            handlers=[on_change],
        )
        log_event(
            logger,
            "peering.discovery.started",
            service=SERVICE_TYPE,
            self=self._self_peer_id,
        )

    async def stop(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:  # noqa: BLE001
                logger.exception("peering: browser cancel failed")
            self._browser = None
        if self._zc is not None:
            try:
                await self._zc.async_close()
            except Exception:  # noqa: BLE001
                logger.exception("peering: zeroconf close failed")
            self._zc = None
        self._peers.clear()
        log_event(logger, "peering.discovery.stopped")

    def peers(self) -> list[PeerSeen]:
        """Snapshot of currently-seen peers (excluding self)."""
        return list(self._peers.values())

    # ---- internal ----

    async def _handle_change(
        self, zeroconf, service_type: str, name: str, state_change,
    ) -> None:
        ServiceStateChange = self._ServiceStateChange  # type: ignore[attr-defined]

        if state_change is ServiceStateChange.Added or state_change is ServiceStateChange.Updated:
            try:
                info = await zeroconf.async_get_service_info(service_type, name)
            except Exception:  # noqa: BLE001
                logger.exception("peering: service_info failed for %s", name)
                return
            if info is None:
                logger.debug("peering: got null service_info for %s", name)
                return
            peer = _parse_service_info(name, info)
            if peer is None:
                return
            if peer.peer_id == self._self_peer_id:
                return  # ignore our own ad
            self._peers[name] = peer
            log_event(
                logger,
                "peering.discovery.peer_seen",
                peer=peer.peer_id,
                room=peer.room,
                primary=int(peer.primary),
                addr=peer.address,
            )
            await self._fire(peer)
        elif state_change is ServiceStateChange.Removed:
            peer = self._peers.pop(name, None)
            if peer is None:
                return
            log_event(
                logger,
                "peering.discovery.peer_gone",
                peer=peer.peer_id,
                addr=peer.address,
            )
            await self._fire(PeerGone(peer_id=peer.peer_id, address=peer.address))

    async def _fire(self, event: PeerSeen | PeerGone) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            logger.exception("peering: discovery on_event callback raised")


def _parse_service_info(name: str, info) -> Optional[PeerSeen]:
    """Extract peer metadata from a zeroconf ServiceInfo."""
    try:
        properties = {
            k.decode("utf-8", errors="replace") if isinstance(k, bytes) else k:
                v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }
    except Exception:  # noqa: BLE001
        logger.exception("peering: TXT decode failed for %s", name)
        return None

    peer_id = properties.get("peer_id", "").strip()
    if not peer_id:
        logger.debug("peering: %s missing peer_id TXT record", name)
        return None

    addresses = info.parsed_scoped_addresses() if hasattr(info, "parsed_scoped_addresses") else info.parsed_addresses()
    if not addresses:
        logger.debug("peering: %s has no addresses", name)
        return None

    return PeerSeen(
        peer_id=peer_id,
        room=properties.get("room", "").strip() or "default",
        primary=properties.get("primary", "0").strip() == "1",
        address=str(addresses[0]),
        port=int(info.port or 0),
    )
