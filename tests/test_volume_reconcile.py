# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.audio_control.volume_reconcile, driven through VolumeCoordinator."""
from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack

import pytest

from tests._async_wait import wait_signalled
from tests._log_events import event_field_maps, event_fields, event_records
from tests.volume_coordinator_fixtures import (
    _assert_persisted,
    _coord,
    _FakeBackend,
    _FakeCamilla,
    _owned_coord,
    _real_coord,
)

from jasper.atomic_io import advisory_file_lock
from jasper.audio_control.camilla import CamillaUnavailable
from jasper.control import measurement_hold
from jasper.dsp_control.dsp_apply import camilla_graph_mutation
from jasper.playback_state.music_sources import Source
from jasper.platform.control_client import DEFAULT_TIMEOUT, ControlError
from jasper.voice import measurement_hold as voice_measurement
from jasper.voice.measurement_hold import MEASUREMENT_AUTOCLEAR_SEC
from jasper.audio_control.volume_coordinator import VolumeCoordinator
from jasper.audio_routes.volume_curve import percent_to_db
from jasper.audio_control.volume_observers import VolumeObserver
from jasper.service_state.volume_persistence import VolumePersistence


@pytest.fixture(autouse=True)
def measurement_hold_served(monkeypatch) -> measurement_hold.MeasurementHold:
    """jasper-control's real hold, free unless a test takes it, served where
    `read_measurement_hold` asks: a reconcile write that raises consults it."""
    hold = measurement_hold.MeasurementHold()
    monkeypatch.setattr(
        "jasper.platform.control_client.get_measurement", lambda **_: hold.snapshot(),
    )
    return hold


# ---------- maybe_reconcile_camilla ------------------------------------------


@pytest.mark.parametrize("source", [Source.SPOTIFY, Source.IDLE])
async def test_every_tick_refreshes_the_level_another_process_saved(tmp_path, source):
    coord, _cam, persistence = _coord(tmp_path, selected=source.value, level=40)
    persistence.save_listening_level(70, mark_user_change=True)

    await coord.maybe_reconcile_camilla(source=source)

    assert coord.get_listening_level() == 70


@pytest.mark.parametrize(
    ("current_db", "level", "writes"),
    [
        # Camilla's own jitter / sub-percentile rounding.
        pytest.param(percent_to_db(70) - 0.3, 70, False, id="dead_band"),
        pytest.param(-18.0, 76, True, id="quiet_drift"),
        pytest.param(-8.0, 70, True, id="loud_drift"),
        pytest.param(0.0, 0, True, id="deep_loud_drift"),
        # No depth is exempt: a quiet drift nobody announced at the writer
        # lock is a stranded fader, not somebody's duck (ADR-0368).
        pytest.param(percent_to_db(70) - 25.0, 70, True, id="deep_quiet_drift"),
    ],
)
async def test_which_drift_the_reconciler_corrects(
    tmp_path, current_db, level, writes,
):
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=current_db, level=level, mark_user_change=True,
    )

    await coord.maybe_reconcile_camilla()

    if writes:
        assert cam.set_calls == [pytest.approx(percent_to_db(level))]
        assert cam.mute_calls == [level == 0]
    else:
        assert cam.set_calls == []
        assert cam.mute_calls == []


@pytest.mark.parametrize(
    ("active", "expected_source"),
    [
        pytest.param({}, Source.IDLE, id="idle"),
        # Push-mode source: `_tick` awaits `_read_spotify_percent()`
        # between resolving `current_active` and calling
        # `maybe_reconcile_camilla(source=...)`, unlike the idle
        # branch — the pass-through must survive that intervening await.
        pytest.param({"spotactive": True}, Source.SPOTIFY, id="spotify"),
    ],
)
async def test_observer_tick_resolves_active_source_once(
    tmp_path, active, expected_source,
):
    """VolumeObserver._tick forwards its own resolved source into
    maybe_reconcile_camilla instead of letting the reconciler re-resolve
    it — one `active_renderers()` fork per tick, not two.

    Uses the dead-band case (no drift to correct) so the reconciler never
    reaches its in-lock re-read, which deliberately re-resolves fresh for
    correctness and is unrelated to this fork count.
    """
    backend = _FakeBackend(active=active)
    coord, _, _ = _real_coord(
        tmp_path, backend=backend, db=percent_to_db(70) - 0.3, level=70,
        mark_user_change=True,
    )
    obs = VolumeObserver(
        coord, librespot_state_path=str(tmp_path / "missing.env"),
    )

    await obs._tick()

    assert obs._last_active_source == expected_source
    assert backend.active_renderers_calls == 1


async def test_reconcile_revalidates_after_cross_daemon_volume_change(tmp_path):
    """A stale preflight cannot overwrite a newer user command.

    The observer and control daemon have separate coordinator/persistence
    instances in production. The reconciler may begin a Camilla read just
    before jasper-control lowers the volume; once it joins the shared
    operation lease, it must re-read both canonical intent and Camilla instead
    of writing its stale, louder target.
    """
    path = str(tmp_path / "speaker_volume.json")
    observer_persistence = VolumePersistence(path)
    observer_persistence.save_listening_level(60, mark_user_change=True)
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={})
    observer = VolumeCoordinator(
        camilla=cam, persistence=observer_persistence, backend=backend,
    )
    control = VolumeCoordinator(
        camilla=cam, persistence=VolumePersistence(path), backend=backend,
    )
    read_started = asyncio.Event()
    release_stale_read = asyncio.Event()
    read_count = 0

    async def stale_first_read():
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            read_started.set()
            await release_stale_read.wait()
            return 0.0, False
        return None

    cam.read_hook = stale_first_read
    reconcile = asyncio.create_task(observer.maybe_reconcile_camilla())
    await wait_signalled(
        read_started,
        "reconcile began its first Camilla read",
        producer=reconcile,
    )

    await control.set_listening_level(20)
    control_write_count = len(cam.set_calls)
    release_stale_read.set()
    await reconcile

    assert cam._db == pytest.approx(percent_to_db(20))
    assert len(cam.set_calls) == control_write_count
    _assert_persisted(
        observer_persistence, level=20, db=percent_to_db(20), db_abs=0.01,
    )


async def test_reconcile_repairs_zero_percent_mute_drift(tmp_path):
    """At 0%, matching dB is not enough; main_mute must also be true."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={}, db=-50.0, level=0, mark_user_change=True,
    )
    persistence.save_now(-50.0)

    await coord.maybe_reconcile_camilla()

    # The fader already carried the floor, so the owner leaves it alone and
    # only the mute is repaired — which is what this test is about.
    assert cam._db == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_reconcile_preserves_toggle_mute_restore_level(tmp_path):
    """Toggle mute persists the restore level separately from audible 0%.

    The voice daemon's 1 Hz reconciler must treat `pre_mute_level` as the
    active mute intent. Otherwise it sees listening_level=59%, expects
    main_mute=false, and immediately undoes a remote mute button press.
    """
    coord, cam, persistence = _real_coord(
        tmp_path, active={}, db=percent_to_db(0), level=59, mark_user_change=True,
    )
    cam.muted = True
    persistence.save_now(percent_to_db(0))
    persistence.save_mute_state(59, "remote-mute")

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls == []
    assert cam.mute_calls == []


async def _gate_voice_session(coord, cam):
    coord.note_voice_session(True)


async def _gate_measurement(coord, cam):
    await coord.note_measurement_active(True)


async def _gate_camilla_offline(coord, cam):
    cam.unavailable = True


async def _gate_none(coord, cam):
    return None


@pytest.mark.parametrize(
    ("active", "gate"),
    [
        pytest.param({}, _gate_voice_session, id="voice_session"),
        pytest.param({}, _gate_measurement, id="measurement_active"),
        pytest.param({}, _gate_camilla_offline, id="camilla_unreachable"),
        pytest.param({"spotactive": True}, _gate_none, id="push_mode_source"),
    ],
)
async def test_the_reconciler_stands_down_behind_each_gate(tmp_path, active, gate):
    """Voice session → wait for the turn to end. Measurement → correction's
    ramp lease owns camilla. Push-mode source → camilla is pinned at 0 dB by
    design and the level lives on the source's own slider. Camilla
    unreachable → skip silently and retry on the next tick.

    Camilla sits 15 dB LOUDER than the level implies in every case — the
    direction the reconciler always corrects, per
    test_which_drift_the_reconciler_corrects[loud_drift] — so only the gate
    can hold the write back. None of them may write or raise.
    """
    coord, cam, _ = _real_coord(
        tmp_path,
        active=active,
        db=0.0,
        level=70,
        mark_user_change=True,
    )
    await gate(coord, cam)

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls == []
    assert cam.mute_calls == []


async def test_the_reconciler_tick_clears_a_stranded_measurement_flag(
    tmp_path, monkeypatch, caplog,
):
    """Same bound, applied on the tick's OWN clock.

    The write doors only bound their own refusal; the 1 Hz reconciler reads the
    raw flag, so a flag the voice rollback path stranded would pause drift
    correction for the life of the process. A tick is not a volume write — it
    IS the clock the pause runs on — so it may clear what it finds lapsed.
    """
    now = [0.0]
    monkeypatch.setattr(voice_measurement, "measurement_monotonic", lambda: now[0])
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=0.0, level=70, mark_user_change=True,
    )
    await coord.note_measurement_active(True)

    now[0] = MEASUREMENT_AUTOCLEAR_SEC - 1.0
    await coord.note_measurement_active(True)  # the window renews
    now[0] = MEASUREMENT_AUTOCLEAR_SEC + 1.0   # past the FIRST window's bound
    cam._db = 0.0
    await coord.maybe_reconcile_camilla()
    assert cam.set_calls == [], "a renewing window must stay paused"

    now[0] += MEASUREMENT_AUTOCLEAR_SEC
    with caplog.at_level(logging.WARNING, logger="jasper"):
        await coord.maybe_reconcile_camilla()
    assert cam.set_calls, "a stranded flag must not pause drift correction"
    assert event_fields(caplog, "volume.measurement_flag_expired")


async def test_reconcile_in_flight_stops_when_measurement_begins(tmp_path):
    """MEASURE_PAUSE may race a tick already awaiting Camilla readback."""
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=-3.15, level=70, mark_user_change=True,
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def blocked_read():
        read_started.set()
        await release_read.wait()
        return -3.15, False

    cam.read_hook = blocked_read
    reconcile = asyncio.create_task(coord.maybe_reconcile_camilla())
    await wait_signalled(
        read_started, "camilla volume/mute read started", producer=reconcile,
    )
    await coord.note_measurement_active(True)
    release_read.set()
    await reconcile

    assert cam.set_calls == []


async def test_measurement_pause_waits_for_in_flight_reconcile_write(tmp_path):
    """Pause acknowledges only after an older Camilla write has finished."""
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=-3.15, level=70, mark_user_change=True,
    )
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    original_set = cam.set_volume_db

    async def blocked_set(db, *, best_effort=False):
        write_started.set()
        await release_write.wait()
        return await original_set(db, best_effort=best_effort)

    cam.set_volume_db = blocked_set
    reconcile = asyncio.create_task(coord.maybe_reconcile_camilla())
    await wait_signalled(
        write_started, "camilla volume write started", producer=reconcile,
    )
    pause = asyncio.create_task(coord.note_measurement_active(True))
    await asyncio.sleep(0)
    assert not pause.done()

    release_write.set()
    await reconcile
    await pause
    writes_at_acquire = len(cam.set_calls)
    await coord.maybe_reconcile_camilla()

    assert writes_at_acquire == 1
    assert len(cam.set_calls) == writes_at_acquire


async def test_reconcile_emits_structured_event(tmp_path, caplog):
    """The reconciler's write carries enough context that a debugger can
    answer "who caused the drift" from journalctl alone."""
    coord, _, _ = _real_coord(
        tmp_path, active={}, db=-18.0, level=76, mark_user_change=True,
    )

    caplog.set_level(logging.INFO, logger="jasper")
    await coord.maybe_reconcile_camilla()

    fields = event_fields(caplog, "volume.reconciled")
    assert fields["level"] == "76%"
    assert fields["current_db"] == "-18.00"
    assert fields["expected_db"] == f"{percent_to_db(76):.2f}"
    assert fields["drift_db"] == f"{percent_to_db(76) - (-18.0):+.2f}"


async def test_reconcile_no_loop_when_already_converged(tmp_path):
    """After one reconcile fires and camilla is at expected, the next
    tick must be a no-op (no write loop)."""
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=-18.0, level=76, mark_user_change=True,
    )
    await coord.maybe_reconcile_camilla()
    first_write_count = len(cam.set_calls)
    assert first_write_count == 1

    await coord.maybe_reconcile_camilla()

    assert len(cam.set_calls) == first_write_count


# ---------- graph-swap duck vs. the 1 Hz reconciler -------------------------


async def test_reconciler_stands_down_while_a_dsp_writer_holds_the_graph(
    tmp_path, caplog,
):
    """The swap's claim on the fader is the writer lock, not the duck's depth.

    The realistic shape: a household volume change lands during the bracket,
    so the ducked fader now reads LOUDER than the level the reconciler
    expects — the one direction it always corrects. Only the lock can hold
    that write back, and once the lock goes the very next tick corrects it,
    which is what makes the stand-down transient rather than a second
    carve-out. The stand-down is one line per episode, not one per tick: the
    volume-floor audition holds the lock for minutes (ADR-0368).
    """
    caplog.set_level(logging.INFO, logger="jasper")
    expected_db = percent_to_db(40)
    coord, cam, client = _owned_coord(tmp_path, db=expected_db)
    await coord.set_listening_level(40)
    client.db = 0.0  # some other writer left camilla far too loud

    async with camilla_graph_mutation(
        source="test.swap", lock_path=cam._graph_mutation_lock_path,
    ):
        for _ in range(3):
            await coord.maybe_reconcile_camilla()
        assert client.db == pytest.approx(0.0)

    await coord.maybe_reconcile_camilla()
    assert client.db == pytest.approx(expected_db)
    assert len(event_records(caplog, "volume.reconcile_deferred")) == 1


@pytest.mark.parametrize(
    ("hold_state", "current_db", "writes"),
    [
        pytest.param("held", percent_to_db(70) - 25.0, False, id="raise_held"),
        pytest.param("unreadable", percent_to_db(70) - 25.0, False, id="raise_unreadable"),
        pytest.param("free", percent_to_db(70) - 25.0, True, id="raise_free"),
        pytest.param("held", 0.0, True, id="lowering_held"),
        pytest.param("unreadable", 0.0, True, id="lowering_unreadable"),
    ],
)
async def test_a_raise_waits_on_the_measurement_hold_and_a_lowering_never_does(
    tmp_path, monkeypatch, caplog, measurement_hold_served,
    hold_state, current_db, writes,
):
    """A measurement whose MEASURE_PAUSE never landed still holds
    jasper-control's hold: the reconciler raises the fader only once that hold
    is free and readable, and lowers it regardless (ADR-0368)."""
    caplog.set_level(logging.INFO, logger="jasper")
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=current_db, level=70, mark_user_change=True,
    )
    if hold_state != "free":
        measurement_hold_served.acquire("correction-measurement")
    if hold_state == "unreadable":
        def refused(**_kwargs: object) -> dict:
            raise ControlError("connection refused")

        monkeypatch.setattr("jasper.platform.control_client.get_measurement", refused)

    await coord.maybe_reconcile_camilla()

    assert cam.set_calls == ([pytest.approx(percent_to_db(70))] if writes else [])
    assert coord.reconcile_deferred is not writes
    deferrals = [
        (fields["reason"], fields["hold"])
        for fields in event_field_maps(caplog, "volume.reconcile_deferred")
    ]
    assert deferrals == ([] if writes else [("measurement_hold", hold_state)])


async def test_a_writer_admitted_while_the_hold_is_read_still_defers_the_raise(
    tmp_path, monkeypatch, measurement_hold_served,
):
    """The writer lock is asked after the measurement hold, with nothing
    between it and the write, so a graph swap or a tone admitted while the
    hold is read still holds the raise off; the read itself gives up sooner
    than the control client's default (ADR-0368)."""
    expected_db = percent_to_db(70)
    coord, cam, client = _owned_coord(tmp_path, db=expected_db)
    await coord.set_listening_level(70)
    client.db = expected_db - 25.0
    writer = ExitStack()
    timeouts: list[object] = []

    def read_while_a_writer_is_admitted(**kwargs: object) -> dict:
        timeouts.append(kwargs.get("timeout"))
        writer.enter_context(advisory_file_lock(cam._graph_mutation_lock_path))
        return measurement_hold_served.snapshot()

    monkeypatch.setattr(
        "jasper.platform.control_client.get_measurement",
        read_while_a_writer_is_admitted,
    )
    with writer:
        await coord.maybe_reconcile_camilla()

        assert client.db == pytest.approx(expected_db - 25.0)
        assert coord.reconcile_deferred is True
    (timeout,) = timeouts
    assert isinstance(timeout, float) and timeout < DEFAULT_TIMEOUT


async def test_a_refused_write_speaks_once_and_says_when_it_lands(
    tmp_path, caplog,
):
    """A camilla that refuses writes refuses them at the observer's 1 Hz, so
    the retries are not news: one line opens the fault, one closes it, and
    `volume.reconciled` claims only a write that landed. Delete with the
    events.
    """
    caplog.set_level(logging.INFO, logger="jasper")
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=-18.0, level=76, mark_user_change=True,
    )
    cam.write_errors = [CamillaUnavailable("camilla restarting") for _ in range(3)]

    for _ in range(4):
        await coord.maybe_reconcile_camilla()

    (failed,) = event_field_maps(caplog, "volume.reconcile_write_failed")
    assert failed["error"] == "CamillaUnavailable: camilla restarting"
    (recovered,) = event_field_maps(caplog, "volume.reconcile_write_recovered")
    assert recovered["consecutive_failures"] == "3"
    assert len(event_records(caplog, "volume.reconciled")) == 1

