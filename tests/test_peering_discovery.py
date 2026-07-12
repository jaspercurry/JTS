# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free contracts for peer mDNS discovery."""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from typing import Awaitable, Callable

import pytest
from zeroconf import ServiceInfo

from jasper.peering.discovery import (
    SERVICE_TYPE,
    PeerDiscovery,
    PeerGone,
    PeerSeen,
    _parse_service_info,
)


SERVICE_NAME = f"peer-one.{SERVICE_TYPE}"


class _StateChange(Enum):
    Added = auto()
    Updated = auto()
    Removed = auto()


class _FakeZeroconf:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def async_get_service_info(self, service_type: str, name: str):
        self.calls.append((service_type, name))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _DelayedFirstZeroconf:
    def __init__(self, first_response: object, later_response: object) -> None:
        self.first_response = first_response
        self.later_response = later_response
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.calls: list[tuple[str, str]] = []

    async def async_get_service_info(self, service_type: str, name: str):
        self.calls.append((service_type, name))
        if len(self.calls) == 1:
            self.first_started.set()
            await self.release_first.wait()
            return self.first_response
        return self.later_response


def _info(
    *,
    peer_id: str | bytes | None = b"peer-a",
    room: str | bytes | None = b"kitchen",
    primary: str | bytes | None = b"0",
    address: str | None = "192.0.2.10",
    port: int | None = 5354,
) -> ServiceInfo:
    properties: dict[str | bytes, str | bytes | None] = {
        b"peer_id": peer_id,
        b"room": room,
        b"primary": primary,
    }
    return ServiceInfo(
        SERVICE_TYPE,
        SERVICE_NAME,
        port=port,
        properties=properties,
        parsed_addresses=[] if address is None else [address],
    )


def _discovery(
    callback: Callable[[PeerSeen | PeerGone], Awaitable[None] | None] | None,
    *,
    self_peer_id: str = "self-peer",
) -> PeerDiscovery:
    discovery = PeerDiscovery(self_peer_id=self_peer_id)
    discovery._ServiceStateChange = _StateChange  # type: ignore[assignment]
    discovery._on_event = callback
    return discovery


async def _change(
    discovery: PeerDiscovery,
    zeroconf: object,
    state: _StateChange,
) -> None:
    await discovery._handle_change(
        zeroconf,
        SERVICE_TYPE,
        SERVICE_NAME,
        state,
    )


def test_parse_service_info_decodes_bytes_and_maps_fields() -> None:
    peer = _parse_service_info(
        SERVICE_NAME,
        _info(
            peer_id=b"peer-a",
            room=b"living-\xffroom",
            primary=b"1",
            address="192.0.2.25",
            port=6000,
        ),
    )

    assert peer == PeerSeen(
        peer_id="peer-a",
        room="living-\ufffdroom",
        primary=True,
        address="192.0.2.25",
        port=6000,
    )


def test_parse_service_info_defaults_legal_bare_optional_txt_values() -> None:
    peer = _parse_service_info(
        SERVICE_NAME,
        _info(peer_id=b"peer-a", room=None, primary=None),
    )

    assert peer is not None
    assert peer.room == "default"
    assert peer.primary is False


@pytest.mark.parametrize("peer_id", [None, b"", "  "])
def test_parse_service_info_rejects_missing_peer_id_without_raising(
    peer_id: str | bytes | None,
) -> None:
    assert _parse_service_info(SERVICE_NAME, _info(peer_id=peer_id)) is None


def test_parse_service_info_rejects_addressless_service() -> None:
    assert _parse_service_info(SERVICE_NAME, _info(address=None)) is None


def test_parse_service_info_uses_legacy_parsed_addresses_fallback() -> None:
    class _LegacyInfo:
        properties = {
            b"peer_id": b"peer-a",
            b"room": b"office",
            b"primary": b"0",
        }
        port = 5354

        def parsed_addresses(self) -> list[str]:
            return ["192.0.2.44"]

    peer = _parse_service_info(SERVICE_NAME, _LegacyInfo())

    assert peer is not None
    assert peer.address == "192.0.2.44"


def test_parse_service_info_is_fail_soft_for_malformed_external_record() -> None:
    class _MalformedInfo:
        properties = {b"peer_id": b"peer-a"}
        port = 5354

        def parsed_scoped_addresses(self) -> list[str]:
            raise RuntimeError("malformed address record")

    assert _parse_service_info(SERVICE_NAME, _MalformedInfo()) is None


def test_parse_service_info_is_fail_soft_for_invalid_port() -> None:
    class _InvalidPortInfo:
        properties = {b"peer_id": b"peer-a"}
        port = "not-a-port"

        def parsed_scoped_addresses(self) -> list[str]:
            return ["192.0.2.10"]

    assert _parse_service_info(SERVICE_NAME, _InvalidPortInfo()) is None


def test_parse_service_info_is_fail_soft_when_properties_access_raises() -> None:
    class _FailingPropertiesInfo:
        port = 5354

        @property
        def properties(self):
            raise RuntimeError("broken TXT record")

        def parsed_scoped_addresses(self) -> list[str]:
            return ["192.0.2.10"]

    assert _parse_service_info(SERVICE_NAME, _FailingPropertiesInfo()) is None


async def test_added_peer_is_stored_and_fired_to_sync_callback() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _FakeZeroconf(_info())

    await _change(discovery, zeroconf, _StateChange.Added)

    expected = PeerSeen("peer-a", "kitchen", False, "192.0.2.10", 5354)
    assert discovery.peers() == [expected]
    assert events == [expected]
    assert zeroconf.calls == [(SERVICE_TYPE, SERVICE_NAME)]


async def test_self_added_is_ignored() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)

    await _change(
        discovery,
        _FakeZeroconf(_info(peer_id=b"self-peer")),
        _StateChange.Added,
    )

    assert discovery.peers() == []
    assert events == []


async def test_same_identity_updated_refreshes_metadata_without_gone() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _FakeZeroconf(_info())
    await _change(discovery, zeroconf, _StateChange.Added)
    events.clear()
    zeroconf.response = _info(
        peer_id=b"peer-a",
        room=b"office",
        primary=b"1",
        address="192.0.2.11",
    )

    await _change(discovery, zeroconf, _StateChange.Updated)

    refreshed = PeerSeen("peer-a", "office", True, "192.0.2.11", 5354)
    assert discovery.peers() == [refreshed]
    assert events == [refreshed]


async def test_identity_updated_fires_gone_before_replacement_seen() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _FakeZeroconf(_info(peer_id=b"peer-a"))
    await _change(discovery, zeroconf, _StateChange.Added)
    events.clear()
    zeroconf.response = _info(
        peer_id=b"peer-b",
        room=b"bedroom",
        address="192.0.2.12",
    )

    await _change(discovery, zeroconf, _StateChange.Updated)

    replacement = PeerSeen("peer-b", "bedroom", False, "192.0.2.12", 5354)
    assert discovery.peers() == [replacement]
    assert events == [
        PeerGone(peer_id="peer-a", address="192.0.2.10"),
        replacement,
    ]


async def test_slow_added_cannot_overwrite_later_updated_identity() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _DelayedFirstZeroconf(
        _info(peer_id=b"peer-a", address="192.0.2.10"),
        _info(peer_id=b"peer-b", address="192.0.2.11"),
    )
    added = asyncio.create_task(
        _change(discovery, zeroconf, _StateChange.Added),
    )
    await zeroconf.first_started.wait()
    updated = asyncio.create_task(
        _change(discovery, zeroconf, _StateChange.Updated),
    )
    await asyncio.sleep(0)
    assert len(zeroconf.calls) == 1

    zeroconf.release_first.set()
    await asyncio.gather(added, updated)

    replacement = PeerSeen("peer-b", "kitchen", False, "192.0.2.11", 5354)
    assert discovery.peers() == [replacement]
    assert events == [
        PeerSeen("peer-a", "kitchen", False, "192.0.2.10", 5354),
        PeerGone(peer_id="peer-a", address="192.0.2.10"),
        replacement,
    ]


async def test_removed_waits_for_slow_added_without_peer_resurrection() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _DelayedFirstZeroconf(_info(), _info())
    added = asyncio.create_task(
        _change(discovery, zeroconf, _StateChange.Added),
    )
    await zeroconf.first_started.wait()
    removed = asyncio.create_task(
        _change(discovery, zeroconf, _StateChange.Removed),
    )
    await asyncio.sleep(0)
    assert discovery.peers() == []

    zeroconf.release_first.set()
    await asyncio.gather(added, removed)

    assert discovery.peers() == []
    assert events == [
        PeerSeen("peer-a", "kitchen", False, "192.0.2.10", 5354),
        PeerGone(peer_id="peer-a", address="192.0.2.10"),
    ]


async def test_foreign_to_self_update_evicts_previous_peer() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _FakeZeroconf(_info(peer_id=b"peer-a"))
    await _change(discovery, zeroconf, _StateChange.Added)
    events.clear()
    zeroconf.response = _info(peer_id=b"self-peer")

    await _change(discovery, zeroconf, _StateChange.Updated)

    assert discovery.peers() == []
    assert events == [PeerGone(peer_id="peer-a", address="192.0.2.10")]


async def test_removed_fires_gone_once_and_clears_bookkeeping() -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _FakeZeroconf(_info())
    await _change(discovery, zeroconf, _StateChange.Added)
    events.clear()

    await _change(discovery, zeroconf, _StateChange.Removed)
    await _change(discovery, zeroconf, _StateChange.Removed)

    assert discovery.peers() == []
    assert events == [PeerGone(peer_id="peer-a", address="192.0.2.10")]


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(None, id="null-resolution"),
        pytest.param(RuntimeError("resolver failed"), id="resolver-exception"),
        pytest.param(_info(peer_id=None), id="transient-invalid-record"),
    ],
)
async def test_failed_update_preserves_previous_peer(response: object) -> None:
    events: list[PeerSeen | PeerGone] = []
    discovery = _discovery(events.append)
    zeroconf = _FakeZeroconf(_info())
    await _change(discovery, zeroconf, _StateChange.Added)
    original = discovery.peers()[0]
    events.clear()
    zeroconf.response = response

    await _change(discovery, zeroconf, _StateChange.Updated)

    assert discovery.peers() == [original]
    assert events == []


async def test_async_callback_is_awaited() -> None:
    events: list[PeerSeen | PeerGone] = []

    async def on_event(event: PeerSeen | PeerGone) -> None:
        events.append(event)

    discovery = _discovery(on_event)

    await _change(discovery, _FakeZeroconf(_info()), _StateChange.Added)

    assert events == discovery.peers()


async def test_callback_exception_is_contained() -> None:
    def on_event(_event: PeerSeen | PeerGone) -> None:
        raise RuntimeError("consumer failed")

    discovery = _discovery(on_event)

    await _change(discovery, _FakeZeroconf(_info()), _StateChange.Added)

    assert len(discovery.peers()) == 1
