# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.volume_observers.

The observers shell out to busctl/bluealsa-cli for AirPlay/BT, and
read /run/librespot/state.json (written by librespot's --onevent hook)
for Spotify. Tests mock the I/O boundary: subprocess for DBus, a
tmp_path-backed state file for Spotify. Coverage:

- AirPlay reader parses busctl variant/double output
- Spotify reader maps librespot's raw 0-65535 volume to 0-100 percent
- BT reader resolves transport path then reads MediaTransport1.Volume
- _maybe_observe fires only on real change (>0.5 unit delta)
- a tick fires observe_source_volume only for active Spotify/BT
- source activation forwards one fresh observation even at same value
- AirPlay ticks read but do not dispatch canonical observations
- observer ignores readers that return None (source not active)
"""
from __future__ import annotations

import pytest

from jasper.volume_observers import VolumeObserver
from jasper.volume_coordinator import Source


class _FakeCoordinator:
    def __init__(self, active: Source = Source.AIRPLAY) -> None:
        self.active = active
        self.observed: list[tuple[Source, float]] = []
        self.transitions: list[tuple[Source, Source]] = []
        self.reconcile_calls: int = 0

    async def _active_source(self):
        return self.active

    async def apply_active_source_transition(self, prev, current):
        self.transitions.append((prev, current))

    async def observe_source_volume(self, source, value):
        self.observed.append((source, float(value)))

    async def maybe_reconcile_camilla(self) -> None:
        self.reconcile_calls += 1


# ---------- AirPlay reader -------------------------------------------------


async def test_read_airplay_db_parses_variant(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.json")

    async def fake_busctl(*args, **kwargs):
        return 'v d -10.500000'

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    val = await obs._read_airplay_db()
    assert val == pytest.approx(-10.5)


async def test_read_airplay_db_clamps_to_range(monkeypatch):
    """shairport reports -144 when iPhone slider is at 0 — observer
    clamps to AIRPLAY_DB_MIN (the coordinator then maps that to 0%)."""
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.json")

    async def fake_busctl(*args, **kwargs):
        return 'v d -144.000000'

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    val = await obs._read_airplay_db()
    from jasper.volume_coordinator import AIRPLAY_DB_MIN
    assert val == AIRPLAY_DB_MIN


async def test_read_airplay_returns_none_on_busctl_failure(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.json")

    async def fake_busctl(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    assert await obs._read_airplay_db() is None


# ---------- Spotify reader -------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"{}", json_data: dict | None = None) -> None:
        self.status_code = status_code
        self.content = content
        self._json = json_data or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json


class _FakeHTTPClient:
    def __init__(self, response) -> None:
        self._response = response
        self.calls: list[str] = []

    async def get(self, url):
        self.calls.append(url)
        return self._response

    async def aclose(self) -> None:
        return None


async def test_read_spotify_percent_maps_raw_to_pct(tmp_path):
    """librespot reports volume as raw 0-65535 (16-bit) in the state
    file written by --onevent. Observer maps to 0-100 percent."""
    import json
    state = tmp_path / "librespot.state.json"
    state.write_text(json.dumps({"volume": "32768"}))  # ~50%
    obs = VolumeObserver(
        _FakeCoordinator(),
        librespot_state_path=str(state),
    )
    pct = await obs._read_spotify_percent()
    # 32768/65535 ≈ 0.5000076... → 50% rounded
    assert pct == 50


async def test_read_spotify_percent_handles_missing_state_file(tmp_path):
    """No state file (librespot hasn't fired any event yet) → None."""
    obs = VolumeObserver(
        _FakeCoordinator(),
        librespot_state_path=str(tmp_path / "missing.json"),
    )
    assert await obs._read_spotify_percent() is None


async def test_read_spotify_percent_handles_missing_volume_key(tmp_path):
    """State file present but no volume key (e.g. only track_id was
    captured) → None."""
    import json
    state = tmp_path / "librespot.state.json"
    state.write_text(json.dumps({"track_id": "spotify:track:X"}))
    obs = VolumeObserver(
        _FakeCoordinator(),
        librespot_state_path=str(state),
    )
    assert await obs._read_spotify_percent() is None


# ---------- Bluetooth reader ----------------------------------------------


async def test_read_bluetooth_returns_none_when_no_transport(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.json")

    async def fake_path():
        return None

    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )
    assert await obs._read_bluetooth_volume() is None


async def test_read_bluetooth_parses_uint16(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.json")

    async def fake_path():
        return "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source"

    async def fake_busctl(*args, **kwargs):
        return "v q 95"

    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )
    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    assert await obs._read_bluetooth_volume() == 95


# ---------- _maybe_observe filtering --------------------------------------


async def test_maybe_observe_first_value_propagates():
    """First observation per source propagates — the source's reality
    on first contact is what listening_level should reflect (each source
    owns its own remembered volume; we mirror that)."""
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.json")
    await obs._maybe_observe(Source.AIRPLAY, -10.0)
    assert coord.observed == [(Source.AIRPLAY, -10.0)]


async def test_maybe_observe_skips_micro_drift():
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.json")
    await obs._maybe_observe(Source.AIRPLAY, -10.0)
    await obs._maybe_observe(Source.AIRPLAY, -10.2)  # < 0.5 delta
    # only first call propagated
    assert len(coord.observed) == 1


async def test_maybe_observe_fires_on_real_change():
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.json")
    await obs._maybe_observe(Source.AIRPLAY, -10.0)
    await obs._maybe_observe(Source.AIRPLAY, -15.0)
    assert coord.observed == [
        (Source.AIRPLAY, -10.0),
        (Source.AIRPLAY, -15.0),
    ]


# ---------- full tick -------------------------------------------------------


@pytest.mark.parametrize(
    ("active", "expected"),
    [
        (Source.AIRPLAY, None),
        (Source.SPOTIFY, Source.SPOTIFY),
        (Source.BLUETOOTH, Source.BLUETOOTH),
    ],
)
async def test_tick_dispatches_only_active_source(
    active, expected, monkeypatch, tmp_path,
):
    """A single tick reads all sources, but only the active source's
    volume is allowed to update the canonical listening_level."""
    import json
    coord = _FakeCoordinator(active=active)
    state = tmp_path / "librespot.state.json"
    state.write_text(json.dumps({"volume": 65535}))  # 100%
    obs = VolumeObserver(
        coord,
        librespot_state_path=str(state),
    )

    async def fake_busctl(bus_name, object_path, interface, prop, **kwargs):
        if prop == "AirplayVolume":
            return "v d -5.0"
        if prop == "Volume":
            return "v q 64"
        return None

    async def fake_path():
        return "/org/bluealsa/hci0/dev_X/a2dpsnk/source"

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )

    await obs._tick()

    expected_sources = [] if expected is None else [expected]
    assert [s for s, _ in coord.observed] == expected_sources


async def test_tick_forwards_same_value_on_source_activation(
    monkeypatch, tmp_path,
):
    """Reactivating Spotify at the same cached percent must still reach
    the coordinator so a degraded push guard can be cleared."""
    import json

    coord = _FakeCoordinator(active=Source.SPOTIFY)
    state = tmp_path / "librespot.state.json"
    state.write_text(json.dumps({"volume": 65535}))  # 100%
    obs = VolumeObserver(coord, librespot_state_path=str(state))
    obs._last_active_source = Source.AIRPLAY
    obs._last_seen[Source.SPOTIFY] = 100.0

    async def fake_busctl(*args, **kwargs):
        return None

    async def fake_path():
        return None

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )

    await obs._tick()

    assert coord.transitions == [(Source.AIRPLAY, Source.SPOTIFY)]
    assert coord.observed == [(Source.SPOTIFY, 100.0)]


async def test_tick_skips_inactive_sources(monkeypatch, tmp_path):
    coord = _FakeCoordinator(active=Source.IDLE)
    obs = VolumeObserver(
        coord,
        librespot_state_path=str(tmp_path / "missing.json"),
    )

    async def fake_busctl(*args, **kwargs):
        return None  # all DBus reads fail

    async def fake_path():
        return None  # no BT transport

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )

    await obs._tick()
    assert coord.observed == []


async def test_tick_calls_reconciler_every_tick(monkeypatch, tmp_path):
    """Self-healing convergence runs on every tick. The reconciler
    is idempotent and gated internally so it's safe to call
    unconditionally — the observer's job is just to drive the
    cadence."""
    coord = _FakeCoordinator(active=Source.IDLE)
    obs = VolumeObserver(
        coord,
        librespot_state_path=str(tmp_path / "missing.json"),
    )

    async def fake_busctl(*args, **kwargs):
        return None

    async def fake_path():
        return None

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )

    await obs._tick()
    await obs._tick()
    await obs._tick()
    assert coord.reconcile_calls == 3


async def test_tick_continues_when_reconciler_raises(monkeypatch, tmp_path, caplog):
    """The reconciler is supposed to swallow internally, but if a
    future bug makes it raise the observer must keep running —
    observation is the more important responsibility."""
    import logging

    class _BrokenCoord(_FakeCoordinator):
        async def maybe_reconcile_camilla(self) -> None:
            raise RuntimeError("simulated reconciler bug")

    coord = _BrokenCoord(active=Source.IDLE)
    obs = VolumeObserver(
        coord,
        librespot_state_path=str(tmp_path / "missing.json"),
    )

    async def fake_busctl(*args, **kwargs):
        return None

    async def fake_path():
        return None

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )
    caplog.set_level(logging.WARNING, logger="jasper.volume_observers")
    # Must not raise out of _tick.
    await obs._tick()
    assert any(
        "reconciler raised" in r.message for r in caplog.records
    )
