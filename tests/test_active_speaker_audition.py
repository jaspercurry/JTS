# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the audition door.

Four questions, one altitude each: does the reduced layer differ from the full
one on EXACTLY one axis, does the graph always come back, is the durable anchor
really untouched, and does a measurement session keep the door shut.
"""

from __future__ import annotations

from tests.active_speaker_fixtures import compile_applied_fixture, isolated_candidate_bank as isolated_candidate_bank


import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("isolated_candidate_bank")
import yaml as yaml_lib

from jasper.active_speaker.audition import (
    _refuse_if_graph_is_claimed,
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
from jasper.output_topology import topology_config_fingerprint
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.runtime_contract import GRAPH_APPROVED_ACTIVE_RUNTIME

from tests.test_active_speaker_profile import _two_way_preset
from tests.test_active_speaker_runtime_contract import _active_topology, _dynamic_bass_descriptor

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

    full_text, full_issues = compile_applied_fixture(
        topology, applied_profile=applied,
    )
    reduced_text, reduced_issues = compile_applied_fixture(
        topology,
        applied_profile=applied,
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


@pytest.mark.parametrize("bass_extension", [False, True])
def test_the_household_layers_survive_the_reduction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bass_extension: bool,
) -> None:
    from jasper.active_speaker.audition import build_reduced_yaml
    from tests.test_active_speaker_baseline_profile import _ROOM_CORRECTION  # lazy: fixture cycle

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
    if bass_extension:
        applied["recomposition_snapshot"]["bass_extension"] = _dynamic_bass_descriptor()
    applied["recomposition_snapshot"]["room_correction"] = _ROOM_CORRECTION
    full_text, issues = compile_applied_fixture(
        topology, applied_profile=applied,
    )
    assert issues == [] and full_text is not None

    from tests.active_speaker_fixtures import declare_applied_fixture
    declare_applied_fixture(monkeypatch, topology, applied)
    reduced, issues = build_reduced_yaml(
        topology, applied_profile=applied,
    )
    assert issues == [] and reduced is not None
    filters = _filters(reduced)

    assert not [
        n for n in filters if n.startswith("as_blend_") or "_linearization" in n
    ]
    preference = [
        v for n, v in filters.items()
        if v.get("type") == "Biquad"
        and float((v.get("parameters") or {}).get("freq", 0.0)) == 640.0
    ]
    assert preference, sorted(filters)
    headroom = filters["active_baseline_headroom"]["parameters"]["gain"]
    assert headroom <= -3.0
    full = _filters(full_text)
    room_names = {name for name in full if name.startswith("room_peq_")}
    assert room_names
    assert {name: filters[name] for name in room_names} == {name: full[name] for name in room_names}
    bass_names = {name for name in filters if name.startswith("bass_ext_dynamic")}
    assert bool(bass_names) is bass_extension
    assert all(filters[name] == full[name] for name in bass_names)


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
    from jasper.active_speaker.audition import build_reduced_yaml
    from tests.active_speaker_fixtures import declare_applied_fixture

    topology = _active_topology("mono", "active_2_way")
    applied = _applied_profile(topology)
    declare_applied_fixture(monkeypatch, topology, applied)
    before = sorted(tmp_path.rglob("*"))
    text, issues = build_reduced_yaml(topology, applied_profile=applied)
    assert not issues
    assert not any("linearization" in name or name.startswith("as_blend_") for name in _filters(text))
    assert sorted(tmp_path.rglob("*")) == before


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
    full_text, issues = compile_applied_fixture(
        topology, applied_profile=applied,
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
        lambda _topology, *, applied_profile: (
            compile_applied_fixture(
                topology,
                applied_profile=applied_profile,
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


async def _never_sleeps(_seconds: float) -> None:
    raise AssertionError("the displaced owner should stand down before waiting")


def _arm(cam: _Cam, full_text: str, **kwargs: Any) -> dict[str, Any]:
    started = asyncio.run(
        start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE, **kwargs)
    )
    assert started["status"] == "auditioning"
    assert cam.running != full_text
    return started


def _exit_by_stop(cam, full_text, monkeypatch):
    _arm(cam, full_text)
    assert asyncio.run(stop_audition(cam=cam))["layer"] == AUDITION_LAYER_FULL
    return None


def _exit_by_deadline(cam, full_text, monkeypatch):
    now = [1000.0]
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    started = _arm(cam, full_text, clock=lambda: now[0])
    reason = asyncio.run(
        hold_audition(started, cam=cam, clock=lambda: now[0], sleep=_sleep)
    )
    assert reason == "deadline"
    assert slept, "the hold returned without ever waiting"
    assert now[0] >= started["deadline_at"]
    return None


def _exit_by_expired_deadline(cam, full_text, monkeypatch):
    # `stop_audition` re-reads the record, so a `start` landing between the
    # hold's "is this record mine" check and that re-read would be un-swapped
    # and un-recorded unless the hold's OWN token travels with the call. The
    # stale-token-stop row pins what stop does with it; this pins it is given.
    from jasper.active_speaker import audition as audition_module

    started = _arm(cam, full_text)
    seen: dict[str, object] = {}
    real_stop = audition_module.stop_audition

    async def _spy(**kwargs):
        seen.update(kwargs)
        return await real_stop(**kwargs)

    async def _expire(_seconds: float) -> None:
        raise AssertionError("the deadline should already have passed")

    monkeypatch.setattr(audition_module, "stop_audition", _spy)
    asyncio.run(
        hold_audition(
            started, cam=cam, clock=lambda: started["deadline_at"] + 1.0,
            sleep=_expire,
        )
    )
    assert seen["expect_token"] == started["token"]
    return None


def _exit_by_cancellation(cam, full_text, monkeypatch):
    # Ctrl-C, an SSH drop, or any raise inside the wait: the restore must
    # complete BEFORE the cancellation propagates, or the speaker is stranded
    # on a graph nobody chose — the failure this door exists to make impossible.
    started = _arm(cam, full_text)
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
    return None


def _exit_by_unrecordable_arm(cam, full_text, monkeypatch):
    # Swapped-but-unrecorded is the one state nothing else would ever repair:
    # no owner, no deadline, no /state disclosure. `start` must put the applied
    # graph back before the error escapes.
    from jasper.active_speaker import audition as audition_module

    def _cannot_write(*_args, **_kwargs):
        raise OSError("read-only /run")

    monkeypatch.setattr(audition_module, "atomic_write_json", _cannot_write)
    with pytest.raises(OSError):
        asyncio.run(start_audition(cam=cam, layer=AUDITION_LAYER_BASELINE))
    return None


def _exit_by_stale_token_stop(cam, full_text, monkeypatch):
    # The check-then-act window between "is this record mine" and the restore:
    # a `start` landing in that gap must survive the previous owner walking
    # out, or the speaker plays the applied graph while the record and the live
    # owner both still say it is reduced.
    leaving = _arm(cam, full_text)
    replacement = _arm(cam, full_text)
    outcome = asyncio.run(stop_audition(cam=cam, expect_token=leaving["token"]))
    assert outcome["status"] == "superseded"
    return replacement


def _exit_by_displacement(cam, full_text, monkeypatch):
    # The displaced owner stands down rather than restoring somebody else's
    # swap out from under them.
    first = _arm(cam, full_text)
    second = _arm(cam, full_text)
    assert first["token"] != second["token"]
    reason = asyncio.run(hold_audition(first, cam=cam, sleep=_never_sleeps))
    assert reason == "superseded"
    return second


@pytest.mark.parametrize(
    "exit_path",
    [
        pytest.param(_exit_by_stop, id="explicit-stop"),
        pytest.param(_exit_by_deadline, id="deadline"),
        pytest.param(_exit_by_expired_deadline, id="deadline-already-passed"),
        pytest.param(_exit_by_cancellation, id="cancelled-hold"),
        pytest.param(_exit_by_unrecordable_arm, id="unrecordable-arm"),
        pytest.param(_exit_by_stale_token_stop, id="stale-token-stop"),
        pytest.param(_exit_by_displacement, id="displaced-owner"),
    ],
)
def test_every_exit_path_leaves_the_graph_where_the_record_says(
    audition_box, exit_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) + (c): however an audition ends, the live graph and the record agree
    and the durable anchor never moved.

    An exit that hands the graph on returns the record that now owns it; every
    other exit returns ``None`` and owes the applied graph back. ``path_writes``
    is the crash-safety half — an audition may only ever load a graph with
    ``set_active_config_raw`` and never repoint the persisted config path, so a
    check that asked only "is the right YAML running" would pass on the unsafe
    loader.
    """

    cam, anchor, full_text, state = audition_box
    before = anchor.read_bytes()

    successor = exit_path(cam, full_text, monkeypatch)

    if successor is None:
        assert cam.running == full_text
        assert read_audition_state(state) is None
    else:
        assert cam.running != full_text
        live = read_audition_state(state)
        assert live is not None and live["token"] == successor["token"]
    assert anchor.read_bytes() == before
    assert cam.path_writes == []


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
    """The interlock's real shape: a measurement sweep.

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


@pytest.mark.parametrize("muted,trim,method", [(False, 0.0, "unchanged"), (True, 1.5, "parameters")])
def test_rear_compare_changes_only_two_parameters(muted, trim, method):
    from jasper.active_speaker.audition import rear_compare_yaml
    from jasper.active_speaker.rear_calibration import rear_stage_gain_name
    from jasper.sound.live_edit import plan_live_edit
    from tests.test_rear_output_foundation import _cardioid_baseline

    applied = _cardioid_baseline()[2]
    wanted = rear_compare_yaml(applied, rear_muted=muted, trim_db=trim)
    before, after = yaml_lib.safe_load(applied), yaml_lib.safe_load(wanted)
    assert plan_live_edit(applied, wanted).method == method
    rear = rear_stage_gain_name(2, "output")
    assert after["filters"][rear]["parameters"]["mute"] is muted
    gain = before["filters"]["active_baseline_headroom"]["parameters"]["gain"]
    assert after["filters"]["active_baseline_headroom"]["parameters"]["gain"] == gain - trim
    after["filters"][rear]["parameters"]["mute"] = False
    after["filters"]["active_baseline_headroom"]["parameters"]["gain"] = gain
    assert after == before
    assert after["devices"]["volume_limit"] == 0.0


@pytest.mark.parametrize("trim", [-0.1, 6.1, float("nan"), float("inf")])
def test_rear_compare_trim_bounds(trim):
    from jasper.active_speaker.audition import rear_compare_yaml
    from tests.test_rear_output_foundation import _cardioid_baseline

    with pytest.raises(ValueError):
        rear_compare_yaml(_cardioid_baseline()[2], rear_muted=True, trim_db=trim)


@pytest.mark.parametrize("case,code", [("missing", "audition_no_rear_stage"), ("muted", "audition_rear_muted_in_tune"), ("multiple", "audition_no_rear_stage")])
def test_rear_compare_requires_one_audible_rear(case, code):
    from jasper.active_speaker.audition import rear_compare_yaml
    from jasper.active_speaker.rear_calibration import rear_stage_gain_name
    from tests.test_rear_output_foundation import _cardioid_baseline, _rear_document

    graph = yaml_lib.safe_load(_cardioid_baseline(_rear_document(rear_muted=case == "muted"))[2])
    if case == "missing":
        del graph["filters"][rear_stage_gain_name(2, "output")]
    elif case == "multiple":
        graph["filters"][rear_stage_gain_name(1, "output")] = graph["filters"][rear_stage_gain_name(2, "output")]
    with pytest.raises(AuditionRefused) as exc:
        rear_compare_yaml(yaml_lib.safe_dump(graph), rear_muted=True, trim_db=0.0)
    assert exc.value.reason == code


@pytest.fixture()
def compare_box(audition_box, monkeypatch):
    from tests.test_rear_output_foundation import _cardioid_baseline

    cam, anchor, _, state = audition_box
    _, topology, applied = _cardioid_baseline()
    anchor.write_text(applied)
    cam.running = applied
    monkeypatch.setattr("jasper.output_topology.load_output_topology", lambda: topology)
    return cam, anchor, applied, state


def test_web_compare_off_on_normal_and_unchanged(compare_box):
    from jasper.active_speaker.audition import set_compare_state, rear_compare_yaml
    from jasper.sound.live_edit import plan_live_edit

    cam, anchor, applied, path = compare_box
    first = asyncio.run(set_compare_state("off", cam=cam, trim_db=0.0))
    assert plan_live_edit(cam.running, rear_compare_yaml(applied, rear_muted=True, trim_db=0.0)).method == "unchanged"
    assert cam.ducked == [False]
    second = asyncio.run(set_compare_state("on", cam=cam, trim_db=0.0))
    assert plan_live_edit(cam.running, applied).method == "unchanged"
    assert cam.ducked == [False, False]
    assert second["expires_at"] >= first["expires_at"]
    assert second["token"] != first["token"]
    assert read_audition_state()["state"] == "on"
    asyncio.run(set_compare_state("normal", cam=cam, trim_db=0.0))
    assert cam.ducked == [False, False]
    assert not path.exists()
    assert anchor.read_text() == applied
    assert cam.path_writes == []


@pytest.mark.parametrize("exit_kind", ["normal", "deadline", "takeover"])
def test_compare_restore_and_takeover(compare_box, monkeypatch, exit_kind):
    from jasper.active_speaker import audition
    from jasper.sound.live_edit import plan_live_edit

    cam, _, applied, path = compare_box
    state = asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
    if exit_kind == "normal":
        asyncio.run(audition.set_compare_state("normal", cam=cam, trim_db=0.0))
    elif exit_kind == "deadline":
        assert asyncio.run(hold_audition(state, cam=cam, clock=lambda: state["expires_at"])) == "deadline"
    else:
        replacement = asyncio.run(start_audition(cam=cam))
        assert asyncio.run(hold_audition(state, cam=cam, sleep=_never_sleeps)) == "superseded"
        assert read_audition_state()["token"] == replacement["token"]
        assert cam.ducked == [False, True]
        with pytest.raises(AuditionRefused) as exc:
            asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
        assert exc.value.reason == "audition_running_graph_differs"
        asyncio.run(stop_audition(cam=cam))
        asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
        assert asyncio.run(hold_audition(replacement, cam=cam, sleep=_never_sleeps)) == "superseded"
        asyncio.run(audition.set_compare_state("normal", cam=cam, trim_db=0.0))
    assert plan_live_edit(cam.running, applied).method == "unchanged"
    assert not path.exists()
    assert cam.ducked[-1] is False


@pytest.mark.parametrize("state", ["off", "on"])
@pytest.mark.parametrize("code", ["audition_measurement_session_active", "audition_commission_load_active"])
def test_compare_interlocks_on_every_flip(compare_box, monkeypatch, state, code):
    from jasper.active_speaker import audition

    cam, _, applied, path = compare_box
    def refused():
        raise AuditionRefused(code, "claimed")
    monkeypatch.setattr(audition, "_refuse_if_graph_is_claimed", refused)
    with pytest.raises(AuditionRefused) as exc:
        asyncio.run(audition.set_compare_state(state, cam=cam, trim_db=0.0))
    assert exc.value.reason == code
    assert cam.running == applied
    assert not path.exists()


def test_displaced_token_is_checked_after_writer_lock(compare_box, monkeypatch):
    from contextlib import asynccontextmanager
    from jasper.active_speaker.audition import set_compare_state

    cam, _, _, path = compare_box
    state = asyncio.run(set_compare_state("off", cam=cam, trim_db=0.0))
    @asynccontextmanager
    async def overtaken(*args, **kwargs):
        path.write_text(json.dumps({**state, "token": "new-owner"}))
        yield
    monkeypatch.setattr("jasper.dsp_apply.dsp_writer_lock", overtaken)
    verdict = asyncio.run(stop_audition(cam=cam, expect_token=state["token"]))
    assert verdict["status"] == "superseded"
    assert cam.ducked == [False]
    assert read_audition_state()["token"] == "new-owner"


@pytest.mark.parametrize("layer", ["baseline", "rear_compare"])
def test_audition_summary_excludes_ownership_fields(audition_box, monkeypatch, layer):
    from jasper.active_speaker import audition

    _, _, _, path = audition_box
    path.write_text(json.dumps({"kind": audition.AUDITION_STATE_KIND, "schema_version": 1,
        "layer": layer, "state": "off" if layer == "rear_compare" else None,
        "deadline_at": 150.0, "token": "private-owner", "owner_pid": 123}))
    monkeypatch.setattr(audition.time, "time", lambda: 100.0)
    assert audition.audition_summary() == {"layer": layer,
        "state": "off" if layer == "rear_compare" else None, "expires_in_s": 50}


@pytest.mark.parametrize("cause", ["expiry", "normal", "takeover", "graph_replaced"])
def test_web_holder_ends_and_releases_idle_hold(compare_box, monkeypatch, cause):
    import threading
    from contextlib import contextmanager
    from jasper.active_speaker import audition
    from jasper.sound.live_edit import plan_live_edit

    cam, _, applied, path = compare_box
    if cause == "expiry":
        monkeypatch.setattr(audition, "AUDITION_DEADLINE_S", 0.0)
    state = asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
    ready, release = threading.Event(), threading.Event()
    held, controllers, closed = [], [], []
    @contextmanager
    def idle_hold():
        held.append(True)
        try:
            yield
        finally:
            held.append(False)
    class HolderCam:
        def __getattr__(self, name):
            return getattr(cam, name)
        async def close(self):
            closed.append(self)
    def factory():
        assert held == [True]
        assert threading.current_thread() is not threading.main_thread()
        asyncio.get_running_loop()
        fresh = HolderCam()
        controllers.append(fresh)
        return fresh
    async def pause(_seconds):
        ready.set()
        assert await asyncio.to_thread(release.wait, 5)
    real_hold = audition.hold_audition
    async def hold(state, *, cam):
        return await real_hold(state, cam=cam, sleep=pause)
    monkeypatch.setattr(audition, "hold_audition", hold)
    thread = audition.start_web_audition_holder(state, factory, idle_hold)
    try:
        if cause != "expiry":
            assert ready.wait(5)
            if cause == "normal":
                asyncio.run(audition.set_compare_state("normal", cam=cam, trim_db=0.0))
            elif cause == "takeover":
                newer = asyncio.run(audition.set_compare_state("on", cam=cam, trim_db=0.0))
            else:
                cam.running = applied
                audition.graph_replaced()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert held == [True, False]
    assert len(controllers) == 1
    assert closed == controllers
    assert plan_live_edit(cam.running, applied).method == "unchanged"
    if cause == "takeover":
        assert read_audition_state()["token"] == newer["token"]
        asyncio.run(stop_audition(cam=cam))
    assert not path.exists()


@pytest.mark.parametrize("failure,code", [("no_anchor", "audition_no_durable_anchor"), ("load", "audition_load_refused"), ("transport", "audition_load_refused"), ("restore", "audition_restore_failed")])
def test_compare_failure_codes_and_restore_record(compare_box, monkeypatch, failure, code):
    from jasper.active_speaker import audition
    from jasper.camilla import CamillaUnavailable

    cam, _, _, path = compare_box
    if failure == "restore":
        asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
    async def failed(*args, **kwargs):
        if failure == "transport":
            raise CamillaUnavailable("unreachable")
        return False
    async def no_anchor(**kwargs):
        return None
    if failure == "no_anchor":
        monkeypatch.setattr(cam, "get_config_file_path", no_anchor)
    else:
        monkeypatch.setattr(cam, "set_active_config_raw", failed)
    with pytest.raises(AuditionRefused) as exc:
        asyncio.run(audition.set_compare_state("normal" if failure == "restore" else "off", cam=cam, trim_db=0.0))
    assert exc.value.reason == code
    assert path.exists() is (failure == "restore")


def test_failed_compare_takeover_clears_record_after_successful_undo(compare_box, monkeypatch):
    from jasper.active_speaker import audition
    from jasper.sound.live_edit import plan_live_edit

    cam, _, applied, path = compare_box
    asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
    def unavailable(*args, **kwargs):
        raise OSError("record unavailable")
    monkeypatch.setattr(audition, "atomic_write_json", unavailable)
    with pytest.raises(OSError):
        asyncio.run(audition.set_compare_state("on", cam=cam, trim_db=0.0))
    assert not path.exists()
    assert plan_live_edit(cam.running, applied).method == "unchanged"


@pytest.mark.parametrize("layer", ["baseline", "rear_compare"])
def test_restore_ignores_measurement_refusal(compare_box, monkeypatch, layer):
    from jasper.active_speaker import audition
    from jasper.sound.live_edit import plan_live_edit

    cam, _, applied, path = compare_box
    asyncio.run(start_audition(cam=cam, layer=layer, compare_state="off"))
    monkeypatch.setattr(audition, "_refuse_if_graph_is_claimed", _refuse_if_graph_is_claimed)
    monkeypatch.setattr("jasper.active_speaker.session_volume_plan.live_measurement_session",
                        lambda **kwargs: "unresolved_volume_safety")
    with pytest.raises(AuditionRefused) as exc:
        audition._refuse_if_graph_is_claimed()
    assert exc.value.reason == "audition_measurement_session_active"
    if layer == "rear_compare":
        result = asyncio.run(audition.set_compare_state("normal", cam=cam, trim_db=0.0))
    else:
        result = asyncio.run(stop_audition(cam=cam))
    assert result["status"] == "restored"
    assert plan_live_edit(cam.running, applied).method == "unchanged"
    assert not path.exists()


@pytest.mark.parametrize("pid_case", ["recycled", "other_user", "self"])
def test_startup_recovers_every_other_web_owner(compare_box, monkeypatch, pid_case):
    from unittest.mock import Mock
    from jasper.active_speaker import audition
    from jasper.sound.live_edit import plan_live_edit

    cam, _, applied, path = compare_box
    state = asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
    state["owner_pid"] += int(pid_case != "self")
    path.write_text(json.dumps(state))
    kill = Mock(side_effect=PermissionError() if pid_case == "other_user" else None)
    monkeypatch.setattr(audition.os, "kill", kill)
    asyncio.run(audition.recover_web_audition(cam))
    kill.assert_not_called()
    assert path.exists() is (pid_case == "self")
    assert cam.ducked == ([False] if pid_case == "self" else [False, False])
    if pid_case != "self":
        assert plan_live_edit(cam.running, applied).method == "unchanged"


@pytest.mark.parametrize("change", ["parameters", "pipeline"])
def test_compare_refuses_unsaved_live_edit(compare_box, change):
    from jasper.active_speaker import audition

    cam, anchor, applied, path = compare_box
    graph = yaml_lib.safe_load(applied)
    if change == "parameters":
        graph["filters"]["active_baseline_headroom"]["parameters"]["gain"] -= 1.0
    else:
        graph["pipeline"] = graph["pipeline"][:-1]
    draft = yaml_lib.safe_dump(graph)
    cam.running = draft
    with pytest.raises(AuditionRefused) as exc:
        asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
    assert exc.value.reason == "audition_running_graph_differs"
    assert cam.running == draft
    assert cam.ducked == []
    assert not path.exists()
    assert anchor.read_text() == applied


@pytest.mark.parametrize("path", [("filters",), ("devices", "playback", "channels"),
    ("filters", "active_baseline_headroom"), ("filters", "active_baseline_headroom", "parameters")])
@pytest.mark.parametrize("damage", ["missing", "null"])
def test_malformed_compare_graph_is_named(path, damage):
    from jasper.active_speaker.audition import rear_compare_yaml
    from tests.test_rear_output_foundation import _cardioid_baseline

    graph = yaml_lib.safe_load(_cardioid_baseline()[2])
    node = graph
    for key in path[:-1]:
        node = node[key]
    if damage == "missing":
        del node[path[-1]]
    else:
        node[path[-1]] = None
    with pytest.raises(AuditionRefused) as exc:
        rear_compare_yaml(yaml_lib.safe_dump(graph), rear_muted=True, trim_db=0.0)
    assert exc.value.reason == "audition_malformed_graph"


@pytest.mark.parametrize("deadline", [None, "1800", [], {}, True, float("nan"), float("inf")])
def test_malformed_record_has_no_summary(compare_box, deadline):
    from jasper.active_speaker import audition

    cam, _, _, path = compare_box
    state = asyncio.run(audition.set_compare_state("off", cam=cam, trim_db=0.0))
    if deadline is None:
        del state["deadline_at"]
    else:
        state["deadline_at"] = deadline
    path.write_text(json.dumps(state))
    assert read_audition_state() is None
    assert audition.audition_summary() is None
