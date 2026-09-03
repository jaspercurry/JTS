# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib

import pytest
from dbus_next import Variant
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
        paired: bool = True,
    ) -> None:
        self.calls = calls
        self.fail_on = fail_on
        self.paired = paired

    async def call_get_all(self, _iface: str) -> dict:
        return {"Paired": Variant("b", self.paired)}

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


def test_pairing_mode_off_attempts_every_close_after_property_failure(monkeypatch):
    calls, fake_bus = _patch_message_bus(
        monkeypatch,
        fail_on=("Discoverable", False),
    )

    with pytest.raises(RuntimeError, match="Discoverable failed"):
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


@pytest.mark.parametrize(
    "authorization",
    (
        ("RequestAuthorization", ()),
        ("AuthorizeService", ("0000110b-0000-1000-8000-00805f9b34fb",)),
    ),
    ids=("pairing", "service"),
)
@pytest.mark.parametrize("paired", (True, False))
def test_no_code_agent_trusts_only_bonded_devices(authorization, paired):
    """Trust never reaches a device BlueZ has not bonded, from either entry.

    RequestAuthorization fires before the bond exists and AuthorizeService
    after it; both must honour the guard. Why:
    `NoCodeAgent._trust_device`.
    """
    method, extra_args = authorization
    calls: list[tuple[str, object]] = []
    agent = NoCodeAgent(_FakeBus(_FakeProps(calls, paired=paired)))

    asyncio.run(_agent_call(agent, method, "/dev", *extra_args))

    assert calls == ([("Trusted", True)] if paired else [])


class _FakeManagedProps:
    def __init__(self, writes: list[tuple[str, bool]], path: str) -> None:
        self._writes = writes
        self._path = path

    async def call_set(self, _iface: str, key: str, value) -> None:
        self._writes.append((self._path, bool(value.value)))


class _FakeManagedBus:
    """Enough ObjectManager to exercise untrust_unbonded's own decision."""

    def __init__(self, managed: dict, writes: list[tuple[str, bool]]) -> None:
        self._managed = managed
        self.writes = writes

    async def introspect(self, _service: str, _path: str):
        return object()

    def get_proxy_object(self, _service: str, path: str, _intro):
        managed = self._managed
        writes = self.writes

        class _Obj:
            def get_interface(self, name: str):
                if name == "org.freedesktop.DBus.ObjectManager":
                    class _OM:
                        async def call_get_managed_objects(self):
                            return managed
                    return _OM()
                return _FakeManagedProps(writes, path)

        return _Obj()

    def disconnect(self) -> None:
        return None


def _device(paired: bool, trusted: bool, address: str) -> dict:
    return {
        "org.bluez.Device1": {
            "Paired": Variant("b", paired),
            "Trusted": Variant("b", trusted),
            "Address": Variant("s", address),
        },
    }


def test_untrust_unbonded_drops_only_trusted_and_unpaired(monkeypatch):
    """The sweep's own decision, not a stand-in for it.

    Faking the sweep function pins the caller but leaves this predicate free:
    inverting the Paired test would untrust every healthy remote on the box
    and no bluetooth test would notice.
    """
    writes: list[tuple[str, bool]] = []
    managed = {
        "/org/bluez/hci0/dev_AA": _device(False, True, "AA"),   # strand -> drop
        "/org/bluez/hci0/dev_BB": _device(True, True, "BB"),    # healthy -> keep
        "/org/bluez/hci0/dev_CC": _device(False, False, "CC"),  # already off
        "/org/bluez/hci1/dev_DD": _device(False, True, "DD"),   # other adapter
    }
    bus = _FakeManagedBus(managed, writes)

    @contextlib.asynccontextmanager
    async def fake_bus():
        yield bus

    monkeypatch.setattr(adapter, "_system_bus", fake_bus)

    dropped = asyncio.run(adapter.untrust_unbonded("hci0"))

    assert dropped == ("AA",)
    assert writes == [("/org/bluez/hci0/dev_AA", False)]


def test_unbonded_trust_sweep_drops_and_reports(caplog):
    """Trust must not outlive the bond.

    Granting trust only to a bonded device is not enough: a pairing that was
    never bonded disappears on the next disconnect and leaves the trust
    behind, which is what strands a remote. The sweep also heals a device
    stranded before that guard existed.
    """
    async def sweep():
        return ("CA:AC:04:04:09:D7",)

    with caplog.at_level("INFO"):
        dropped = asyncio.run(
            no_code_agent._sweep_unbonded_trust_once(sweep=sweep),
        )

    assert dropped == ("CA:AC:04:04:09:D7",)
    assert any(
        "bluetooth_agent.untrusted_unbonded" in record.getMessage()
        for record in caplog.records
    )


def test_unbonded_trust_sweep_survives_a_failing_probe():
    """A sweep that cannot read BlueZ must not kill the agent's poll loop."""
    async def sweep():
        raise RuntimeError("bluez unreachable")

    assert asyncio.run(
        no_code_agent._sweep_unbonded_trust_once(sweep=sweep),
    ) == ()


def test_floor_watch_runs_the_unbonded_trust_sweep():
    """The sweep must run from the agent's existing poll, not just exist.

    Tested separately from the sweep itself: an isolated sweep test stays
    green when the call site is deleted, which leaves a stranded device
    un-healed with nothing failing.
    """
    swept = 0

    async def read_state():
        return {"pairable": False, "discoverable": False}

    async def close_pairing_window(_value):
        raise AssertionError("must not close a window it did not open")

    stop = asyncio.Event()

    async def sweep():
        nonlocal swept
        swept += 1
        stop.set()
        return ()

    async def scenario():
        # Bounded on purpose: a watch that never calls the sweep leaves `stop`
        # unset and would otherwise spin forever instead of failing.
        await asyncio.wait_for(
            no_code_agent._pairable_floor_watch(
                stop,
                interval=0.01,
                read_state=read_state,
                close_pairing_window=close_pairing_window,
                sweep=sweep,
            ),
            timeout=2.0,
        )

    try:
        asyncio.run(scenario())
    except asyncio.TimeoutError:
        pass

    assert swept == 1


def test_floor_needs_two_observations_before_closing_a_window():
    """A single observation would lower the bondable flag mid-pair.

    An outbound pair raises Pairable for the whole Pair() call -- up to its
    60 s timeout on a remote that needs a button press -- from a different
    process, and reads pairable-without-discoverable the entire time. Closing
    on the first sighting makes the bond silently not form, which is the
    defect this watch would cause rather than catch.
    """
    closes: list[bool] = []
    stop = asyncio.Event()
    passes = 0

    async def read_state():
        return {"pairable": True, "discoverable": False}

    async def close_pairing_window(value):
        closes.append(value)

    async def sweep():
        nonlocal passes
        passes += 1
        # Observe how many closes happened after ONE pass, then let a second
        # pass run and stop.
        if passes == 1:
            assert closes == [], "closed on a single observation"
        if passes >= 2:
            stop.set()
        return ()

    async def scenario():
        await asyncio.wait_for(
            no_code_agent._pairable_floor_watch(
                stop,
                interval=0.01,
                read_state=read_state,
                close_pairing_window=close_pairing_window,
                sweep=sweep,
            ),
            timeout=2.0,
        )

    try:
        asyncio.run(scenario())
    except asyncio.TimeoutError:
        pass

    assert closes == [False], "second consecutive observation must close"


class _AgentManagerStub:
    async def call_register_agent(self, _path: str, _capability: str) -> None:
        return None

    async def call_request_default_agent(self, _path: str) -> None:
        return None

    async def call_unregister_agent(self, _path: str) -> None:
        return None


class _CountingBus:
    """One system bus that records what a long-lived agent costs BlueZ.

    Counts the two recurring costs the floor watch used to pay per pass: a
    fresh bus connection (counted by the caller's MessageBus factory) and an
    introspection per object path.
    """

    def __init__(self, stop_after: int) -> None:
        self.introspects: list[str] = []
        self.sweeps = 0
        self.sets: list[tuple[str, object]] = []
        self.disconnected = False
        self._stop_after = stop_after
        self.done = asyncio.Event()

    async def connect(self):
        return self

    def export(self, _path: str, _agent) -> None:
        return None

    def unexport(self, _path: str) -> None:
        return None

    async def introspect(self, _service: str, path: str):
        self.introspects.append(path)
        return object()

    def get_proxy_object(self, _service: str, _path: str, _intro):
        return self

    def disconnect(self) -> None:
        self.disconnected = True

    # -- proxy-object surface ------------------------------------------
    def get_interface(self, name: str):
        if name == "org.bluez.AgentManager1":
            return _AgentManagerStub()
        return self

    async def call_get_all(self, _iface: str) -> dict:
        return {
            "Powered": Variant("b", True),
            "Pairable": Variant("b", False),
            "Discoverable": Variant("b", False),
        }

    async def call_set(self, _iface: str, key: str, value) -> None:
        self.sets.append((key, value.value))

    async def call_get_managed_objects(self) -> dict:
        self.sweeps += 1
        if self.sweeps >= self._stop_after:
            self.done.set()
        return {}


def test_agent_lifetime_opens_one_bus_connection(monkeypatch):
    """Steady-state idle must cost zero new connections and zero re-probing.

    The watch used to call two adapter helpers that each opened their own
    system bus and introspected BlueZ again, so an idle speaker paid
    2 connections + 2 introspections per pass forever.
    """
    monkeypatch.setattr(no_code_agent, "PAIRABLE_FLOOR_POLL_SEC", 0.01)
    bus = _CountingBus(stop_after=4)
    connections = 0

    def _factory(*_args, **_kwargs):
        nonlocal connections
        connections += 1
        return bus

    monkeypatch.setattr(no_code_agent, "MessageBus", _factory)
    monkeypatch.setattr(adapter, "MessageBus", _factory)

    stop_agent: list = []
    real_agent = no_code_agent.NoCodeAgent

    def _capture(bus_, on_release=None):
        stop_agent.append(on_release)
        return real_agent(bus_, on_release=on_release)

    monkeypatch.setattr(no_code_agent, "NoCodeAgent", _capture)

    async def scenario():
        run = asyncio.create_task(no_code_agent._run())
        await asyncio.wait_for(bus.done.wait(), timeout=5.0)
        stop_agent[0]()
        await asyncio.wait_for(run, timeout=5.0)

    asyncio.run(scenario())

    assert bus.sweeps >= 4
    assert connections == 1
    assert bus.introspects.count("/org/bluez/hci0") == 1
    assert bus.introspects.count("/") == 1
    assert bus.disconnected is True


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
