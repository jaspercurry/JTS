# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the xAI Grok Voice adapter.

Grok bills a flat ~$3/hour by connection uptime, but JTS's daily spend
cap is token-based — so the cap only constrains Grok if a
``ConnectionUptimeMeter`` records uptime intervals that the spend
queries fold in. ``GrokRealtimeConnection`` subclasses
``OpenAIRealtimeConnection`` and inherits the meter plumbing; these
tests pin that down on the Grok class specifically (rather than relying
on the base-class test) plus the two facts that make the daemon wire a
meter for Grok at all:

  1. ``GrokRealtimeConnection`` exposes ``set_uptime_meter`` and fires
     the meter's ``mark_connected`` / ``mark_disconnected`` hooks on
     open / teardown.
  2. The bundled rate card for Grok's default model carries
     ``flat_per_hour_usd > 0`` — the gate the daemon checks before
     wiring a meter (``_make_connection`` grok branch + the
     ``pricing.flat_per_hour_usd > 0`` guard in ``run()``).

The fakes mirror ``test_openai_session.py`` — a stub connect factory so
no real xAI WebSocket is opened.
"""
from __future__ import annotations

import asyncio

from jasper.tools import ToolRegistry
from jasper.usage import load_pricing_overrides, pricing_for_model
from jasper.voice.grok_session import (
    GROK_WEBSOCKET_BASE_URL,
    GrokRealtimeConnection,
)


class _FakeConn:
    def __init__(self) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, event: dict) -> None:
        self.sent.append(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class _FakeAsyncCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeConnectFactory:
    def __init__(self) -> None:
        self.conns: list[_FakeConn] = []
        self.models: list[str] = []

    def __call__(self, *, model: str) -> _FakeAsyncCM:
        self.models.append(model)
        c = _FakeConn()
        self.conns.append(c)
        return _FakeAsyncCM(c)


def _make_grok_conn() -> tuple[GrokRealtimeConnection, _FakeConnectFactory]:
    factory = _FakeConnectFactory()
    conn = GrokRealtimeConnection(
        api_key="fake",
        backoff_schedule=(0.0, 0.0),
        connect_factory=factory,
    )
    return conn, factory


def test_grok_routes_to_xai_endpoint() -> None:
    conn, _ = _make_grok_conn()
    assert conn.PROVIDER_NAME == "grok"
    assert conn._base_url == GROK_WEBSOCKET_BASE_URL


async def test_grok_uptime_meter_hooks_fire_on_open_and_teardown() -> None:
    """The Grok connection must call the wired uptime meter on a
    successful open and on teardown — the bridge that makes time-billed
    (Grok) cost non-zero against the daily spend cap. Regression guard:
    if Grok ever stops inheriting the meter hooks, its cost silently
    reverts to $0 while token-billed tests stay green."""
    conn, _factory = _make_grok_conn()
    events: list[str] = []

    class _StubMeter:
        def mark_connected(self) -> None:
            events.append("connected")

        def mark_disconnected(self) -> None:
            events.append("disconnected")

    # The connection must expose the wiring point the daemon calls.
    assert callable(getattr(conn, "set_uptime_meter", None))
    conn.set_uptime_meter(_StubMeter())

    registry = ToolRegistry()
    await conn.start(registry, "")
    # _open_session is awaited inside start(); the open is recorded
    # deterministically by the time start() returns.
    assert events == ["connected"]
    await conn.stop()
    assert events == ["connected", "disconnected"]


async def test_grok_no_meter_by_default_is_safe() -> None:
    """No meter wired (e.g. a misconfigured deploy) must be a no-op on
    open/teardown, not a crash — fail-safe on the wake-blocking path."""
    conn, _factory = _make_grok_conn()
    assert conn._uptime_meter is None
    registry = ToolRegistry()
    await conn.start(registry, "")
    await conn.stop()  # must not raise with no meter set


def test_grok_default_model_is_time_billed() -> None:
    """The daemon only wires a meter when ``pricing.flat_per_hour_usd >
    0``. Grok's default model must carry a positive hourly rate in the
    bundled card, or the cap stays inoperative for Grok regardless of
    the connection-side plumbing."""
    default_model = GrokRealtimeConnection.__init__.__defaults__[0]
    assert default_model == "grok-voice-think-fast-1.0"
    pricing = pricing_for_model(
        default_model, overrides=load_pricing_overrides(),
    )
    assert pricing.flat_per_hour_usd > 0
