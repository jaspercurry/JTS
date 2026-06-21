# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest
from dbus_next.errors import DBusError

from jasper.bluetooth import adapter
from jasper.bluetooth import no_code_agent
from jasper.bluetooth.agent import (
    DEFAULT_AGENT_PATH,
    NoCodeAgent,
    REJECTED_DBUS_NAME,
    register_agent,
)


class _FakeProps:
    def __init__(
        self,
        calls: list[tuple[str, object]],
        fail_on: tuple[str, object] | None = None,
    ) -> None:
        self.calls = calls
        self.fail_on = fail_on

    async def call_set(self, _iface: str, key: str, value) -> None:
        self.calls.append((key, value.value))
        if self.fail_on == (key, value.value):
            raise RuntimeError(f"{key} failed")


class _FakeProxy:
    def __init__(self, props: _FakeProps) -> None:
        self._props = props

    def get_interface(self, name: str):
        if name == "org.freedesktop.DBus.Properties":
            return self._props
        return object()


class _FakeBus:
    def __init__(self, props: _FakeProps) -> None:
        self._props = props
        self.disconnected = False

    async def connect(self):
        return self

    async def introspect(self, _service: str, _path: str):
        return object()

    def get_proxy_object(self, _service: str, _path: str, _intro):
        return _FakeProxy(self._props)

    def disconnect(self) -> None:
        self.disconnected = True


def _patch_message_bus(monkeypatch, fail_on: tuple[str, object] | None = None):
    calls: list[tuple[str, object]] = []
    props = _FakeProps(calls, fail_on=fail_on)
    fake_bus = _FakeBus(props)

    def _factory(*_args, **_kwargs):
        return fake_bus

    monkeypatch.setattr(adapter, "MessageBus", _factory)
    return calls, fake_bus


def test_pairing_mode_on_sets_pairable_and_discoverable(monkeypatch):
    calls, fake_bus = _patch_message_bus(monkeypatch)

    asyncio.run(adapter.set_discoverable(True, timeout_sec=42))

    assert calls == [
        ("PairableTimeout", 42),
        ("Pairable", True),
        ("DiscoverableTimeout", 42),
        ("Discoverable", True),
    ]
    assert fake_bus.disconnected is True


def test_pairing_mode_on_rolls_back_if_open_fails(monkeypatch):
    calls, fake_bus = _patch_message_bus(
        monkeypatch,
        fail_on=("DiscoverableTimeout", 42),
    )

    with pytest.raises(RuntimeError, match="DiscoverableTimeout failed"):
        asyncio.run(adapter.set_discoverable(True, timeout_sec=42))

    assert calls == [
        ("PairableTimeout", 42),
        ("Pairable", True),
        ("DiscoverableTimeout", 42),
        ("Discoverable", False),
        ("Pairable", False),
        ("DiscoverableTimeout", 0),
        ("PairableTimeout", 0),
    ]
    assert fake_bus.disconnected is True


def test_pairing_mode_off_closes_pairable_and_discoverable(monkeypatch):
    calls, fake_bus = _patch_message_bus(monkeypatch)

    asyncio.run(adapter.set_discoverable(False))

    assert calls == [
        ("Discoverable", False),
        ("Pairable", False),
        ("DiscoverableTimeout", 0),
        ("PairableTimeout", 0),
    ]
    assert fake_bus.disconnected is True


def test_no_code_agent_startup_floor_uses_adapter_close_api():
    calls: list[bool] = []

    async def fake_close(value: bool) -> None:
        calls.append(value)

    ok = asyncio.run(
        no_code_agent._close_pairing_window_floor(
            "startup",
            close_pairing_window=fake_close,
        ),
    )

    assert ok is True
    assert calls == [False]


def test_no_code_agent_closes_pairable_when_window_is_not_open():
    calls: list[bool] = []

    async def fake_state() -> dict[str, bool]:
        return {"discoverable": False, "pairable": True}

    async def fake_close(value: bool) -> None:
        calls.append(value)

    closed = asyncio.run(
        no_code_agent._enforce_pairable_floor_once(
            read_state=fake_state,
            close_pairing_window=fake_close,
        ),
    )

    assert closed is True
    assert calls == [False]


def test_no_code_agent_leaves_open_pairing_window_alone():
    calls: list[bool] = []

    async def fake_state() -> dict[str, bool]:
        return {"discoverable": True, "pairable": True}

    async def fake_close(value: bool) -> None:
        calls.append(value)

    closed = asyncio.run(
        no_code_agent._enforce_pairable_floor_once(
            read_state=fake_state,
            close_pairing_window=fake_close,
        ),
    )

    assert closed is False
    assert calls == []


def _assert_rejected(exc: BaseException) -> None:
    assert isinstance(exc, DBusError)
    assert exc.type == REJECTED_DBUS_NAME


async def _agent_call(agent: NoCodeAgent, method_name: str, *args):
    method = getattr(agent, method_name)
    return await method.__wrapped__(agent, *args)  # type: ignore[attr-defined]


def _agent_call_sync(agent: NoCodeAgent, method_name: str, *args):
    method = getattr(agent, method_name)
    return method.__wrapped__(agent, *args)  # type: ignore[attr-defined]


def test_no_code_agent_rejects_interactive_pairing_methods():
    agent = NoCodeAgent()

    with pytest.raises(Exception) as pin:
        _agent_call_sync(agent, "RequestPinCode", "/dev")
    _assert_rejected(pin.value)

    with pytest.raises(Exception) as display_pin:
        _agent_call_sync(agent, "DisplayPinCode", "/dev", "123456")
    _assert_rejected(display_pin.value)

    with pytest.raises(Exception) as passkey:
        _agent_call_sync(agent, "RequestPasskey", "/dev")
    _assert_rejected(passkey.value)

    with pytest.raises(Exception) as display_passkey:
        _agent_call_sync(agent, "DisplayPasskey", "/dev", 123456, 0)
    _assert_rejected(display_passkey.value)

    with pytest.raises(Exception) as confirm:
        _agent_call_sync(agent, "RequestConfirmation", "/dev", 123456)
    _assert_rejected(confirm.value)


def test_no_code_agent_accepts_no_code_authorization():
    agent = NoCodeAgent()

    assert (
        asyncio.run(_agent_call(agent, "RequestAuthorization", "/dev"))
        is None
    )
    assert asyncio.run(
        _agent_call(
            agent,
            "AuthorizeService",
            "/dev",
            "0000110b-0000-1000-8000-00805f9b34fb",
        ),
    ) is None


def test_no_code_agent_trusts_authorized_devices():
    calls: list[tuple[str, object]] = []
    agent = NoCodeAgent(_FakeBus(_FakeProps(calls)))

    asyncio.run(_agent_call(agent, "RequestAuthorization", "/dev"))

    assert calls == [("Trusted", True)]


def test_no_code_agent_release_notifies_owner():
    released = False

    def _mark_released() -> None:
        nonlocal released
        released = True

    agent = NoCodeAgent(on_release=_mark_released)

    _agent_call_sync(agent, "Release")

    assert released is True


class _FakeAgentManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def call_register_agent(self, path: str, capability: str) -> None:
        self.calls.append(("register", path, capability))

    async def call_request_default_agent(self, path: str) -> None:
        self.calls.append(("default", path, None))


class _FakeAgentProxy:
    def __init__(self, manager: _FakeAgentManager) -> None:
        self._manager = manager

    def get_interface(self, _name: str):
        return self._manager


class _FakeAgentBus:
    def __init__(self, export_exc: Exception | None = None) -> None:
        self.export_exc = export_exc
        self.manager = _FakeAgentManager()

    def export(self, _path: str, _agent) -> None:
        if self.export_exc is not None:
            raise self.export_exc

    async def introspect(self, _service: str, _path: str):
        return object()

    def get_proxy_object(self, _service: str, _path: str, _intro):
        return _FakeAgentProxy(self.manager)


def test_register_agent_rejects_unexpected_export_failure():
    bus = _FakeAgentBus(RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(register_agent(bus, NoCodeAgent()))

    assert bus.manager.calls == []


def test_register_agent_allows_duplicate_local_export():
    bus = _FakeAgentBus(ValueError("agent path already exported"))

    asyncio.run(register_agent(bus, NoCodeAgent()))

    assert bus.manager.calls == [
        ("register", DEFAULT_AGENT_PATH, "NoInputNoOutput"),
        ("default", DEFAULT_AGENT_PATH, None),
    ]
