# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Handoff transaction tests against its concrete carrier I/O boundary.

Tests below `_warnings` also drive `VolumeHandoff` through the coordinator
(`VolumeCoordinator.set_listening_level`/`apply_active_source_transition`),
for guard and transition behavior a synthetic carrier can't reach.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from tests._log_events import event_field_maps, parse_event
from tests.volume_coordinator_fixtures import (
    _assert_persisted,
    _coord,
    _FakeBackend,
    _real_coord,
    pushes as pushes,
)

from jasper import renderer
from jasper.playback_state.music_sources import Source
from jasper.volume_curve import percent_to_db
from jasper.volume_handoff import VolumeHandoff
from jasper.service_state.volume_persistence import VolumePersistence
from jasper.volume_state import VolumeState


@pytest.fixture
def carrier(tmp_path):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    cam = SimpleNamespace(db=0.0, muted=False, set_calls=[], mute_calls=[])

    async def write_guard(db, *, context, persist):
        cam.db = db
        cam.set_calls.append(db)
        if persist:
            persistence.save_now(db)
        return True

    async def write_level(level):
        cam.muted = level == 0
        cam.mute_calls.append(cam.muted)
        return await write_guard(percent_to_db(level), context="set_camilla", persist=True)

    owner = VolumeHandoff(
        effective_level=lambda: VolumeState.from_record(persistence.load()).effective_percent,
        read_carrier=AsyncMock(side_effect=lambda: (cam.db, cam.muted)),
        persisted_carrier=lambda: persistence.load().main_volume_db,
        write_guard=AsyncMock(side_effect=write_guard),
        push_source=AsyncMock(return_value=True),
        stamp_outbound=lambda source: None,
        write_level=AsyncMock(side_effect=write_level),
        voice_session_active=lambda: False,
        active_source=AsyncMock(return_value=Source.IDLE),
        mux_last_handoff=AsyncMock(return_value=None),
        refresh=lambda: None,
        mutation=nullcontext,
        publish=AsyncMock(),
        handoff_settle_sec=0.0,
        push_settle_sec=0.0,
    )
    return owner, cam, persistence


async def test_handoff_spotify_to_airplay_guards_camilla_before_gate(carrier):
    """Push-mode → camilla-master handoff lowers Camilla before mux
    exposes the AirPlay lane."""
    owner, cam, persistence = carrier
    persistence.save_listening_level(50)

    handoff = await owner.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert handoff.guard_db == pytest.approx(percent_to_db(50))
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(50))


async def test_handoff_finalize_honors_mute_landed_after_prepare(carrier):
    """A remote mute between prepare and finalize must keep the new lane silent."""
    owner, cam, persistence = carrier
    persistence.save_listening_level(60)
    handoff = await owner.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )
    assert handoff.ok

    # jasper-control handling the remote while mux owns this coordinator's
    # source-transition sequence.
    persistence.save_mute_state(60, "remote-mute")

    assert await owner.finalize_source_handoff(handoff) is True
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(0))
    assert cam.mute_calls[-1] is True


async def test_handoff_catches_lower_level_during_guard_settle(carrier):
    """If the user lowers volume while Camilla is settling, handoff
    catches down before mux opens the target lane."""
    owner, cam, persistence = carrier
    persistence.save_listening_level(50)
    original_set_camilla_db = owner._set_camilla_db
    lowered = False

    async def set_and_lower_once(db, *, context, persist):
        nonlocal lowered
        ok = await original_set_camilla_db(db, context=context, persist=persist)
        if context == "source_handoff_guard" and not lowered:
            persistence.save_listening_level(20, mark_user_change=True)
            lowered = True
        return ok

    owner._set_camilla_db = set_and_lower_once

    handoff = await owner.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )

    assert handoff.ok
    assert handoff.level == 20
    assert handoff.guard_db == pytest.approx(percent_to_db(20))
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(20))


async def test_handoff_airplay_to_spotify_pushes_before_finalize(carrier):
    """Camilla-master → push-mode handoff pushes the source volume
    before mux opens the source, then finalize pins Camilla to 0 dB."""
    owner, cam, persistence = carrier
    persistence.save_listening_level(60)
    cam.db = percent_to_db(60)
    persistence.save_now(cam.db)

    handoff = await owner.prepare_source_handoff(
        Source.AIRPLAY, Source.SPOTIFY, reason="manual",
    )

    assert handoff.ok
    assert handoff.push_ok is True
    assert owner._push_source.await_args_list == [call(Source.SPOTIFY, 60)]
    await owner.finalize_source_handoff(handoff)
    assert cam.set_calls[-1] == pytest.approx(0.0)


async def test_handoff_push_failure_keeps_camilla_guarded(carrier):
    """If a push-mode source cannot accept volume, handoff degrades
    safe by keeping downstream Camilla at the canonical guard."""
    owner, cam, persistence = carrier
    persistence.save_listening_level(40)
    cam.db = percent_to_db(40)
    persistence.save_now(cam.db)

    owner._push_source.return_value = False
    handoff = await owner.prepare_source_handoff(
        Source.AIRPLAY, Source.SPOTIFY, reason="manual",
    )

    assert handoff.result == "degraded_safe"
    assert handoff.push_ok is False
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))
    await owner.finalize_source_handoff(handoff)
    assert cam.set_calls[-1] == pytest.approx(percent_to_db(40))


@pytest.mark.parametrize("source", [Source.SPOTIFY, Source.BLUETOOTH])
@pytest.mark.parametrize(
    "level,live_db,saved_db,muted,include_live,write_ok,expected,writes",
    [
        (0, 0.0, 0.0, False, False, True, (True, True), [percent_to_db(0)]),
        (50, 0.0, 0.0, False, True, True, (True, False), []),
        (50, -20.0, 0.0, False, False, True, (True, False), []),
        (50, -20.0, 0.0, False, True, True, (True, True), [0.0]),
        (50, 0.0, -20.0, False, False, True, (True, True), [0.0]),
        (50, 0.0, 0.0, True, False, True, (True, True), [0.0]),
        (50, -20.0, -20.0, False, True, False, (False, False), [0.0]),
    ],
)
async def test_push_confirmation_reports_only_completed_carrier_changes(
    carrier, source, level, live_db, saved_db, muted, include_live,
    write_ok, expected, writes,
):
    owner, cam, persistence = carrier
    cam.db, cam.muted = live_db, muted
    persistence.save_now(saved_db)
    owner._set_camilla_db.side_effect = None
    owner._set_camilla_db.return_value = write_ok

    assert await owner.confirm_push_mode_carrier_with_mutation(
        source, level, context="test_confirm", include_live_guard=include_live,
    ) == expected
    assert [call.args[0] for call in owner._set_camilla_db.await_args_list] == writes
    owner._set_camilla.assert_not_awaited()


@pytest.mark.parametrize("push_ok", [True, False])
async def test_finalize_catches_a_lower_level_before_repush(carrier, push_ok):
    owner, cam, persistence = carrier
    persistence.save_listening_level(60)
    handoff = await owner.prepare_source_handoff(
        Source.AIRPLAY, Source.SPOTIFY, reason="manual",
    )
    persistence.save_listening_level(20)

    async def push(source, level):
        assert cam.db == pytest.approx(percent_to_db(20))
        assert (source, level) == (Source.SPOTIFY, 20)
        return push_ok

    owner._push_source.side_effect = push
    assert await owner.finalize_source_handoff(handoff)
    assert cam.db == pytest.approx(0.0 if push_ok else percent_to_db(20))
    owner._set_camilla.assert_not_awaited()


@pytest.mark.parametrize("prev", [Source.SPOTIFY, Source.AIRPLAY])
async def test_abort_restores_the_previous_carrier_from_fresh_intent(carrier, prev):
    owner, cam, persistence = carrier
    persistence.save_listening_level(60)
    handoff = await owner.prepare_source_handoff(prev, Source.USBSINK, reason="manual")
    persistence.save_listening_level(20)

    assert await owner.abort_source_handoff(handoff)
    if prev == Source.SPOTIFY:
        assert cam.db == pytest.approx(0.0)
        owner._set_camilla.assert_not_awaited()
    else:
        owner._set_camilla.assert_awaited_once_with(20)
        assert cam.db == pytest.approx(percent_to_db(20))


async def test_guard_settle_bounds_continuous_catchdown(carrier, monkeypatch):
    owner, cam, persistence = carrier
    persistence.save_listening_level(90)
    waits = []

    async def sleep(delay):
        waits.append(delay)
        persistence.save_listening_level(persistence.load().listening_level - 10)

    monkeypatch.setattr("jasper.volume_handoff.asyncio.sleep", sleep)
    owner._handoff_settle_sec = 0.45
    handoff = await owner.prepare_source_handoff(
        Source.SPOTIFY, Source.AIRPLAY, reason="manual",
    )
    assert not handoff.ok
    assert handoff.detail == "camilla_guard_catchdown_failed"
    assert handoff.level == 50
    assert handoff.settled_ms == 1800
    assert waits == [0.45] * 4
    assert cam.set_calls == pytest.approx([percent_to_db(n) for n in (90, 80, 70, 60)])


@pytest.mark.parametrize("guard_ok", [True, False])
async def test_push_failure_returns_guard_result(carrier, guard_ok):
    owner, _, persistence = carrier
    persistence.save_now(-7.5)
    owner._set_camilla_db.side_effect = None
    owner._set_camilla_db.return_value = guard_ok

    assert await owner.guard_camilla_after_push_failure(
        25, context="test_push",
        warning_prefix="push failed", guarded_warning_suffix="; guarded",
    ) is guard_ok
    owner._set_camilla_db.assert_awaited_once_with(
        percent_to_db(25), context="test_push", persist=True,
    )


# ---------- push_or_guard: dispatch-time guard failures ---------------------


def _warnings(caplog) -> list[str]:
    """Operator-facing prose warnings, whichever volume module words them."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("jasper")
        and record.levelno >= logging.WARNING
        and parse_event(record.getMessage()) is None
    ]


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


# ---------- apply_transition -------------------------------------------------


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

    assert await coord.active_source() is Source.SPOTIFY
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
    ("seen", "skipped", "prev", "current", "handoff", "applies"),
    [
        pytest.param(7, None, Source.AIRPLAY, Source.SPOTIFY, {"id": 8},
                     False, id="mux_handed_it_off"),
        pytest.param(7, None, Source.SPOTIFY, Source.IDLE, {"id": 7},
                     True, id="to_idle_without_a_handoff"),
        pytest.param(7, None, Source.IDLE, Source.SPOTIFY, {"id": 7},
                     True, id="back_to_spotify_without_a_handoff"),
        pytest.param(7, ("deferred", 8), Source.IDLE, Source.SPOTIFY,
                     {"id": 8}, True, id="its_handoff_came_while_deferring"),
        pytest.param(7, ("dropped", 8), Source.IDLE, Source.SPOTIFY,
                     {"id": 8}, True, id="its_handoff_came_while_dropping"),
        pytest.param(7, None, Source.SPOTIFY, Source.IDLE,
                     {"id": 8, "to": "airplay"},
                     True, id="its_newest_handoff_went_elsewhere"),
        pytest.param(7, None, Source.AIRPLAY, Source.SPOTIFY,
                     {"id": 8, "result": "degraded_safe"},
                     True, id="a_degraded_handoff_is_retried"),
        pytest.param(7, None, Source.AIRPLAY, Source.SPOTIFY,
                     {"id": 8, "level": 40},
                     True, id="the_level_moved_after_the_handoff"),
        pytest.param(None, None, Source.AIRPLAY, Source.SPOTIFY, {"id": 8},
                     True, id="first_transition_after_start"),
        pytest.param(7, None, Source.AIRPLAY, Source.SPOTIFY, None,
                     True, id="mux_status_unreadable"),
    ],
)
async def test_the_observer_carries_only_transitions_mux_did_not_hand_off(
    tmp_path, monkeypatch, pushes, seen, skipped, prev, current, handoff,
    applies,
):
    """Mux performs each source handoff (ADR-0150). The observer sees the
    same switch about 1 s later and must not push or write it again. Every
    other switch it applies as before: one mux made with no newer handoff,
    one whose handoff did not deliver it, or one mux cannot say about.
    Either way, the renderer's first report after the switch may still lag
    the push, and it must not become the listening level."""
    status: dict | None = None

    def mux(active: Source, **handoff) -> dict:
        return {"active_source": active.value, "last_handoff": {
            "to": "spotify", "result": "ok", "level": 50, **handoff}}

    async def mux_status(_command, *, timeout):
        if status is None:
            raise OSError("mux down")
        return status

    monkeypatch.setattr(renderer, "mux_socket_command", mux_status)
    backend = renderer.RendererClient(librespot_state_path=str(tmp_path / "none"))
    backend.active_renderers = AsyncMock(return_value={"spotactive": True})
    # The fader starts where the previous source's carrier holds it.
    fader_db = 0.0 if prev is Source.SPOTIFY else percent_to_db(50)
    coord, cam, persistence = _coord(
        tmp_path, backend=backend, db=fader_db, level=50,
    )
    persistence.save_now(fader_db)
    if seen is not None:
        # Any earlier transition reads mux's handoff; idle → airplay writes nothing.
        status = mux(Source.AIRPLAY, id=seen)
        await coord.apply_active_source_transition(Source.IDLE, Source.AIRPLAY)
    if skipped is not None:
        # Mux hands off to Spotify while a voice session defers the observer's
        # switch, or while it queues for a lane mux has left (it drops it).
        how, handoff_id = skipped
        status = mux(Source.SPOTIFY, id=handoff_id)
        coord.note_voice_session(how == "deferred")
        await coord.apply_active_source_transition(
            Source.AIRPLAY,
            Source.SPOTIFY if how == "deferred" else Source.BLUETOOTH,
        )
        coord.note_voice_session(False)
    status = None if handoff is None else mux(current, **handoff)
    pushes_before, writes_before = len(pushes.spotify), len(cam.set_calls)

    await coord.apply_active_source_transition(prev, current)

    pushed, written = {
        Source.SPOTIFY: ([50], [0.0]),
        Source.IDLE: ([], [percent_to_db(50)]),
    }[current] if applies else ([], [])
    assert pushes.spotify[pushes_before:] == pushed
    assert cam.set_calls[writes_before:] == pytest.approx(written)
    assert not await coord.observe_source_volume(Source.SPOTIFY, 60, initial=True)
    assert coord.get_volume_state().listening_level == 50


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
    persistence.save_mute_state(80, "remote-mute")

    await coord.apply_active_source_transition(Source.AIRPLAY, Source.SPOTIFY)

    assert pushes.spotify == [0]
    assert coord.get_volume_state().restore_percent == 80
