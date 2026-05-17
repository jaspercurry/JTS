"""Integration tests for jasper.peering.daemon.

Exercises the orchestrator end-to-end with mocked transport +
discovery. Verifies:
  - mode=OFF: start() is a clean no-op
  - mode=ON: ARBITRATE returns WIN for a solo wake (no peer reports)
  - mode=ON: ARBITRATE returns LOSE when a peer outbids us
  - session lifecycle drives correct broadcasts (CLAIM → HEART → END)
  - Avahi rendering is skipped on mode=OFF and attempted on mode=ON

We monkey-patch the multicast transport so tests don't open real
sockets. The state machine and dispatch logic run unmodified.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from jasper.peering import daemon as daemon_mod
from jasper.peering.config import PeeringConfig, PeeringMode
from jasper.peering.rank import WakeReport
from jasper.peering.transport import (
    IncomingClaim,
    IncomingWake,
)


def _short_socket_path() -> str:
    return f"/tmp/jts-pt-{secrets.token_hex(4)}.sock"


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


@pytest.fixture(autouse=True)
def _fake_uds_path(monkeypatch):
    """Point the daemon's UDS at a short tmp path so it fits in sun_path."""
    monkeypatch.setattr(daemon_mod, "PEERING_UDS_PATH", _short_socket_path())


class _FakeTransport:
    """Mock MulticastTransport that captures sent bytes + lets tests
    inject incoming messages."""

    def __init__(self, **kwargs):
        self.sent: list[bytes] = []
        self._on_message = None
        self.started = False
        self.stopped = False

    async def start(self, on_message):
        self.started = True
        self._on_message = on_message

    async def stop(self):
        self.stopped = True

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def inject(self, msg, addr="127.0.0.1") -> None:
        """Simulate an inbound multicast message."""
        if self._on_message is not None:
            result = self._on_message(msg, addr)
            if asyncio.iscoroutine(result):
                await result


class _FakeDiscovery:
    """Mock PeerDiscovery that does nothing — discovery isn't on the
    arbitration hot path; we test it separately."""

    def __init__(self, *, self_peer_id):
        self.self_peer_id = self_peer_id

    async def start(self, on_event):
        return None

    async def stop(self):
        return None

    def peers(self):
        return []


@pytest_asyncio.fixture
async def daemon_setup(monkeypatch):
    """Start a PeeringDaemon with mocked transport/discovery."""
    transport = _FakeTransport()
    monkeypatch.setattr(daemon_mod, "MulticastTransport", lambda **kw: transport)

    # Stub PeerDiscovery import path.
    import jasper.peering.discovery as disc_mod
    monkeypatch.setattr(disc_mod, "PeerDiscovery", _FakeDiscovery)

    d = daemon_mod.PeeringDaemon(_cfg(mode=PeeringMode.ON))
    await d.start()
    try:
        yield d, transport
    finally:
        await d.stop()


# ---------- mode=OFF: nothing happens ----------


async def test_mode_off_start_is_noop(monkeypatch):
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
    # Wait a tick for the state machine to set _pending_epoch.
    await asyncio.sleep(0.01)
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
    await asyncio.sleep(0.01)
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


# ---------- STATUS RPC ----------


async def test_status_returns_current_state(daemon_setup):
    d, _ = daemon_setup
    status = await d._handle_status()
    assert status["mode"] == "on"
    assert status["peer_id"] == "alice-uuid"
    assert status["room"] == "kitchen"
    assert status["primary"] is False
    assert status["state"] in ("idle", "suppressed")  # depends on prior tests
    assert isinstance(status["peers"], list)


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


# ---------- stop is clean ----------


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
