# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Offline harness checks. Connections and prompt synthesis are always replaced."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from jasper.audio_io import confirmed_tts_flush
from jasper.config import Config
from jasper.tools import ToolRegistry, dispatch_tool, tool
from jasper.voice import trace
from jasper.voice.session import AudioOutChunk, TurnCapture, TurnUsage
from tests._live_turn_fake import FakeLiveTurn
from tests.voice_eval import harness as harness_mod
from tests.voice_eval import tts
from tests.voice_eval.harness import _build_test_registry
from tests.voice_eval.trace_registry import traced_registry
from tests.voice_eval.turn_trace import TurnTrace, reset_active, set_active

# Synthetic but well-formed values — enough to flip the `*_enabled`
# Config properties on. No network fires at construction.
_ALL_BACKENDS_ENV = {
    "JASPER_VOICE_PROVIDER": "gemini",
    "GEMINI_API_KEY": "test-key",
    "JASPER_SUBWAY_STATION_ID": "D24",
    "JASPER_SUBWAY_DEFAULT_DIRECTION": "",
    "JASPER_BUS_STOPS": "MTA_308209|Test Stop",
    "JASPER_MTA_BUSTIME_KEY": "test-bus-key",
    "JASPER_CITIBIKE_STATIONS": "66dc120f-0aca-11e7-82f6-3863bb44ef7c|Test Dock",
    "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic-Test_Key",
    "JASPER_TRANSIT_LAT": "40.758",
    "JASPER_TRANSIT_LON": "-73.985",
    "JASPER_HA_URL": "http://homeassistant.local:8123",
    "JASPER_HA_TOKEN": "test-token",
}

# Transit/HA vars that must be cleared for the "unconfigured" case.
_BACKEND_ENV_KEYS = (
    "JASPER_SUBWAY_STATION_ID",
    "JASPER_BUS_STOPS",
    "JASPER_MTA_BUSTIME_KEY",
    "JASPER_CITIBIKE_STATIONS",
    "GOOGLE_ROUTES_API_KEY",
    "JASPER_TRANSIT_LAT",
    "JASPER_TRANSIT_LON",
    "JASPER_HA_URL",
    "JASPER_HA_TOKEN",
)


def _cleanup(test_state: dict) -> None:
    """Remove tmp artifacts the builder creates so the guard doesn't
    litter /tmp on every CI run."""
    db = test_state.get("timer_db_path")
    if isinstance(db, str) and os.path.exists(db):
        os.unlink(db)
    research_db = test_state.get("research_db_path")
    if isinstance(research_db, str) and os.path.exists(research_db):
        os.unlink(research_db)
    wake_dir = test_state.get("wake_events_dir")
    if isinstance(wake_dir, str):
        shutil.rmtree(wake_dir, ignore_errors=True)


def test_build_test_registry_constructs_with_all_backends_enabled(monkeypatch):
    """The builder must construct cleanly with transit + HA enabled.

    Regression guard for the shipped defect where `_build_test_registry`
    referenced non-existent `Config` attributes / `BusClient` kwargs and
    raised `AttributeError` inside every transit-enabled paid scenario.
    """
    for key, value in _ALL_BACKENDS_ENV.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env()

    test_state: dict[str, object] = {}
    try:
        registry = _build_test_registry(cfg, test_state=test_state)
        names = set(registry.tools)
        # The transit + HA branches are exactly the ones that drifted —
        # assert each registered a tool the model can see.
        assert {
            "get_subway_arrivals",
            "get_bus_arrivals",
            "get_citibike_status",
            "get_travel_routes",
            "home_assistant",
        } <= names
        # And the always-on backends construct too.
        assert {"get_weather", "get_current_time", "set_timer", "get_volume"} <= names
        assert "volume_coordinator" in test_state
        assert "google_clients" in test_state
        assert "research_scheduler" in test_state
    finally:
        _cleanup(test_state)


def test_harness_populates_test_state_eagerly_at_construction(monkeypatch):
    """Side-channel handles must exist BEFORE the first paid call.

    The volume scenarios read `test_state["volume_coordinator"]` at
    scenario start to snapshot the level they later restore — without
    a second LLM turn. The harness used to build the registry lazily
    inside `_ensure_connection`, so `test_state` was empty at scenario
    start and the entire volume suite skipped as "wiring regressed"
    (caught by the 2026-06-11 on-Pi run, $0 spent). Construction is
    free; only the LiveConnection is paid — pin the eager contract.
    """
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for key in _BACKEND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env()

    from tests.voice_eval.harness import VoiceEvalHarness

    h = VoiceEvalHarness(cfg)
    try:
        assert h.test_state.get("volume_coordinator") is not None
        assert "timer_scheduler" in h.test_state
        assert "research_scheduler" in h.test_state
        assert h._connection is None, "construction must not open a session"
    finally:
        _cleanup(h.test_state)


def test_build_test_registry_constructs_with_backends_unconfigured(monkeypatch):
    """The builder must also construct with transit/HA unconfigured —
    the common laptop case — registering only the always-on tools and
    none of the gated ones."""
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for key in _BACKEND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env()

    test_state: dict[str, object] = {}
    try:
        registry = _build_test_registry(cfg, test_state=test_state)
        names = set(registry.tools)
        assert {"get_weather", "get_current_time", "set_timer", "get_volume"} <= names
        assert "volume_coordinator" in test_state
        assert test_state["google_clients"] is None
        assert "get_subway_arrivals" not in names
        assert "get_bus_arrivals" not in names
        assert "home_assistant" not in names
    finally:
        _cleanup(test_state)


def test_tts_cache_write_publishes_with_replace(monkeypatch, tmp_path):
    real_replace = os.replace
    promoted: list[tuple[str, str]] = []

    def capture_replace(src, dst):
        promoted.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", capture_replace)
    path = tmp_path / "cached.wav"

    tts._write_wav_atomic(path, b"\0\0" * 16, sample_rate=tts.DAEMON_RATE_HZ)

    assert path.exists()
    assert len(promoted) == 1
    src, dst = promoted[0]
    assert dst == str(path)
    assert src != str(path)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_tts_cache_write_failure_does_not_publish_partial_file(monkeypatch, tmp_path):
    def fail_after_partial_temp(path, _pcm, *, sample_rate):
        path.write_bytes(b"partial")
        raise RuntimeError("boom")

    monkeypatch.setattr(tts, "_write_wav", fail_after_partial_temp)
    path = tmp_path / "cached.wav"

    with pytest.raises(RuntimeError, match="boom"):
        tts._write_wav_atomic(path, b"\0\0" * 16, sample_rate=tts.DAEMON_RATE_HZ)

    assert not path.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("provider", ["gemini", "openai", "grok"])
@pytest.mark.parametrize("outcome,complete,observed_usage", [
    ("answer", True, True),
    ("no_audio", True, True),
    ("no_transcript", True, True),
    ("interrupt", False, False),
    ("interrupt", False, True),
    ("timeout", False, False),
    ("timeout", False, True),
    ("interrupt", True, True),
    ("timeout", True, True),
])
async def test_harness_shared_contract_and_evidence(
    monkeypatch, tmp_path, provider, outcome, complete, observed_usage,
):
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", provider)
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.setenv(key, "offline-key")
    cfg = Config.from_env()
    pauses = []
    sleep = asyncio.sleep

    async def paced_sleep(seconds):
        if seconds:
            pauses.append(seconds)
        await sleep(0)

    monkeypatch.setattr(asyncio, "sleep", paced_sleep)
    pcm = b"\x01\0" * (harness_mod.MicCapture.OUTPUT_FRAME_SAMPLES + 17)
    usage = TurnUsage(100, 50, {
        "input_tokens": 100, "output_tokens": 50,
        "input_token_details": {"text_tokens": 100},
        "output_token_details": {"audio_tokens": 50},
    }) if observed_usage else TurnUsage()

    class Turn(FakeLiveTurn):
        def __init__(self):
            super().__init__()
            self.sent = []
            self.interrupted = asyncio.Event()

        def request_local_interrupt(self):
            self.interrupted.set()

        async def send_audio(self, chunk):
            self.sent.append(chunk)

        async def audio_out_chunks(self):
            trace.emit("text_out", {"delta": "stale trace text"})
            trace.emit("turn_complete", {"tokens": {"input_tokens": 999999}})
            if outcome != "no_audio":
                for _ in range(3 if outcome == "interrupt" else 1):
                    yield AudioOutChunk(b"\x02\0" * 24, "answer")
            if outcome == "timeout":
                await asyncio.Event().wait()

        def server_turn_complete(self):
            return complete

        async def wait_for_interrupt(self):
            await self.interrupted.wait()

        def capture(self):
            return TurnCapture(assistant_text=None if outcome == "no_transcript" else "Final native text")

        def usage(self):
            return usage

        async def release(self):
            self.release_calls += 1
            if connection.meter:
                connection.meter.mark_ended()

    class Connection:
        meter = None

        def set_billable_activity_meter(self, meter):
            self.meter = meter

        async def start(self, *args):
            return None

        def is_paused(self):
            return False

        async def acquire_turn(self):
            if self.meter:
                self.meter.mark_started()
                # Seed one known billed interval, without waiting or using token proxies.
                harness._usage_store._conn.execute(
                    "UPDATE connection_intervals SET opened_at = ?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),),
                )
            return turn

        async def stop(self):
            return None

    turn, connection = Turn(), Connection()
    monkeypatch.setattr(harness_mod, "_build_test_registry", lambda *a, **k: ToolRegistry())
    monkeypatch.setattr(harness_mod, "_make_connection", lambda _cfg: connection)
    monkeypatch.setattr(harness_mod.tts, "synth", AsyncMock(return_value=tmp_path / "prompt.wav"))
    monkeypatch.setattr(harness_mod, "_load_wav_pcm", lambda _path: pcm)
    monkeypatch.setattr(harness_mod, "TRANSCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(harness_mod, "TRACES_DIR", tmp_path)
    harness = harness_mod.VoiceEvalHarness(cfg)
    try:
        result = None
        if outcome in ("no_audio", "timeout"):
            with pytest.raises(TimeoutError if outcome == "timeout" else AssertionError):
                await harness.ask("hello", turn_timeout_sec=0.05)
        elif outcome == "interrupt":
            result, ack = await harness._run_turn("hello", 30.0, interrupt=True)
            assert confirmed_tts_flush(ack)
        else:
            result = await harness.ask("hello")
            result.trace.events.clear()
            assert result.audio == b"\x02\0" * 24
            assert result.usage == usage
            if outcome == "no_transcript":
                with pytest.raises(pytest.xfail.Exception):
                    result.require_spoken_text()
            else:
                assert result.require_spoken_text() == "Final native text"
        assert pauses == [harness_mod.MicCapture.OUTPUT_FRAME_SAMPLES / 16000, 34 / 32000]
        assert b"".join(turn.sent) == pcm
        assert [len(chunk) for chunk in turn.sent] == [harness_mod.MicCapture.OUTPUT_FRAME_SAMPLES * 2, 34]
        assert turn.end_input_calls == turn.release_calls == 1
        assert len(list(tmp_path.glob("*.md"))) == 1
        assert len(list(tmp_path.glob("*.response.wav"))) == 1
        events = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
        final = events[-1]["payload"]
        if provider == "grok":
            expected_cost = pytest.approx(harness._pricing.flat_per_hour_usd / 60, abs=0.001)
        elif complete:
            expected_cost = harness._pricing.estimate_cost(usage.breakdown)
        else:
            expected_cost = None
        expected_status = "incomplete" if expected_cost is None else "estimated"
        assert final["estimated_cost_usd"] == expected_cost
        assert final["cost_status"] == expected_status
        assert final["usage"]["input_tokens"] == usage.input_tokens
        assert final["usage"]["output_tokens"] == usage.output_tokens
        if result is not None:
            assert result.usage == usage
            assert result.estimated_cost_usd == expected_cost
            assert result.cost_status == expected_status
        assert final["outcome"] == {
            "no_audio": "AssertionError", "interrupt": "interrupted", "timeout": "TimeoutError",
        }.get(outcome, "complete")
        if outcome == "interrupt":
            assert len(final["simulated_flush"]["events"]) == 1
    finally:
        await harness.aclose()


async def test_tool_records_come_from_execution_without_trace_events():
    @tool()
    async def echo(value: str) -> dict:
        """Return the supplied value."""
        if value == "second":
            raise ValueError(value)
        return {"value": value}

    registry = ToolRegistry()
    registry.register(echo)
    records = []
    wrapped = traced_registry(registry, records=lambda: records)
    for value in ("first", "second"):
        await dispatch_tool(wrapped, "echo", {"value": value})
    assert [r.args for r in records] == [{"value": "first"}, {"value": "second"}]
    assert records[0].result == {"value": "first"}
    assert records[0].error is None
    assert records[1].result is None
    assert records[1].error is not None


async def test_late_tool_evidence_stays_with_its_original_turn():
    entered, release = asyncio.Event(), asyncio.Event()

    @tool()
    async def delayed() -> dict:
        """Return after the test releases the executor."""
        entered.set()
        await release.wait()
        return {"done": True}

    registry = ToolRegistry()
    registry.register(delayed)
    wrapped = traced_registry(registry)
    old = TurnTrace("old", "session", "test")
    fresh = TurnTrace("fresh", "session", "test")
    token = set_active(old)
    pending = asyncio.create_task(dispatch_tool(wrapped, "delayed", {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        reset_active(token)
        token = set_active(fresh)
        release.set()
        await pending
        assert not fresh.events
        assert old.tool_returns()[0].payload["result"] == {"done": True}
    finally:
        reset_active(token)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
