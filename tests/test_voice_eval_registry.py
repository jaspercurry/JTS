"""Hardware-free guard for the voice-eval tool-registry builder.

`tests/voice_eval/harness.py::_build_test_registry` mirrors the daemon's
`_build_registry`, but it is ONLY ever invoked behind a live, *paid*
`harness.ask()` call. So a drifted client constructor — a wrong kwarg, or
a `Config` attribute that no longer exists — is invisible to the
hardware-free CI suite and only explodes when an operator spends money
running the eval.

That exact bug shipped: the bus and subway branches referenced
`cfg.bus_stop_id` / `cfg.subway_lines` (neither exists on `Config`) and
`BusClient(stop_id=…, configured_routes=…)` (neither is a real
parameter), so every transit-enabled `harness.ask()` raised
`AttributeError` before any assertion ran — silently disabling the
bus-outage regression scenario it was supposed to guard.

CI runs `pytest --ignore=tests/voice_eval`, so this guard deliberately
lives in the top-level `tests/` package (which CI *does* collect) and
imports the builder directly. It constructs the registry with every
transit + Home Assistant backend enabled and asserts the build succeeds
and registers the expected tools — catching the whole class of
harness-vs-real-signature drift cheaply, with no network and no paid
session. The clients store their config and create HTTP clients lazily,
so construction is genuinely hardware-free.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from types import SimpleNamespace

import pytest
from jasper.config import Config
from tests.voice_eval import harness as harness_mod
from tests.voice_eval import tts
from tests.voice_eval.harness import _build_test_registry

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
    "JASPER_HA_URL": "http://homeassistant.local:8123",
    "JASPER_HA_TOKEN": "test-token",
}

# Transit/HA vars that must be cleared for the "unconfigured" case.
_BACKEND_ENV_KEYS = (
    "JASPER_SUBWAY_STATION_ID",
    "JASPER_BUS_STOPS",
    "JASPER_MTA_BUSTIME_KEY",
    "JASPER_CITIBIKE_STATIONS",
    "JASPER_HA_URL",
    "JASPER_HA_TOKEN",
)


def _cleanup(test_state: dict) -> None:
    """Remove the tmp artifacts the builder creates (timer SQLite DB,
    wake-events dir) so the guard doesn't litter /tmp on every CI run."""
    db = test_state.get("timer_db_path")
    if isinstance(db, str) and os.path.exists(db):
        os.unlink(db)
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
            "home_assistant",
        } <= names
        # And the always-on backends construct too.
        assert {"get_weather", "get_current_time", "set_timer", "get_volume"} <= names
        assert "volume_coordinator" in test_state
        assert "google_clients" in test_state
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


def test_harness_writes_transcript_on_drain_timeout(monkeypatch, tmp_path):
    class FakeTurn:
        def __init__(self) -> None:
            self.released = False

        async def send_audio(self, _pcm: bytes) -> None:
            return None

        async def end_input(self) -> None:
            return None

        async def audio_out(self):
            while True:
                await asyncio.sleep(0.1)
                if False:  # pragma: no cover - keeps this an async generator
                    yield b""

        def server_turn_complete(self) -> bool:
            return False

        def usage_tokens(self) -> dict:
            return {}

        async def release(self) -> None:
            self.released = True

    class FakeConnection:
        def __init__(self, turn: FakeTurn) -> None:
            self.turn = turn

        async def acquire_turn(self) -> FakeTurn:
            return self.turn

    async def fake_synth(_text: str, *, cache_dir):
        return tmp_path / "prompt.wav"

    transcript_calls: list[tuple[str, list[str], bytes]] = []

    def fake_write_transcript(prompt, trace, audio, *, out_dir):
        transcript_calls.append((prompt, [event.kind for event in trace.events], audio))
        return tmp_path / "turn.md", tmp_path / "turn.response.wav"

    turn = FakeTurn()
    harness = harness_mod.VoiceEvalHarness.__new__(harness_mod.VoiceEvalHarness)
    harness.cfg = SimpleNamespace(voice_provider="gemini")
    harness.audio_cache_dir = tmp_path
    harness._session_id = "session-test"

    async def fake_ensure_connection():
        return FakeConnection(turn)

    harness._ensure_connection = fake_ensure_connection

    monkeypatch.setattr(harness_mod.tts, "synth", fake_synth)
    monkeypatch.setattr(harness_mod, "_load_wav_pcm", lambda _path: b"")
    monkeypatch.setattr(harness_mod, "_write_transcript", fake_write_transcript)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(harness.ask("hello", turn_timeout_sec=0.01))

    assert turn.released is True
    assert len(transcript_calls) == 1
    prompt, kinds, audio = transcript_calls[0]
    assert prompt == "hello"
    assert audio == b""
    assert "turn_end" in kinds


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
