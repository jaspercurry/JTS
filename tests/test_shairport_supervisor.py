"""Unit tests for jasper.control.shairport_supervisor.

Tests drive `_tick` directly with a probe/gate/restart trio overridden
on a subclass. This sidesteps the `run()` sleep loop entirely and pins
the policy contract:

  - Threshold consecutive probe failures → exactly one restart
  - Active-session gate suppresses restart and resets the counter
  - Rate limit blocks a second supervisor-driven restart in-window
  - Probe success resets the counter
  - Probe exception → counted as a failure
  - Gate exception → fails safe to "active" (no restart)

A separate group exercises the default RTSP probe against a real
asyncio TCP server.
"""
from __future__ import annotations

import asyncio
import contextlib

from jasper.control.shairport_supervisor import (
    ShairportSupervisor,
    _OPTIONS_REQUEST,
    snapshot,
)


# ---------- policy tests ----------


class _FakeSupervisor(ShairportSupervisor):
    """Drives `_tick` with scripted probe/gate/restart outcomes."""

    def __init__(self, **kw) -> None:
        super().__init__(
            interval_sec=0.0,
            jitter_sec=0.0,
            cold_start_sec=0.0,
            **kw,
        )
        self.probe_results: list = []
        self.gate_results: list = []
        self.restart_calls = 0
        self.now: float = 0.0

    async def probe(self) -> bool:
        result = self.probe_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def is_session_active(self) -> bool:
        if not self.gate_results:
            raise AssertionError(
                "_FakeSupervisor.gate_results exhausted — test "
                "under-scripted the gate. Each tick that reaches "
                "the threshold pops one entry."
            )
        result = self.gate_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def restart_shairport(self) -> None:
        self.restart_calls += 1

    def _now(self) -> float:
        return self.now


async def test_healthy_probe_keeps_counter_zero():
    sup = _FakeSupervisor()
    sup.probe_results = [True, True, True]
    for _ in range(3):
        await sup._tick()
    assert sup.consecutive_failures == 0
    assert sup.restart_calls == 0
    assert sup.last_probe_ok is True


async def test_one_failure_does_not_trigger_restart():
    sup = _FakeSupervisor(failure_threshold=3)
    sup.probe_results = [False]
    await sup._tick()
    assert sup.consecutive_failures == 1
    assert sup.restart_calls == 0


async def test_threshold_triggers_restart_when_no_active_session():
    sup = _FakeSupervisor(failure_threshold=3)
    sup.probe_results = [False, False, False]
    sup.gate_results = [False]
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 1
    assert sup.restart_count == 1
    assert sup.consecutive_failures == 0  # reset after action


async def test_active_session_suppresses_restart():
    sup = _FakeSupervisor(failure_threshold=3)
    sup.probe_results = [False, False, False]
    sup.gate_results = [True]
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 0
    assert sup.suppressed_count == 1
    assert sup.consecutive_failures == 0  # reset on suppress


async def test_success_resets_counter_between_failures():
    sup = _FakeSupervisor(failure_threshold=3)
    sup.probe_results = [False, False, True, False]
    for _ in range(4):
        await sup._tick()
    assert sup.consecutive_failures == 1
    assert sup.restart_calls == 0


async def test_rate_limit_blocks_second_restart_in_window():
    sup = _FakeSupervisor(failure_threshold=3, rate_limit_sec=600.0)
    sup.probe_results = [False] * 6
    sup.gate_results = [False, False]
    sup.now = 0.0
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 1
    sup.now = 300.0  # half-way through the window
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 1  # still blocked


async def test_rate_limit_allows_second_restart_after_window():
    sup = _FakeSupervisor(failure_threshold=3, rate_limit_sec=600.0)
    sup.probe_results = [False] * 6
    sup.gate_results = [False, False]
    sup.now = 0.0
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 1
    sup.now = 700.0  # past window
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 2


async def test_probe_exception_is_counted_as_failure():
    sup = _FakeSupervisor(failure_threshold=3)
    sup.probe_results = [RuntimeError("boom")] * 3
    sup.gate_results = [False]
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 1


async def test_gate_exception_fails_safe_to_active():
    """An unknown error in the gate must NOT cause a restart — the
    supervisor errs on 'don't disrupt a possibly-live listener.'"""
    sup = _FakeSupervisor(failure_threshold=3)
    sup.probe_results = [False] * 3
    sup.gate_results = [RuntimeError("dbus boom")]
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 0
    assert sup.suppressed_count == 1


async def test_snapshot_keys_and_values():
    sup = _FakeSupervisor()
    sup.probe_results = [True]
    await sup._tick()
    snap = sup.snapshot()
    assert set(snap.keys()) == {
        "enabled", "parked_by_role", "last_probe_at", "last_probe_ok",
        "consecutive_failures", "restart_count", "last_restart_at",
        "suppressed_count",
    }
    assert snap["enabled"] is True
    assert snap["last_probe_ok"] is True
    assert snap["consecutive_failures"] == 0
    assert snap["restart_count"] == 0


def test_module_snapshot_when_disabled():
    """`snapshot()` returns enabled=False when no supervisor has been
    started — the /state default for fresh installs and for
    JASPER_SHAIRPORT_SUPERVISOR=disabled."""
    assert snapshot() == {"enabled": False}


# ---------- default probe IO tests ----------


@contextlib.asynccontextmanager
async def _fake_rtsp_server(handler):
    """Run a fake RTSP server on localhost:0 and yield its port."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


async def test_default_probe_returns_true_on_200_response():
    async def handler(reader, writer):
        await reader.read(256)
        writer.write(
            b"RTSP/1.0 200 OK\r\n"
            b"CSeq: 0\r\n"
            b"Public: OPTIONS, ANNOUNCE, SETUP\r\n"
            b"\r\n"
        )
        await writer.drain()
        writer.close()

    async with _fake_rtsp_server(handler) as port:
        sup = ShairportSupervisor(port=port, probe_timeout_sec=1.0)
        assert await sup.probe() is True


async def test_default_probe_returns_false_on_non_200():
    async def handler(reader, writer):
        await reader.read(256)
        writer.write(b"RTSP/1.0 400 Bad Request\r\nCSeq: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async with _fake_rtsp_server(handler) as port:
        sup = ShairportSupervisor(port=port, probe_timeout_sec=1.0)
        assert await sup.probe() is False


async def test_default_probe_returns_false_on_connection_refused():
    # Bind to a free port, then close, so connect() refuses.
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    sup = ShairportSupervisor(port=port, probe_timeout_sec=1.0)
    assert await sup.probe() is False


async def test_default_probe_returns_false_when_server_never_responds():
    """The wedge signature: server accepts the connection but never
    sends a response. Probe must time out and return False — and within
    the configured `probe_timeout_sec`, not the OS default."""
    accepted = asyncio.Event()

    async def hang_forever(reader, writer):
        accepted.set()
        try:
            await asyncio.sleep(5.0)
        finally:
            writer.close()

    async with _fake_rtsp_server(hang_forever) as port:
        sup = ShairportSupervisor(port=port, probe_timeout_sec=0.2)
        result = await sup.probe()
    assert accepted.is_set()  # confirms we measured a hang, not a refusal
    assert result is False


async def test_default_is_session_active_fails_safe_when_busctl_missing(
    monkeypatch,
):
    """Contract: gate returns True (fail-safe to active) when the
    probe itself errors, so we never restart shairport on an unknown
    DBus state. Pins the documented behaviour in HANDOFF-resilience
    Tier 3 against a future refactor."""
    async def boom(*args, **kwargs):
        raise FileNotFoundError("no such file: busctl")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    sup = ShairportSupervisor()
    assert await sup.is_session_active() is True


async def test_default_is_session_active_fails_safe_on_non_zero_exit(
    monkeypatch,
):
    """Same fail-safe contract for the busctl-returned-non-zero case
    (DBus service missing, property absent, etc.)."""
    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    sup = ShairportSupervisor()
    assert await sup.is_session_active() is True


async def test_default_restart_invokes_systemctl_with_both_units(monkeypatch):
    """Pin the exact systemctl argv lists so a typo in unit names or
    a missing --no-block flag surfaces in CI rather than the first
    time the wedge happens in the wild."""
    invocations: list[tuple] = []

    class _FakeProc:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        invocations.append(args)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    sup = ShairportSupervisor()
    await sup.restart_shairport()
    assert invocations == [
        (
            "systemctl", "reset-failed",
            "shairport-sync.service", "nqptp.service",
        ),
        (
            "systemctl", "--no-block", "restart",
            "shairport-sync.service", "nqptp.service",
        ),
    ]


async def test_default_probe_sends_rfc_2326_options():
    """Pins the wire format. If a future shairport tightens its parser,
    we want the test to surface that rather than the probe silently
    becoming a noop."""
    received = bytearray()
    received_event = asyncio.Event()

    async def handler(reader, writer):
        data = await reader.read(256)
        received.extend(data)
        received_event.set()
        writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async with _fake_rtsp_server(handler) as port:
        sup = ShairportSupervisor(port=port, probe_timeout_sec=1.0)
        await sup.probe()
        await asyncio.wait_for(received_event.wait(), timeout=1.0)
    assert bytes(received) == _OPTIONS_REQUEST


async def test_bonded_follower_parks_the_probe():
    """The dumb-follower profile deliberately stops shairport-sync; the
    wedge probe must idle (no probe, no WARN buildup, no restart) and
    say so in the snapshot. Recovery: the first un-parked tick probes
    again from a clean counter."""
    sup = _FakeSupervisor(failure_threshold=3)
    sup.parked = True
    sup.shairport_parked_by_role = lambda: sup.parked  # type: ignore[method-assign]
    # No probe_results scripted — a probe would pop an empty list and
    # raise, so completing cleanly proves nothing probed.
    for _ in range(3):
        await sup._tick()
    assert sup.restart_calls == 0
    assert sup.consecutive_failures == 0
    assert sup.snapshot()["parked_by_role"] is True
    # Un-park: probing resumes with a clean confidence window.
    sup.parked = False
    sup.probe_results = [True]
    await sup._tick()
    assert sup.last_probe_ok is True
    assert sup.snapshot()["parked_by_role"] is False
