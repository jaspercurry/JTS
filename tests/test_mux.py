# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.mux — the renderer source-arbiter.

Probes are stubbed at the source-arbiter boundary.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import signal
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import ANY, DEFAULT, AsyncMock, MagicMock, call

import pytest

import jasper.airplay_session as airplay_session
import jasper.mux as mux_module
from jasper.accounts import Account
from jasper.busctl import BusctlResult
from jasper.music_sources import MUSIC_SOURCES, VolumeMode
from jasper.mux import Mux, Source
from jasper.spotify_router import (
    ACCOUNT_OK,
    AccountClient,
    AccountStatus,
    BuildResult,
    Router,
)
from jasper.volume_coordinator import VolumeCoordinator
from jasper.volume_curve import percent_to_db
from jasper.volume_handoff import SourceHandoff
from jasper.volume_persistence import VolumePersistence

from ._async_wait import wait_signalled
from ._log_events import event_field_maps, event_fields, event_records
from .fake_clock_fixtures import FakeClock

# Shorthand for the snapshot tables below.
S, A, B, U = Source.SPOTIFY, Source.AIRPLAY, Source.BLUETOOTH, Source.USBSINK


class _FakeVolumeCoordinator:
    """The coordinator's handoff surface; every step lands in `events`."""

    def __init__(self):
        self.events: list[str] = []
        self.next_result = "ok"
        self.finalize_result = True
        self.on_handoff_lease_exit = None

    @asynccontextmanager
    async def source_handoff_operation(self):
        self.events.append("handoff_lease_enter")
        try:
            yield
        finally:
            if self.on_handoff_lease_exit is not None:
                self.on_handoff_lease_exit()
            self.events.append("handoff_lease_exit")

    async def prepare_source_handoff(self, prev, current, *, reason):
        self.events.append(f"prepare:{current.value}")
        return SourceHandoff(
            prev, current, reason, level=50,
            prev_mode=VolumeMode.CAMILLA_MASTER,
            current_mode=VolumeMode.CAMILLA_MASTER,
            result=self.next_result,
        )

    async def finalize_source_handoff(self, handoff):
        self.events.append(f"finalize:{handoff.current_source.value}")
        return self.finalize_result

    async def abort_source_handoff(self, handoff):
        self.events.append(f"abort:{handoff.current_source.value}")
        return True

    async def publish_volume_context(self):
        self.events.append("publish_volume_context")


def _handoff(source: Source) -> list[str]:
    """One successful handoff's coordinator steps, as `_record_order` logs them."""
    return [
        "handoff_lease_enter",
        f"prepare:{source.value}",
        f"select:{source.value}",
        f"finalize:{source.value}",
        "handoff_lease_exit",
        "publish_volume_context",
    ]


def _new_mux(tmp_path, coordinator=None) -> Mux:
    """A Mux on per-test state files. Its gate commands are observed, not
    replaced: the NONE latch and the failure episode live in the real
    methods, so a fan-in fault goes in at ``mux_module.fanin_command``."""
    m = Mux(
        librespot_state_path=str(tmp_path / "librespot.state.env"),
        volume_coordinator=(
            coordinator if coordinator is not None else _FakeVolumeCoordinator()
        ),
        mode_state_path=str(tmp_path / "mux_mode.json"),
    )
    for name in ("_fanin_select", "_fanin_select_label", "_fanin_none"):
        setattr(m, name, AsyncMock(wraps=getattr(m, name)))
    return m


@pytest.fixture
def mux(tmp_path, monkeypatch):
    monkeypatch.setattr(mux_module, "fanin_command", AsyncMock(return_value={}))
    monkeypatch.setattr(
        airplay_session, "run_busctl",
        AsyncMock(return_value=BusctlResult(0, b"", b"")),
    )
    return _new_mux(tmp_path)


class _Probes:
    """The four source probes; `play(*sources)` sets the next snapshot."""

    def __init__(self, mocks: dict[Source, AsyncMock]) -> None:
        self._mocks = mocks

    def __getitem__(self, source: Source) -> AsyncMock:
        return self._mocks[source]

    def play(self, *sources: Source) -> None:
        for source, probe in self._mocks.items():
            probe.return_value = source in sources


@pytest.fixture
def probes(monkeypatch, mux):
    mocks = {source: AsyncMock(return_value=False) for source in MUSIC_SOURCES}
    for source in (S, A, B):
        monkeypatch.setattr(mux_module, f"{source.value}_playing", mocks[source])
    monkeypatch.setattr(mux, "_usbsink_streaming", mocks[U])
    return _Probes(mocks)


@pytest.fixture
def mux_clock(monkeypatch):
    """Advanceable jasper.mux clock. The asyncio loop clock is untouched."""
    clock = FakeClock(start=1000.0)
    monkeypatch.setattr(mux_module, "time", clock)
    return clock


def _stub_pauses(mux: Mux):
    mux._pause = AsyncMock()
    mux._airplay_session.release = AsyncMock()


def _record_order(mux: Mux) -> list[str]:
    """Log gate moves and losing-source cleanup into the coordinator's events."""
    events = mux._volume_coordinator.events

    async def select(source, **_):
        events.append(f"select:{source.value}")
        return DEFAULT

    async def pause(source):
        events.append(f"pause:{source.value}")

    async def drop():
        events.append("drop:airplay")

    mux._fanin_select.side_effect = select
    mux._pause = pause
    mux._airplay_session.release = drop
    return events


def _usb_direct_lane(mux: Mux, monkeypatch, streaming):
    """Drive the real USB liveness probe from fan-in's per-tick STATUS: each
    element is the direct lane's ``streaming`` edge, ``None`` a STATUS miss;
    the last element repeats."""
    monkeypatch.setattr(
        mux, "_usbsink_streaming", Mux._usbsink_streaming.__get__(mux, Mux),
    )
    ticks = list(streaming)

    async def status(_socket_path, **_):
        value = ticks.pop(0) if len(ticks) > 1 else ticks[0]
        if value is None:
            return None
        return {
            "inputs": [
                {"label": "spotify", "source": "lane", "frames_read": 5},
                {
                    "label": "usbsink",
                    "source": "direct",
                    "direct": {"streaming": value},
                },
            ],
        }

    monkeypatch.setattr(mux_module, "local_status_json", status)


class _ControlWriter:
    def __init__(self):
        self.body = bytearray()

    def write(self, data):
        self.body.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


async def _control(mux, command: str) -> dict:
    reader = asyncio.StreamReader()
    reader.feed_data(command.encode() + b"\n")
    reader.feed_eof()
    writer = _ControlWriter()
    await mux._handle_control_client(reader, writer)
    return json.loads(writer.body)


# --- Alerts, the control socket and the reconciler -------------------------


async def test_alerts_only_mark_their_source_dirty(mux):
    """An alert is a wake hint, never a route; repeats coalesce."""
    for _ in range(2):
        assert await _control(mux, "NOTIFY usbsink") == {
            "accepted": True,
            "source": "usbsink",
            "policy_applied": False,
        }

    status = mux._status_payload()
    usb = status["sources"]["usbsink"]
    assert (usb["notifications"], usb["notifications_coalesced"]) == (2, 1)
    assert usb["last_notification_via"] == "uds"
    assert status["reconciler"]["pending_sources"] == ["usbsink"]
    mux._fanin_select.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "method", "args"),
    [
        ("STATUS", "_control_status", ()),
        ("AUTO", "auto_select", ()),
        ("NOTIFY spotify", "_control_notify", ("spotify",)),
        ("PREEMPT airplay", "_control_preempt", ("airplay",)),
        ("SELECT spotify", "_control_select", ("spotify",)),
        ("TEST_SELECT correction owner", "select_test_fanin_label",
         ("correction", "owner")),
        ("TEST_RELEASE owner", "release_test_fanin_label", ("owner",)),
        # Lines no verb's shape accepts: too many, too few, none at all.
        ("STATUS now", None, None),
        ("NOTIFY", None, None),
        ("TEST_SELECT correction", None, None),
        ("TEST_RELEASE a b", None, None),
        ("NOPE", None, None),
    ],
)
async def test_control_verb_table_parses_each_shape(mux, command, method, args):
    """One parse for the whole vocabulary: a line either reaches its verb's
    method with that verb's arguments, or reaches none and is refused."""
    handlers = {
        verb.method: AsyncMock(return_value={"routed": verb.method})
        for verb in mux_module._CONTROL_VERBS.values()
    }
    for name, handler in handlers.items():
        setattr(mux, name, handler)

    payload = await _control(mux, command)

    if method is None:
        assert "error" in payload
        assert not any(h.await_count for h in handlers.values())
    else:
        assert payload == {"routed": method}
        handlers[method].assert_awaited_once_with(*args)


@pytest.mark.parametrize(
    "command",
    ["PREEMPT spotify", "PREEMPT bluetooth", "PREEMPT usbsink",
     "PREEMPT betamax", "PREEMPT correction"],
)
async def test_preempt_control_command_serves_airplay_only(mux, command):
    _stub_pauses(mux)

    assert "error" in await _control(mux, command)
    mux._pause.assert_not_awaited()
    mux._airplay_session.release.assert_not_awaited()


@pytest.mark.parametrize(
    ("alerted", "elapsed_sec", "winner"),
    [
        (True, 0.0, A),
        (False, mux_module.EVENT_BACKED_PROBE_SEC / 2, None),
        (False, mux_module.EVENT_BACKED_PROBE_SEC, A),
    ],
    ids=["alerted", "lost-alert-inside-window", "lost-alert-repaired"],
)
async def test_a_start_is_noticed_by_its_event_or_within_the_repair_window(
    mux, probes, mux_clock, alerted, elapsed_sec, winner,
):
    """With its event delivered, a subprocess-backed start is noticed at once;
    with the event lost, no later than EVENT_BACKED_PROBE_SEC, and the patrol
    reports that as a repair."""
    _stub_pauses(mux)
    await mux._reconcile(trigger="patrol", dirty_sources=set())

    probes.play(A)
    mux_clock.now += elapsed_sec
    if alerted:
        mux.notify_source_changed(A, "dbus")
    dirty = set(mux._dirty_sources)
    mux._dirty_sources.clear()
    await mux._reconcile(trigger="alert" if dirty else "patrol", dirty_sources=dirty)

    assert mux._winner is winner
    reconciler = mux._status_payload()["reconciler"]
    assert reconciler["patrol_repairs"] == int(winner is not None and not alerted)
    assert (reconciler["last"]["dirty_sources"], reconciler["last"]["changed"]) == (
        ["airplay"] if alerted else [], winner is not None,
    )


async def test_idle_patrols_defer_the_subprocess_backed_probes(
    mux, probes, mux_clock,
):
    """An idle minute of patrols must not fork busctl/bluealsa-cli 60 times."""
    patrols = int(60 / mux.POLL_INTERVAL_SEC)
    for _ in range(patrols):
        await mux._reconcile(trigger="patrol", dirty_sources=set())
        mux_clock.now += mux.POLL_INTERVAL_SEC

    forked = math.ceil(
        patrols * mux.POLL_INTERVAL_SEC / mux_module.EVENT_BACKED_PROBE_SEC,
    )
    assert {source: probes[source].await_count for source in MUSIC_SOURCES} == {
        S: patrols, A: forked, B: forked, U: patrols,
    }


async def test_noop_alert_reconcile_logs_at_debug(mux, probes, caplog):
    with caplog.at_level(logging.DEBUG, logger="jasper.mux"):
        await mux._reconcile(trigger="alert", dirty_sources={S})

    (record,) = event_records(caplog, "mux.source_reconcile")
    assert record.levelno == logging.DEBUG


def _parked(started: asyncio.Event):
    async def park(*_args, **_kwargs):
        started.set()
        await asyncio.Future()

    return park


def _run(mux, monkeypatch, reconcile, *, patrol_sec, control=None, adapter=None):
    """`mux.run()` with its I/O parked and `reconcile` standing in for policy."""
    mux.POLL_INTERVAL_SEC = patrol_sec
    mux._fanin_none_best_effort = AsyncMock()
    mux._run_control_server = control or _parked(asyncio.Event())
    mux._reconcile = reconcile
    monkeypatch.setattr(
        mux_module,
        "start_source_event_tasks",
        lambda *_a, **_k: [asyncio.create_task(adapter())] if adapter else [],
    )
    return asyncio.create_task(mux.run())


async def _stop(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_startup_reconcile_failure_recovers_on_patrol_without_restart(
    mux, monkeypatch,
):
    control_started, adapter_started, recovered = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(),
    )
    calls = []

    async def reconcile(*, trigger, dirty_sources):
        calls.append((trigger, set(dirty_sources)))
        if trigger == "startup":
            raise RuntimeError("transient startup probe failure")
        recovered.set()

    task = _run(
        mux, monkeypatch, reconcile, patrol_sec=0.01,
        control=_parked(control_started), adapter=_parked(adapter_started),
    )
    try:
        await wait_signalled(control_started, "control server started", producer=task)
        await wait_signalled(adapter_started, "adapter tasks started", producer=task)
        await wait_signalled(recovered, "patrol after the failed startup", producer=task)
        assert not task.done()
    finally:
        await _stop(task)

    assert calls[:2] == [("startup", set()), ("patrol", set())]


async def test_alert_storm_does_not_postpone_fixed_patrol(mux, monkeypatch):
    triggers = []
    two_patrols_seen = asyncio.Event()

    async def reconcile(*, trigger, dirty_sources):
        triggers.append(trigger)
        if dirty_sources:
            mux._last_alert_reconcile_at = asyncio.get_running_loop().time()
        if sum("patrol" in seen for seen in triggers) >= 2:
            two_patrols_seen.set()

    async def alert_storm():
        while True:
            mux.notify_source_changed(A, "test")
            await asyncio.sleep(0.005)

    task = _run(mux, monkeypatch, reconcile, patrol_sec=0.02)
    loop = asyncio.get_running_loop()
    start = loop.time()
    storm = asyncio.create_task(alert_storm())
    try:
        # The storm runs alongside the wait: a fixed storm window raced the
        # reconcile cadence and flaked under load (#1909). wait_signalled only
        # breaks a hang; this ceiling, 100x the patrol, pins the cadence.
        await wait_signalled(
            two_patrols_seen, "second patrol-tagged reconcile", producer=task,
        )
        assert loop.time() - start < 2.0
    finally:
        await _stop(storm, task)


async def test_alert_during_coalesce_does_not_queue_empty_reconcile(
    mux, monkeypatch,
):
    """run() labels an empty reconcile "patrol", so with the patrol 30 s out
    any reconcile past the two alerts is the stranded wake this forbids."""
    reconciles = []
    seen = {"startup": asyncio.Event(), 1: asyncio.Event(), 2: asyncio.Event()}

    async def reconcile(*, trigger, dirty_sources):
        reconciles.append((trigger, sorted(s.value for s in dirty_sources)))
        if trigger == "startup":
            seen["startup"].set()
        elif "alert" in trigger:
            mux._last_alert_reconcile_at = asyncio.get_running_loop().time()
            alerts = sum("alert" in done for done, _ in reconciles)
            if alerts in seen:
                seen[alerts].set()

    task = _run(mux, monkeypatch, reconcile, patrol_sec=30.0)
    try:
        await wait_signalled(seen["startup"], "startup reconcile", producer=task)
        mux.notify_source_changed(A, "test")
        await wait_signalled(seen[1], "first alert reconcile", producer=task)
        mux.notify_source_changed(A, "test")
        await asyncio.sleep(0.01)
        mux.notify_source_changed(A, "test")
        await wait_signalled(seen[2], "second alert reconcile", producer=task)
        # A stranded wake runs its empty reconcile one ALERT_COALESCE_SEC later.
        await asyncio.sleep(mux_module.ALERT_COALESCE_SEC * 3)
    finally:
        await _stop(task)

    assert reconciles == [
        ("startup", []),
        ("alert", ["airplay"]),
        ("alert", ["airplay"]),
    ]


@pytest.mark.parametrize(
    ("unknown_for", "winner", "observation"),
    [
        (0.0, B, "unknown"),
        (mux_module.UNKNOWN_ACTIVE_HOLD_SEC, None, "unknown_expired"),
    ],
    ids=["held-without-flutter", "expired-not-pinned"],
)
async def test_an_unreadable_probe_holds_its_last_state_for_a_bounded_grace(
    mux, probes, mux_clock, unknown_for, winner, observation,
):
    _stub_pauses(mux)
    probes.play(B)
    await mux._tick()

    probes[B].return_value = None
    mux_clock.now += unknown_for
    await mux._tick()

    assert mux._winner is winner
    source = mux._status_payload()["sources"]["bluetooth"]
    assert (source["playing"], source["observation"]) == (
        winner is not None, observation,
    )


# --- Automatic arbitration: the latest start wins --------------------------


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([((), None, ())], id="idle"),
        pytest.param([((S,), S, ())], id="first-start-preempts-nothing"),
        pytest.param([((U,), U, ()), ((), None, ())], id="usb-alone-then-idle"),
        pytest.param(
            [((S,), S, ()), ((S, A), A, (S,)), ((A,), A, ())],
            id="newest-wins-and-pauses-once",
        ),
        pytest.param([((U,), U, ()), ((U, A), A, (U,))], id="airplay-over-usb"),
        pytest.param(
            [((B,), B, ()), ((B, S), S, (B,)), ((B, S, A), A, (S, B))],
            id="three-way",
        ),
        pytest.param([((S, A), A, (S,))], id="one-snapshot-ties-on-registry-order"),
        pytest.param(
            [((A,), A, ()), ((A, U), U, ()), ((S, A, U), S, (U,)), ((A, U), U, ())],
            id="winner-stop-falls-back-to-the-newest-start",
        ),
    ],
)
async def test_the_latest_start_wins(mux, probes, steps):
    """Each step is one snapshot: who plays, who wins, whom that tick paused.
    AirPlay is dropped rather than paused (see the AirPlay cleanup pins)."""
    _stub_pauses(mux)
    preempt = mux._usbsink_set_preempt = AsyncMock()
    for playing, winner, paused in steps:
        mux._pause.reset_mock()
        probes.play(*playing)
        await mux._tick()
        assert mux._winner is winner
        assert sorted(c.args[0] for c in mux._pause.await_args_list) == sorted(paused)
    # USB was never muted, so there is no unmute to send.
    preempt.assert_not_awaited()


async def test_one_pause_failure_does_not_abort_pausing_the_rest(mux, probes):
    """One renderer's pause raising (Web API down, busctl missing) must not
    skip the remaining sources or fail the tick."""
    _stub_pauses(mux)
    probes.play(S, B)
    await mux._tick()

    probes.play(S, B, A)
    mux._pause = AsyncMock(side_effect=[RuntimeError("web api down"), None])
    await mux._tick()

    assert mux._winner is A
    assert {c.args[0] for c in mux._pause.await_args_list} == {S, B}


def test_spotify_router_carries_the_household_accounts(mux, tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "a" * 32)
    monkeypatch.setenv(
        "JASPER_SPOTIFY_ACCOUNTS_PATH", str(tmp_path / "accounts.json"),
    )
    (tmp_path / "accounts.json").write_text(
        '{"accounts": [{"name": "jasper", "cache_path": "/nope"}], '
        '"default": "jasper"}'
    )
    client = AccountClient(
        account=Account(name="jasper", cache_path="/nope"), sp=MagicMock(),
    )
    monkeypatch.setattr(
        "jasper.spotify_router.build_clients",
        lambda _registry, **_: BuildResult(
            clients={"jasper": client},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
            default_name="jasper",
        ),
    )

    router = mux._ensure_spotify_router()

    assert isinstance(router, Router)
    assert router.clients == {"jasper": client}
    assert router.statuses[0].state == ACCOUNT_OK
    assert mux._ensure_spotify_router() is router


async def test_without_a_client_id_there_is_no_web_api_pause(mux, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)

    assert mux._ensure_spotify_router() is None
    assert await mux._spotify_pause_via_web_api() is False


# --- USB: liveness off fan-in's direct lane, preemption as a lane mute ------


@pytest.mark.parametrize(
    ("streaming", "winners"),
    [
        ([False, True, True], [None, U, U]),
        ([False], [None] * 4),
        ([False, True, None, True], [None, U, U, U]),
    ],
    ids=["streaming-edge-wins", "idle-frames-never-win", "one-status-miss-holds"],
)
async def test_usb_liveness_is_fanins_direct_streaming_edge(
    mux, probes, monkeypatch, streaming, winners,
):
    _stub_pauses(mux)
    _usb_direct_lane(mux, monkeypatch, streaming)

    for winner in winners:
        await mux._tick()
        assert mux._winner is winner

    status = mux._status_payload()
    assert status["active_source"] == ("usbsink" if winners[-1] else "idle")
    assert status["sources"]["usbsink"]["playing"] is (winners[-1] is not None)


async def test_usb_lane_is_muted_while_preempted_and_unmuted_once_others_idle(
    mux, probes, monkeypatch,
):
    """The mute sits downstream of the lane's telemetry, so a muted host still
    reads as playing (no release/re-mute flap). Fan-in does not persist the
    mute, so it is reasserted every tick until every other source is idle."""
    mute = mux._fanin_lane_mute = AsyncMock(return_value={})
    _usb_direct_lane(mux, monkeypatch, [False, True, True, False])
    await mux._tick()
    await mux._tick()
    assert mux._winner is U

    probes.play(A)
    await mux._tick()
    assert mux._winner is A
    assert mux._status_payload()["sources"]["usbsink"]["playing"] is True

    probes.play()
    await mux._tick()
    assert mute.await_args_list == [
        call("usbsink", True), call("usbsink", True), call("usbsink", False),
    ]
    assert mux._usbsink_preempted is False


async def test_a_new_usb_start_clears_its_old_mute_inside_the_handoff(
    mux, probes,
):
    """Latest-start-wins holds for USB too: a restart while AirPlay owns the
    gate unmutes the lane under the transition lock, so a concurrent manual
    selection cannot land between the unmute and the handoff."""
    _stub_pauses(mux)
    unmutes = []

    async def set_preempt(silenced, *, reason):
        unmutes.append((silenced, reason, mux._transition_lock.locked()))
        mux._usbsink_preempted = silenced

    mux._usbsink_set_preempt = set_preempt
    mux._usbsink_preempted = True
    probes.play(A)
    await mux._tick()

    probes.play(A, U)
    await mux._tick()

    assert mux._winner is U
    assert unmutes == [(False, "new_transition", True)]
    mux._airplay_session.release.assert_awaited()


async def test_a_failed_usb_mute_is_retried_not_latched(mux, caplog):
    """A failed mute is one WARN; the tracked flag stays put so the next tick
    re-attempts it."""
    mute = mux._fanin_lane_mute = AsyncMock(side_effect=RuntimeError("gone"))
    with caplog.at_level(logging.WARNING, logger="jasper.mux"):
        await mux._usbsink_set_preempt(True, reason="preempted_by_winner")
    assert mux._usbsink_preempted is False
    assert event_fields(caplog, "usbsink.preempt_failed")["reason"] == (
        "preempted_by_winner"
    )

    mute.side_effect = None
    await mux._usbsink_set_preempt(True, reason="preempted_by_winner")
    assert mux._usbsink_preempted is True


async def test_status_holds_the_last_committed_source_mid_handoff(mux, probes):
    """Mid-handoff the losing source has already stopped while the winner is
    not committed yet. The honest "idle" would let the volume coordinator
    resolve a carrier against a lane this mux is about to leave."""
    _stub_pauses(mux)
    probes.play(A)
    await mux._tick()
    assert mux._status_payload()["active_source"] == "airplay"

    mid_handoff: list[str] = []
    commit = mux._transition_to_source_locked

    async def observing(*args, **kwargs):
        mid_handoff.append(mux._status_payload()["active_source"])
        return await commit(*args, **kwargs)

    mux._transition_to_source_locked = observing
    probes.play(S)
    await mux._tick()

    assert mid_handoff == ["airplay"]
    assert mux._status_payload()["active_source"] == "spotify"


async def test_source_observations_serialize_probe_and_record(mux):
    """Concurrent patrol/control refreshes cannot commit snapshots out of order."""
    first_probe_started = asyncio.Event()
    release_first_probe = asyncio.Event()
    calls = 0

    async def probe_sources(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_probe_started.set()
            await release_first_probe.wait()
        return {source: False for source in MUSIC_SOURCES}

    mux._probe_sources = probe_sources
    first = asyncio.create_task(mux._observe_sources())
    await wait_signalled(first_probe_started, "first source probe started", producer=first)
    second = asyncio.create_task(mux._observe_sources())
    await asyncio.sleep(0)
    assert calls == 1

    release_first_probe.set()
    await asyncio.gather(first, second)
    assert calls == 2


# --- Manual selection and return to Auto -----------------------------------


async def test_select_source_reports_the_manual_pin(mux, probes):
    probes.play(S, A, B)

    status = await mux.select_source(A)

    assert (status["mode"], status["selected_source"], status["active_source"]) == (
        "manual", "airplay", "airplay",
    )
    assert (
        status["last_handoff"]["id"],
        status["last_handoff"]["from"],
        status["last_handoff"]["to"],
    ) == (1, "idle", "airplay")


async def test_a_manual_pin_ignores_new_starts_that_auto_then_honours(mux, probes):
    _stub_pauses(mux)
    mux._manual_source = mux._winner = A
    probes.play(A)
    await mux._tick()
    probes.play(A, U)
    await mux._tick()

    mux._pause.assert_not_awaited()
    assert mux._fanin_select.await_args_list == [call(A, reason=ANY)] * 2
    assert mux._winner is A

    await mux.auto_select()
    assert (mux._manual_source, mux._winner) == (None, U)
    mux._airplay_session.release.assert_awaited()


async def test_a_control_refresh_records_start_edges_for_auto(mux, probes):
    """select_source's status refresh must record, not consume, a start."""
    _stub_pauses(mux)
    probes.play(S)
    await mux._tick()

    probes.play(S, U)
    status = await mux.select_source(A)
    sources = status["sources"]
    assert sources["usbsink"]["started_seq"] > sources["spotify"]["started_seq"]

    await mux.auto_select()
    assert mux._winner is U


@pytest.mark.parametrize(
    ("playing", "winner", "paused"),
    [((A,), A, []), ((S, A), A, [S]), ((), None, [])],
    ids=["newest-takes-the-gate", "others-are-preempted", "nothing-holds-none"],
)
async def test_auto_select_drops_the_pin_for_latest_start_wins(
    mux, probes, playing, winner, paused,
):
    _stub_pauses(mux)
    mux._manual_source = mux._winner = B
    probes.play(*playing)

    status = await mux.auto_select()

    assert (status["mode"], status["selected_source"], mux._winner) == (
        "auto", None, winner,
    )
    assert status["active_source"] == (winner.value if winner else "idle")
    assert [c.args[0] for c in mux._pause.await_args_list] == paused
    if winner is None:
        mux._fanin_none.assert_awaited_once()
    else:
        mux._fanin_select.assert_awaited_once_with(winner, reason=ANY)


@pytest.mark.parametrize(
    ("return_to_auto", "persisted", "restored"),
    [
        (False, {"mode": "manual", "selected_source": "bluetooth"},
         ("manual", "bluetooth")),
        (True, {"mode": "auto"}, ("auto", None)),
    ],
    ids=["pin", "auto"],
)
async def test_the_mode_survives_a_restart(
    mux, probes, tmp_path, return_to_auto, persisted, restored,
):
    """jasper-mux is Restart=always; a restart is a fresh Mux on the same file."""
    probes.play(A)
    await mux.select_source(B)
    if return_to_auto:
        await mux.auto_select()

    assert json.loads((tmp_path / "mux_mode.json").read_text()) == persisted
    status = _new_mux(tmp_path)._status_payload()
    assert (status["mode"], status["selected_source"]) == restored


# --- The fan-in test lane ----------------------------------------------------


@pytest.mark.parametrize("pin", [None, A], ids=["auto", "manual"])
async def test_a_held_test_gate_keeps_the_lane_whatever_plays(mux, probes, pin):
    """The diagnostic lane outranks the pin and any start that raced the
    owner's idle precheck, and it never becomes the household's selection."""
    mux._manual_source = mux._winner = pin
    probes.play(*MUSIC_SOURCES)

    await mux.select_test_fanin_label("correction", "doctor-aec-probe")
    await mux._tick()

    status = mux._status_payload()
    assert (status["active_source"], status["test_owner"]) == (
        "correction", "doctor-aec-probe",
    )
    assert (status["mode"], status["selected_source"]) == (
        ("manual", "airplay") if pin else ("auto", None)
    )
    mux._fanin_select.assert_not_awaited()
    mux._fanin_select_label.assert_awaited_with("correction", reason=ANY)


@pytest.mark.parametrize(
    ("pin", "recorded"), [(A, True), (None, False)],
    ids=["restores-the-pin", "unrecorded-owner-restores-idle"],
)
async def test_releasing_the_test_gate_restores_the_household_gate(
    mux, pin, recorded,
):
    """An owner whose SELECT landed but whose reply was lost can release too."""
    mux._manual_source = mux._winner = pin
    if recorded:
        mux._test_fanin_label = "correction"
        mux._test_fanin_owner = "correction-measurement"

    status = await mux.release_test_fanin_label("correction-measurement")

    assert (status["test_source"], status["test_owner"]) == (None, None)
    assert status["active_source"] == (pin.value if pin else "idle")
    if pin is None:
        mux._fanin_none.assert_awaited_once()
    else:
        mux._fanin_select.assert_awaited_once_with(pin, reason=ANY)
        mux._fanin_none.assert_not_awaited()


async def test_the_test_gate_refuses_an_empty_owner(mux):
    assert "error" in await mux.select_test_fanin_label("correction", " \t")
    assert "error" in await mux.release_test_fanin_label(" \t")
    mux._fanin_select_label.assert_not_awaited()
    mux._fanin_none.assert_not_awaited()
    assert mux._status_payload()["test_owner"] is None


async def test_the_test_gate_is_idempotent_for_its_owner_and_busy_for_others(mux):
    owner = "correction-measurement"
    first = await mux.select_test_fanin_label("correction", owner)
    retry = await mux.select_test_fanin_label("correction", owner)
    assert first["test_owner"] == retry["test_owner"] == owner

    assert "error" in await mux.select_test_fanin_label("correction", "other-owner")
    assert "error" in await mux.release_test_fanin_label("other-owner")
    assert "error" in await mux.select_test_fanin_label("spotify", owner)
    assert mux._fanin_select_label.await_count == 2
    assert mux._status_payload()["test_owner"] == owner
    assert (await mux.release_test_fanin_label(owner))["test_owner"] is None


@pytest.mark.parametrize("action", ["manual", "auto"])
async def test_source_selection_is_refused_before_any_probe_during_test_gate(
    mux, probes, action,
):
    """A held test lease refuses both selection paths before any mutation:
    no fan-in call, no coordinator event, no source probe."""
    mux._test_fanin_label = "correction"
    mux._test_fanin_owner = "correction-measurement"
    mux._test_fanin_expires_at = 100.0

    result = await (mux.select_source(A) if action == "manual" else mux.auto_select())

    assert "error" in result
    mux._fanin_select.assert_not_awaited()
    mux._fanin_none.assert_not_awaited()
    assert mux._volume_coordinator.events == []
    for source in MUSIC_SOURCES:
        probes[source].assert_not_awaited()
    assert mux._test_fanin_owner == "correction-measurement"


async def test_renewing_the_test_gate_extends_its_lease(mux, mux_clock):
    first = await mux.select_test_fanin_label("correction", "correction-measurement")
    mux_clock.now += 40.0
    renewed = await mux.select_test_fanin_label("correction", "correction-measurement")

    assert first["test_lease_remaining_sec"] == mux_module.FANIN_TEST_LEASE_SEC
    assert renewed["test_lease_remaining_sec"] == mux_module.FANIN_TEST_LEASE_SEC


@pytest.mark.parametrize(
    ("rollback", "held"),
    [({}, (None, None)),
     (RuntimeError("fanin down"), ("correction-measurement", "correction"))],
    ids=["rolled-back", "rollback-failed-owner-kept"],
)
async def test_a_failed_test_select_keeps_the_owner_unless_rolled_back(
    mux, rollback, held,
):
    """The SELECT may have landed though its reply was lost, so the owner is
    kept for its release unless the household gate is provably restored."""
    mux_module.fanin_command.side_effect = [RuntimeError("response lost"), rollback]

    failed = await mux.select_test_fanin_label("correction", "correction-measurement")

    assert "error" in failed
    status = mux._status_payload()
    assert (status["test_owner"], status["test_source"]) == held


async def test_a_failed_test_release_keeps_the_owner_until_a_retry_lands(mux):
    mux._test_fanin_label = "correction"
    mux._test_fanin_owner = "correction-measurement"
    mux._test_fanin_expires_at = 100.0
    mux_module.fanin_command.side_effect = [RuntimeError("fanin down"), {}]

    assert "error" in await mux.release_test_fanin_label("correction-measurement")
    assert mux._status_payload()["test_owner"] == "correction-measurement"

    released = await mux.release_test_fanin_label("correction-measurement")
    assert "error" not in released
    assert (released["test_owner"], released["test_lease_remaining_sec"]) == (
        None, None,
    )


async def _fanin_refuses_none(command, **_):
    if command == "NONE":
        raise RuntimeError("fanin down")
    return {}


@pytest.mark.parametrize(
    ("none_lands", "owner"),
    [(True, None), (False, "correction-measurement")],
    ids=["self-clears", "restore-failed-stays-owned"],
)
async def test_an_expired_test_gate_clears_only_through_a_landed_restore(
    mux, probes, none_lands, owner,
):
    mux._test_fanin_label = "correction"
    mux._test_fanin_owner = "correction-measurement"
    mux._test_fanin_expires_at = 0.0
    if not none_lands:
        mux_module.fanin_command.side_effect = _fanin_refuses_none

    await mux._tick()

    mux._fanin_none.assert_awaited_once()
    assert mux._status_payload()["test_owner"] == owner
    # Still owned, so the tick keeps music off the lane.
    assert mux._fanin_select_label.await_count == (0 if none_lands else 1)


# --- Handoffs: volume first, then the gate, then losing-source cleanup ------


@pytest.mark.parametrize(
    ("entry", "established", "playing", "target", "cleanup"),
    [
        ("select", None, (S, A, B), A, []),
        ("tick", S, (S, A), A, ["pause:spotify"]),
        ("tick", A, (A, U), U, ["drop:airplay"]),
    ],
    ids=["manual-never-pauses", "auto-pauses-the-loser", "auto-drops-airplay"],
)
async def test_a_handoff_moves_volume_then_the_gate_then_cleans_up(
    mux, probes, entry, established, playing, target, cleanup,
):
    """Slow cloud/Web API pauses and the AirPlay drop run only after the gate
    has moved and the coordinator has published the new volume context."""
    events = _record_order(mux)
    if established is not None:
        probes.play(established)
        await mux._tick()
        events.clear()

    probes.play(*playing)
    if entry == "select":
        await mux.select_source(target)
    else:
        await mux._tick()

    assert events == [*_handoff(target), *cleanup]
    assert mux._winner is target


@pytest.mark.parametrize(
    ("fault", "steps", "result", "mode"),
    [
        ("prepare", ["prepare:airplay"], "failed", "auto"),
        ("fanin", ["prepare:airplay", "select:airplay", "abort:airplay"],
         "fanin_select_failed", "auto"),
        ("finalize", ["prepare:airplay", "select:airplay", "finalize:airplay"],
         "finalize_failed", "manual"),
    ],
)
async def test_a_failed_handoff_still_republishes_the_volume_context(
    mux, probes, fault, steps, result, mode,
):
    probes.play(S, A)
    coord = mux._volume_coordinator
    _record_order(mux)
    if fault == "prepare":
        coord.next_result = "failed"
    elif fault == "fanin":
        mux_module.fanin_command.side_effect = RuntimeError("fanin down")
    else:
        coord.finalize_result = False

    status = await mux.select_source(A)

    assert coord.events == [
        "handoff_lease_enter", *steps, "handoff_lease_exit", "publish_volume_context",
    ]
    assert (status["last_handoff"]["result"], status["mode"]) == (result, mode)


@pytest.mark.parametrize(
    ("entry", "pin", "playing", "seen"),
    [
        ("select", None, (S, A), (A, A)),
        ("tick", None, (S,), (None, S)),
        ("auto", S, (A,), (None, A)),
    ],
    ids=["manual", "auto-tick", "return-to-auto"],
)
async def test_the_new_owner_is_published_before_the_volume_lease_releases(
    mux, probes, entry, pin, playing, seen,
):
    """An observer the lease unblocks must see the lane's new owner, and
    return-to-auto must not expose the former pin after the gate moves."""
    _stub_pauses(mux)
    mux._manual_source = mux._winner = pin
    probes.play(*playing)
    at_release = []
    mux._volume_coordinator.on_handoff_lease_exit = lambda: at_release.append(
        (mux._manual_source, mux._winner),
    )

    if entry == "select":
        await mux.select_source(A)
    elif entry == "tick":
        await mux._tick()
    else:
        await mux.auto_select()

    assert at_release == [seen]


async def test_real_coordinator_handoff_publishes_without_lock_reentry_deadlock(
    tmp_path, probes,
):
    """Context snapshotting runs after the coordinator's handoff lease exits."""
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    persistence.save_listening_level(50)
    camilla = SimpleNamespace(
        get_volume_and_mute=AsyncMock(return_value=(percent_to_db(50), False)),
        get_volume_db=AsyncMock(return_value=percent_to_db(50)),
        set_volume_db=AsyncMock(return_value=True),
        set_main_mute=AsyncMock(return_value=True),
    )
    backend = SimpleNamespace(
        selected_source=AsyncMock(return_value=None),
        active_renderers=AsyncMock(return_value={}),
    )
    published = []

    async def publish(context):
        published.append(context)

    coordinator = VolumeCoordinator(
        camilla=camilla,
        persistence=persistence,
        backend=backend,
        spotify_router=None,
        volume_context_publisher=publish,
        handoff_settle_sec=0.0,
        push_settle_sec=0.0,
    )
    coordinator.load_persisted_level()
    real_mux = _new_mux(tmp_path, coordinator)
    real_mux._usbsink_streaming = probes[U]
    probes.play(A)

    status = await asyncio.wait_for(real_mux.select_source(A), timeout=1.0)

    assert status["selected_source"] == "airplay"
    assert len(published) == 1


async def test_a_failed_auto_handoff_with_no_winner_holds_fanin_none(mux, probes):
    _stub_pauses(mux)
    probes.play(A)
    mux._volume_coordinator.next_result = "failed"

    await mux._tick()

    mux._fanin_select.assert_not_awaited()
    mux._fanin_none.assert_awaited_once()
    assert mux._winner is None
    assert mux._volume_coordinator.events.count("publish_volume_context") == 1


@pytest.mark.parametrize(
    ("playing", "winner", "reason"),
    [((S, A), A, "auto_retry"), ((S, A, U), U, "auto_new_source")],
    ids=["retried", "superseded-by-a-newer-start"],
)
async def test_a_failed_auto_handoff_is_retried_unless_a_newer_start_wins(
    mux, probes, playing, winner, reason,
):
    """An old pending retry must not consume a newer source-start edge."""
    _stub_pauses(mux)
    coord = mux._volume_coordinator
    probes.play(S)
    await mux._tick()
    coord.next_result = "failed"
    probes.play(S, A)
    await mux._tick()
    assert (mux._winner, mux._pending_auto_target) == (S, A)
    assert mux._last_handoff["result"] == "failed"

    coord.next_result = "ok"
    probes.play(*playing)
    await mux._tick()

    assert (mux._winner, mux._pending_auto_target) == (winner, None)
    handoff = mux._last_handoff
    assert (handoff["id"], handoff["to"], handoff["reason"], handoff["result"]) == (
        3, winner.value, reason, "ok",
    )


# --- The fan-in gate's idle latch and failure episode ----------------------


async def test_idle_is_reasserted_after_every_select(mux, probes):
    """SELECT clears the idle latch, so the winner stopping sends NONE again."""
    _stub_pauses(mux)
    for playing in ((), (A,), ()):
        probes.play(*playing)
        await mux._tick()

    assert mux._fanin_none.await_count == 2
    assert mux._winner is None


@pytest.mark.parametrize(
    "side_effect, awaits, consecutive_failures",
    [
        (None, 1, None),
        ([RuntimeError("fanin down"), RuntimeError("fanin down"), DEFAULT],
         3, "2"),
    ],
    ids=["lands_first_try", "retried_through_outage"],
)
async def test_idle_fanin_none_is_asserted_until_it_lands(
    mux, probes, caplog, side_effect, awaits, consecutive_failures,
):
    """Idle is an edge, not a 1 Hz heartbeat: NONE repeats only while it
    fails, and the failure episode is reported on its two edges."""
    mux_module.fanin_command.side_effect = side_effect

    with caplog.at_level(logging.INFO, logger=mux_module.__name__):
        for _ in range(3):
            await mux._tick()

    assert mux._fanin_none.await_count == awaits
    failed = event_field_maps(caplog, "mux.fanin_gate_failed")
    recovered = event_field_maps(caplog, "mux.fanin_gate_recovered")
    if consecutive_failures is None:
        assert (failed, recovered) == ([], [])
    else:
        assert [fields["reason"] for fields in failed] == ["auto_idle"]
        assert [fields["consecutive_failures"] for fields in recovered] == [
            consecutive_failures,
        ]


async def test_landed_strict_gate_closes_the_best_effort_failure_episode(
    mux, probes, caplog,
):
    """One episode across both paths: the strict SELECT of a manual pin
    closes the episode the idle NONE opened, under its own reason."""
    with caplog.at_level(logging.INFO, logger=mux_module.__name__):
        mux_module.fanin_command.side_effect = RuntimeError("fanin down")
        await mux._tick()
        mux_module.fanin_command.side_effect = None
        probes.play(A)
        await mux.select_source(A)

    recovered = event_field_maps(caplog, "mux.fanin_gate_recovered")
    assert [
        (fields["reason"], fields["consecutive_failures"])
        for fields in recovered
    ] == [("manual", "1")]


# --- Losing-source cleanup: AirPlay's session, Bluetooth's AVRCP -----------


@pytest.mark.parametrize("return_to_auto", [False, True])
@pytest.mark.parametrize("prior", [(), (A,)], ids=["never-played", "paused"])
async def test_paused_airplay_session_is_released_on_takeover(
    mux, probes, monkeypatch, return_to_auto, prior,
):
    drop = AsyncMock(return_value=BusctlResult(0, b"", b""))
    monkeypatch.setattr(airplay_session, "run_busctl", drop)
    probes.play(*prior)
    await mux._tick()
    probes.play()
    await mux._tick()
    drop.assert_not_awaited()

    if return_to_auto:
        await mux.select_source(U)
        drop.assert_not_awaited()
    probes.play(U)
    if return_to_auto:
        await mux.auto_select()
    else:
        await mux._tick()
    assert mux._winner is U
    assert [c.args[-1] for c in drop.await_args_list] == ["DropSession"]
    await mux._tick()
    assert drop.await_count == 1


@pytest.mark.parametrize(
    "responses,methods,status,reason",
    [
        ([BusctlResult(0, b"", b"")], ["DropSession"], "ok", "drop_acknowledged"),
        ([None, BusctlResult(0, b"", b"")], ["DropSession", "Stop"], "degraded", "stop_unconfirmed"),
        ([None, None], ["DropSession", "Stop"], "degraded", "cleanup_failed"),
        ([BusctlResult(1, b"", b'Call failed: Name "org.gnome.ShairportSync" does not exist')], ["DropSession"], "ok", "receiver_absent"),
    ],
)
async def test_airplay_cleanup_outcome_keeps_new_source_authoritative(
    mux, probes, monkeypatch, responses, methods, status, reason,
):
    drop = AsyncMock(side_effect=responses)
    monkeypatch.setattr(airplay_session, "run_busctl", drop)
    probes.play(A)
    await mux._tick()
    assert mux._status_payload()["airplay_session_cleanup"]["status"] == "unobserved"
    probes.play(U)
    await mux._tick()
    assert mux._winner is U
    assert [c.args[-1] for c in drop.await_args_list] == methods
    fact = mux._status_payload()["airplay_session_cleanup"]
    assert (fact["status"], fact["reason"], fact["attempts"]) == (status, reason, 1)
    assert fact["attempted_at"] > 0


@pytest.mark.parametrize("preempt", [False, True], ids=["takeover", "preempt"])
async def test_airplay_cleanup_finishes_before_a_new_selection(
    mux, probes, monkeypatch, preempt,
):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def drop(*args):
        assert mux._winner is U
        entered.set()
        await finish.wait()
        assert mux._winner is U
        return BusctlResult(0, b"", b"")

    monkeypatch.setattr(airplay_session, "run_busctl", drop)
    probes.play(A)
    await mux._tick()
    probes.play(U)
    if preempt:
        await mux.select_source(U)
    operation = _control(mux, "PREEMPT airplay") if preempt else mux._tick()
    takeover = asyncio.create_task(operation)
    await wait_signalled(entered, "AirPlay cleanup entered", producer=takeover)
    newer_selection = asyncio.create_task(mux.select_source(A))
    await asyncio.sleep(0)
    assert not takeover.done()
    assert not newer_selection.done()
    assert mux._status_payload()["airplay_session_cleanup"]["reason"] == "cleanup_pending"
    finish.set()
    result, _ = await asyncio.gather(takeover, newer_selection)
    if preempt:
        assert result == {"preempted": "airplay"}
    assert mux._status_payload()["airplay_session_cleanup"]["reason"] == "drop_acknowledged"
    assert mux._manual_source is A


@pytest.mark.parametrize(
    ("error", "event", "fields"),
    [
        (None, "bluetooth.preempt_pause", {"result": "ok"}),
        (RuntimeError("no player"), "bluetooth.preempt_pause_failed",
         {"action": "phone_side_pause_required"}),
    ],
    ids=["paused", "no-avrcp-player"],
)
async def test_bluetooth_preempt_is_a_best_effort_avrcp_pause(
    mux, monkeypatch, caplog, error, event, fields,
):
    avrcp = AsyncMock(side_effect=error)
    monkeypatch.setattr(mux_module, "bluetooth_avrcp_call", avrcp)

    with caplog.at_level(logging.INFO, logger="jasper.mux"):
        await mux._pause(B)

    avrcp.assert_awaited_once_with("Pause")
    assert event_fields(caplog, event).items() >= fields.items()


# --- The daemon ----------------------------------------------------------------


def _daemon_main(monkeypatch, tmp_path, ready: asyncio.Event) -> asyncio.Task:
    """Start `_amain` with its I/O stubbed, signalling `ready` on `mux.ready`."""
    real_log_event = mux_module.log_event

    def signalling_log_event(target, name, **kwargs):
        real_log_event(target, name, **kwargs)
        if name == "mux.ready":
            ready.set()

    monkeypatch.setattr(mux_module, "log_event", signalling_log_event)
    monkeypatch.setattr(
        mux_module, "MUX_CONTROL_SOCKET_PATH", str(tmp_path / "control.sock"),
    )
    monkeypatch.setattr(
        mux_module, "start_source_event_tasks", lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(Mux, "_fanin_none_best_effort", AsyncMock())
    monkeypatch.setattr(Mux, "_reconcile", AsyncMock())
    return asyncio.create_task(
        mux_module._amain(
            SimpleNamespace(librespot_state=str(tmp_path / "librespot.env")),
        ),
    )


async def test_the_daemon_reports_ready_once_and_unwinds_on_sigterm(
    monkeypatch, tmp_path, caplog,
):
    """SIGTERM's default disposition kills the interpreter with `run()`'s
    finally — the control server and renderer event tasks — unrun, so the
    daemon asks asyncio to unwind instead and says so once. The handler is
    captured rather than raised for real: a regression would otherwise kill
    the pytest process instead of failing this test. Delete with the events.
    """
    caplog.set_level(logging.INFO, logger=mux_module.__name__)
    handlers: dict[int, object] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb),
    )
    ready = asyncio.Event()
    task = _daemon_main(monkeypatch, tmp_path, ready)
    await wait_signalled(ready, "mux announced readiness", producer=task)

    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    handlers[signal.SIGTERM]()  # what the kernel's signal would reach
    await asyncio.wait_for(task, timeout=10.0)

    (fields,) = event_field_maps(caplog, "mux.ready")
    assert fields["patrol_s"] == str(Mux.POLL_INTERVAL_SEC)
    assert len(event_records(caplog, "mux.shutdown")) == 1
