# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for jasper.peering.daemon.

Exercises the orchestrator end-to-end with a mocked transport: we
monkey-patch the multicast transport so tests don't open real sockets.
The state machine and dispatch logic run unmodified.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from jasper.peering import daemon as daemon_mod
from jasper.peering.config import PeeringConfig, PeeringMode
from jasper.peering.rank import WakeReport
from jasper.peering.transport import (
    IncomingClaim,
    IncomingWake,
)
from tests._socket_paths import short_unix_socket_path as _short_socket_path


def _cfg(mode=PeeringMode.ON, primary=False) -> PeeringConfig:
    return PeeringConfig(
        mode=mode,
        peer_id="alice-uuid",
        room="kitchen",
        primary=primary,
        arb_window_ms=80,        # short so tests run fast
        break_threshold=0.85,
    )


@pytest.fixture(autouse=True)
def _silence_avahi(monkeypatch):
    """Don't touch real avahi-daemon during tests."""
    monkeypatch.setattr(daemon_mod.avahi, "render_and_install", lambda **kw: True)
    monkeypatch.setattr(daemon_mod.avahi, "uninstall", lambda **kw: None)


@pytest.fixture
def avahi_uninstalls(monkeypatch, _silence_avahi):
    """Records every ``avahi.uninstall()``; ``stop()`` is its only caller."""
    calls: list[int] = []
    monkeypatch.setattr(
        daemon_mod.avahi, "uninstall", lambda **kw: calls.append(1),
    )
    return calls


@pytest.fixture(autouse=True)
def _fake_uds_path(monkeypatch):
    """Point the daemon's UDS at a short tmp path so it fits in sun_path."""
    monkeypatch.setattr(daemon_mod, "PEERING_UDS_PATH", _short_socket_path())


class _FakeTransport:
    """Mock MulticastTransport that captures sent bytes + lets tests
    inject incoming messages."""

    def __init__(self, *, raise_on_start: BaseException | None = None, **kwargs):
        self.sent: list[bytes] = []
        self._on_message = None
        self.started = False
        self.stopped = False
        self._raise_on_start = raise_on_start

    async def start(self, on_message):
        if self._raise_on_start is not None:
            raise self._raise_on_start
        self.started = True
        self._on_message = on_message

    async def stop(self):
        self.stopped = True

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def inject(self, msg) -> None:
        """Simulate an inbound multicast message."""
        if self._on_message is not None:
            result = self._on_message(msg)
            if asyncio.iscoroutine(result):
                await result


async def _await_pending_epoch(d, timeout: float = 2.0) -> None:
    """Block until the daemon publishes a pending epoch.

    Raises TimeoutError if it never appears, so an arbitration that
    fails to start fails the test loudly instead of racing a sleep.
    """
    async def _poll() -> None:
        while d._pending_epoch is None:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout)


@pytest_asyncio.fixture
async def daemon_setup(monkeypatch):
    """Start a PeeringDaemon with a mocked transport."""
    transport = _FakeTransport()
    monkeypatch.setattr(daemon_mod, "MulticastTransport", lambda **kw: transport)

    d = daemon_mod.PeeringDaemon(_cfg(mode=PeeringMode.ON))
    await d.start()
    try:
        yield d, transport
    finally:
        await d.stop()


# ---------- start() failure paths ----------


@pytest.mark.parametrize(
    ("interrupt_at", "start_raises", "transport_torn_down"),
    (
        # The bind itself fails, so start() re-raises and leaves no
        # bound transport for stop() to release.
        ("multicast_bind", True, False),
        # The transport is already bound when the UDS listen fails, so
        # start() re-raises and stop() must release the socket rather
        # than leave it holding the fd with its recv task pending.
        ("uds_listen", True, True),
    ),
)
async def test_start_failure_unwinds_exactly_what_it_acquired(
    monkeypatch,
    avahi_uninstalls,
    interrupt_at,
    start_raises,
    transport_torn_down,
):
    """However far start() got, stop() releases exactly that and no
    more. The advert always goes: stop() is the only caller of
    avahi.uninstall(), so whatever it skips leaves the box advertising
    _jasper-peer._udp for a daemon that cannot arbitrate, until someone
    edits it by hand."""
    bind_fails = interrupt_at == "multicast_bind"
    transport = _FakeTransport(
        raise_on_start=OSError("address in use") if bind_fails else None,
    )
    monkeypatch.setattr(daemon_mod, "MulticastTransport", lambda **kw: transport)

    async def _boom(**kw):
        raise OSError("socket path too long")

    if not bind_fails:
        monkeypatch.setattr(daemon_mod.uds, "serve", _boom)

    d = daemon_mod.PeeringDaemon(_cfg(mode=PeeringMode.ON))
    if start_raises:
        with pytest.raises(OSError):
            await d.start()
    else:
        await d.start()

    await d.stop()
    assert transport.stopped is transport_torn_down
    assert avahi_uninstalls == [1]


# ---------- mode=OFF: nothing happens ----------


async def test_mode_off_start_is_noop(monkeypatch, avahi_uninstalls):
    """An OFF household is the default: neither start() nor stop() may
    reach the filesystem or spawn an avahi reload on a jasper-control
    restart."""
    transport_constructed = []
    monkeypatch.setattr(
        daemon_mod, "MulticastTransport",
        lambda **kw: transport_constructed.append(1) or _FakeTransport(),
    )
    d = daemon_mod.PeeringDaemon(_cfg(mode=PeeringMode.OFF))
    await d.start()
    # No transport, no UDS, nothing.
    assert transport_constructed == []
    assert d._uds_server is None
    assert d._transport is None
    await d.stop()  # safe to call even though start was a noop
    assert avahi_uninstalls == []


async def test_stop_cancels_retained_send_tasks(daemon_setup):
    daemon, transport = daemon_setup
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_send(_payload: bytes) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    transport.send = blocked_send
    daemon._spawn_send(b"payload")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert len(daemon._send_tasks) == 1
    await daemon.stop()

    assert cancelled.is_set()
    assert daemon._send_tasks == set()


async def test_stop_refuses_send_spawned_while_cancellation_yields(daemon_setup):
    daemon, transport = daemon_setup
    started: list[bytes] = []
    first_cancelled = asyncio.Event()

    async def blocked_send(payload: bytes) -> None:
        started.append(payload)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancelled.set()
            # Reproduce a state-machine action racing with stop's gather. The
            # late send must be refused, not created and then erased un-drained.
            daemon._spawn_send(b"late")
            await asyncio.sleep(0)
            raise

    transport.send = blocked_send
    daemon._spawn_send(b"first")
    while started != [b"first"]:
        await asyncio.sleep(0)

    await daemon.stop()

    assert first_cancelled.is_set()
    assert started == [b"first"]
    assert daemon._send_tasks == set()


# ---------- mode=ON, solo arbitration ----------


async def test_solo_arbitrate_wins(daemon_setup):
    """No peer reports during the arb window → we win our own
    arbitration."""
    d, transport = daemon_setup
    result = await d._handle_arbitrate({
        "score": 0.8, "snr_db": 18.0, "rms_dbfs": -22.0, "can_serve": True,
    })
    assert result["result"] == "WIN"
    assert result["epoch"]  # non-empty


async def test_solo_arbitrate_broadcasts_wake_then_claim(daemon_setup):
    """A winning solo arbitration produces a WAKE broadcast immediately
    + a CLAIM broadcast after the window closes."""
    d, transport = daemon_setup
    await d._handle_arbitrate({
        "score": 0.8, "snr_db": 18.0, "rms_dbfs": -22.0, "can_serve": True,
    })
    # Both should be in transport.sent. Verify by decoding.
    from jasper.peering.transport import decode, IncomingClaim, IncomingWake
    parsed = [decode(p) for p in transport.sent]
    assert any(isinstance(m, IncomingWake) for m in parsed)
    assert any(isinstance(m, IncomingClaim) for m in parsed)


# ---------- mode=ON, peer outbids us ----------


async def test_peer_higher_confidence_makes_us_lose(daemon_setup):
    """We bid 0.6; bob bids 0.9. We should lose after the window
    closes."""
    d, transport = daemon_setup

    # Start arbitration in the background — it'll block on the arb
    # window timer.
    task = asyncio.create_task(d._handle_arbitrate({
        "score": 0.6, "snr_db": 10.0, "rms_dbfs": -25.0, "can_serve": True,
    }))
    await _await_pending_epoch(d)
    epoch = d._pending_epoch
    assert epoch is not None

    # Bob's WAKE arrives during the window with higher confidence.
    bob_report = WakeReport(
        peer_id="bob-uuid", score=0.9, snr_db=22.0, rms_dbfs=-15.0,
        primary=False, can_serve=True,
    )
    await transport.inject(IncomingWake(epoch=epoch, report=bob_report, ts_ns=0))

    # Wait for arb window to close + dispatch to complete.
    result = await task
    assert result["result"] == "LOSE"


async def test_foreign_claim_makes_us_lose(daemon_setup):
    """If a foreign CLAIM arrives during our window, we concede
    immediately (don't even wait for the window to close)."""
    d, transport = daemon_setup

    task = asyncio.create_task(d._handle_arbitrate({
        "score": 0.6, "snr_db": 10.0, "rms_dbfs": -25.0, "can_serve": True,
    }))
    await _await_pending_epoch(d)
    epoch = d._pending_epoch
    assert epoch is not None

    # Bob CLAIMs the session right away.
    await transport.inject(IncomingClaim(epoch=epoch, peer_id="bob-uuid", ts_ns=0))

    result = await task
    assert result["result"] == "LOSE"


# ---------- session lifecycle on WIN ----------


async def test_session_started_then_ended(daemon_setup):
    """After WIN, voice notifies session started → we send heartbeats.
    Voice notifies session ended → we send END."""
    d, transport = daemon_setup

    # Win an arbitration.
    result = await d._handle_arbitrate({
        "score": 0.8, "snr_db": 18.0, "rms_dbfs": -22.0, "can_serve": True,
    })
    assert result["result"] == "WIN"
    epoch = result["epoch"]
    transport.sent.clear()  # ignore the CLAIM from arbitration

    # Voice tells us the turn opened.
    await d._handle_session_started(epoch)
    # Send is spawned as a fire-and-forget task — yield to let it run.
    await asyncio.sleep(0)
    from jasper.peering.transport import decode, IncomingHeartbeat
    parsed = [decode(p) for p in transport.sent]
    assert any(isinstance(m, IncomingHeartbeat) for m in parsed)

    transport.sent.clear()
    # Voice tells us the turn ended.
    await d._handle_session_ended(epoch, "user_silence")
    await asyncio.sleep(0)
    parsed = [decode(p) for p in transport.sent]
    from jasper.peering.transport import IncomingEnd
    assert any(
        isinstance(m, IncomingEnd) and m.reason == "user_silence"
        for m in parsed
    )


# ---------- self-loopback filter ----------


async def test_own_datagram_is_dropped_before_dispatch(daemon_setup):
    """IP_MULTICAST_LOOP=1 echoes every send back to us. A datagram whose
    sender is this speaker must not reach the state machine, or a peer
    would ingest its own WAKE as a foreign report and self-CLAIM.

    Pinned per verb because the sender id sits at a different attribute
    path for WAKE (nested in the report) than for the flat verbs."""
    d, transport = daemon_setup
    own = d._cfg.peer_id
    before = d._sm.state

    await transport.inject(IncomingWake(
        epoch="ep-self",
        report=WakeReport(
            peer_id=own, score=0.99, snr_db=30.0,
            rms_dbfs=-10.0, primary=False, can_serve=True,
        ),
        ts_ns=0,
    ))
    await transport.inject(IncomingClaim(epoch="ep-self", peer_id=own, ts_ns=0))

    assert d._sm.state is before
    assert d._pending_epoch is None


# ---------- terminal wake paths resolve the RPC ----------


async def _arm_suppressed(d, transport):
    """A peer CLAIMed first, so we are SUPPRESSED for its session."""
    await transport.inject(IncomingClaim(epoch="ep-bob", peer_id="bob-uuid", ts_ns=0))


async def _arm_already_winner(d, transport):
    """We already won a round, so a second wake arrives in WINNER."""
    first = await d._handle_arbitrate({
        "score": 0.9, "snr_db": 18.0, "rms_dbfs": -22.0, "can_serve": True,
    })
    assert first["result"] == "WIN"


async def _claim_for_another_epoch(d, transport):
    """A peer CLAIMs an epoch that is not ours while we arbitrate."""
    await _await_pending_epoch(d)
    await transport.inject(IncomingClaim(epoch="ep-other", peer_id="bob-uuid", ts_ns=0))


@pytest.mark.parametrize(("arm", "during"), (
    (_arm_suppressed, None),
    (_arm_already_winner, None),
    (None, _claim_for_another_epoch),
))
async def test_terminal_wake_paths_resolve_arbitrate_as_lose(
    daemon_setup, arm, during,
):
    """Every wake path that declines to arbitrate must still resolve
    the ARBITRATE RPC as LOSE.

    Leaving the future unresolved fails open to WIN once
    ARBITRATE_RPC_TIMEOUT_SEC elapses, so the speaker that should have
    stayed quiet answers alongside the one that won the round.
    """
    d, transport = daemon_setup
    if arm is not None:
        await arm(d, transport)

    rpc = asyncio.ensure_future(d._handle_arbitrate({
        # Under break_threshold=0.85, which the SUPPRESSED path keys
        # off; the other two paths never look at the score.
        "score": 0.60, "snr_db": 18.0, "rms_dbfs": -22.0, "can_serve": True,
    }))
    if during is not None:
        await during(d, transport)

    # Comfortably above the fail-open timeout: a regression shows up as
    # "WIN", not as a hung test.
    result = await asyncio.wait_for(rpc, timeout=daemon_mod.ARBITRATE_RPC_TIMEOUT_SEC * 2)
    assert result["result"] == "LOSE"


# ---------- timeout fail-open ----------


async def test_arbitrate_timeout_fails_open_as_win(monkeypatch, daemon_setup):
    """If the state machine somehow doesn't emit StartSession/StandDown
    within the RPC timeout, we resolve as WIN — voice falls back to
    solo behavior rather than hanging."""
    d, transport = daemon_setup
    # Force a very short timeout so the test runs fast.
    monkeypatch.setattr(daemon_mod, "ARBITRATE_RPC_TIMEOUT_SEC", 0.02)

    # Break the state machine deliberately — replace handle() with a
    # noop so neither StartSession nor StandDown ever fires.
    d._sm.handle = MagicMock(return_value=[])

    result = await d._handle_arbitrate({
        "score": 0.8, "snr_db": 18.0, "rms_dbfs": -22.0, "can_serve": True,
    })
    assert result["result"] == "WIN"


# ---------- fail-open timeout margin (DA-0020) ----------


def test_arbitrate_rpc_timeout_strictly_above_max_arb_window():
    """DA-0020: the fail-open ARBITRATE RPC timeout must sit strictly
    ABOVE the widest configured arb window (the clamp ceiling), not
    merely equal it. The state machine only emits StartSession/StandDown
    when the arb-window timer fires; a timeout equal to the window max
    (the old hardcoded 0.5 s == 500 ms clamp) could race the real
    decision at the window boundary and fail open with a stale WIN."""
    from jasper.peering.config import ARBITRATE_RPC_MARGIN_SEC, MAX_ARB_WINDOW_MS

    assert daemon_mod.ARBITRATE_RPC_TIMEOUT_SEC > MAX_ARB_WINDOW_MS / 1000.0
    # And the margin is the one we intend — a real, non-zero gap.
    assert ARBITRATE_RPC_MARGIN_SEC > 0.0
    assert daemon_mod.ARBITRATE_RPC_TIMEOUT_SEC == (
        MAX_ARB_WINDOW_MS / 1000.0 + ARBITRATE_RPC_MARGIN_SEC
    )


async def test_stop_resolves_pending_decision_as_win(monkeypatch, daemon_setup):
    """If stop() is called mid-arbitration (e.g. systemd restart), the
    pending RPC should resolve as WIN so the voice caller doesn't
    hang on the read."""
    d, transport = daemon_setup
    # Don't let the state machine emit anything.
    d._sm.handle = MagicMock(return_value=[])
    monkeypatch.setattr(daemon_mod, "ARBITRATE_RPC_TIMEOUT_SEC", 5.0)

    task = asyncio.create_task(d._handle_arbitrate({
        "score": 0.5, "snr_db": 10.0, "rms_dbfs": -25.0, "can_serve": True,
    }))
    await asyncio.sleep(0.01)
    await d.stop()
    result = await task
    assert result["result"] == "WIN"
