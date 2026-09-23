# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Volume observers with stubbed subprocess I/O and temporary Spotify state."""
from __future__ import annotations

import asyncio

import pytest

from jasper import bluealsa_probe
from jasper import volume_observers as observer_mod
from jasper.music_sources import Source
from jasper.volume_observers import VolumeObserver

from tests._async_wait import wait_signalled
from tests._librespot_state import write_librespot_state
from tests._log_events import event_field_maps


@pytest.fixture(autouse=True)
def _reset_bluealsa_probe_state():
    bluealsa_probe.note_probe_success()
    yield
    bluealsa_probe.note_probe_success()


class _FakeCoordinator:
    def __init__(self, active: Source = Source.AIRPLAY) -> None:
        self.active = active
        self.observed: list[tuple[Source, float]] = []
        self.observation_initials: list[bool] = []
        self.transitions: list[tuple[Source, Source]] = []
        self.reconcile_calls: int = 0
        self.reconcile_sources: list[Source | None] = []
        self.observation_revision: str | None = None
        self.accept_observations = True

    async def _active_source(self):
        return self.active

    async def apply_active_source_transition(self, prev, current):
        self.transitions.append((prev, current))

    def source_observation_revision(self, source):
        return self.observation_revision

    async def observe_source_volume(self, source, value, *, initial=False):
        self.observed.append((source, float(value)))
        self.observation_initials.append(initial)
        return self.accept_observations

    async def maybe_reconcile_camilla(self, source: Source | None = None) -> None:
        self.reconcile_calls += 1
        self.reconcile_sources.append(source)


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
    state = write_librespot_state(
        tmp_path / "librespot.state.env", volume=32768,  # ~50%
    )
    obs = VolumeObserver(
        _FakeCoordinator(),
        librespot_state_path=str(state),
    )
    pct = await obs._read_spotify_percent()
    # 32768/65535 ≈ 0.5000076... → 50% rounded
    assert pct == 50


async def test_read_spotify_percent_handles_missing_state_file(tmp_path, caplog):
    """No state file (librespot hasn't fired any event yet) → None.
    Expected until first Spotify play, and polled at 1 Hz — must not log."""
    obs = VolumeObserver(
        _FakeCoordinator(),
        librespot_state_path=str(tmp_path / "missing.env"),
    )
    with caplog.at_level("DEBUG", logger="jasper.librespot_state"):
        assert await obs._read_spotify_percent() is None
    assert caplog.records == []


async def test_read_spotify_percent_handles_missing_volume_key(tmp_path):
    """State file present but no volume key (e.g. only track_id was
    captured) → None."""
    state = write_librespot_state(
        tmp_path / "librespot.state.env", track_id="spotify:track:X",
    )
    obs = VolumeObserver(
        _FakeCoordinator(),
        librespot_state_path=str(state),
    )
    assert await obs._read_spotify_percent() is None


# ---------- Bluetooth reader ----------------------------------------------


async def test_read_bluetooth_returns_none_when_no_transport(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.env")

    async def fake_path():
        return None

    monkeypatch.setattr(
        "jasper.volume_observers._bluez_alsa_active_transport_path", fake_path,
    )
    assert await obs._read_bluetooth_volume() is None


async def test_read_bluetooth_parses_uint16(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.env")

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


async def test_bluealsa_transport_path_suppresses_after_cli_failure(monkeypatch):
    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"permission denied"

    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        fake_exec,
    )

    assert await observer_mod._bluez_alsa_active_transport_path() is None
    assert await observer_mod._bluez_alsa_active_transport_path() is None
    assert calls["n"] == 1


# ---------- _maybe_observe filtering --------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # First observation per source propagates — the source's reality on
        # first contact is what listening_level should reflect.
        ((40.0,), [40.0]),
        ((40.0, 40.2), [40.0]),          # < 0.5 unit drift is polling churn
        ((40.0, 45.0), [40.0, 45.0]),
    ],
)
async def test_maybe_observe_propagates_only_real_change(values, expected):
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.env")

    for value in values:
        await obs._maybe_observe(Source.SPOTIFY, value)

    assert coord.observed == [(Source.SPOTIFY, v) for v in expected]


async def test_maybe_observe_same_zero_again_for_new_mute_revision():
    """A second mute token must cross the observer even if Spotify stayed 0."""
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.env")

    coord.observation_revision = "mute-a"
    await obs._maybe_observe(Source.SPOTIFY, 0.0)
    coord.observation_revision = "mute-b"
    await obs._maybe_observe(Source.SPOTIFY, 0.0)

    assert coord.observed == [
        (Source.SPOTIFY, 0.0),
        (Source.SPOTIFY, 0.0),
    ]
    assert coord.observation_initials == [True, False]


async def test_maybe_observe_retries_declined_unchanged_value():
    """Declined policy input is not cached as if it became canonical truth."""
    coord = _FakeCoordinator()
    coord.accept_observations = False
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.env")

    await obs._maybe_observe(Source.SPOTIFY, 65.0)
    await obs._maybe_observe(Source.SPOTIFY, 65.0)

    assert coord.observed == [
        (Source.SPOTIFY, 65.0),
        (Source.SPOTIFY, 65.0),
    ]


# ---------- full tick -------------------------------------------------------


class _ProbeSpy:
    """Counts probes at the boundary each reader delegates to: busctl
    get-property for the BT volume, bluealsa-cli's transport lookup for BT,
    the state-file read for Spotify. Every one but Spotify's forks a child,
    which is what an idle tick must not spend."""

    def __init__(self) -> None:
        self.spotify = 0
        self.bluetooth = 0

    def install(self, monkeypatch) -> None:
        async def fake_busctl(bus_name, object_path, interface, prop, **kwargs):
            if prop == "Volume":
                return "v q 64"
            return None

        async def fake_path():
            self.bluetooth += 1
            return "/org/bluealsa/hci0/dev_X/a2dpsnk/source"

        real_volume_percent = observer_mod.librespot_state.volume_percent

        def counting_volume_percent(path):
            self.spotify += 1
            return real_volume_percent(path)

        monkeypatch.setattr(
            "jasper.volume_observers._busctl_get_property_value", fake_busctl,
        )
        monkeypatch.setattr(
            "jasper.volume_observers._bluez_alsa_active_transport_path",
            fake_path,
        )
        monkeypatch.setattr(
            observer_mod.librespot_state, "volume_percent",
            counting_volume_percent,
        )


@pytest.mark.parametrize(
    ("active", "probes", "expected_observed"),
    [
        (Source.IDLE, (0, 0), []),
        (Source.USBSINK, (0, 0), []),
        (Source.AIRPLAY, (0, 0), []),
        (Source.SPOTIFY, (1, 0), [(Source.SPOTIFY, 100.0)]),
        (Source.BLUETOOTH, (0, 1), [(Source.BLUETOOTH, 64.0)]),
    ],
)
async def test_tick_probes_only_the_active_source(
    active, probes, expected_observed, monkeypatch, tmp_path,
):
    """A tick asks at most one reader — the active source's — and none at
    all on an idle box or on a source this observer does not poll. AirPlay
    volume arrives event-driven through shairport's hook (ADR-0206), so an
    AirPlay tick forks nothing."""
    coord = _FakeCoordinator(active=active)
    state = write_librespot_state(
        tmp_path / "librespot.state.env", volume=65535,  # 100%
    )
    obs = VolumeObserver(coord, librespot_state_path=str(state))
    spy = _ProbeSpy()
    spy.install(monkeypatch)

    await obs._tick()

    assert (spy.spotify, spy.bluetooth) == probes
    assert coord.observed == expected_observed


async def test_tick_forwards_same_value_on_source_activation(
    monkeypatch, tmp_path,
):
    """Reactivating Spotify at the same cached percent must still reach
    the coordinator so a degraded push guard can be cleared."""
    coord = _FakeCoordinator(active=Source.SPOTIFY)
    state = write_librespot_state(
        tmp_path / "librespot.state.env", volume=65535,  # 100%
    )
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


async def test_tick_calls_reconciler_every_tick(monkeypatch, tmp_path):
    """Self-healing convergence runs on every tick. The reconciler
    is idempotent and gated internally so it's safe to call
    unconditionally — the observer's job is just to drive the
    cadence. The tick's own resolved source is forwarded so the
    reconciler does not re-resolve it (one `_active_source()` per
    tick, not two)."""
    coord = _FakeCoordinator(active=Source.IDLE)
    obs = VolumeObserver(
        coord,
        librespot_state_path=str(tmp_path / "missing.env"),
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
    assert coord.reconcile_sources == [Source.IDLE, Source.IDLE, Source.IDLE]


async def test_tick_continues_when_reconciler_raises(monkeypatch, tmp_path, caplog):
    """The reconciler is supposed to swallow internally, but if a
    future bug makes it raise the observer must keep running —
    observation is the more important responsibility."""
    import logging

    class _BrokenCoord(_FakeCoordinator):
        async def maybe_reconcile_camilla(self, source: Source | None = None) -> None:
            raise RuntimeError("simulated reconciler bug")

    coord = _BrokenCoord(active=Source.IDLE)
    obs = VolumeObserver(
        coord,
        librespot_state_path=str(tmp_path / "missing.env"),
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


async def test_tick_failure_speaks_on_its_edges_not_every_second(
    tmp_path, caplog,
):
    """A daemon that stays down would otherwise be 3,600 WARN lines an hour at
    POLL_INTERVAL_SEC. One line opens the fault, one closes it and says how
    long it held. Delete with the events.
    """
    import logging

    caplog.set_level(logging.INFO, logger="jasper.volume_observers")
    obs = VolumeObserver(
        _FakeCoordinator(), librespot_state_path=str(tmp_path / "missing.env"),
    )
    obs.POLL_INTERVAL_SEC = 0.0
    settled = asyncio.Event()
    ticks = 0

    async def flaky_tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks <= 3:
            raise RuntimeError("busctl vanished")
        if ticks >= 5:
            settled.set()

    obs._tick = flaky_tick
    task = asyncio.create_task(obs._run())
    try:
        await wait_signalled(settled, "observer recovered", producer=task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    (failed,) = event_field_maps(caplog, "volume.observer_tick_failed")
    assert failed["error"] == "RuntimeError: busctl vanished"
    (recovered,) = event_field_maps(caplog, "volume.observer_tick_recovered")
    assert recovered["consecutive_failures"] == "3"
