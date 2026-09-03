# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.volume_observers.

The observers shell out to busctl/bluealsa-cli for AirPlay/BT, and
read /run/librespot/state.env (written by librespot's --onevent hook)
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
- _run answers cancel() on each of _tick's directly-awaited chains (#2003)
"""
from __future__ import annotations

import asyncio

import pytest

from jasper import bluealsa_probe
from jasper import volume_coordinator
from jasper import volume_observers as observer_mod
from jasper.renderer import RendererClient
from jasper.volume_observers import VolumeObserver
from jasper.volume_coordinator import Source

from tests._librespot_state import write_librespot_state


@pytest.fixture(autouse=True)
def _reset_bluealsa_probe_state():
    bluealsa_probe._reset_for_tests()
    yield
    bluealsa_probe._reset_for_tests()


class _FakeCoordinator:
    def __init__(self, active: Source = Source.AIRPLAY) -> None:
        self.active = active
        self.observed: list[tuple[Source, float]] = []
        self.observation_initials: list[bool] = []
        self.transitions: list[tuple[Source, Source]] = []
        self.reconcile_calls: int = 0
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

    async def maybe_reconcile_camilla(self) -> None:
        self.reconcile_calls += 1


# ---------- AirPlay reader -------------------------------------------------


async def test_read_airplay_db_parses_variant(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.env")

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
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.env")

    async def fake_busctl(*args, **kwargs):
        return 'v d -144.000000'

    monkeypatch.setattr(
        "jasper.volume_observers._busctl_get_property_value", fake_busctl,
    )
    val = await obs._read_airplay_db()
    from jasper.volume_coordinator import AIRPLAY_DB_MIN
    assert val == AIRPLAY_DB_MIN


async def test_read_airplay_returns_none_on_busctl_failure(monkeypatch):
    obs = VolumeObserver(_FakeCoordinator(), librespot_state_path="/nonexistent.env")

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


async def test_maybe_observe_first_value_propagates():
    """First observation per source propagates — the source's reality
    on first contact is what listening_level should reflect (each source
    owns its own remembered volume; we mirror that)."""
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.env")
    await obs._maybe_observe(Source.AIRPLAY, -10.0)
    assert coord.observed == [(Source.AIRPLAY, -10.0)]


async def test_maybe_observe_skips_micro_drift():
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.env")
    await obs._maybe_observe(Source.AIRPLAY, -10.0)
    await obs._maybe_observe(Source.AIRPLAY, -10.2)  # < 0.5 delta
    # only first call propagated
    assert len(coord.observed) == 1


async def test_maybe_observe_fires_on_real_change():
    coord = _FakeCoordinator()
    obs = VolumeObserver(coord, librespot_state_path="/nonexistent.env")
    await obs._maybe_observe(Source.AIRPLAY, -10.0)
    await obs._maybe_observe(Source.AIRPLAY, -15.0)
    assert coord.observed == [
        (Source.AIRPLAY, -10.0),
        (Source.AIRPLAY, -15.0),
    ]


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
    coord = _FakeCoordinator(active=active)
    state = write_librespot_state(
        tmp_path / "librespot.state.env", volume=65535,  # 100%
    )
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


async def test_tick_skips_inactive_sources(monkeypatch, tmp_path):
    coord = _FakeCoordinator(active=Source.IDLE)
    obs = VolumeObserver(
        coord,
        librespot_state_path=str(tmp_path / "missing.env"),
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


# ---------------------------------------------------------------------------
# #2003 — VolumeObserver._run must answer cancel(), even when the awaited
# reply lands in the same event-loop tick as the cancellation.
#
# _run is a cancellation-only `while True:` and its only documented shutdown is
# `stop()`, which cancels the task and then awaits it. On CPython <= 3.11
# `asyncio.wait_for` SWALLOWS a CancelledError that arrives in the tick its
# awaited future completes (Lib/asyncio/tasks.py: `except CancelledError: if
# fut.done(): return fut.result()`), so a swallowed cancel there makes the
# observer IMMORTAL and hangs that `await`.
#
# `asyncio.gather` is NOT affected: its own `_cancel_requested` bookkeeping
# re-raises the parent's cancellation once its children finish, whether or not
# a child swallowed its own. That already cleared _tick's gathered readers
# (#1952). What it does not clear are _tick's DIRECTLY awaited chains. There
# are FOUR of them, reaching THREE terminal call sites -- enumerate the chains,
# not the `wait_for` grep, or the next audit will miss the two that only look
# like coordinator bookkeeping:
#
#   1. every tick   _tick:150 -> VolumeCoordinator._active_source
#                             -> RendererClient.selected_source   (renderer.py)
#   2. on transition _tick:156 -> apply_active_source_transition
#                             -> _set_push_source_for_handoff -> _set_bluetooth
#                             -> bluealsa_probe.list_pcms      (bluealsa_probe.py)
#                             -> _busctl_set_property     (volume_coordinator.py)
#   3. on observation _tick:182/184 -> _maybe_observe
#                             -> observe_source_volume (its own _active_source,
#                                volume_coordinator.py:726/739) -> as chain 1
#   4. every tick   _tick:193 -> maybe_reconcile_camilla (_active_source at
#                                volume_coordinator.py:1900/1944) -> as chain 1
#
# Chains 3 and 4 need no separate fix -- they terminate at the same
# selected_source chain 1 does -- but they are why "insulate the two chains I
# can see" would have been the wrong frame. Three terminal sites, pinned
# below. They live together because the invariant they protect is one loop's,
# not three modules'.
#
# NOTE ON WHAT THESE TESTS CAN OBSERVE: CPython 3.12 rewrote wait_for on top of
# asyncio.timeout(), so on 3.12/3.13 these pass with or without the fix. Only a
# py3.11 interpreter goes red on the pre-fix code -- same as #1935 and #1952.
# Whether any CI leg runs one is the pytest matrix's call (tests.yml); while
# none does, these pins document the 3.11 hazard requires-python still permits.
# ---------------------------------------------------------------------------


class _PendingReader:
    """Reader whose readline() blocks on a future the test resolves.

    Needed so the race lands at an exact, test-chosen event-loop tick instead
    of immediately -- an AsyncMock reply resolves before the caller ever parks
    in the bounded wait, which is the one offset that never reproduces.
    """

    def __init__(self, reply: "asyncio.Future[bytes]") -> None:
        self._reply = reply

    async def readline(self) -> bytes:
        return await self._reply


class _NoopWriter:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeProc:
    """Subprocess whose communicate() blocks on a future the test resolves."""

    def __init__(self, reply: "asyncio.Future[tuple[bytes, bytes]]") -> None:
        self._reply = reply
        self.returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return await self._reply

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return 0


async def _race_cancel_against(task, resolve) -> None:
    """Park `task` in its bounded wait, then resolve + cancel in one tick.

    Both wake-ups queue in the same event-loop iteration because there is no
    `await` between `resolve()` and `task.cancel()` -- the race is constructed,
    not sampled.
    """
    for _ in range(4):
        await asyncio.sleep(0)
    resolve()
    task.cancel()


async def test_run_answers_cancellation_racing_the_mux_status_reply(
    tmp_path, monkeypatch,
):
    """The every-tick chain: _tick -> _active_source -> selected_source.

    This one drives the REAL RendererClient call through the REAL observer
    loop, because that composition is the reported symptom: an immortal
    observer whose `stop()` never returns.
    """
    loop = asyncio.get_running_loop()
    reply: asyncio.Future[bytes] = loop.create_future()

    async def fake_open_unix_connection(_path):
        return _PendingReader(reply), _NoopWriter()

    monkeypatch.setattr(
        "jasper.renderer.asyncio.open_unix_connection",
        fake_open_unix_connection,
    )
    renderer = RendererClient(librespot_state_path=str(tmp_path / "missing.env"))

    class _RendererBackedCoordinator(_FakeCoordinator):
        """_active_source that goes through the real renderer call.

        _FakeCoordinator's returns a constant, which cannot reproduce anything:
        the defect is one call further down, inside selected_source's wait.
        """

        async def _active_source(self):
            await renderer.selected_source()
            return Source.IDLE

    obs = VolumeObserver(
        _RendererBackedCoordinator(),
        librespot_state_path=str(tmp_path / "missing.env"),
    )
    task = asyncio.create_task(obs._run())
    await _race_cancel_against(
        task, lambda: reply.set_result(b'{"selected_source":"idle"}\n'),
    )

    _done, pending = await asyncio.wait({task}, timeout=10.0)
    assert not pending, (
        "VolumeObserver._run ignored cancellation and is still polling -- a "
        "swallowed CancelledError makes the observer immortal and hangs "
        "VolumeObserver.stop()'s `await self._task` (#2003)"
    )
    assert task.cancelled()


async def test_busctl_set_property_answers_cancellation_racing_the_subprocess(
    monkeypatch,
):
    """The transition chain's last hop (jasper/volume_coordinator.py).

    Reached from _tick only when the active source changes, so narrower than
    the mux-status path above -- but the same swallow, and the same immortal
    observer when it fires.
    """
    loop = asyncio.get_running_loop()
    reply: asyncio.Future[tuple[bytes, bytes]] = loop.create_future()

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(reply)

    monkeypatch.setattr(
        "jasper.volume_coordinator.asyncio.create_subprocess_exec", fake_exec,
    )
    task = asyncio.create_task(
        volume_coordinator._busctl_set_property(
            "org.bluealsa", "/path", "org.bluez.MediaTransport1",
            "Volume", "q", "64",
        )
    )
    await _race_cancel_against(task, lambda: reply.set_result((b"", b"")))

    _done, pending = await asyncio.wait({task}, timeout=10.0)
    assert not pending, (
        "_busctl_set_property ignored cancellation -- a swallowed "
        "CancelledError on the active-source transition path makes "
        "VolumeObserver._run immortal (#2003)"
    )
    assert task.cancelled()


async def test_bluealsa_list_pcms_answers_cancellation_racing_the_subprocess(
    monkeypatch,
):
    """The transition chain one hop earlier (jasper/bluealsa_probe.py).

    _set_bluetooth resolves the transport path through this probe BEFORE it
    calls _busctl_set_property, both directly awaited. Fixing only the later
    call would leave the loop just as immortal when the cancel lands here.
    """
    import logging

    loop = asyncio.get_running_loop()
    reply: asyncio.Future[tuple[bytes, bytes]] = loop.create_future()

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc(reply)

    monkeypatch.setattr(
        "jasper.bluealsa_probe.asyncio.create_subprocess_exec", fake_exec,
    )
    task = asyncio.create_task(
        bluealsa_probe.list_pcms(logging.getLogger("test"))
    )
    await _race_cancel_against(task, lambda: reply.set_result((b"", b"")))

    _done, pending = await asyncio.wait({task}, timeout=10.0)
    assert not pending, (
        "bluealsa_probe.list_pcms ignored cancellation -- a swallowed "
        "CancelledError on the active-source transition path makes "
        "VolumeObserver._run immortal (#2003)"
    )
    assert task.cancelled()
