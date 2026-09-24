# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""xAI endpoint, wire format and time-billed pricing for the Grok adapter."""
from __future__ import annotations

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
from jasper.voice.daemon_main import _wire_billable_activity_meter
from tests._provider_fakes import RealtimeConnect as _FakeConnectFactory
from tests._log_events import parse_event


def _make_grok_conn() -> tuple[GrokRealtimeConnection, _FakeConnectFactory]:
    factory = _FakeConnectFactory()
    conn = GrokRealtimeConnection(api_key="fake", connect_factory=factory)
    return conn, factory


def test_grok_routes_to_xai_endpoint() -> None:
    conn, _ = _make_grok_conn()
    assert conn.PROVIDER_NAME == "grok"
    assert conn._base_url == GROK_WEBSOCKET_BASE_URL


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


async def test_grok_journal_lines_name_grok_not_openai(caplog) -> None:
    """Inherited provider events must identify Grok. See issue #3855."""
    conn, _factory = _make_grok_conn()
    registry = ToolRegistry()
    with caplog.at_level(logging.DEBUG, logger="jasper.voice.openai_session"):
        await conn.start(registry, "")
        await conn.stop()

    events = [parse_event(record.getMessage()) for record in caplog.records]
    fields = [event[1] for event in events if event and "provider" in event[1]]
    assert fields
    assert all(field.get("provider") == conn.PROVIDER_NAME for field in fields)


def test_grok_session_uses_xai_manual_audio_shape():
    conn, _ = _make_grok_conn()
    conn._system_instruction_provider = lambda: "instruction"
    assert conn._build_session_payload() == {
        "instructions": "instruction",
        "voice": "eve",
        "turn_detection": {"type": None},
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 24000}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        },
        "tools": [],
    }
