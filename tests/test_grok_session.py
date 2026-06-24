# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the xAI Grok Voice adapter.

Grok Voice publishes a flat realtime rate, but the xAI dashboard shows
idle warm WebSocket time is not billed like active conversation time.
JTS therefore estimates Grok spend from billable turn intervals, not
socket-open wall clock. ``GrokRealtimeConnection`` subclasses
``OpenAIRealtimeConnection`` and inherits the meter plumbing; these
tests pin that down on the Grok class specifically (rather than relying
on the base-class test) plus the two facts that make the daemon wire a
meter for Grok at all:

  1. ``GrokRealtimeConnection`` exposes ``set_billable_activity_meter`` and fires
     the meter's ``mark_started`` / ``mark_ended`` hooks on
     turn acquire / release.
  2. The bundled rate card for Grok's default model carries
     ``flat_per_hour_usd > 0`` — the gate the daemon checks before
     wiring a meter (``_make_connection`` grok branch + the
     ``pricing.flat_per_hour_usd > 0`` guard in ``run()``).

The fakes mirror ``test_openai_session.py`` — a stub connect factory so
no real xAI WebSocket is opened.
"""
from __future__ import annotations

import asyncio
import logging

from jasper.tools import ToolRegistry
from jasper.usage import (
    BillableActivityMeter,
    UsageStore,
    load_pricing_overrides,
    pricing_for_model,
)
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


async def test_grok_activity_meter_hooks_fire_on_turn_acquire_and_release() -> None:
    """The Grok connection must meter active turns, not idle socket time.

    Regression guard: if Grok ever starts marking the meter on WebSocket
    open, the local spend estimate can run far ahead of xAI's dashboard
    and false-trip the daily cap.
    """
    conn, _factory = _make_grok_conn()
    events: list[str] = []

    class _StubMeter:
        def mark_started(self) -> None:
            events.append("started")

        def mark_ended(self) -> None:
            events.append("ended")

    # The connection must expose the wiring point the daemon calls.
    assert callable(getattr(conn, "set_billable_activity_meter", None))
    conn.set_billable_activity_meter(_StubMeter())

    registry = ToolRegistry()
    await conn.start(registry, "")
    # Warm idle connection time is not billed by the local estimate.
    assert events == []
    turn = await conn.acquire_turn()
    assert events == ["started"]
    await turn.release()
    assert events == ["started", "ended"]
    await conn.stop()
    assert events == ["started", "ended"]


async def test_grok_no_meter_by_default_is_safe() -> None:
    """No meter wired (e.g. a misconfigured deploy) must be a no-op on
    open/teardown, not a crash — fail-safe on the wake-blocking path."""
    conn, _factory = _make_grok_conn()
    assert conn._billable_activity_meter is None
    registry = ToolRegistry()
    await conn.start(registry, "")
    await conn.stop()  # must not raise with no meter set


def test_grok_default_model_is_time_billed() -> None:
    """The daemon only wires a meter when ``pricing.flat_per_hour_usd >
    0``. Grok's default model must carry a positive realtime hourly rate
    in the bundled card, or the cap stays inoperative for Grok regardless
    of the connection-side plumbing."""
    default_model = GrokRealtimeConnection.__init__.__defaults__[0]
    assert default_model == "grok-voice-think-fast-1.0"
    pricing = pricing_for_model(
        default_model, overrides=load_pricing_overrides(),
    )
    assert pricing.flat_per_hour_usd > 0


def test_flat_rate_meter_wiring_uses_generic_activity_hook(tmp_path) -> None:
    from jasper.voice.daemon_main import _wire_billable_activity_meter

    class _FlatRateConnection:
        meter = None

        def set_billable_activity_meter(self, meter) -> None:
            self.meter = meter

    conn = _FlatRateConnection()
    store = UsageStore(str(tmp_path / "usage.db"))
    wired = _wire_billable_activity_meter(
        connection=conn,  # type: ignore[arg-type]
        usage_store=store,
        provider="future-flat",
        flat_per_hour_usd=2.5,
    )

    assert wired is True
    assert isinstance(conn.meter, BillableActivityMeter)


def test_flat_rate_provider_without_meter_hook_warns(tmp_path, caplog) -> None:
    from jasper.voice.daemon_main import _wire_billable_activity_meter

    store = UsageStore(str(tmp_path / "usage.db"))
    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        wired = _wire_billable_activity_meter(
            connection=object(),  # type: ignore[arg-type]
            usage_store=store,
            provider="future-flat",
            flat_per_hour_usd=2.5,
        )

    assert wired is False
    assert any(
        "event=pricing.flat_rate_meter_unavailable" in r.getMessage()
        and "provider=future-flat" in r.getMessage()
        for r in caplog.records
    )
