# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the audition door.

Four questions, one altitude each: does the reduced layer differ from the full
one on EXACTLY one axis, does the graph always come back, is the durable anchor
really untouched, and does a measurement session keep the door shut.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml as yaml_lib

from jasper.active_speaker.audition import (
    AUDITION_LAYER_BASELINE,
    AUDITION_LAYER_FULL,
    AuditionRefused,
    REFUSE_MEASUREMENT_ACTIVE,
    audition_state_path,
    hold_audition,
    read_audition_state,
    start_audition,
    stop_audition,
)
from jasper.active_speaker.baseline_profile import (
    recompose_applied_baseline_yaml,
    topology_config_fingerprint,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.runtime_contract import GRAPH_APPROVED_ACTIVE_RUNTIME

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology

ACTIVE_PCM = "hw:CARD=DAC8x,DEV=0"

# One boosting shelf and one cut peak per role, plus a cuts-only blend filter —
# the two stages the baseline layer drops, shaped so the boost also exercises
# the pre-split headroom the reduction gives back.
LINEARIZATION = {
    "woofer": [
        {"biquad_type": "Lowshelf", "freq": 200.0, "q": 0.7071067811865476,
         "gain": -3.0},
        {"biquad_type": "Peaking", "freq": 420.0, "q": 3.0, "gain": -2.5},
    ],
    "tweeter": [
        {"biquad_type": "Highshelf", "freq": 9000.0, "q": 0.7071067811865476,
         "gain": -4.0},
        {"biquad_type": "Peaking", "freq": 5200.0, "q": 2.0, "gain": 2.0},
    ],
}
BLEND = [{"biquad_type": "Peaking", "freq": 1900.0, "q": 2.0, "gain": -2.5}]


def _applied_profile(topology: Any) -> dict[str, Any]:
    """An APPLIED record carrying both measured-correction stages."""

    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("mono"))
    return {
        "status": "applied",
        "baseline_id": "baseline-audition-test",
        "recomposition_snapshot": {
            "schema_version": 1,
            "domain": "full",
            "topology_id": topology.topology_id,
            "topology_fingerprint": topology_config_fingerprint(topology),
            "preset": preset.to_dict(),
            "corrections": {
                "woofer": {"gain_db": -1.5, "delay_ms": 0.4, "inverted": False},
                "tweeter": {"gain_db": -4.25, "delay_ms": 0.0, "inverted": True},
            },
            "playback_device": ACTIVE_PCM,
            "linearization": LINEARIZATION,
            "blend_correction": BLEND,
        },
    }


def _filters(text: str) -> dict[str, Any]:
    return dict(yaml_lib.safe_load(text)["filters"])


# --------------------------------------------------------------------------- #
# (a) one axis
# --------------------------------------------------------------------------- #


def test_baseline_layer_drops_only_the_measured_correction_stages() -> None:
    """The reduced graph must carry NO linearization and NO blend filter, and
    every other filter must be identical to the full graph's.

    This is the whole promise of the door: the owner attributes what they hear
    to the measured correction only because nothing else moved. Asserted on the
    parsed filter table, so a renamed filter or a re-solved trim fails here.
    """

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_profile(topology)

    full_text, full_issues = recompose_applied_baseline_yaml(
        topology, applied_profile=applied, bass_extension_profile=None,
    )
    reduced_text, reduced_issues = recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied,
        bass_extension_profile=None,
        drop_measured_correction=True,
    )
    assert full_issues == [] and reduced_issues == []
    assert full_text is not None and reduced_text is not None

    full, reduced = _filters(full_text), _filters(reduced_text)
    dropped = set(full) - set(reduced)

    # Something was actually removed, and everything removed belongs to one of
    # the two measured stages: `as_blend_*` is the summed correction, and the
    # per-driver stage names itself `as_<role>_linearization_*`.
    assert dropped, "the reduced layer removed nothing — the fixture is inert"
    assert all(
        name.startswith("as_blend_") or "_linearization" in name
        for name in dropped
    ), sorted(dropped)
    assert not any(
        name.startswith("as_blend_") or "_linearization" in name
        for name in reduced
    ), sorted(reduced)

    # Everything the reduced graph DID keep is byte-identical, headroom aside:
    # the pre-split gain legitimately moves, because the attenuation that paid
    # for the tweeter's +2 dB boost goes away with the boost.
    shared = set(reduced) - {"active_baseline_headroom"}
    assert shared, "the reduced layer kept nothing — the fixture is inert"
    for name in shared:
        assert reduced[name] == full[name], name

    # Crossover, trims, delays and polarity are what "kept" has to mean.
    assert {n for n in shared if "gain" in n or "delay" in n or "_hp_" in n
            or "_lp_" in n}, sorted(shared)


def test_the_household_layers_survive_the_reduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audition's OWN derivation, with nothing stubbed between it and the
    emitter.

    The reduction must take exactly one thing away. The household's preference
    EQ and its output trim are read from live sound state rather than from the
    applied snapshot, so they are the layers most likely to be dropped by
    accident — and dropping them would silently add a second difference to an
    A/B whose whole value is that there is only one.
    """

    from jasper.active_speaker.audition import build_reduced_yaml

    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "sound.json"))
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    (tmp_path / "sound.json").write_text(
        json.dumps({
            "enabled": True,
            "curve_id": "flat",
            "parametric_bands": [
                {"type": "peaking", "freq_hz": 640.0, "gain_db": -2.0, "q": 1.5},
            ],
        }),
        encoding="utf-8",
    )
    (tmp_path / "settings.json").write_text(
        json.dumps({"headroom_trim_db": 3.0}), encoding="utf-8"
    )

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_profile(topology)
    anchor_file = tmp_path / "anchor.yml"
    full_text, issues = recompose_applied_baseline_yaml(
        topology, applied_profile=applied, bass_extension_profile=None,
    )
    assert issues == [] and full_text is not None
    anchor_file.write_text(full_text, encoding="utf-8")

    reduced, issues = build_reduced_yaml(
        topology, applied_profile=applied, anchor_path=str(anchor_file),
    )
    assert issues == [] and reduced is not None
    filters = _filters(reduced)

    # The measured correction is gone...
    assert not [
        n for n in filters if n.startswith("as_blend_") or "_linearization" in n
    ]
    # ...and the household's own preference band survived the trip.
    preference = [
        v for n, v in filters.items()
        if v.get("type") == "Biquad"
        and float((v.get("parameters") or {}).get("freq", 0.0)) == 640.0
    ]
    assert preference, sorted(filters)
    # The manual headroom trim rode along with it into the one common gain.
    headroom = filters["active_baseline_headroom"]["parameters"]["gain"]
    assert headroom <= -3.0


def test_the_level_disclosure_counts_the_cuts_it_gives_back() -> None:
    """The A/B is not level-matched, and the number that says so must be the
    LEVEL one, not just the headroom charge.

    Removing a cut filter hands its depth back in that filter's own band, and
    the headroom charge is separately ``0.0`` whenever the branch's crossover
    and trim already swallowed the linearization's boost — which is the common
    case. A disclosure built only from the headroom would read ``0.0`` for a
    profile whose deepest cut is 4 dB, and tell the owner the two layers play
    at the same level when they do not.
    """

    from jasper.active_speaker.audition import level_give_back_db
    from jasper.active_speaker.baseline_profile import profile_program_headroom_db

    applied = _applied_profile(_active_topology("mono", "active_2_way"))
    deepest_cut = max(
        -f["gain"]
        for filters in LINEARIZATION.values()
        for f in filters
        if f["gain"] < 0
    )

    assert profile_program_headroom_db(applied) == 0.0
    assert level_give_back_db(applied) == pytest.approx(deepest_cut)
    # A speaker carrying neither stage gives nothing back.
    assert level_give_back_db({"recomposition_snapshot": {}}) == 0.0


def test_the_audition_asks_for_a_reduced_graph_and_never_a_written_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_reduced_yaml`` is where the door's promise becomes a call.

    Two arguments carry it: ``drop_measured_correction=True`` is the whole
    layer semantics, and ``out_path=None`` is what keeps a reduced graph
    something to listen to rather than something a box could boot from. Spied
    on the real function, so a future refactor that stages the graph to disk
    fails here.
    """

    from jasper.active_speaker import audition as audition_module
    from jasper.active_speaker import baseline_profile

    seen: dict[str, object] = {}
    real = baseline_profile.recompose_applied_baseline_yaml

    def _spy(topology, **kwargs):
        seen.update(kwargs)
        return real(topology, **kwargs)

    monkeypatch.setattr(baseline_profile, "recompose_applied_baseline_yaml", _spy)

    topology = _active_topology("mono", "active_2_way")
    anchor = tmp_path / "anchor.yml"
    anchor.write_text("devices: {}\n", encoding="utf-8")
    text, issues = audition_module.build_reduced_yaml(
        topology,
        applied_profile=_applied_profile(topology),
        anchor_path=str(anchor),
    )

    assert issues == [] and text is not None
    assert seen["drop_measured_correction"] is True
    assert seen["out_path"] is None
    assert list(tmp_path.iterdir()) == [anchor]


# --------------------------------------------------------------------------- #
# (b) + (c) the graph comes back, and the durable anchor never moved
# --------------------------------------------------------------------------- #


class _Cam:
    """A CamillaDSP double that records which loader each call used.

    The distinction IS the crash-safety argument: ``set_active_config_raw``
    leaves the persisted path alone, ``set_config_file_path`` moves it. A test
    that only checked "the right YAML is running" would pass on the unsafe one.
    """

    def __init__(self, anchor: Path) -> None:
        self.anchor = anchor
        self.running = anchor.read_text(encoding="utf-8")
        self.path_writes: list[str] = []
        self.ducked: list[bool] = []

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return str(self.anchor)

    async def set_active_config_raw(
        self, config: str, *, best_effort: bool = False, duck: bool = True,
    ) -> bool:
        self.running = config
        self.ducked.append(duck)
        return True

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False,
    ) -> bool:
        self.path_writes.append(path)
        return True

    async def normalize_config_raw(
        self, config: str, *, best_effort: bool = False,
    ) -> str:
        return config

    async def get_active_config_raw(self, *, best_effort: bool = False) -> str:
        return self.running


@pytest.fixture()
def audition_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A box with an applied profile, a durable anchor, and no session claims."""

    from jasper.active_speaker import audition as audition_module

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_profile(topology)
    full_text, issues = recompose_applied_baseline_yaml(
        topology, applied_profile=applied, bass_extension_profile=None,
    )
    assert issues == [] and full_text is not None

    anchor = tmp_path / "active_speaker_baseline_candidate_x.yml"
    anchor.write_text(full_text, encoding="utf-8")
    state = tmp_path / "audition.json"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_AUDITION_STATE", str(state))
    # Keeps the writer lock inside the tmpdir (dsp_apply._production_or_pytest_lock_path).
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )

    monkeypatch.setattr(audition_module, "_refuse_if_graph_is_claimed", lambda: None)
    monkeypatch.setattr(
        audition_module,
        "build_reduced_yaml",
        lambda _topology, *, applied_profile, anchor_path: (
            recompose_applied_baseline_yaml(
                topology,
                applied_profile=applied_profile,
                bass_extension_profile=None,
                drop_measured_correction=True,
            )
        ),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
        lambda *_a, **_k: applied,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.baseline_profile.applied_profile_displacement",
        lambda *_a, **_k: "",
    )
    monkeypatch.setattr("jasper.output_topology.load_output_topology", lambda: topology)
    monkeypatch.setattr(
        "jasper.active_speaker.runtime_contract.classify_bass_extension_graph",
        lambda *_a, **_k: _ApprovedGraph(),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_v2.composition.confirm_graph_is_live",
        _noop_confirm,
    )
    return _Cam(anchor), anchor, full_text, state


class _ApprovedGraph:
    allowed = True
    classification = GRAPH_APPROVED_ACTIVE_RUNTIME
    issues: list[dict[str, str]] = []


async def _noop_confirm(_cam: Any, _yaml: str) -> None:
    return None


def test_stop_restores_the_durable_graph_and_never_moves_the_pointer(
    audition_box,
) -> None:
    """(b) + (c) for the explicit stop."""

    cam, anchor, full_text, state = audition_box
    before = anchor.read_bytes()

    started = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    assert started["status"] == "auditioning"
    assert cam.running != full_text
    assert read_audition_state(state) is not None

    restored = asyncio.run(stop_audition(cam=cam))

    assert restored["layer"] == AUDITION_LAYER_FULL
    assert cam.running == full_text
    assert read_audition_state(state) is None
    assert anchor.read_bytes() == before
    assert cam.path_writes == []


def test_the_deadline_restores_a_walked_away_audition(audition_box) -> None:
    """(b) for the watchdog: the owner puts the graph back at the deadline
    without anybody typing stop."""

    cam, anchor, full_text, state = audition_box
    now = [1000.0]
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    started = asyncio.run(
        start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE, clock=lambda: now[0])
    )
    assert cam.running != full_text

    reason = asyncio.run(
        hold_audition(started, cam=cam, clock=lambda: now[0], sleep=_sleep)
    )

    assert reason == "deadline"
    assert slept, "the hold returned without ever waiting"
    assert now[0] >= started["deadline_at"]
    assert cam.running == full_text
    assert read_audition_state(state) is None
    assert cam.path_writes == []


def test_an_interrupted_hold_still_restores(audition_box) -> None:
    """Ctrl-C, an SSH drop, or any raise inside the wait ends the audition the
    same way the deadline does. The restore completes BEFORE the cancellation
    propagates — stranding a speaker on a graph nobody chose is the failure
    this door exists to make impossible."""

    cam, _anchor, full_text, state = audition_box
    started = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    assert cam.running != full_text

    reached = asyncio.Event()

    async def _waits_forever(_seconds: float) -> None:
        reached.set()
        await asyncio.sleep(3600)

    async def _cancel_mid_wait() -> None:
        task = asyncio.create_task(
            hold_audition(started, cam=cam, sleep=_waits_forever)
        )
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel_mid_wait())

    assert cam.running == full_text
    assert read_audition_state(state) is None
    assert cam.path_writes == []


def test_a_departing_owner_will_not_un_swap_its_replacement(
    audition_box,
) -> None:
    """Closes the check-then-act window between "is this record mine" and the
    restore. A ``start`` landing in that gap must survive the previous owner
    walking out, or the speaker plays the applied graph while the record and
    the live owner both still say it is reduced."""

    cam, _anchor, full_text, state = audition_box
    leaving = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    replacement = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))

    outcome = asyncio.run(
        stop_audition(cam=cam, expect_token=leaving["token"])
    )

    assert outcome["status"] == "superseded"
    assert cam.running != full_text
    live = read_audition_state(state)
    assert live is not None and live["token"] == replacement["token"]


def test_an_undo_that_also_fails_is_loud_and_keeps_the_real_error(
    audition_box, monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """The worst branch: the swap took, the record could not be written, and
    the put-back failed too. The speaker is on a reduced graph nothing will
    repair, so the undo must (a) leave the caller's ORIGINAL error intact —
    replacing it hides why the arm failed — and (b) never do it silently."""

    import logging as _logging

    from jasper.active_speaker import audition as audition_module

    cam, _anchor, _full_text, _state = audition_box

    def _cannot_write(*_a, **_k):
        raise OSError("read-only /run")

    async def _cannot_restore(*_a, **_k):
        raise RuntimeError("program graph load was not confirmed")

    monkeypatch.setattr(audition_module, "atomic_write_json", _cannot_write)
    monkeypatch.setattr(audition_module, "_put_back", _cannot_restore)

    with caplog.at_level(_logging.CRITICAL):
        with pytest.raises(OSError):
            asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))

    assert [r for r in caplog.records if "undo_failed_arm" in r.getMessage()]


def test_a_second_start_takes_over_without_the_first_un_swapping_it(
    audition_box,
) -> None:
    """A replacement audition owns the graph; the displaced owner stands down
    rather than restoring somebody else's swap out from under them."""

    cam, _anchor, full_text, state = audition_box
    first = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    second = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    assert first["token"] != second["token"]

    reason = asyncio.run(hold_audition(first, cam=cam, sleep=_never_sleeps))

    assert reason == "superseded"
    assert cam.running != full_text
    live = read_audition_state(state)
    assert live is not None and live["token"] == second["token"]


def test_the_hold_hands_its_token_to_the_restore(audition_box) -> None:
    """The hold verifies the record is its own, then calls ``stop_audition``,
    which re-reads. A ``start`` landing between those two reads would be
    un-swapped and un-recorded by the departing owner unless the token travels
    with the call — so the token travelling is the thing to pin.

    ``test_a_departing_owner_will_not_un_swap_its_replacement`` pins what
    ``stop_audition`` does with it; this pins that it is given one at all.
    """

    from jasper.active_speaker import audition as audition_module

    cam, _anchor, _full_text, _state = audition_box
    started = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    seen: dict[str, object] = {}
    real_stop = audition_module.stop_audition

    async def _spy(**kwargs):
        seen.update(kwargs)
        return await real_stop(**kwargs)

    async def _expire(_seconds: float) -> None:
        raise AssertionError("the deadline should already have passed")

    audition_module.stop_audition = _spy
    try:
        asyncio.run(
            hold_audition(
                started,
                cam=cam,
                clock=lambda: started["deadline_at"] + 1.0,
                sleep=_expire,
            )
        )
    finally:
        audition_module.stop_audition = real_stop

    assert seen["expect_token"] == started["token"]


async def _never_sleeps(_seconds: float) -> None:
    raise AssertionError("the displaced owner should stand down before waiting")


def test_a_swap_that_cannot_be_recorded_is_undone(
    audition_box, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one state nothing else would ever repair: the graph swapped, but no
    record exists, so there is no owner, no deadline and no /state disclosure.
    ``start`` must put the applied graph back before the error escapes."""

    from jasper.active_speaker import audition as audition_module

    cam, _anchor, full_text, state = audition_box

    def _cannot_write(*_args, **_kwargs):
        raise OSError("read-only /run")

    monkeypatch.setattr(audition_module, "atomic_write_json", _cannot_write)

    with pytest.raises(OSError):
        asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))

    assert cam.running == full_text
    assert read_audition_state(state) is None
    assert cam.path_writes == []


# --------------------------------------------------------------------------- #
# (d) the interlock
# --------------------------------------------------------------------------- #


def test_start_is_refused_while_a_measurement_session_holds_the_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tuning session owns the graph and the fader. Swapping under it would
    measure through a graph nobody chose, so the door refuses by name."""

    from jasper.active_speaker.session_volume_plan import (
        SCHEMA_VERSION,
        STATE_KIND,
    )

    session_state = tmp_path / "session_volume.json"
    # OPENED NOW, deliberately: a session past its own wall-clock ceiling is the
    # crashed shape `live_measurement_session` refuses to treat as live, so a
    # zero timestamp here would test the wrong branch and pass for the wrong
    # reason.
    session_state.write_text(
        json.dumps({
            "kind": STATE_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "active",
            "opened_at": time.time(),
            "wall_clock_ceiling_s": 1800.0,
            "measurement_volume_db": -20.0,
            "original_main_volume_db": -30.0,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        session_state,
    )

    with pytest.raises(AuditionRefused) as refusal:
        asyncio.run(start_audition(cam=object(), layer=AUDITION_LAYER_BASELINE))

    assert refusal.value.reason == REFUSE_MEASUREMENT_ACTIVE


def test_start_is_refused_by_a_control_hold_with_no_volume_statefile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interlock's real shape: a ``/correction/`` room sweep.

    That flow takes jasper-control's measurement hold and never builds a
    ``SessionVolumePlan``, so there is no volume statefile to find. A door that
    asked the statefile alone would admit the audition and swap the graph out
    from under a running capture — which is why this asks the canonical
    ``live_measurement_session``, whose authority is the hold.
    """

    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        tmp_path / "no-such-session-volume.json",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.read_measurement_hold",
        lambda *_a, **_k: {"active": True},
    )

    with pytest.raises(AuditionRefused) as refusal:
        asyncio.run(start_audition(cam=object(), layer=AUDITION_LAYER_BASELINE))

    assert refusal.value.reason == REFUSE_MEASUREMENT_ACTIVE


def test_state_path_is_runtime_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audition record must not survive a reboot: an audition is a live
    listening pass, not durable intent, and the durable anchor already wins on
    every restart."""

    monkeypatch.delenv("JASPER_ACTIVE_SPEAKER_AUDITION_STATE", raising=False)
    assert audition_state_path().is_relative_to(Path("/run"))


def test_every_audition_swap_ducks_the_fader(audition_box) -> None:
    """An audition replaces the pipeline UNDER live household audio, both
    directions, so both swaps take `set_active_config_raw`'s duck. The
    measurement session passes `duck=False` because it already owns the fader;
    copying that here would step the graph 40 dB under a household programme."""

    cam, _anchor, _full_text, _state = audition_box

    started = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    assert started["status"] == "auditioning"
    asyncio.run(stop_audition(cam=cam))

    assert cam.ducked, "the arm and the restore both load a graph"
    assert all(cam.ducked)


def test_a_restore_that_raises_outside_the_old_tuple_is_still_a_refusal(
    audition_box, monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """A truncated durable anchor makes `set_active_config_raw` raise
    `ValueError`, and a non-UTF-8 one makes `read_text` raise
    `UnicodeDecodeError` — a `ValueError` too. Neither is an `OSError` or a
    `RuntimeError`, so audition's own catch tuple let both past: the CRITICAL
    line never fired and the CLI printed a traceback instead of a refusal."""

    import logging as _logging

    from jasper.active_speaker import audition as audition_module

    cam, _anchor, _full_text, _state = audition_box

    started = asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    assert started["status"] == "auditioning"

    async def _empty_anchor(*_a, **_k):
        raise ValueError("config must be a non-empty YAML string")

    monkeypatch.setattr(audition_module, "_put_back", _empty_anchor)

    with caplog.at_level(_logging.CRITICAL):
        with pytest.raises(AuditionRefused) as refusal:
            asyncio.run(stop_audition(cam=cam))

    assert refusal.value.reason == audition_module.REFUSE_RESTORE
    assert [r for r in caplog.records if "action=stop" in r.getMessage()]
    # The record stays: /state keeps disclosing, and the next stop can retry.
    assert read_audition_state() is not None
