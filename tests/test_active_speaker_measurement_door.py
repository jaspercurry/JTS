# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The kernel path an operator door rides: what it takes, and what it gives back.

Every double here is a real seam implementation driven against the REAL plan,
the REAL owner, the REAL session graph and the REAL emitter — only CamillaDSP
and the isolation window are replaced, because those are the two things a
hardware-free run cannot have. A door whose give-back were proven against a fake
graph would prove nothing about the fader it strands.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2.door import (
    REFUSE_NO_VOLUME_OWNER,
    REFUSE_SESSION_LIVE,
    REFUSE_VOLUME_NOT_OPEN,
    MeasurementDoorRefused,
    measurement_door,
)
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile
from jasper.active_speaker.session_volume_plan import (
    SessionVolumePlan,
    live_measurement_session,
)
from jasper.volume_owner import VolumeOwner, install_volume_owner
from jasper.measurement_window import measurement_window as real_window
from tests.active_speaker_fixtures import mono_output_topology
from tests.crossover_v2_fixtures import _preset
from tests._async_wait import wait_signalled
from tests.test_cli_measure import HOUSEHOLD_DB, FakeCam

ENTRY_CONFIG = "entry.yml"
VOLUME_STATE = "session_volume.json"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A speaker with a fake DSP, a real owner, and no isolation to acquire."""
    from jasper import measurement_window as coordinator

    class _NoWindow:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr(coordinator, "measurement_window", lambda **kw: _NoWindow())
    entry = tmp_path / ENTRY_CONFIG
    entry.write_text("devices: {}\n", encoding="utf-8")
    cam = FakeCam(entry, volume_db=HOUSEHOLD_DB)
    install_volume_owner(
        VolumeOwner(
            set_fader_db=lambda db: cam.set_volume_db(db, best_effort=True),
            get_fader_db=lambda: cam.get_volume_db(best_effort=True),
        )
    )
    try:
        yield cam
    finally:
        install_volume_owner(None)


def _profile() -> MeasurementGraphProfile:
    """The applied speaker, in the PROTECTED-NEUTRAL shape a door always emits.

    The protection sections are not decoration in a test either: the emitter
    refuses a measurement delay against the unprotected shape, because that one
    already carries its own zeroed delay lane and a second mapping key would
    play with no delay and bank as a delayed take.
    """
    from jasper.active_speaker.branch_chain import sections_by_role

    preset = _preset()
    return MeasurementGraphProfile(
        preset=preset,
        topology=mono_output_topology(),
        role_channels={"woofer": 0, "tweeter": 1},
        playback_device="plughw:CARD=Loopback,DEV=0",
        protection_sections_by_role=sections_by_role(preset.crossover_regions),
    )


def _door(tmp_path, cam, **overrides: Any):
    kwargs: dict[str, Any] = {
        "profile": _profile(),
        "spl_monitor": object(),
        "measurement_volume_db": -20.0,
        "camilla_factory": lambda: cam,
        "action": "measuring",
        "config_dir": tmp_path,
        "volume_state_path": tmp_path / VOLUME_STATE,
    }
    kwargs.update(overrides)
    return measurement_door(**kwargs)


async def test_the_door_installs_a_measurement_graph_and_puts_the_entry_back(
    tmp_path, box,
):
    """The whole point: the speaker is held, then left exactly as it was found.

    Both halves are asserted from what the DSP actually received — the last
    graph loaded is the entry text, and the fader is back on the household
    level — because a door that reported a restore it did not perform is the
    one failure this helper exists to make impossible.
    """
    entry_loudness = await box.get_loudness_volume_db()
    async with _door(tmp_path, box) as door:
        assert door.measurement_loudness_volume_db == pytest.approx(-20.0)
        assert await box.get_loudness_volume_db() == pytest.approx(-20.0)
        assert door.graph_fingerprint
        assert box.loaded, "no measurement graph reached the DSP"
        assert box.loaded[-1] != (tmp_path / ENTRY_CONFIG).read_text()
        assert box.volume_db == pytest.approx(-20.0)

    assert box.loaded[-1] == (tmp_path / ENTRY_CONFIG).read_text()
    assert box.volume_db == pytest.approx(HOUSEHOLD_DB)
    assert await box.get_loudness_volume_db() == entry_loudness


async def test_a_body_that_raises_still_gives_the_speaker_back(tmp_path, box):
    """The give-back is a ``finally``, and the body's failure is what propagates.

    A door that swallowed the body's exception would hide the reason a
    measurement stopped; one that restored only on the happy path would leave a
    speaker on a measurement graph at measurement volume after any error.
    """
    with pytest.raises(RuntimeError, match="the body failed"):
        async with _door(tmp_path, box):
            raise RuntimeError("the body failed")

    assert box.loaded[-1] == (tmp_path / ENTRY_CONFIG).read_text()
    assert box.volume_db == pytest.approx(HOUSEHOLD_DB)


@pytest.mark.parametrize("failure", ["write", "readback"])
async def test_unconfirmed_bass_reference_refuses_capture_and_restores_main(tmp_path, box, failure):
    entry = await box.get_loudness_volume_db()
    write = box.set_loudness_volume_db
    read = box.get_loudness_volume_db

    async def set_aux(db, **kwargs):
        if db == -20.0 and failure == "write":
            return False
        return await write(db, **kwargs)

    async def get_aux(**kwargs):
        current = await read(**kwargs)
        return current + 1.0 if failure == "readback" and current == -20.0 else current

    box.set_loudness_volume_db = set_aux
    box.get_loudness_volume_db = get_aux
    with pytest.raises(MeasurementDoorRefused) as caught:
        async with _door(tmp_path, box):
            pytest.fail("unconfirmed Aux reached capture")

    assert caught.value.reason == REFUSE_VOLUME_NOT_OPEN
    assert box.volume_db == pytest.approx(HOUSEHOLD_DB)
    assert await read() == entry
    assert not SessionVolumePlan(state_path=tmp_path / VOLUME_STATE).needs_recovery


async def test_cancel_during_bass_reference_write_restores_both_faders(tmp_path, box):
    entry = await box.get_loudness_volume_db()
    reached = asyncio.Event()
    write = box.set_loudness_volume_db

    async def set_aux(db, **kwargs):
        await write(db, **kwargs)
        if db == -20.0:
            reached.set()
            await asyncio.Event().wait()
        return True

    box.set_loudness_volume_db = set_aux

    async def run():
        async with _door(tmp_path, box):
            pytest.fail("cancelled Aux write reached capture")

    task = asyncio.create_task(run())
    await wait_signalled(reached, "Aux write began", producer=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert box.volume_db == pytest.approx(HOUSEHOLD_DB)
    assert await box.get_loudness_volume_db() == entry


async def test_bass_reference_restore_failure_does_not_strand_main(tmp_path, box):
    entry = await box.get_loudness_volume_db()
    write = box.set_loudness_volume_db

    async def set_aux(db, **kwargs):
        return False if db == entry else await write(db, **kwargs)

    box.set_loudness_volume_db = set_aux
    with pytest.raises(MeasurementDoorRefused) as caught:
        async with _door(tmp_path, box):
            pass

    assert caught.value.reason == REFUSE_VOLUME_NOT_OPEN
    assert box.loaded[-1] == (tmp_path / ENTRY_CONFIG).read_text()
    assert box.volume_db == pytest.approx(HOUSEHOLD_DB)
    assert not SessionVolumePlan(state_path=tmp_path / VOLUME_STATE).needs_recovery


async def test_the_interlock_refuses_before_anything_is_taken(tmp_path, box):
    """A live measurement elsewhere: refused, with nothing touched.

    The refusal carries the interlock's OWN sentence rather than a sentence
    this module writes, so an operator reads the same words whichever door
    they tried.
    """
    busy = "another measurement holds the speaker"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "jasper.active_speaker.session_volume_plan.live_measurement_session",
            lambda **kw: busy,
        )
        with pytest.raises(MeasurementDoorRefused) as caught:
            async with _door(tmp_path, box):
                pytest.fail("the door opened under a live measurement")

    assert caught.value.reason == REFUSE_SESSION_LIVE
    assert caught.value.detail == busy
    assert box.loaded == []
    assert box.volume_db == pytest.approx(HOUSEHOLD_DB)


async def test_a_process_with_no_fader_owner_refuses_rather_than_minting_one(
    tmp_path, box,
):
    """No owner is a WIRING defect, and the door says so instead of writing.

    A second authority over one fader is the arbitration failure the owner
    exists to delete, so a door that fell back to writing the fader directly
    would reintroduce it in the one process least able to arbitrate.
    """
    install_volume_owner(None)

    with pytest.raises(MeasurementDoorRefused) as caught:
        async with _door(tmp_path, box):
            pytest.fail("the door opened with no fader owner")

    assert caught.value.reason == REFUSE_NO_VOLUME_OWNER
    assert box.loaded == []


async def test_a_cancel_inside_the_open_leaves_no_durable_record(tmp_path, box):
    """B1: ``plan.open`` persists BEFORE it writes, so a cancel there must drain.

    The plan writes its durable ``active`` intent before the first volume
    mutation — deliberately, so a crash hydrates as recoverable. A cancellation
    landing in that gap (Ctrl-C, or the coordinator's isolation-loss abort) is
    therefore the one failure that can leave a record with no owner, and every
    operator door then reads a live measurement for the whole wall-clock
    ceiling. The cancel is delivered while the fader write is in flight, which
    is exactly where the gap is.

    Asserted at two altitudes because either alone is weaker than it reads: the
    plan's own ``needs_recovery`` is the structural fact, and
    ``live_measurement_session`` is the sentence the NEXT door would have been
    refused with.
    """
    reached = asyncio.Event()
    release = asyncio.Event()
    real_set = box.set_volume_db

    async def _hang(db: float, best_effort: bool = True) -> bool:
        reached.set()
        await release.wait()
        return await real_set(db, best_effort=best_effort)

    box.set_volume_db = _hang

    async def _run() -> None:
        async with _door(tmp_path, box):
            pytest.fail("the door opened through a cancelled fader write")

    task = asyncio.ensure_future(_run())
    await wait_signalled(reached, "the fader write began", producer=task)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    state_path = tmp_path / VOLUME_STATE
    assert SessionVolumePlan(state_path=state_path).needs_recovery is False
    assert live_measurement_session(state_path=state_path, action="measuring") is None
    assert box.volume_db == pytest.approx(HOUSEHOLD_DB)


async def test_a_stale_record_from_a_crashed_run_does_not_lock_the_door(
    tmp_path, box,
):
    """N5: a crashed run's leftover is drained, not treated as a live session.

    ``live_measurement_session`` deliberately lets a stale-active record
    through — blocking on it would make a crash permanently un-openable — and
    ``plan.open`` then refuses over it. Without the force-drain the two
    together make every later run fail at the volume, which is the crash
    recovery nobody can perform.

    The leftover is written by a REAL plan on a frozen clock, so the record is
    the shape the plan actually persists rather than a hand-built guess.
    """
    from jasper.active_speaker.crossover_v2.volume_claim import (
        MeasurementVolumeClaim,
        OwnerVolumeDoor,
    )
    from jasper.volume_owner import volume_owner

    state_path = tmp_path / VOLUME_STATE
    crashed = SessionVolumePlan(
        state_path=state_path, wall_clock_ceiling_s=1.0, clock=lambda: 0.0,
    )
    owner = volume_owner()
    claim = MeasurementVolumeClaim(owner)
    await crashed.open(
        -20.0,
        OwnerVolumeDoor(
            owner,
            read_fader=lambda: box.get_volume_db(best_effort=False),
            claim=claim,
        ),
    )
    # The CLAIM dies with the process; only the durable record survives a
    # crash. Holding one here would make the leftover outrank the drain and
    # test a state no crash can produce.
    await claim.release()
    assert state_path.exists(), "the crashed run left no record to drain"

    async with _door(tmp_path, box) as door:
        assert door.graph_fingerprint

    assert box.loaded[-1] == (tmp_path / ENTRY_CONFIG).read_text()


async def test_the_variant_axes_reach_the_emitter_through_the_door(tmp_path, box):
    """Three axes, no ``variant`` parameter — the graph seam's own arity.

    Driven through the door's own graph so the axes are proven to survive the
    binding, not merely to exist on the emitter: a door that bound a
    profile-only emit would silently drop every flip, delay and trim and bank
    records naming coordinates that never played.
    """
    async with _door(tmp_path, box) as door:
        base = door.graph_fingerprint
        flipped = await door.graph.install(("tweeter",), {"woofer": 120.0}, {})
        levelled = await door.graph.install((), {}, {"tweeter": -9.5})

    assert len({base, flipped, levelled}) == 3, (
        "each variant axis must make a different graph with its own fingerprint"
    )
    assert "# inverted_roles=" in box.loaded[1]


async def test_the_door_holds_the_gate_under_the_owner_its_caller_states(
    tmp_path, monkeypatch,
):
    """The gate identity REACHES ``measurement_window``, it is not merely stored.

    ``mux.FANIN_TEST_OWNERS`` is a CLOSED allowlist: an owner missing from it is
    refused the fan-in diagnostic gate, the correction lane never carries the
    stimulus, and a door measures silence with every daemon healthy and no gate
    tripped. So a caller has to be able to state its own — and asserting that a
    constant EXISTS would not catch a door that accepted the argument and then
    called ``measurement_window()`` bare, which is exactly the shape this
    parameter was added to fix.

    Default unchanged: ``None`` keeps the wizard's owner, so every caller that
    does not care is byte-identical to before.
    """
    from jasper import measurement_window as coordinator
    from jasper.mux import FANIN_TEST_OWNERS

    seen: list[str | None] = []

    class _Window:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    def _window(**kw: Any) -> Any:
        seen.append(kw.get("gate_owner"))
        return _Window()

    monkeypatch.setattr(coordinator, "measurement_window", _window)
    entry = tmp_path / ENTRY_CONFIG
    entry.write_text("devices: {}\n", encoding="utf-8")
    cam = FakeCam(entry, volume_db=HOUSEHOLD_DB)
    install_volume_owner(
        VolumeOwner(
            set_fader_db=lambda db: cam.set_volume_db(db, best_effort=True),
            get_fader_db=lambda: cam.get_volume_db(best_effort=True),
        )
    )
    try:
        async with _door(tmp_path, cam, gate_owner="jasper-null"):
            pass
        async with _door(tmp_path, cam):
            pass
    finally:
        install_volume_owner(None)

    assert seen == ["jasper-null", coordinator.MEASUREMENT_GATE_OWNER]
    # A stated owner the allowlist does not carry is refused the gate on the
    # box, so a door may only name one that is registered.
    assert set(seen) <= FANIN_TEST_OWNERS


@pytest.mark.parametrize("inverted,delays,trims", [
    ((), {}, {}),
    (("tweeter",), {"woofer": 120.0}, {"tweeter": -9.5}),
])
def test_the_wizard_emits_through_the_shared_home(tmp_path, monkeypatch, inverted, delays, trims):
    from types import SimpleNamespace

    from jasper.active_speaker.measurement_emit import emit_measurement_graph
    from jasper.web import correction_crossover_v2 as host

    profile = _profile()
    playback = host.bind_production_play(
        camilla_factory=lambda: object(),
        evidence_store=SimpleNamespace(bundle_dir=tmp_path),
        capture_session_id="door_pin",
        topology=profile.topology, preset=profile.preset,
        role_channels=profile.role_channels,
        playback_device=profile.playback_device,
        safety_profile={}, role_targets={}, session_volume_db=-20.0,
        protection_sections_by_role=profile.protection_sections_by_role,
        program_for_phase=lambda phase: None,
    )

    actual = playback.graph.graph_yaml(inverted, delays, trims)
    expected = emit_measurement_graph(profile, inverted, delays, trims)
    assert actual == expected


async def test_level_window_needs_a_watch_before_any_volume_write(tmp_path, box):
    with pytest.raises(MeasurementDoorRefused) as refused:
        async with _door(tmp_path, box, spl_monitor=None):
            pytest.fail("unwatched window yielded")
    assert refused.value.reason == "measure_spl_calibration_required"
    assert box.volume_db == HOUSEHOLD_DB
    assert box.loaded == []


async def test_one_hold_renews_all_three_leases_across_two_level_windows(tmp_path, box, monkeypatch):
    from jasper import measurement_window as coordinator
    from jasper.active_speaker.crossover_v2.door import bind_measurement_graph, isolation_hold, level_window

    counts = dict(voice=0, gate=0, hold=0, resume=0, gate_release=0, hold_release=0)
    renewed, target = asyncio.Event(), 2
    def note(key):
        counts[key] += 1
        if min(counts[key] for key in ("voice", "gate", "hold")) >= target:
            renewed.set()
    async def voice(_path, command, **kwargs):
        if command == "MEASURE_PAUSE":
            note("voice")
        elif command == "MEASURE_RESUME":
            note("resume")
        return {"result": "ok", "state": "WAKE"}
    async def gate(**kwargs):
        note("gate")
    async def release_gate(**kwargs):
        note("gate_release")
    async def hold_command(path, body):
        note("hold" if path == "/measurement/hold" else "hold_release")
        return 200, {"measurement": {"active": True, "owner": body.get("owner")}}
    monkeypatch.setattr(coordinator, "measurement_window", real_window)
    monkeypatch.setattr(coordinator, "_voice_uds_command", voice)
    monkeypatch.setattr(coordinator, "_acquire_measurement_gate", gate)
    monkeypatch.setattr(coordinator, "_release_measurement_gate", release_gate)
    monkeypatch.setattr(coordinator, "_measurement_hold_command", hold_command)
    monkeypatch.setattr(coordinator, "MEASUREMENT_LEASE_REFRESH_SEC", .001)
    monkeypatch.setattr(coordinator, "MEASUREMENT_GATE_REFRESH_SEC", .001)
    graph = bind_measurement_graph(_profile(), camilla_factory=lambda: box, config_dir=tmp_path)
    async with isolation_hold(graph=graph, camilla_factory=lambda: box, action="test",
                              volume_state_path=tmp_path / VOLUME_STATE) as held:
        for level in (-20, -14):
            async with level_window(level, hold=held, spl_monitor=object()):
                await wait_signalled(renewed, "all three leases renewed")
            assert counts["resume"] == counts["gate_release"] == counts["hold_release"] == 0
            target = max(counts[key] for key in ("voice", "gate", "hold")) + 1
            renewed.clear()
            await wait_signalled(renewed, "leases renewed between windows")
    assert counts["resume"] == counts["gate_release"] == counts["hold_release"] == 1
    assert box.volume_db == HOUSEHOLD_DB
