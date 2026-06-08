"""Unit tests for jasper.mdns — the ONE one-shot mDNS-SD browse primitive.

``browse_once`` is the shared, fail-soft browse+resolve+parse moved out of
``rooms_setup._discover_speakers``. These tests are strictly hardware-free:
they never stand up a real multicast listener. Two seams are exercised:

  1. **Fail-soft.** If zeroconf is unavailable (the lazy import inside
     ``_browse`` raises) OR ``asyncio.run`` blows up, ``browse_once``
     returns ``[]`` — never raises. The page above it must render an empty
     directory, not 500.

  2. **Parse mapping.** With a *fake* ``zeroconf`` injected into
     ``sys.modules`` (so the lazy ``from zeroconf import ...`` inside
     ``_browse`` resolves to fakes), a fake ``AsyncServiceInfo`` maps to a
     ``DiscoveredService`` with the right name / server / addresses / port /
     TXT — and the documented per-entry skips (failed resolve,
     address-less instance) drop that entry without failing the browse.

The fake ``AsyncServiceBrowser`` drives the handler synchronously at
construction (via ``loop.call_soon_threadsafe``, matching how real zeroconf
hops onto the loop thread), so a ``timeout=0`` browse still "sees" the
seeded instances without any wall-clock wait.
"""
from __future__ import annotations

import sys
import types

import pytest

from jasper import mdns
from jasper.mdns import DiscoveredService, browse_once


# ----------------------------------------------------------------------
# Fakes — a minimal `zeroconf` + `zeroconf.asyncio` surface matching the
# exact call shape jasper.mdns._browse uses.
# ----------------------------------------------------------------------


class _FakeInfo:
    """Stand-in for zeroconf.asyncio.AsyncServiceInfo.

    Seeded per instance name with the resolve outcome the test wants:
    ``ok`` (async_request return), ``server``, ``port``, ``addresses``
    (what parsed_scoped_addresses returns), and ``properties`` (raw bytes
    TXT, exactly as python-zeroconf hands them over)."""

    def __init__(self, type_, name):  # noqa: ANN001
        self._type = type_
        self.name = name
        spec = _RESOLVE_MAP.get(name, {})
        self._ok = spec.get("ok", True)
        self.server = spec.get("server", "")
        self.port = spec.get("port", 0)
        self._addresses = spec.get("addresses", [])
        self.properties = spec.get("properties", {})

    async def async_request(self, zc, timeout_ms):  # noqa: ANN001
        if isinstance(self._ok, Exception):
            raise self._ok
        return self._ok

    def parsed_scoped_addresses(self):
        return list(self._addresses)


class _FakeZeroconfCore:
    pass


class _FakeAsyncZeroconf:
    def __init__(self, *a, ip_version=None, **k):  # noqa: ANN001
        self.zeroconf = _FakeZeroconfCore()
        self.closed = False

    async def async_close(self):
        self.closed = True


class _FakeAsyncServiceBrowser:
    """Drives the handler for each seeded name at construction.

    Real zeroconf calls the handler from its own thread; ``_browse`` hops
    onto the loop via ``loop.call_soon_threadsafe``. We mirror that: schedule
    one ``Added`` callback per seeded name so a ``timeout=0`` browse collects
    them when the loop next runs."""

    def __init__(self, zc, types_, handlers=None):  # noqa: ANN001
        import asyncio

        loop = asyncio.get_running_loop()
        self.cancelled = False
        state_added = _FakeServiceStateChange.Added
        for h in handlers or []:
            for name in _BROWSE_NAMES:
                loop.call_soon_threadsafe(h, zc, types_[0], name, state_added)

    async def async_cancel(self):
        self.cancelled = True


class _FakeIPVersion:
    V4Only = "v4only"


class _FakeServiceStateChange:
    Added = "added"
    Updated = "updated"


# Per-test config the fakes read. Set by _install_fake_zeroconf.
_RESOLVE_MAP: dict = {}
_BROWSE_NAMES: list = []

# A small but non-zero browse window for the parse-mapping tests. The fake
# browser delivers names via ``loop.call_soon_threadsafe`` at construction;
# ``asyncio.sleep(0)`` has a fast-path that can resume before that ready-queue
# drains, so we give the loop one real timer tick (cheap — ~20 ms — and still
# hardware-free). Production never browses with a zero window either.
_T = 0.02


def _install_fake_zeroconf(monkeypatch, *, names, resolve):
    """Inject a fake ``zeroconf`` + ``zeroconf.asyncio`` into sys.modules so
    the lazy import inside ``_browse`` resolves to our fakes. ``names`` is the
    list of instance names the browser "discovers"; ``resolve`` maps each name
    to its resolve spec (ok/server/port/addresses/properties)."""
    global _RESOLVE_MAP, _BROWSE_NAMES
    _RESOLVE_MAP = dict(resolve)
    _BROWSE_NAMES = list(names)

    zc_mod = types.ModuleType("zeroconf")
    zc_mod.IPVersion = _FakeIPVersion
    zc_mod.ServiceStateChange = _FakeServiceStateChange

    aio_mod = types.ModuleType("zeroconf.asyncio")
    aio_mod.AsyncServiceBrowser = _FakeAsyncServiceBrowser
    aio_mod.AsyncServiceInfo = _FakeInfo
    aio_mod.AsyncZeroconf = _FakeAsyncZeroconf
    zc_mod.asyncio = aio_mod

    monkeypatch.setitem(sys.modules, "zeroconf", zc_mod)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", aio_mod)


# ----------------------------------------------------------------------
# Fail-soft: zeroconf unavailable / browse raises -> [].
# ----------------------------------------------------------------------


def test_browse_once_returns_empty_when_zeroconf_import_fails(monkeypatch):
    """The lazy ``from zeroconf import ...`` failing (no zeroconf installed)
    degrades to [] — never raises. Simulated by making the import raise."""
    # Poison the import: a module whose attribute access raises ImportError
    # on the names _browse pulls. Simplest is to remove zeroconf from
    # sys.modules and block re-import via a finder-free sentinel.
    monkeypatch.setitem(sys.modules, "zeroconf", None)  # forces ImportError
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", None)
    assert browse_once("_jasper-control._tcp.local.", timeout=0.0) == []


def test_browse_once_returns_empty_when_asyncio_run_raises(monkeypatch):
    """A total failure at the ``asyncio.run`` boundary (the outer guard)
    degrades to [] with a logged exception, never propagates."""
    def _boom(coro):
        # Close the coroutine so we don't leak a 'never awaited' warning.
        coro.close()
        raise RuntimeError("event loop exploded")

    monkeypatch.setattr(mdns.asyncio, "run", _boom)
    assert browse_once("_jasper-control._tcp.local.", timeout=0.0) == []


# ----------------------------------------------------------------------
# Parse mapping: a fake AsyncServiceInfo -> DiscoveredService.
# ----------------------------------------------------------------------


def test_browse_once_maps_fake_service_info(monkeypatch):
    """A resolved instance maps to a DiscoveredService with name / server /
    addresses / port / decoded TXT all populated from the (fake) info."""
    _install_fake_zeroconf(
        monkeypatch,
        names=["JTS jasper-control on jts3._jasper-control._tcp.local."],
        resolve={
            "JTS jasper-control on jts3._jasper-control._tcp.local.": {
                "ok": True,
                "server": "jts3.local.",
                "port": 8780,
                "addresses": ["192.168.1.9"],
                "properties": {b"name": b"Living Room", b"room": b"bedroom"},
            },
        },
    )
    out = browse_once("_jasper-control._tcp.local.", timeout=_T)
    assert len(out) == 1
    svc = out[0]
    assert isinstance(svc, DiscoveredService)
    assert svc.name == "JTS jasper-control on jts3._jasper-control._tcp.local."
    assert svc.server == "jts3.local."
    assert svc.addresses == ("192.168.1.9",)
    assert svc.port == 8780
    assert svc.txt == {"name": "Living Room", "room": "bedroom"}


def test_browse_once_decodes_txt_bytes_with_replacement(monkeypatch):
    """TXT keys/values are bytes from zeroconf; they decode UTF-8 with
    'replace' (invalid bytes become U+FFFD, never raise). A value of None
    (a bare key with no '=') decodes to ''."""
    name = "x._jasper-control._tcp.local."
    _install_fake_zeroconf(
        monkeypatch,
        names=[name],
        resolve={
            name: {
                "server": "x.local.",
                "port": 8780,
                "addresses": ["10.0.0.5"],
                # 0xFF is not valid UTF-8 -> U+FFFD; flag has value None.
                "properties": {b"label": b"caf\xc3\xa9", b"bad": b"\xff", b"flag": None},
            },
        },
    )
    out = browse_once("_jasper-control._tcp.local.", timeout=_T)
    assert len(out) == 1
    txt = out[0].txt
    assert txt["label"] == "café"
    assert txt["bad"] == "�"
    assert txt["flag"] == ""


def test_browse_once_skips_unresolvable_entry_keeps_others(monkeypatch):
    """A single instance failing to resolve (async_request raises OR returns
    False) is skipped — it does not fail the whole browse; siblings still
    come back."""
    raises = "raises._jasper-control._tcp.local."
    falsey = "falsey._jasper-control._tcp.local."
    good = "good._jasper-control._tcp.local."
    _install_fake_zeroconf(
        monkeypatch,
        names=[raises, falsey, good],
        resolve={
            raises: {"ok": RuntimeError("resolve boom")},
            falsey: {"ok": False},  # async_request returned False
            good: {
                "ok": True, "server": "good.local.", "port": 8780,
                "addresses": ["10.0.0.7"], "properties": {},
            },
        },
    )
    out = browse_once("_jasper-control._tcp.local.", timeout=_T)
    assert [s.server for s in out] == ["good.local."]


def test_browse_once_drops_addressless_instance(monkeypatch):
    """An instance that resolves but yields NO address is dropped — an
    address is the one field every consumer hard-requires."""
    no_addr = "noaddr._jasper-control._tcp.local."
    with_addr = "ok._jasper-control._tcp.local."
    _install_fake_zeroconf(
        monkeypatch,
        names=[no_addr, with_addr],
        resolve={
            no_addr: {"ok": True, "server": "noaddr.local.", "port": 8780,
                      "addresses": [], "properties": {}},
            with_addr: {"ok": True, "server": "ok.local.", "port": 8780,
                        "addresses": ["10.0.0.8"], "properties": {}},
        },
    )
    out = browse_once("_jasper-control._tcp.local.", timeout=_T)
    assert [s.server for s in out] == ["ok.local."]
    assert out[0].addresses == ("10.0.0.8",)


def test_browse_once_dedupes_repeated_names(monkeypatch):
    """The browser may report the same instance via both Added and Updated;
    ``_browse`` de-dupes by name so it resolves once, not N times."""
    name = "dup._jasper-control._tcp.local."
    # Seed the SAME name three times into the browse stream.
    _install_fake_zeroconf(
        monkeypatch,
        names=[name, name, name],
        resolve={
            name: {"ok": True, "server": "dup.local.", "port": 8780,
                   "addresses": ["10.0.0.9"], "properties": {}},
        },
    )
    out = browse_once("_jasper-control._tcp.local.", timeout=_T)
    assert len(out) == 1
    assert out[0].server == "dup.local."


def test_browse_once_defaults_port_to_zero_when_absent(monkeypatch):
    """When the SRV record reports no port, ``port`` is 0 (the caller — e.g.
    rooms_setup — decides whether to default it). Pinned so the primitive
    stays policy-free."""
    name = "p._jasper-control._tcp.local."
    _install_fake_zeroconf(
        monkeypatch,
        names=[name],
        resolve={
            name: {"ok": True, "server": "p.local.", "port": 0,
                   "addresses": ["10.0.0.10"], "properties": {}},
        },
    )
    out = browse_once("_jasper-control._tcp.local.", timeout=_T)
    assert out[0].port == 0


def test_discovered_service_is_frozen():
    """DiscoveredService is an immutable value object — the boundary contract
    other agents code against."""
    svc = DiscoveredService(
        name="n", server="s.local.", addresses=("1.2.3.4",), port=8780, txt={},
    )
    with pytest.raises(Exception):  # FrozenInstanceError (dataclasses)
        svc.port = 9999  # type: ignore[misc]
