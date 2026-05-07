"""Unit tests for jasper.volume_observers.

The observers shell out to busctl/bluealsa-cli for AirPlay/BT, and
read /run/librespot/state.json (written by librespot's --onevent hook)
for Spotify. Tests mock the I/O boundary: subprocess for DBus, a
tmp_path-backed state file for Spotify. Coverage:

- AirPlay reader parses busctl variant/double output
- Spotify reader maps librespot's raw 0-65535 volume to 0-100 percent
- BT reader resolves transport path then reads MediaTransport1.Volume
- _maybe_observe fires only on real change (>0.5 unit delta)
- a tick fires observe_source_volume per source change
- observer ignores readers that return None (source not active)
"""
from __future__ import annotations

import pytest

from jasper.volume_observers import VolumeObserver
from jasper.volume_coordinator import Source


class _FakeCoordinator:
    def __init__(self) -> None:
        self.observed: list[tuple[Source, float]] = []

    async def observe_source_volume(self, source, value):
        self.observed.append((source, float(value)))


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


async def test_tick_dispatches_per_source(monkeypatch, tmp_path):
    """A single tick reads all three sources and propagates each
    observed value to the coordinator."""
    import json
    coord = _FakeCoordinator()
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

    sources = {s for s, _ in coord.observed}
    assert Source.AIRPLAY in sources
    assert Source.SPOTIFY in sources
    assert Source.BLUETOOTH in sources


async def test_tick_skips_inactive_sources(monkeypatch, tmp_path):
    coord = _FakeCoordinator()
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
