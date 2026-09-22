# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Handoff transaction tests against its concrete carrier I/O boundary."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from jasper.music_sources import Source
from jasper.volume_curve import percent_to_db
from jasper.volume_diagnostics import read_diagnostics
from jasper.volume_handoff import VolumeHandoff
from jasper.volume_persistence import VolumePersistence
from jasper.volume_state import VolumeState


@pytest.fixture
def carrier(tmp_path, monkeypatch):
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(tmp_path / "volume_policy.json"))
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
        camilla_locked=AsyncMock(return_value=False),
        write_level=AsyncMock(side_effect=write_level),
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
    persistence.save_mute_state(60, None)

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
    assert owner._set_push_source_for_handoff.await_args_list == [call(Source.SPOTIFY, 60)]
    await owner.finalize_source_handoff(handoff)
    assert cam.set_calls[-1] == pytest.approx(0.0)


async def test_handoff_push_failure_keeps_camilla_guarded(carrier):
    """If a push-mode source cannot accept volume, handoff degrades
    safe by keeping downstream Camilla at the canonical guard."""
    owner, cam, persistence = carrier
    persistence.save_listening_level(40)
    cam.db = percent_to_db(40)
    persistence.save_now(cam.db)

    owner._set_push_source_for_handoff.return_value = False
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
    "level,live_db,saved_db,muted,include_live,locked,write_ok,expected,writes",
    [
        (0, 0.0, 0.0, False, False, False, True, (True, True), [percent_to_db(0)]),
        (50, 0.0, 0.0, False, True, False, True, (True, False), []),
        (50, -20.0, 0.0, False, False, False, True, (True, False), []),
        (50, -20.0, 0.0, False, True, False, True, (True, True), [0.0]),
        (50, 0.0, -20.0, False, False, False, True, (True, True), [0.0]),
        (50, 0.0, 0.0, True, False, False, True, (True, True), [0.0]),
        (50, -20.0, -20.0, False, True, True, True, (False, False), []),
        (50, -20.0, -20.0, False, True, False, False, (False, False), [0.0]),
    ],
)
async def test_push_confirmation_reports_only_completed_carrier_changes(
    carrier, source, level, live_db, saved_db, muted, include_live,
    locked, write_ok, expected, writes,
):
    owner, cam, persistence = carrier
    cam.db, cam.muted = live_db, muted
    persistence.save_now(saved_db)
    owner._camilla_locked.return_value = locked
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

    owner._set_push_source_for_handoff.side_effect = push
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


@pytest.mark.parametrize("source", [Source.SPOTIFY, Source.BLUETOOTH])
@pytest.mark.parametrize("guard_ok", [True, False])
async def test_push_failure_records_only_confirmed_guards(carrier, source, guard_ok):
    owner, _, persistence = carrier
    persistence.save_now(-7.5)
    owner._set_camilla_db.side_effect = None
    owner._set_camilla_db.return_value = guard_ok

    assert await owner.guard_camilla_after_push_failure(
        source, 25, context="test_push", reason="push_write_failed",
        warning_prefix="push failed", guarded_warning_suffix="; guarded",
    ) is guard_ok
    owner._set_camilla_db.assert_awaited_once_with(
        percent_to_db(25), context="test_push", persist=True,
    )
    diagnostics = read_diagnostics()
    if guard_ok:
        assert diagnostics["push_guard"]["active"] is True
        assert diagnostics["push_guard"]["source"] == source.value
        assert diagnostics["push_guard"]["previous_db"] == -7.5
    else:
        assert "push_guard" not in diagnostics
