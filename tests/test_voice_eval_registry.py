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

import os
import shutil

from jasper.config import Config
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
    assert cfg.subway_enabled and cfg.bus_enabled and cfg.citibike_enabled

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


def test_build_test_registry_constructs_with_backends_unconfigured(monkeypatch):
    """The builder must also construct with transit/HA unconfigured —
    the common laptop case — registering only the always-on tools and
    none of the gated ones."""
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    for key in _BACKEND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env()
    assert not cfg.subway_enabled and not cfg.bus_enabled

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
