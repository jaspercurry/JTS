# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.volume_coordinator.

The coordinator is the product's only writer in front of
``CamillaController.set_volume_db``, so the mute, unmute-ordering, guard and
duck-arbitration pins here sit on AGENTS.md non-negotiable 1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._async_wait import wait_signalled
from tests._log_events import (
    event_field_maps,
    event_fields,
    event_records,
    parse_event,
)
from tests.volume_coordinator_fixtures import (
    _assert_persisted,
    _coord,
    _FakeBackend,
    _FakeCamilla,
    _owned_coord,
    _Pushes,
    _real_coord,
    _use_real_pushes,
)

from jasper import bluealsa_probe, camilla, renderer, volume_process
from jasper import spotify_router as spotify_router_mod
from jasper import volume_push_sources as vps_mod
from jasper.accounts import Account
from jasper.atomic_io import advisory_file_lock
from jasper.camilla import CamillaUnavailable
from jasper.control import measurement_hold
from jasper.control.volume_ops import with_coordinator
from jasper.dsp_apply import camilla_graph_mutation
from jasper.spotify_router import AccountClient, Router
from jasper.music_sources import Source
from jasper.platform.control_client import DEFAULT_TIMEOUT, ControlError
from jasper.voice import measurement_hold as voice_measurement
from jasper.voice.measurement_hold import MEASUREMENT_AUTOCLEAR_SEC
from jasper.volume_coordinator import VolumeCoordinator
from jasper.volume_echo import ECHO_WINDOW_SEC
from jasper.volume_scales import (
    BT_VOLUME_MAX,
    bt_volume_to_listening_level,
    listening_level_to_bt_volume,
    listening_level_to_spotify_percent,
    spotify_percent_to_listening_level,
)
from jasper.volume_observers import VolumeObserver
from jasper.volume_owner import (
    ClaimKind,
    VolumeClaimRefused,
    VolumeOwner,
    volume_owner,
)
from jasper.volume_persistence import VolumePersistence
from jasper.volume_curve import percent_to_db
from jasper.web import sound_profile_apply


@pytest.fixture(autouse=True)
def _reset_bluealsa_probe_state():
    bluealsa_probe.note_probe_success()
    yield
    bluealsa_probe.note_probe_success()


@pytest.fixture(autouse=True)
def measurement_hold_served(monkeypatch) -> measurement_hold.MeasurementHold:
    """jasper-control's real hold, free unless a test takes it, served where
    `read_measurement_hold` asks: a reconcile write that raises consults it."""
    hold = measurement_hold.MeasurementHold()
    monkeypatch.setattr(
        "jasper.platform.control_client.get_measurement", lambda **_: hold.snapshot(),
    )
    return hold


@pytest.fixture(autouse=True)
def pushes(monkeypatch) -> _Pushes:
    """Every Spotify/Bluetooth push, delivered unless a test refuses it."""
    return _Pushes.install(monkeypatch)


# ---------- mapping helpers -------------------------------------------------


@pytest.mark.parametrize("level", [0, 50, 100])
def test_spotify_round_trip(level):
    pct = listening_level_to_spotify_percent(level)
    assert spotify_percent_to_listening_level(pct) == level


@pytest.mark.parametrize("level", [0, 25, 50, 75, 100])
def test_bt_round_trip(level):
    vol = listening_level_to_bt_volume(level)
    assert 0 <= vol <= BT_VOLUME_MAX
    # ±1pp slack for the percent↔127 conversion at non-multiples
    assert abs(bt_volume_to_listening_level(vol) - level) <= 1


def test_clamping_below_zero_and_above_100():
    assert listening_level_to_bt_volume(-10) == 0
    assert listening_level_to_bt_volume(150) == BT_VOLUME_MAX


def test_level_one_is_strictly_above_the_mute_floor():
    assert percent_to_db(1) > percent_to_db(0)


# ---------- log readers ----------------------------------------------------


def _warnings(caplog) -> list[str]:
    """Operator-facing prose warnings, whichever volume module words them."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("jasper")
        and record.levelno >= logging.WARNING
        and parse_event(record.getMessage()) is None
    ]


# ---------- outbound dispatch ----------------------------------------------


async def test_set_volume_idle_writes_camilla(tmp_path):
    coord, cam, _ = _coord(tmp_path, active={})
    await coord.set_listening_level(70)
    assert cam.set_calls == [pytest.approx(percent_to_db(70))]
    assert cam.mute_calls[-1] is False


async def test_set_volume_zero_hard_mutes_camilla_master(tmp_path):
    """0% content volume is a real mute, not just the dB curve bottom."""
    coord, cam, persistence = _coord(tmp_path, active={})

    await coord.set_listening_level(0)

    assert cam.mute_calls[-1] is True
    assert cam.set_calls == [pytest.approx(percent_to_db(0))]
    _assert_persisted(persistence, level=0, db=percent_to_db(0))


async def test_set_volume_nonzero_clears_mute_after_volume_write(tmp_path):
    """Unmute order is volume first, then main_mute=false, so there is
    no full-scale transient while returning from a 0% content mute."""
    coord, cam, _ = _coord(tmp_path, active={}, db=-50.0)
    cam.muted = True

    await coord.set_listening_level(75)

    assert cam.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(75))),
        ("mute", False),
    ]


async def test_set_volume_airplay_active_routes_to_camilla(tmp_path, pushes):
    """AirPlay is camilla-as-master: remote/voice/HTTP changes must be
    audible even though modern AirPlay 2 sender slider reflection via
    shairport-sync is unavailable."""
    coord, cam, _ = _coord(tmp_path, active={"aplactive": True})
    await coord.set_listening_level(50)
    assert cam.set_calls == [pytest.approx(percent_to_db(50))]
    assert pushes.calls == []


async def test_manual_selected_source_overrides_raw_renderer_probe(
    tmp_path, pushes,
):
    """Source selection gates what the speaker actually passes, so
    volume dispatch follows mux's manual selection over raw activity."""
    coord, cam, _ = _coord(
        tmp_path, active={"aplactive": True}, selected="spotify",
    )

    await coord.set_listening_level(55)

    assert pushes.spotify == [55]
    assert cam.set_calls == []  # the AirPlay carrier was never written


async def test_set_volume_spotify_active_routes_to_spotify(tmp_path, pushes):
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True}, db=-25.0)
    await coord.set_listening_level(40)
    assert pushes.spotify == [40]
    assert cam.set_calls == []  # Spotify is push-mode; camilla untouched


async def test_push_mode_zero_sets_final_mute_after_source_push(tmp_path, pushes):
    coord, cam, persistence = _coord(tmp_path, active={"spotactive": True})

    await coord.set_listening_level(0)

    assert pushes.spotify == [0]
    assert cam.mute_calls[-1] is True
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    _assert_persisted(persistence, db=percent_to_db(0))


async def test_push_mode_nonzero_clears_stale_final_mute(tmp_path, pushes):
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True}, db=-50.0)
    cam.muted = True

    await coord.set_listening_level(75)

    assert pushes.spotify == [75]
    assert cam.events[-2:] == [
        ("volume", pytest.approx(0.0)),
        ("mute", False),
    ]


async def test_set_volume_spotify_failure_updates_camilla_guard(tmp_path, pushes):
    """If the active push source cannot accept volume, normal user
    volume changes still keep the audible path guarded by Camilla."""
    coord, cam, _ = _coord(tmp_path, active={"spotactive": True})
    pushes.ok[Source.SPOTIFY] = False

    await coord.set_listening_level(25)

    assert cam.set_calls[-1] == pytest.approx(percent_to_db(25))


# ---------- the real Spotify/Bluetooth pushes (every other test stubs them) --


def _spotify_account(*, devices_fn, volume_fn=None) -> AccountClient:
    sp = MagicMock()
    sp.devices = devices_fn
    if volume_fn is not None:
        sp.volume = volume_fn
    return AccountClient(account=Account(name="primary"), sp=sp)


@pytest.mark.parametrize(
    ("case", "expect_ok"),
    [
        ("hung", False),
        ("no_match", False),
        ("write_raises", False),
        ("ok", True),
    ],
)
async def test_set_spotify_push_result_and_echo_stamp(
    tmp_path, monkeypatch, case, expect_ok,
):
    monkeypatch.setattr(spotify_router_mod, "DEVICES_TIMEOUT_SEC", 0.05)
    release = threading.Event()
    volume_calls: list[int] = []

    if case == "hung":
        def _hang():
            release.wait(timeout=10.0)
            return {"devices": []}
        ac = _spotify_account(devices_fn=_hang)
    elif case == "no_match":
        ac = _spotify_account(
            devices_fn=lambda: {"devices": [{"name": "Phone", "id": "p1"}]},
        )
    elif case == "write_raises":
        def _raise(pct, device_id):
            volume_calls.append(pct)
            raise RuntimeError("simulated write failure")
        ac = _spotify_account(
            devices_fn=lambda: {"devices": [{"name": "JTS", "id": "jts-1"}]},
            volume_fn=_raise,
        )
    else:
        def _ok(pct, device_id):
            volume_calls.append(pct)
        ac = _spotify_account(
            devices_fn=lambda: {"devices": [{"name": "JTS", "id": "jts-1"}]},
            volume_fn=_ok,
        )

    router = Router(clients={"primary": ac}, default_name="primary")
    _use_real_pushes(monkeypatch)
    coord, cam, _ = _real_coord(
        tmp_path, active={"spotactive": True},
        spotify_router=router, spotify_device_name="JTS",
    )
    try:
        await asyncio.wait_for(coord.set_listening_level(55), timeout=5.0)
    finally:
        release.set()

    # The push result: only a refused push leaves Camilla guarding the level.
    assert cam.set_calls == (
        [] if expect_ok else [pytest.approx(percent_to_db(55))]
    )
    # The echo stamp: only a delivered push makes the next reading our echo.
    assert await coord.observe_source_volume(Source.SPOTIFY, 30) is (not expect_ok)
    if case == "ok":
        assert volume_calls == [listening_level_to_spotify_percent(55)]


@pytest.mark.parametrize("ok", [True, False], ids=["ok", "failure"])
async def test_set_bluetooth_push_result_and_echo_stamp(tmp_path, monkeypatch, ok):
    monkeypatch.setattr(
        vps_mod,
        "_bluez_alsa_active_transport_path",
        AsyncMock(return_value="/transport"),
    )
    set_property = AsyncMock(return_value=ok)
    monkeypatch.setattr(vps_mod.busctl, "set_property", set_property)
    _use_real_pushes(monkeypatch)
    coord, cam, _ = _real_coord(tmp_path, active={"btactive": True})

    await coord.set_listening_level(55)

    set_property.assert_awaited_once_with(
        "org.bluealsa",
        "/transport",
        "org.bluez.MediaTransport1",
        "Volume",
        "q",
        str(listening_level_to_bt_volume(55)),
        bus="--system",
    )
    assert cam.set_calls == ([] if ok else [pytest.approx(percent_to_db(55))])
    observed = listening_level_to_bt_volume(30)
    assert await coord.observe_source_volume(Source.BLUETOOTH, observed) is (not ok)


def _assert_push_failure_warning(
    caplog, *, guard_confirmed: bool, unconfirmed_warning: str,
) -> None:
    if not guard_confirmed:
        assert _warnings(caplog) == [unconfirmed_warning]
        return
    assert len(_warnings(caplog)) == 1


def _refuse_push_and_guard(
    pushes, cam, source: Source, guard_confirmed: bool, caplog,
) -> None:
    """Refuse the source push; an unconfirmed guard is Camilla refusing the
    mute half, so the guard's dB and context still show."""
    pushes.ok[source] = False
    cam.mute_accepted = guard_confirmed
    caplog.set_level(logging.DEBUG, logger="jasper")


def _assert_guard(cam, caplog, persistence, *, guard_db, context, confirmed):
    """One guard write at ``guard_db`` under ``context``; persisted once it
    is confirmed, never before."""
    assert cam.set_calls == [pytest.approx(guard_db)]
    assert event_field_maps(caplog, "volume.main_mute") == [{
        "muted": "false",
        "context": context,
        "result": "accepted" if confirmed else "failed",
    }]
    _assert_persisted(persistence, db=round(guard_db, 2) if confirmed else -7.5)


@pytest.mark.parametrize(
    ("source", "active_key", "context"),
    [
        (Source.SPOTIFY, "spotactive", "dispatch_spotify_degraded"),
        (Source.BLUETOOTH, "btactive", "dispatch_bluetooth_degraded"),
    ],
)
@pytest.mark.parametrize("guard_confirmed", [True, False])
async def test_push_dispatch_failure_guard_preserves_guard_and_warning(
    tmp_path,
    caplog,
    pushes,
    source: Source,
    active_key: str,
    context: str,
    guard_confirmed: bool,
):
    coord, cam, persistence = _coord(tmp_path, active={active_key: True}, level=70)
    persistence.save_now(-7.5)
    _refuse_push_and_guard(pushes, cam, source, guard_confirmed, caplog)
    level = 25
    guard_db = percent_to_db(level)

    await coord.set_listening_level(level)

    _assert_guard(
        cam, caplog, persistence,
        guard_db=guard_db, context=context, confirmed=guard_confirmed,
    )
    _assert_push_failure_warning(
        caplog,
        guard_confirmed=guard_confirmed,
        unconfirmed_warning=(
            f"{source.value} volume dispatch failed and camilla guard could "
            f"not be confirmed for {guard_db:.1f} dB"
        ),
    )


async def test_set_volume_bluetooth_active_routes_to_bt(tmp_path, pushes):
    coord, cam, _ = _coord(tmp_path, active={"btactive": True}, db=-25.0)
    await coord.set_listening_level(60)
    assert pushes.bluetooth == [60]
    assert cam.set_calls == []  # BT is push-mode; camilla untouched


# Each row: the renderer set before the set, the fader dB it starts at, the
# level set on it and what that set wrote to camilla ("setup"), the renderer
# set mux flips to (None = unchanged), the transition reported, then what the
# transition itself wrote to camilla ("carrier" None = camilla was never
# written at all) and what each source received at the end.
_TRANSITION_CARRIERS = [
    dict(
        id="idle_to_push", before={"spotactive": True}, db=-25.0, level=50,
        setup=[], after=None, prev=Source.IDLE, current=Source.SPOTIFY,
        writes=[0.0], carrier=0.0, spotify=[50, 50], bt=[],
    ),
    dict(
        id="camilla_master_to_push", before={"aplactive": True}, db=0.0,
        level=60, setup=[percent_to_db(60)], after={"spotactive": True},
        prev=Source.AIRPLAY, current=Source.SPOTIFY,
        writes=[0.0], carrier=0.0, spotify=[60], bt=[],
    ),
    dict(
        id="push_to_idle", before={}, db=0.0, level=60,
        setup=[percent_to_db(60)], after=None,
        prev=Source.SPOTIFY, current=Source.IDLE,
        writes=[], carrier=percent_to_db(60), spotify=[], bt=[],
    ),
    dict(
        id="push_to_camilla_master", before={"spotactive": True}, db=0.0,
        level=50, setup=[], after={"aplactive": True},
        prev=Source.SPOTIFY, current=Source.AIRPLAY,
        writes=[percent_to_db(50)], carrier=percent_to_db(50),
        spotify=[50], bt=[],
    ),
    dict(
        id="idle_to_camilla_master", before={}, db=0.0, level=40,
        setup=[percent_to_db(40)], after={"aplactive": True},
        prev=Source.IDLE, current=Source.AIRPLAY,
        writes=[], carrier=percent_to_db(40), spotify=[], bt=[],
    ),
    dict(
        id="push_to_push", before={"spotactive": True}, db=0.0, level=55,
        setup=[], after={"btactive": True},
        prev=Source.SPOTIFY, current=Source.BLUETOOTH,
        writes=[], carrier=None, spotify=[55], bt=[55],
    ),
]


@pytest.mark.parametrize(
    "case", _TRANSITION_CARRIERS, ids=lambda case: case["id"],
)
async def test_which_attenuator_carries_the_level_across_a_transition(
    tmp_path, pushes, case,
):
    """A camilla-master lane keeps percent_to_db(level) on the fader; a
    push-mode lane pins the fader to 0 dB and puts the level on the source's
    own slider. The transition writes only what changing carriers needs.
    """
    backend = _FakeBackend(active=case["before"])
    coord, cam, _ = _coord(tmp_path, backend=backend, db=case["db"])
    await coord.set_listening_level(case["level"])
    before = len(cam.set_calls)
    assert cam.set_calls == [pytest.approx(db) for db in case["setup"]]
    if case["after"] is not None:
        backend._active = dict(case["after"])

    await coord.apply_active_source_transition(case["prev"], case["current"])

    assert cam.set_calls[before:] == [
        pytest.approx(db) for db in case["writes"]
    ]
    if case["carrier"] is None:
        assert cam.set_calls == []
    else:
        assert cam.set_calls[-1] == pytest.approx(case["carrier"])
    assert pushes.spotify == case["spotify"]
    assert pushes.bluetooth == case["bt"]


async def test_transition_drops_a_verdict_the_lease_no_longer_agrees_with(
    tmp_path, pushes,
):
    """The observer resolves the source before the cross-daemon lease. A
    handoff that commits while the verdict queues must not end with camilla
    pinned to 0 dB against a lane whose own slider JTS never writes."""
    backend = _FakeBackend(active={}, selected="spotify")
    coord, cam, _ = _coord(tmp_path, backend=backend, level=50)
    answers = ["spotify", "airplay"]

    async def selected_source() -> str:
        return answers.pop(0) if answers else "airplay"

    backend.selected_source = selected_source
    before = list(cam.set_calls)

    assert await coord._active_source() is Source.SPOTIFY
    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert pushes.spotify == []
    assert cam.set_calls == before


async def test_transition_waits_for_a_held_handoff_lease(tmp_path, pushes):
    """The observer's transition takes the mux handoff's lease: while a
    handoff holds it, the transition neither pushes nor writes Camilla."""
    coord, cam, _ = _coord(tmp_path, active={}, selected="spotify", level=50)

    async with coord.source_handoff_operation():
        transition = asyncio.create_task(
            coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY),
        )
        await asyncio.sleep(0)
        assert not transition.done()
        assert (pushes.calls, cam.set_calls) == ([], [])
    await transition

    assert pushes.spotify == [50]


async def test_transition_suppressed_during_voice_session(tmp_path, pushes):
    """note_voice_session(True) gates apply_active_source_transition."""
    coord, cam, _ = _coord(tmp_path, active={}, selected="spotify")
    coord.note_voice_session(True)
    initial_calls = list(cam.set_calls)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)
    assert cam.set_calls == initial_calls

    coord.note_voice_session(False)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)

    assert pushes.spotify[-1] == coord.get_listening_level()
    assert cam.muted is False


@pytest.mark.parametrize(
    ("active", "level"),
    [
        pytest.param(
            {"aplactive": True, "spotactive": True, "btactive": True},
            50,
            id="over_spotify_and_bt",
        ),
        pytest.param(
            {"aplactive": True, "usbsinkactive": True}, 55, id="over_usbsink",
        ),
    ],
)
async def test_airplay_outranks_every_other_active_renderer(
    tmp_path, pushes, active, level,
):
    """Several renderers can report active during a mux transition window.
    The chain is airplay > spotify > bluetooth > usbsink, matching mux's
    first-source-defined-wins behaviour: a phone-controlled AirPlay session
    is not silently overridden by a Mac plugged into the USB port."""
    coord, cam, _ = _coord(tmp_path, active=active)

    await coord.set_listening_level(level)

    assert cam.set_calls == [pytest.approx(percent_to_db(level))]
    assert pushes.calls == []
    # AirPlay, not the USB sink, is the source whose readings count.
    assert await coord.observe_source_volume(Source.USBSINK, level) is False
    assert await coord.observe_source_volume(Source.AIRPLAY, level) is True


async def test_adjust_volume(tmp_path, pushes):
    """Push-mode adjust path: each set/adjust pushes a fresh value
    to the source's slider."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(50)
    await coord.adjust_listening_level(15)
    assert pushes.spotify == [50, 65]


async def test_adjust_clamps_to_0_and_100(tmp_path, pushes):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(95)
    await coord.adjust_listening_level(20)
    assert pushes.spotify[-1] == 100
    await coord.adjust_listening_level(-200)
    assert pushes.spotify[-1] == 0


async def test_mute_then_unmute(tmp_path, pushes):
    coord, cam, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(70)
    saved = await coord.mute()
    assert saved == 70
    assert pushes.spotify[-1] == 0  # silence
    assert cam.mute_calls[-1] is True
    assert coord.is_muted()
    # The canonical level remains the restore target; every external surface
    # consumes the shared effective projection and therefore renders 0%.
    assert coord.get_listening_level() == 70
    assert coord.get_volume_state().effective_percent == 0
    assert coord.get_volume_state().restore_percent == 70
    record = persistence.load()
    assert record is not None
    assert record.listening_level == 70
    assert record.pre_mute_level == 70
    restored = await coord.unmute()
    assert restored == 70
    assert pushes.spotify[-1] == 70
    assert cam.mute_calls[-1] is False
    assert not coord.is_muted()


async def test_push_observer_preserves_cross_process_mute_restore_level(
    tmp_path, monkeypatch,
):
    """A remote mute's renderer-side 0% echo cannot overwrite its restore level."""
    _use_real_pushes(monkeypatch)
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    control_coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
    )
    observer_coord = VolumeCoordinator(
        camilla=cam, persistence=persistence, backend=backend,
    )

    await control_coord.set_listening_level(60)
    await control_coord.mute()

    accepted = await observer_coord.observe_source_volume(Source.SPOTIFY, 0)

    assert accepted is True
    state = observer_coord.get_volume_state()
    assert state.effective_percent == 0
    assert state.restore_percent == 60
    assert persistence.load().listening_level == 60
    assert persistence.load().pre_mute_level == 60

    # A subsequent, explicit non-zero source-side change ends the temporary
    # mute and becomes the new canonical level.
    await observer_coord.observe_source_volume(Source.SPOTIFY, 65)
    state = observer_coord.get_volume_state()
    assert state.effective_percent == 65
    assert state.restore_percent is None


async def test_push_observer_rejects_stale_nonzero_while_mute_push_pending(
    tmp_path, pushes,
):
    """A pre-push renderer reading cannot cancel another process's mute."""
    state_path = str(tmp_path / "speaker_volume.json")
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    control_coord = VolumeCoordinator(
        camilla=cam,
        persistence=VolumePersistence(state_path),
        backend=backend,
    )
    observer_coord = VolumeCoordinator(
        camilla=cam,
        persistence=VolumePersistence(state_path),
        backend=backend,
    )
    mute_push_started = asyncio.Event()
    release_mute_push = asyncio.Event()

    async def hold_the_mute_push(_source: Source, level: int) -> None:
        if level == 0:
            mute_push_started.set()
            await release_mute_push.wait()

    pushes.hook = hold_the_mute_push
    await control_coord.set_listening_level(60)

    mute_task = asyncio.create_task(control_coord.mute())
    await wait_signalled(
        mute_push_started,
        "mute push began",
        producer=mute_task,
    )

    # mute() has persisted its latch and asserted Camilla main_mute, but the
    # slow source surface still exposes its old 60%. A second process waits for
    # the in-flight intent to finish rather than interpreting half-applied
    # physical state.
    observation = asyncio.create_task(
        observer_coord.observe_source_volume(Source.SPOTIFY, 60),
    )
    await asyncio.sleep(0)
    assert observation.done() is False

    release_mute_push.set()
    await mute_task
    accepted = await observation
    assert accepted is False
    pending = observer_coord.get_volume_state()
    assert pending.effective_percent == 0
    assert pending.restore_percent == 60
    assert pending.mute_token is not None

    # Seeing zero for this exact mute token opens the barrier. A later nonzero
    # observation is now an unambiguous source-side user edit.
    assert await observer_coord.observe_source_volume(Source.SPOTIFY, 0) is True
    assert await observer_coord.observe_source_volume(Source.SPOTIFY, 65) is True
    settled = observer_coord.get_volume_state()
    assert settled.effective_percent == 65
    assert settled.restore_percent is None
    assert settled.mute_token is None


async def test_push_observer_requires_zero_for_each_new_mute_token(tmp_path):
    """Confirmation from an older mute cannot authorize a newer transition."""
    state_path = str(tmp_path / "speaker_volume.json")
    writer = VolumePersistence(state_path)
    observer = VolumeCoordinator(
        camilla=_FakeCamilla(db=0.0),
        persistence=VolumePersistence(state_path),
        backend=_FakeBackend(active={"spotactive": True}),
    )
    writer.save_listening_level(60)
    writer.save_mute_state(60, "mute-a")
    assert await observer.observe_source_volume(Source.SPOTIFY, 0) is True

    # An unmute + a second mute that both land between observer polls: the
    # remembered token-A confirmation must not leak into token B.
    writer.save_mute_state(None, None)
    writer.save_listening_level(60)
    writer.save_mute_state(60, "mute-b")

    assert await observer.observe_source_volume(Source.SPOTIFY, 60) is False
    state = observer.get_volume_state()
    assert state.effective_percent == 0
    assert state.restore_percent == 60
    assert state.mute_token == "mute-b"


async def test_unmute_without_prior_mute_uses_fallback(tmp_path):
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    restored = await coord.unmute(fallback_level=50)
    assert restored == 50


# ---------- echo prevention ------------------------------------------------


# Echo-prevention tests use SPOTIFY as a representative push-mode source.


@pytest.mark.parametrize("observed", [60, 30])
async def test_observe_within_echo_window_ignored(tmp_path, pushes, observed):
    """A poll can briefly see either our own value echoed back or stale
    source state right after our write, especially during source handoff;
    ignore the whole echo window regardless of what it reports."""
    coord, _, _ = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)

    await coord.observe_source_volume(Source.SPOTIFY, observed)

    assert coord.get_listening_level() == 60
    assert pushes.spotify == [60]


async def test_observe_outside_echo_window_becomes_canonical(
    tmp_path, monkeypatch, pushes,
):
    coord, cam, persistence = _coord(tmp_path, active={"spotactive": True})
    await coord.set_listening_level(60)
    # Fast-forward past the echo window without sleeping.
    fake_now = time.monotonic() + ECHO_WINDOW_SEC + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)

    await coord.observe_source_volume(Source.SPOTIFY, 40)

    assert coord.get_listening_level() == 40
    _assert_persisted(persistence, level=40)
    # An observation must NOT trigger an outbound dispatch (no echo).
    assert pushes.spotify == [60]


@pytest.mark.parametrize("seeded_level", [50, 100])
async def test_observe_spotify_clears_degraded_guard(tmp_path, seeded_level):
    """A source-side Spotify slider move proves the source volume surface is
    carrying user intent — including when it lands on the level JTS already
    remembers. Clear any degraded-safe Camilla guard so the path returns to
    normal push-mode loudness."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0, level=seeded_level,
    )
    persistence.save_now(-25.0)

    await coord.observe_source_volume(Source.SPOTIFY, 100)

    assert coord.get_listening_level() == 100
    assert cam.set_calls[-1] == pytest.approx(0.0)
    _assert_persisted(persistence, level=100, db=0.0)


async def test_equal_spotify_observation_publishes_only_when_guard_changes(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, _, persistence = _real_coord(
        tmp_path,
        active={"spotactive": True},
        db=-25.0,
        level=100,
        volume_context_publisher=publish,
    )
    persistence.save_now(-25.0)

    await coord.observe_source_volume(Source.SPOTIFY, 100)
    assert len(published) == 1
    assert published[0].downstream_db == pytest.approx(0.0)

    published.clear()
    await coord.observe_source_volume(Source.SPOTIFY, 100)
    assert published == []


async def test_observe_spotify_repairs_live_guard_after_false_clear(tmp_path):
    """Recover from the legacy split-brain: persistence claimed the push
    guard was clear, but live Camilla was still attenuating the path."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-13.0, level=90,
    )
    persistence.save_now(0.0)

    await coord.observe_source_volume(Source.SPOTIFY, 90)

    assert cam.set_calls[-1] == pytest.approx(0.0)
    _assert_persisted(persistence, level=90, db=0.0)


async def test_successful_push_dispatch_clears_degraded_guard(tmp_path, pushes):
    """If a later outbound push succeeds, Camilla should stop carrying
    the degraded fallback attenuation."""
    coord, cam, persistence = _coord(
        tmp_path, active={"spotactive": True}, db=-25.0, level=50,
    )
    persistence.save_now(-25.0)

    await coord.set_listening_level(50)

    assert pushes.spotify == [50]
    assert cam.set_calls[-1] == pytest.approx(0.0)
    _assert_persisted(persistence, level=50, db=0.0)


async def test_observe_respects_recent_cross_process_write(tmp_path):
    """Hardware knobs hit jasper-control, which has a separate
    coordinator and no shared outbound stamp. A stale observer poll
    should not undo the freshly persisted knob level."""
    coord, _, persistence = _coord(
        tmp_path, active={"spotactive": True}, level=70,
    )

    # jasper-control in another process handling a knob twist.
    persistence.save_listening_level(80)

    assert await coord.observe_source_volume(Source.SPOTIFY, 70) is False

    assert coord.get_listening_level() == 80
    _assert_persisted(persistence, level=80)


async def test_observe_revalidates_active_source_at_mutation_boundary(tmp_path):
    """A queued observation cannot land after mux has switched lanes."""
    backend = _FakeBackend(active={"spotactive": True})
    coord, _, _ = _coord(tmp_path, backend=backend, level=60)
    selections = iter([Source.SPOTIFY.value, Source.BLUETOOTH.value])

    async def changing_selection():
        return next(selections)

    backend.selected_source = changing_selection

    assert await coord.observe_source_volume(Source.SPOTIFY, 40) is False
    assert coord.get_volume_state().effective_percent == 60


# ---------- initialize / boot regression ----------------------------------


async def test_initialize_first_boot_uses_default(tmp_path):
    coord, cam, persistence = _coord(tmp_path, active={})
    target, reason = await coord.initialize(first_boot_default_pct=42)
    assert target == 42
    assert "first-boot" in reason
    _assert_persisted(persistence, level=42)


async def test_initialize_does_not_bump_last_used_at(tmp_path):
    """Boot-time restore must NOT update last_used_at — otherwise
    every restart resets the idle-reset clock and yesterday's
    bedtime 90% never gets clamped."""
    coord, _, persistence = _coord(tmp_path, active={})
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    persistence.path.write_text(json.dumps({
        "version": 2,
        "main_volume_db": -25.0,
        "listening_level": 90,
        "last_used_at": "2026-01-01T00:00:00Z",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }))

    await coord.initialize(
        stale_after_sec=60.0,
        safe_low_pct=20, safe_high_pct=70,
        first_boot_default_pct=50,
    )

    rec = persistence.load()
    assert rec is not None
    assert rec.last_used_at is not None
    # 1 s tolerance for the persistence round-trip.
    assert abs((rec.last_used_at - old_ts).total_seconds()) < 1.0


async def test_user_change_bumps_last_used_at(tmp_path):
    coord, _, persistence = _coord(tmp_path, active={})
    await coord.set_listening_level(45)
    rec = persistence.load()
    assert rec is not None
    assert rec.last_used_at is not None
    age = (datetime.now(timezone.utc) - rec.last_used_at).total_seconds()
    assert 0 <= age < 5


# ---------- AirPlay camilla-master dispatch --------------------------------


async def test_set_airplay_delegates_to_camilla_without_subprocess(
    tmp_path, monkeypatch,
):
    """Real AirPlay dispatch: use CamillaDSP as the reliable audible
    AirPlay volume surface, not shairport-sync DACP/DBus."""
    coord, cam, _ = _real_coord(tmp_path, active={"aplactive": True})

    async def fail_spawn(*args, **kwargs):
        raise AssertionError("AirPlay should not spawn a control subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    await coord.set_listening_level(75)

    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(75))
    # No outbound stamp: the sender's next reading is an edit, not our echo.
    assert await coord.observe_source_volume(Source.AIRPLAY, 30) is True


async def test_observe_airplay_moves_the_camilla_master(tmp_path):
    """The sender's slider is an inbound control surface (ADR-0206): its
    observation becomes the canonical level and, because AirPlay is
    camilla-as-master, lands on CamillaDSP's ramped fader."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={"aplactive": True}, level=70,
    )

    accepted = await coord.observe_source_volume(Source.AIRPLAY, 30)

    assert accepted is True
    assert coord.get_listening_level() == 30
    _assert_persisted(persistence, level=30)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(
        percent_to_db(30)
    )


@pytest.mark.parametrize(
    ("initial", "expect_accepted"),
    [(False, True), (True, False)],
)
async def test_observe_airplay_while_muted_turns_on_initial(
    tmp_path, initial, expect_accepted,
):
    """`observation_initial` is the whole session-start guard (ADR-0206).

    A plain observation is the user reaching for the volume, so it clears the
    mute. The one observation shairport pushes when a sender connects is
    marked initial, and that one must leave a latched mute alone — otherwise
    connecting a Mac unmutes a speaker the owner silenced.
    """
    coord, _, _ = _real_coord(tmp_path, active={"aplactive": True}, level=70)
    await coord.set_muted(True)
    assert coord.get_volume_state().pre_mute_level is not None

    accepted = await coord.observe_source_volume(
        Source.AIRPLAY, 40, initial=initial,
    )

    assert accepted is expect_accepted
    state = coord.get_volume_state()
    if expect_accepted:
        assert state.pre_mute_level is None
        assert state.listening_level == 40
    else:
        assert state.pre_mute_level is not None


@pytest.mark.parametrize(
    ("active", "observed_source"),
    [
        pytest.param({"spotactive": True}, Source.AIRPLAY, id="airplay_vs_spotify"),
        pytest.param({"aplactive": True}, Source.USBSINK, id="usbsink_vs_airplay"),
    ],
)
async def test_observe_inactive_source_is_ignored(
    tmp_path, active, observed_source,
):
    """Stale readings from a non-current renderer must not steal the
    canonical level from the active source."""
    coord, _, _ = _real_coord(tmp_path, active=active, level=70)

    assert await coord.observe_source_volume(observed_source, 30) is False
    assert coord.get_listening_level() == 70


# ---------- source handoff -------------------------------------------------


async def test_observer_transition_push_failure_preserves_guard(tmp_path, pushes):
    """The observer backstop must not undo mux's degraded-safe guard.

    If Spotify/Bluetooth cannot accept a source-side volume write,
    Camilla remains the fallback safety carrier instead of being
    cleared to 0 dB on the next active-source observer tick.
    """
    backend = _FakeBackend(active={"aplactive": True})
    coord, cam, _ = _coord(tmp_path, backend=backend)
    await coord.set_listening_level(40)

    pushes.ok[Source.SPOTIFY] = False
    backend._active = {"spotactive": True}

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert cam.set_calls
    assert 0.0 not in cam.set_calls
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))


@pytest.mark.parametrize(
    ("prev_source", "current_source", "context", "pair"),
    [
        (
            Source.AIRPLAY,
            Source.SPOTIFY,
            "active_source_transition_push_degraded",
            "airplay → spotify",
        ),
        (
            Source.SPOTIFY,
            Source.BLUETOOTH,
            "active_source_transition_push_push_degraded",
            "spotify → bluetooth (push→push)",
        ),
    ],
)
@pytest.mark.parametrize("guard_confirmed", [True, False])
async def test_transition_push_failure_guard_preserves_guard_and_warning(
    tmp_path,
    caplog,
    pushes,
    prev_source: Source,
    current_source: Source,
    context: str,
    pair: str,
    guard_confirmed: bool,
):
    level = 42
    coord, cam, persistence = _coord(
        tmp_path, active={}, level=level, selected=current_source.value,
    )
    persistence.save_now(-7.5)
    _refuse_push_and_guard(pushes, cam, current_source, guard_confirmed, caplog)
    guard_db = percent_to_db(level)

    await coord.apply_active_source_transition(prev_source, current_source)

    _assert_guard(
        cam, caplog, persistence,
        guard_db=guard_db, context=context, confirmed=guard_confirmed,
    )
    _assert_push_failure_warning(
        caplog,
        guard_confirmed=guard_confirmed,
        unconfirmed_warning=(
            f"active source: {pair}; source volume push failed and camilla "
            f"guard could not be confirmed for {guard_db:.1f} dB"
        ),
    )


async def test_get_camilla_target_db_preserves_degraded_push_guard(tmp_path):
    """Push-mode normally restores Camilla to 0 dB, but a degraded
    handoff guard is intentional safety state and must survive restore."""
    coord, _, persistence = _coord(
        tmp_path, active={"spotactive": True}, selected="spotify",
    )
    await coord.set_listening_level(35)
    guard_db = -32.5
    persistence.save_now(guard_db)

    assert await coord.get_camilla_target_db() == pytest.approx(guard_db)

    persistence.save_now(0.0)
    assert await coord.get_camilla_target_db() == pytest.approx(0.0)


async def test_fanin_voice_session_keeps_live_camilla_volume_control(tmp_path):
    """Fan-in owns program ducking, not Camilla, so an in-session remote edit
    must land immediately while source transitions remain session-gated."""
    coord, cam, _ = _real_coord(tmp_path, active={}, db=-25.0)
    coord.note_voice_session(True)

    await coord.set_listening_level(46)

    assert cam.set_calls[-1] == pytest.approx(percent_to_db(46))
    before_transition = list(cam.set_calls)
    await coord.apply_active_source_transition(Source.IDLE, Source.SPOTIFY)
    assert cam.set_calls == before_transition


# ---------- volume-context publishing --------------------------------------


async def test_dispatch_publishes_absolute_canonical_and_downstream_facts(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, _, _ = _coord(
        tmp_path,
        active={"spotactive": True},
        volume_context_publisher=publish,
    )

    await coord.set_listening_level(46)

    assert len(published) == 2
    assert all(
        context.canonical_db == pytest.approx(percent_to_db(46))
        for context in published
    )
    assert all(
        context.downstream_db == pytest.approx(0.0) for context in published
    )
    assert all(context.muted is False for context in published)


async def test_nonzero_intent_publishes_before_slow_spotify_dispatch(
    tmp_path, pushes,
):
    published = []
    cloud_started = asyncio.Event()
    release_cloud = asyncio.Event()

    async def publish(context):
        published.append(context)

    async def blocked_cloud(_source: Source, _level: int) -> None:
        cloud_started.set()
        await release_cloud.wait()

    coord, _, _ = _real_coord(
        tmp_path,
        active={"spotactive": True},
        volume_context_publisher=publish,
    )
    pushes.hook = blocked_cloud

    operation = asyncio.create_task(coord.set_listening_level(67))
    await wait_signalled(cloud_started, "spotify dispatch started", producer=operation)

    assert len(published) == 1
    assert published[0].canonical_db == pytest.approx(percent_to_db(67))
    assert published[0].muted is False

    release_cloud.set()
    assert await operation == 67
    assert len(published) == 2
    assert published[-1].canonical_db == pytest.approx(percent_to_db(67))
    assert published[-1].muted is False


@pytest.mark.parametrize("blocker", ["source_push", "camilla_mute"])
async def test_mute_intent_is_local_and_published_before_the_slow_write(
    tmp_path, pushes, blocker,
):
    """The mute is local intent. Whichever downstream write is slow — the
    Spotify cloud round trip or Camilla's own main_mute — the muted context
    is already published before it returns."""
    published = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def publish(context):
        published.append(context)

    coord, cam, _ = _real_coord(
        tmp_path,
        active={"spotactive": True} if blocker == "source_push" else {},
        db=percent_to_db(59),
        level=59,
        volume_context_publisher=publish,
    )
    if blocker == "source_push":
        async def blocked_cloud(_source: Source, _level: int) -> None:
            started.set()
            await release.wait()

        pushes.hook = blocked_cloud
    else:
        first_call = True

        async def blocked_set_mute(_target: bool) -> None:
            nonlocal first_call
            if first_call:
                first_call = False
                started.set()
                await release.wait()

        cam.mute_hook = blocked_set_mute

    operation = asyncio.create_task(coord.mute())
    await wait_signalled(started, "slow downstream write started", producer=operation)

    if blocker == "source_push":
        # Camilla's mute has already landed; only the source push is slow.
        assert cam.muted is True
    assert len(published) == 1
    assert published[0].muted is True

    release.set()
    assert await operation == 59
    assert len(published) == 2
    assert published[-1].muted is True


async def test_overlapping_push_writes_keep_source_persistence_and_context_aligned(
    tmp_path, pushes,
):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    persistence.save_listening_level(50)
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    applied = []
    published = []

    async def push(_source: Source, level: int) -> None:
        if level == 20:
            first_started.set()
            await release_first.wait()
        applied.append(level)

    async def publish(context):
        published.append(context)

    first = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        volume_context_publisher=publish,
    )
    second = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        volume_context_publisher=publish,
    )
    pushes.hook = push

    older = asyncio.create_task(first.set_listening_level(20))
    await wait_signalled(first_started, "older push write started", producer=older)
    newer = asyncio.create_task(second.set_listening_level(80))
    await asyncio.sleep(0)
    assert newer.done() is False
    release_first.set()
    assert await older == 20
    assert await newer == 80

    record = persistence.load()
    assert record is not None
    newest_context = max(published, key=lambda context: context.stamp_boot_ns)
    assert applied[-1] == 80
    assert record.listening_level == 80
    assert newest_context.canonical_db == pytest.approx(percent_to_db(80))


@pytest.mark.parametrize("camilla_readable", [True, False])
async def test_persisted_mute_intent_outranks_what_camilla_reports(
    tmp_path, camilla_readable,
):
    """A stale unmuted readback — or no readback at all — cannot resurrect
    audio the owner muted."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={}, db=percent_to_db(59), level=59,
    )
    persistence.save_mute_state(59, None)
    cam.muted = False
    cam.unavailable = not camilla_readable

    context = await coord.effective_volume_context()

    assert context.muted is True
    if camilla_readable:
        assert context.canonical_db == pytest.approx(percent_to_db(59))


async def test_unmute_and_push_mode_nonzero_publish_unmuted_context(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, cam, persistence = _real_coord(
        tmp_path,
        active={},
        db=percent_to_db(59),
        level=59,
        volume_context_publisher=publish,
    )
    persistence.save_mute_state(59, None)
    cam.muted = True

    await coord.unmute()
    assert published[-1].muted is False

    push = VolumeCoordinator(
        camilla=_FakeCamilla(db=0.0),
        persistence=persistence,
        backend=_FakeBackend(active={"spotactive": True}),
    )
    assert (await push.effective_volume_context()).muted is False


async def test_publisher_failure_never_breaks_volume_operation(tmp_path):
    async def fail(_context):
        raise OSError("fanin unavailable")

    coord, _, _ = _coord(tmp_path, volume_context_publisher=fail)
    assert await coord.set_listening_level(47) == 47


async def test_context_snapshot_retries_after_concurrent_volume_change(tmp_path):
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(30), level=30,
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    first = True

    async def blocked_read():
        nonlocal first
        if first:
            first = False
            read_started.set()
            await release_read.wait()
        return None

    cam.read_hook = blocked_read
    snapshot = asyncio.create_task(coord.effective_volume_context())
    await wait_signalled(
        read_started, "camilla volume/mute read started", producer=snapshot,
    )
    await coord.set_listening_level(80)
    release_read.set()
    context = await snapshot

    assert context.canonical_db == pytest.approx(percent_to_db(80))
    assert context.downstream_db == pytest.approx(percent_to_db(80))


async def test_context_snapshot_stamp_is_bound_before_slow_probe(
    tmp_path, monkeypatch,
):
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(30), level=30,
    )
    stamp_bound = False

    def bind_stamp():
        nonlocal stamp_bound
        stamp_bound = True
        return 123

    async def verify_stamp_precedes_probe():
        assert stamp_bound is True
        return None

    monkeypatch.setattr(
        "jasper.volume_coordinator.volume_context_stamp_boot_ns", bind_stamp,
    )
    cam.read_hook = verify_stamp_precedes_probe

    context = await coord.effective_volume_context()

    assert context.stamp_boot_ns == 123


async def test_set_camilla_fast_spin_regression(tmp_path):
    """Fast remote spin batching 3 detents (+12% / +6 dB) with no session.

    The old dB-comparison heuristic read that as an `inferred_duck`, deferred,
    and persisted listening_level while main_volume stayed put — so every
    later twist read the inflated level and deferred again, trapping the user
    with a knob that did nothing until they spun all the way down.
    """
    coord, cam, _ = _real_coord(
        tmp_path,
        active={},
        db=-18.0,  # in sync with listening_level=64%, per the production log
        level=64,
        mark_user_change=True,
    )

    await coord.adjust_listening_level(12)

    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(76))
    assert coord.get_listening_level() == 76

    # No cascade: subsequent small twists keep tracking 1:1.
    await coord.adjust_listening_level(4)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(80))
    assert coord.get_listening_level() == 80


# ---- maybe_reconcile_camilla (self-healing backstop) --------------------
#
# The reconciler runs at 1 Hz inside VolumeObserver._tick and converges
# main_volume_db back toward percent_to_db(listening_level) when they have
# drifted, catching any other writer or transient that creates a desync.


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


async def test_a_measurement_claim_outranks_a_household_volume_set(tmp_path):
    """The household write is a CLAIM now, and it can be outranked.

    A crossover-v2 session holds the fader at the level its excitation-safety
    ledger admitted each program against. A volume twist landing inside that
    session must not move the speaker out from under the stimulus — and must
    not be thrown away either: it is what the fader lands on at release.
    """
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(40), level=40,
    )
    await coord.set_listening_level(40)
    claim = await coord.volume_owner.acquire_level(
        ClaimKind.SESSION_MEASUREMENT, -12.5,
    )
    assert cam._db == pytest.approx(-12.5)
    cam.set_calls.clear()

    await coord.set_listening_level(70)

    assert cam.set_calls == []
    assert cam._db == pytest.approx(-12.5)
    assert coord.get_listening_level() == 70

    await coord.volume_owner.release(claim)

    assert cam._db == pytest.approx(percent_to_db(70))


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
    persistence.save_mute_state(59, None)

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


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(lambda coord: coord.set_listening_level(95), id="set_up"),
        pytest.param(lambda coord: coord.set_listening_level(25), id="set_down"),
        pytest.param(lambda coord: coord.adjust_listening_level(35), id="adjust_up"),
        pytest.param(
            lambda coord: coord.adjust_listening_level(-35), id="adjust_down",
        ),
    ],
)
async def test_the_level_doors_refuse_every_write_while_measuring(tmp_path, door):
    """The voice tools reach these IN-PROCESS, never through jasper-control.

    ``jasper.tools.audio`` calls them on this coordinator whenever the box is
    not a bonded follower, so the HTTP measurement hold never sees the request.
    Direction cannot be the test: the measurement drives camilla's main_volume
    directly and never writes the persistence file, so the persisted level
    these doors would compare against says nothing about where the fader sits,
    and a "quieter" can be a large step UP on the stimulus. The refusal type is
    what ``tools.dispatch_tool`` turns into the model-visible error payload.
    """
    coord, cam, persistence = _real_coord(tmp_path, active={}, level=60)
    await coord.note_measurement_active(True)

    with pytest.raises(VolumeClaimRefused):
        await door(coord)

    assert cam.set_calls == []
    _assert_persisted(persistence, level=60)


@pytest.mark.parametrize("source", [Source.USBSINK, Source.SPOTIFY])
@pytest.mark.parametrize("observed", [0, 40])
async def test_source_observation_preserves_measurement_reference_except_emergency_mute(
    tmp_path, source, observed,
):
    coord, cam, _ = _coord(tmp_path, selected=source.value, level=60, db=-20.0)
    await coord.note_measurement_active(True)

    accepted = await coord.observe_source_volume(source, observed)

    assert accepted is (observed == 0)
    if observed == 0:
        assert cam.muted is True
    else:
        assert cam.set_calls == []
        assert coord.get_listening_level() == 60


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(lambda coord: coord.unmute(), id="unmute"),
        pytest.param(lambda coord: coord.set_muted(False), id="set_muted_false"),
        pytest.param(lambda coord: coord.toggle_mute(), id="toggle_from_muted"),
    ],
)
async def test_unmute_is_refused_while_measuring(tmp_path, door):
    """Unmute restores the household level onto the fader — a level write."""
    coord, cam, _ = _real_coord(tmp_path, active={}, level=60)
    await coord.mute()
    await coord.note_measurement_active(True)
    cam.set_calls.clear()

    with pytest.raises(VolumeClaimRefused):
        await door(coord)

    assert cam.set_calls == []
    assert coord.is_muted()


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(lambda coord: coord.mute(), id="mute"),
        pytest.param(lambda coord: coord.set_muted(True), id="set_muted_true"),
        pytest.param(lambda coord: coord.toggle_mute(), id="toggle_to_muted"),
    ],
)
async def test_mute_still_lands_while_measuring(tmp_path, door):
    """The emergency door: a human reaching for silence mid-sweep gets it."""
    coord, cam, _ = _real_coord(tmp_path, active={}, level=60)
    await coord.note_measurement_active(True)

    await door(coord)

    assert coord.is_muted()
    assert cam.mute_calls[-1] is True


async def test_a_stranded_measurement_flag_lapses_at_the_autoclear(
    tmp_path, monkeypatch, caplog,
):
    """The voice rollback path can drop note_measurement_active(False).

    Past its aggregate deadline the resume coroutine is closed unawaited and
    the safety task is already cancelled, so nothing is left to lower the flag.
    Without the lapse this refuses every level write for the life of the
    process, invisibly to /state. The bound is per WINDOW, not per process: a
    later window that strands its own flag lapses, and says so, again.
    """
    now = [0.0]
    monkeypatch.setattr(voice_measurement, "measurement_monotonic", lambda: now[0])
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=0.0, level=70, mark_user_change=True,
    )
    await coord.note_measurement_active(True)

    now[0] = MEASUREMENT_AUTOCLEAR_SEC - 1.0
    with pytest.raises(VolumeClaimRefused):
        await coord.set_listening_level(20)

    now[0] = MEASUREMENT_AUTOCLEAR_SEC
    with caplog.at_level(logging.WARNING, logger="jasper"):
        assert await coord.set_listening_level(20) == 20
        assert await coord.adjust_listening_level(-5) == 15
        # event_fields asserts exactly one: once per lapse, not once per write.
        assert event_fields(caplog, "volume.measurement_flag_expired")

        # A second window strands its flag too. The once-per-lapse latch is
        # reset by note_measurement_active(True), so this one is announced.
        caplog.clear()
        await coord.note_measurement_active(True)
        with pytest.raises(VolumeClaimRefused):
            await coord.set_listening_level(30)
        now[0] += MEASUREMENT_AUTOCLEAR_SEC
        assert await coord.set_listening_level(30) == 30
        assert event_fields(caplog, "volume.measurement_flag_expired")


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


# ---------- duck restore target --------------------------------------------


@pytest.mark.parametrize(
    ("active", "level", "persisted_db", "expected"),
    [
        pytest.param({}, 70, 0.0, percent_to_db(70), id="idle"),
        pytest.param({"aplactive": True}, 40, 0.0, percent_to_db(40), id="airplay"),
        pytest.param({"spotactive": True}, 70, 0.0, 0.0, id="push_mode"),
        pytest.param({"spotactive": True}, 0, 0.0, percent_to_db(0), id="push_mode_at_zero"),
        pytest.param({"spotactive": True}, 70, -1.0, 0.0, id="guard_boundary"),
        pytest.param({"spotactive": True}, 70, -1.01, -1.01, id="guard_active"),
        pytest.param({"spotactive": True}, 0, -25.0, percent_to_db(0), id="mute_before_guard"),
    ],
)
async def test_the_duck_restore_target_follows_the_active_carrier(
    tmp_path, active, level, persisted_db, expected,
):
    coord, cam, persistence = _real_coord(tmp_path, active=active, level=level)
    persistence.save_now(persisted_db)
    cam.unavailable = True

    assert await coord.get_camilla_target_db() == pytest.approx(expected)
    assert (await coord.effective_volume_context()).downstream_db == pytest.approx(expected)


async def test_get_camilla_target_db_uses_effective_temporary_mute(tmp_path):
    """Every carrier path interprets remembered-level + mute through VolumeState."""
    coord, _, persistence = _real_coord(
        tmp_path, active={}, db=percent_to_db(0), level=70,
    )
    persistence.save_mute_state(70, None)

    assert await coord.get_camilla_target_db() == pytest.approx(percent_to_db(0))


async def test_get_camilla_target_db_refreshes_from_disk(tmp_path):
    """Cross-process staleness guard for the duck-restore path.

    The control daemon writes listening_level to disk on every twist;
    voice-daemon's in-memory `_level` only auto-refreshes on its own
    set/adjust/mute/transition calls. Without the refresh, the duck release
    at the end of a wake writes camilla to the stale dB — observed as a 56 dB
    jump at duck-off after a remote spin landed between voice operations.
    """
    coord, _, persistence = _coord(
        tmp_path, active={"aplactive": True}, level=38,
    )
    persistence.save_listening_level(80)  # the control daemon, another process

    assert await coord.get_camilla_target_db() == pytest.approx(percent_to_db(80))
    assert coord.get_listening_level() == 80


async def test_env_target_and_registered_provider_read_current_persisted_intent(
    tmp_path, monkeypatch,
):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    monkeypatch.setattr(volume_process, "volume_state_path", lambda: persistence.path)
    monkeypatch.setattr(camilla, "primary_controller", lambda: _FakeCamilla())
    monkeypatch.setattr(renderer, "RendererClient", lambda **_: _FakeBackend(active={"aplactive": True}))
    volume_process.install_env_canonical_target_provider()
    for level in (35, 71):
        persistence.save_listening_level(level)
        assert await volume_process.env_canonical_target_db() == pytest.approx(percent_to_db(level))
        assert await camilla._canonical_target_db_provider() == pytest.approx(percent_to_db(level))


@pytest.mark.parametrize("builder", ["jasper-control", "sound-settings", "canonical-target"])
async def test_short_lived_coordinators_take_the_registered_owner(
    builder, tmp_path, monkeypatch,
):
    """One owner per process (`volume_owner.install_volume_owner`): once the
    process registers, each short-lived builder's coordinator arbitrates
    through that owner; before it registers, the coordinator builds its own."""
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "speaker_volume.json"))
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setattr(
        camilla, "primary_controller", lambda: _FakeCamilla(db=percent_to_db(50)),
    )
    monkeypatch.setattr(renderer, "RendererClient", lambda **_: _FakeBackend())
    built: list[VolumeCoordinator] = []

    def recording(**kwargs) -> VolumeCoordinator:
        built.append(VolumeCoordinator(**kwargs))
        return built[-1]

    monkeypatch.setattr("jasper.volume_coordinator.VolumeCoordinator", recording)

    async def no_op(_coord) -> None:
        return None

    build = {
        "jasper-control": lambda: with_coordinator(
            no_op, camilla_host="127.0.0.1", camilla_port=1234,
        ),
        "sound-settings": lambda: (
            sound_profile_apply._reconcile_volume_curve_after_settings(
                camilla_factory=camilla.primary_controller,
            )
        ),
        "canonical-target": volume_process.env_canonical_target_db,
    }[builder]

    await build()
    volume_process.install_env_canonical_target_provider()
    await build()

    own, shared = (coord.volume_owner for coord in built)
    assert shared is volume_owner()
    assert isinstance(own, VolumeOwner) and own is not shared


async def test_transition_refreshes_from_disk(tmp_path, pushes):
    """The same cross-process staleness guard on the transition path, which
    is observer-triggered and so never refreshes as a side effect."""
    backend = _FakeBackend(active={"aplactive": True})
    coord, _, persistence = _coord(tmp_path, backend=backend, level=50)
    persistence.save_listening_level(80)  # the control daemon, another process
    backend._active = {"spotactive": True}

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert pushes.spotify == [80]
    assert coord.get_listening_level() == 80


async def test_transition_uses_effective_level_while_temporarily_muted(
    tmp_path, pushes,
):
    coord, _, persistence = _coord(
        tmp_path,
        active={"spotactive": True},
        db=percent_to_db(0),
        level=80,
    )
    persistence.save_mute_state(80, None)

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert pushes.spotify == [0]
    assert coord.get_volume_state().restore_percent == 80


# ---------- camilla restart-blip survival ---------------------------------


async def test_volume_coordinator_proceeds_when_camilla_unreachable(tmp_path):
    """A remote twist arriving during a 2 s camilla restart blip must not
    throw: the user's intent is preserved end-to-end so the next operation
    lands at the right level once camilla is back."""
    coord, cam, persistence = _real_coord(tmp_path, active={})
    cam.unavailable = True

    assert await coord.set_listening_level(70) == 70

    assert coord.get_listening_level() == 70
    _assert_persisted(persistence, level=70)
    # best_effort=True silently dropped the write while the fake was down.
    assert cam.set_calls == []

    cam.unavailable = False
    await coord.set_listening_level(40)
    assert cam.set_calls and cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    assert coord.get_listening_level() == 40


# ---------- USB sink (camilla-master, host-slider observed inbound) --------


async def test_set_volume_usbsink_active_routes_to_camilla(tmp_path, pushes):
    """USB sink behaves like AirPlay for outbound: remote/voice writes
    land on CamillaDSP. The gadget mixer is NOT written back to (the
    host's slider is observed-only)."""
    coord, cam, _ = _coord(tmp_path, active={"usbsinkactive": True})
    await coord.set_listening_level(60)
    assert cam.set_calls == [pytest.approx(percent_to_db(60))]
    assert pushes.calls == []


async def test_observe_usbsink_updates_listening_level_when_active(tmp_path):
    """Host slider moves while USB is the active source — listening
    level follows and CamillaDSP, the USB carrier, is updated."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={"usbsinkactive": True}, level=80,
    )

    accepted = await coord.observe_source_volume(Source.USBSINK, 45)

    assert accepted is True
    assert coord.get_listening_level() == 45
    _assert_persisted(persistence, level=45)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(45))
    assert cam.mute_calls[-1] is False


async def test_observe_usbsink_initial_snapshot_cannot_clear_remote_mute(tmp_path):
    """Bridge activation/restart state yields to an already-latched mute."""
    coord, cam, persistence = _real_coord(
        tmp_path,
        active={"usbsinkactive": True},
        db=percent_to_db(60),
        level=60,
    )
    persistence.save_mute_state(60, "remote-mute")
    cam.muted = True

    accepted = await coord.observe_source_volume(Source.USBSINK, 60, initial=True)

    assert accepted is False
    state = coord.get_volume_state()
    assert state.effective_percent == 0
    assert state.restore_percent == 60

    # A later changed host value is explicit intent and may end the mute.
    assert await coord.observe_source_volume(Source.USBSINK, 65) is True
    assert coord.get_volume_state().effective_percent == 65


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ("airplay", Source.AIRPLAY),
        # Mux holds its last committed answer across a handoff, so "idle" is
        # true idle and takes the attenuating camilla-master carrier. Only a
        # fan-in test-lease label, which is not a source at all, falls through
        # to the raw probes.
        ("idle", Source.IDLE),
        ("correction", Source.SPOTIFY),
        (None, Source.SPOTIFY),
    ],
)
async def test_active_source_honours_mux_over_the_raw_probes(
    tmp_path, selected, expected,
):
    coord, _, _ = _real_coord(
        tmp_path, active={"spotactive": True}, selected=selected,
    )

    assert await coord._active_source() is expected


@pytest.mark.parametrize(
    ("active", "selected"),
    [
        pytest.param({"usbsinkactive": True}, None, id="raw_activity_probe"),
        # Mux selection is the speaker gate, so USB host volume follows it
        # even while the raw usbsink RMS activity probe is quiet.
        pytest.param({}, "usbsink", id="mux_selection_probe_idle"),
    ],
)
async def test_observe_usbsink_unmute_restores_camilla_carrier(
    tmp_path, active, selected,
):
    """A host unmute restores the slider value; because USB is
    camilla-master, that observation must raise Camilla back — volume
    first, then main_mute=false."""
    coord, cam, persistence = _real_coord(
        tmp_path, active=active, selected=selected, db=-50.0, level=0,
    )
    persistence.save_now(-50.0)
    cam.muted = True

    await coord.observe_source_volume(Source.USBSINK, 75)

    assert coord.get_listening_level() == 75
    _assert_persisted(persistence, level=75, db=round(percent_to_db(75), 2))
    assert cam.events[-2:] == [
        ("volume", pytest.approx(percent_to_db(75))),
        ("mute", False),
    ]


@pytest.mark.parametrize(
    "db",
    [
        pytest.param(-20.0, id="fader_drifted"),
        pytest.param(percent_to_db(0), id="fader_at_floor_but_unmuted"),
    ],
)
async def test_observe_usbsink_same_level_repairs_camilla_drift(tmp_path, db):
    """An observation at the level JTS already remembers still reconverges
    Camilla instead of returning early. At 0% convergence means BOTH the
    floor dB and main_mute, so whichever half drifted gets repaired."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={"usbsinkactive": True}, db=db, level=0,
    )
    persistence.save_now(db)

    await coord.observe_source_volume(Source.USBSINK, 0)

    assert coord.get_listening_level() == 0
    assert cam._db == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True
    _assert_persisted(persistence, db=percent_to_db(0))


async def test_equal_usbsink_observation_publishes_repaired_downstream(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, _, persistence = _real_coord(
        tmp_path,
        active={"usbsinkactive": True},
        db=-20.0,
        level=50,
        volume_context_publisher=publish,
    )
    persistence.save_now(-20.0)

    await coord.observe_source_volume(Source.USBSINK, 50)

    assert len(published) == 1
    assert published[0].downstream_db == pytest.approx(percent_to_db(50))


async def test_observe_usbsink_clamps_out_of_range(tmp_path):
    """Defensive: percent outside [0, 100] gets clamped before storage."""
    coord, _, _ = _real_coord(
        tmp_path, active={"usbsinkactive": True}, level=50,
    )

    await coord.observe_source_volume(Source.USBSINK, 150)
    assert coord.get_listening_level() == 100

    await coord.observe_source_volume(Source.USBSINK, -20)
    assert coord.get_listening_level() == 0


# ---------- bluealsa transport-path probe goes through shared backoff -------
#
# volume_push_sources._bluez_alsa_active_transport_path runs in
# jasper-control on every BT volume set from the remote/web. It must reuse
# jasper.bluealsa_probe so a D-Bus permission denial backs off process-wide
# instead of hammering the system bus once per volume set. These tests fail
# if the helper reverts to its own raw `bluealsa-cli list-pcms` subprocess.


def _fake_pcm_list(monkeypatch, stdout: bytes, returncode: int = 0) -> dict[str, int]:
    """Stand in for `bluealsa-cli list-pcms`, counting spawns."""
    calls = {"n": 0}

    class _Proc:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self):
            return stdout, b"permission denied" if returncode else b""

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    return calls


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param(
            b"/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source PCM ...\n",
            "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source",
            id="one_transport",
        ),
        pytest.param(b"", None, id="no_transport"),
    ],
)
async def test_bluez_transport_path_parses_the_pcm_list(
    monkeypatch, stdout, expected,
):
    _fake_pcm_list(monkeypatch, stdout)

    assert await vps_mod._bluez_alsa_active_transport_path() == expected


@pytest.mark.parametrize(
    "tripped_by_another_consumer",
    [pytest.param(False, id="own_failure"), pytest.param(True, id="shared_backoff")],
)
async def test_bluez_transport_path_honours_the_shared_probe_backoff(
    monkeypatch, tripped_by_another_consumer,
):
    """The backoff is process-wide: a D-Bus rejection recorded by ANY
    bluealsa_probe consumer short-circuits this helper's next probe without
    spawning. Pins the 'shared module', not a per-caller, contract."""
    calls = _fake_pcm_list(monkeypatch, b"", returncode=1)
    if tripped_by_another_consumer:
        bluealsa_probe.note_probe_failure("rc=1", vps_mod.logger)
        expected_spawns = 0
    else:
        assert await vps_mod._bluez_alsa_active_transport_path() is None
        expected_spawns = 1

    assert await vps_mod._bluez_alsa_active_transport_path() is None
    assert calls["n"] == expected_spawns


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


# ---------- graph-swap duck release -----------------------------------------


async def test_duck_release_never_lands_above_a_volume_change_made_inside_it(
    tmp_path, monkeypatch,
):
    """A user volume change inside the bracket is what rules out a bare
    relative release: giving back 40 dB on top of the level the coordinator
    just wrote lands tens of dB above what the user asked for. The canonical
    ceiling is the half that prevents it.
    """
    monkeypatch.setattr(camilla, "MAIN_VOLUME_RAMP_SETTLE_S", 0.0)
    coord, cam, client = _owned_coord(tmp_path, db=percent_to_db(70))
    await coord.set_listening_level(70)
    monkeypatch.setattr(
        camilla,
        "_canonical_target_db_provider",
        coord.get_camilla_target_db,
    )

    bracket = cam._graph_mutation("test.swap")
    await bracket.__aenter__()
    await coord.set_listening_level(30)
    lowered_db = percent_to_db(30)
    await bracket.__aexit__(None, None, None)

    assert client.db == pytest.approx(lowered_db), (
        f"the release landed at {client.db:.1f} dB, not the {lowered_db:.1f} dB "
        "the user asked for during the swap"
    )
