"""One-shot mDNS-SD browse primitive — the single place JTS resolves a
service type into the live set of instances on the LAN.

This is a faithful extraction of the `AsyncZeroconf` browse + resolve +
TXT/address parse that previously lived inline in
`jasper/web/rooms_setup.py:_discover_speakers`. It is a *move*, not a
rewrite: the browse/resolve/parse mechanics (IPv4-only zeroconf, the
Added/Updated name collection, the `AsyncServiceInfo.async_request` with a
3 s timeout, the `parsed_scoped_addresses()`/`parsed_addresses()` fallback,
the UTF-8-with-replacement TXT decode) match the original byte-for-byte. The
caller-specific bits stay with the caller:

  - Display-label derivation (TXT `name=` vs SRV host vs stripped instance
    name) is rooms-display policy — it stays in `rooms_setup`.
  - Self-filtering, port defaulting, and the TTL cache are likewise the
    caller's concern.

So this module returns the *raw, parsed* mDNS facts — full instance name,
SRV target host, every resolved address, the port from the SRV record, and
the decoded TXT dict — and lets each consumer apply its own policy.

Fail-soft by construction: `browse_once` never raises. Any failure (no
zeroconf installed, a multicast error, a single instance failing to
resolve) degrades to dropping that entry, and a total failure degrades to
`[]`. The `zeroconf` import is lazy so importing this module stays cheap
when no browse is performed (mirrors `jasper/peering/discovery.py`).

Deliberately NOT routed through here (so the boundary's scope is explicit):

  - `jasper/peering/discovery.py` — a *continuous*, event-driven browser
    (long-lived `AsyncServiceBrowser` reacting to Added/Removed over the
    speaker's lifetime), not a one-shot point-in-time scan. `browse_once`
    stands up and tears down a listener per call by design.
  - `jasper/speaker_name_discovery.py` — needs NAMES-ONLY across MULTIPLE
    service types and must INCLUDE instances that don't resolve to an
    address (a name conflict is real even with no A record). `browse_once`
    is single-type and drops address-less instances, the opposite of what a
    name-collision check needs.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredService:
    """One resolved mDNS-SD instance — the raw, parsed facts, no policy.

    Fields:
      name:      full mDNS instance name, e.g.
                 ``"JTS jasper-control on jts3._jasper-control._tcp.local."``
      server:    the SRV record's target host, e.g. ``"jts3.local."``
      addresses: every address the resolve yielded (scoped where available),
                 in the order zeroconf returned them. Never empty — an
                 instance that resolves to no address is dropped.
      port:      the port from the SRV record (``0`` if zeroconf reported
                 none; the caller decides whether to default it).
      txt:       the decoded TXT records (UTF-8 with replacement). Empty when
                 the service carries no TXT.
    """

    name: str
    server: str
    addresses: tuple[str, ...]
    port: int
    txt: dict[str, str]


def browse_once(service_type: str, *, timeout: float = 2.0) -> list[DiscoveredService]:
    """Best-effort one-shot mDNS-SD browse of ``service_type``.

    Stands up an IPv4-only zeroconf browser, collects every instance that
    appears (Added/Updated) over ``timeout`` seconds, resolves each, and
    returns the parsed instances. Per-instance failures are skipped; a total
    failure returns ``[]``. Never raises.

    ``service_type`` is a fully-qualified mDNS service type with the trailing
    dot, e.g. ``"_jasper-control._tcp.local."``.
    """

    async def _browse() -> list[DiscoveredService]:
        # Lazy import so module load stays cheap when no browse is done
        # (mirrors jasper/peering/discovery.py).
        try:
            from zeroconf import IPVersion, ServiceStateChange
            from zeroconf.asyncio import (
                AsyncServiceBrowser,
                AsyncServiceInfo,
                AsyncZeroconf,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("mdns: zeroconf unavailable: %s", e)
            return []

        # IPv4-only — home LAN, matches jasper/peering/discovery.py.
        aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        loop = asyncio.get_running_loop()
        names: list[str] = []

        def _on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
            # zeroconf calls us from its own thread; hop onto the loop thread
            # to mutate `names`. We only care about appearances for a one-shot
            # scan.
            if state_change in (
                ServiceStateChange.Added,
                ServiceStateChange.Updated,
            ):
                loop.call_soon_threadsafe(names.append, name)

        browser = AsyncServiceBrowser(
            aiozc.zeroconf, [service_type], handlers=[_on_change],
        )
        try:
            await asyncio.sleep(timeout)
            out: list[DiscoveredService] = []
            for name in list(dict.fromkeys(names)):  # de-dupe, keep order
                info = AsyncServiceInfo(service_type, name)
                try:
                    ok = await info.async_request(aiozc.zeroconf, 3000)
                except Exception:  # noqa: BLE001
                    logger.debug("mdns: resolve failed for %s", name, exc_info=True)
                    continue
                if not ok:
                    continue
                try:
                    addresses = (
                        info.parsed_scoped_addresses()
                        if hasattr(info, "parsed_scoped_addresses")
                        else info.parsed_addresses()
                    )
                except Exception:  # noqa: BLE001
                    addresses = []
                if not addresses:
                    continue
                # TXT records: bytes → utf-8 with replacement, matching
                # jasper/peering/discovery.py:_parse_service_info.
                txt: dict[str, str] = {}
                for k, v in (info.properties or {}).items():
                    try:
                        key = (
                            k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
                        )
                        val = (
                            v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
                        ) if v is not None else ""
                        txt[key] = val
                    except Exception:  # noqa: BLE001
                        continue
                out.append(
                    DiscoveredService(
                        name=name,
                        server=getattr(info, "server", "") or "",
                        addresses=tuple(str(a) for a in addresses),
                        port=int(info.port or 0),
                        txt=txt,
                    )
                )
            return out
        finally:
            try:
                await browser.async_cancel()
            finally:
                await aiozc.async_close()

    try:
        return asyncio.run(_browse())
    except Exception:  # noqa: BLE001
        logger.exception("mdns: browse_once(%s) failed", service_type)
        return []
