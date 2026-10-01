# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.audio_control.volume_coordinator.

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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._async_wait import wait_signalled
from tests._log_events import event_field_maps, event_fields
from tests.volume_coordinator_fixtures import (
    _assert_persisted,
    _coord,
    _FakeBackend,
    _FakeCamilla,
    _owned_coord,
    _real_coord,
    _use_real_pushes,
    measurement_hold_served as measurement_hold_served,
    pushes as pushes,
)

from jasper.device_probe import bluealsa_probe
from jasper.audio_control import camilla, renderer, volume_process
from jasper.service_state import spotify_router as spotify_router_mod
from jasper.audio_control import volume_push_sources as vps_mod
from jasper.service_state.accounts import Account
from jasper.control.volume_ops import with_coordinator
from jasper.platform.control_client import ControlError
from jasper.service_state.spotify_router import AccountClient, Router
from jasper.playback_state.music_sources import Source
from jasper.voice import measurement_hold as voice_measurement
from jasper.voice.measurement_hold import MEASUREMENT_AUTOCLEAR_SEC
from jasper.audio_control.volume_coordinator import VolumeCoordinator
from jasper.audio_control.volume_echo import ECHO_WINDOW_SEC
from jasper.audio_control.volume_scales import (
    BT_VOLUME_MAX,
    bt_volume_to_listening_level,
    listening_level_to_bt_volume,
    listening_level_to_spotify_percent,
    spotify_percent_to_listening_level,
)
from jasper.audio_resources.volume_owner import (
    ClaimKind,
    VolumeClaimRefused,
    VolumeOwner,
    volume_owner,
)
from jasper.service_state.volume_persistence import FIRST_BOOT_DEFAULT_PCT, VolumePersistence
from jasper.audio_routes.volume_curve import percent_to_db
from jasper.audio_control.volume_state import VolumeState
from jasper.web import sound_profile_apply


@pytest.fixture(autouse=True)
def _reset_bluealsa_probe_state():
    bluealsa_probe.note_probe_success()
    yield
    bluealsa_probe.note_probe_success()


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


async def test_set_volume_bluetooth_active_routes_to_bt(tmp_path, pushes):
    coord, cam, _ = _coord(tmp_path, active={"btactive": True}, db=-25.0)
    await coord.set_listening_level(60)
    assert pushes.bluetooth == [60]
    assert cam.set_calls == []  # BT is push-mode; camilla untouched


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
    When mux cannot answer, the raw probes' order is airplay > spotify >
    bluetooth > usbsink: a phone-controlled AirPlay session is not silently
    overridden by a Mac plugged into the USB port."""
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


@pytest.mark.parametrize("hold_state", ["free", "held", "unreadable"])
@pytest.mark.parametrize(
    ("active", "source"),
    [({}, Source.IDLE), ({"spotactive": True}, Source.SPOTIFY)],
    ids=["camilla_master", "push"],
)
async def test_a_boot_restore_writes_no_fader_while_a_measurement_holds_it(
    tmp_path, monkeypatch, caplog, measurement_hold_served,
    hold_state, active, source,
):
    """The boot restore writes no fader while a run may own it, in either
    carrier mode. The hold is read the way the reconciler reads it before a
    raise, so an unreadable hold waits too (ADR-0368)."""
    caplog.set_level(logging.INFO, logger="jasper")
    coord, cam, persistence = _coord(
        tmp_path, active=active, db=-40.0, level=70, mark_user_change=True,
    )
    if hold_state != "free":
        measurement_hold_served.acquire("crossover_v2")
    if hold_state == "unreadable":
        def refused(**_kwargs: object) -> dict:
            raise ControlError("connection refused")

        monkeypatch.setattr("jasper.platform.control_client.get_measurement", refused)

    target, _reason = await coord.initialize()

    deferred = hold_state != "free"
    assert (cam.events == []) is deferred
    assert [
        (fields["reason"], fields["hold"], fields["source"])
        for fields in event_field_maps(caplog, "volume.boot_restore_deferred")
    ] == ([("measurement_hold", hold_state, source.value)] if deferred else [])
    _assert_persisted(persistence, level=target)


async def test_user_change_bumps_last_used_at(tmp_path):
    coord, _, persistence = _coord(tmp_path, active={})
    await coord.set_listening_level(45)
    rec = persistence.load()
    assert rec is not None
    assert rec.last_used_at is not None
    age = (datetime.now(timezone.utc) - rec.last_used_at).total_seconds()
    assert 0 <= age < 5


@pytest.mark.parametrize(
    ("fields", "state"),
    [
        pytest.param(None, VolumeState(FIRST_BOOT_DEFAULT_PCT), id="no_record"),
        pytest.param({}, VolumeState(FIRST_BOOT_DEFAULT_PCT), id="level_missing"),
        pytest.param({"listening_level": 30}, VolumeState(30), id="level_present"),
        pytest.param(
            {"listening_level": 30, "pre_mute_level": 30, "mute_token": "m"},
            VolumeState(30, 30, "m"),
            id="latch_present",
        ),
    ],
)
def test_a_record_projects_one_state_for_every_reader(tmp_path, fields, state):
    """GET /volume and /state read through `from_record`; the coordinator
    runs the level they report."""
    coord, _, persistence = _coord(tmp_path, active={})
    if fields is not None:
        persistence.path.write_text(json.dumps({"main_volume_db": 0.0, **fields}))

    assert VolumeState.from_record(persistence.load()) == state
    assert coord.get_volume_state() == state


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


# ---------- measurement-active: claims and the dispatch-level doors --------


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


# ---------- duck restore target --------------------------------------------


@pytest.mark.parametrize(
    ("active", "level", "persisted_db", "expected"),
    [
        pytest.param({}, 70, 0.0, percent_to_db(70), id="idle"),
        pytest.param({"aplactive": True}, 40, 0.0, percent_to_db(40), id="airplay"),
        pytest.param({"spotactive": True}, 70, 0.0, 0.0, id="push_mode"),
        pytest.param({"spotactive": True}, 0, 0.0, percent_to_db(0), id="push_mode_at_zero"),
        pytest.param({"spotactive": True}, 70, -1.01, -1.01, id="guard_active"),
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
    persistence.save_mute_state(70, "remote-mute")

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

    monkeypatch.setattr("jasper.audio_control.volume_coordinator.VolumeCoordinator", recording)

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
    ("active", "selected", "expected"),
    [
        ({"spotactive": True}, "airplay", Source.AIRPLAY),
        # Mux decides (ADR-0150), even against the probes' own first pick.
        ({"aplactive": True, "spotactive": True}, "spotify", Source.SPOTIFY),
        # Mux holds its last committed answer across a handoff, so "idle" is
        # true idle and takes the attenuating camilla-master carrier. Only a
        # fan-in test-lease label, which is not a source at all, falls through
        # to the raw probes.
        ({"spotactive": True}, "idle", Source.IDLE),
        ({"spotactive": True}, "correction", Source.SPOTIFY),
        ({"spotactive": True}, None, Source.SPOTIFY),
        # Neither mux nor the probes answer: idle, the attenuating carrier.
        (RuntimeError("probe failed"), None, Source.IDLE),
    ],
)
async def test_active_source_honours_mux_over_the_raw_probes(
    tmp_path, active, selected, expected,
):
    coord, _, _ = _real_coord(tmp_path, active=active, selected=selected)

    assert await coord.active_source() is expected


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
# jasper.device_probe.bluealsa_probe so a D-Bus permission denial backs off process-wide
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
