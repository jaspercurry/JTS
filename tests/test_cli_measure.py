# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-measure``: what the flags mean, and what one run leaves behind.

Two altitudes, deliberately. The flag layer is exercised with no speaker at all
— it is pure translation and one refusal — and the run is exercised with the
REAL door, plan, session graph, engine and record store, doubling only the three
things a hardware-free box cannot have: CamillaDSP, the isolation window, and
the audio emission itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.crossover_v2 import door as door_module

from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    POLARITY_INVERTED,
)
from jasper.active_speaker.crossover_v2.program_transaction import ProgramForStimulus
from jasper.cli import measure
from jasper.cli.measure import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    REFUSE_CANDIDATE_ID_REQUIRED,
    REFUSE_GRAPH_LOST,
    REFUSE_NO_MIC,
    REFUSE_ONE_POSITION_PER_RUN,
    REFUSE_SPEC_INVALID,
    REFUSE_SPECS_MIXED_POSE,
    REFUSE_SPECS_UNREADABLE,
    REFUSE_SPECS_WITH_TAKE_FLAGS,
    BoxDeclaration,
    MeasureFlagError,
    build_parser,
    spec_from_args,
    specs_from_args,
)
from tests.active_speaker_fixtures import mono_output_topology
from tests.crossover_v2_fixtures import _preset, _roles

ARTIFACTS = "evidence/v1/artifacts"
CAPTURE_RELPATH = "captures/summed/take.wav"
HOUSEHOLD_DB = -14.0


class FakeCam:
    """CamillaDSP, as the door actually uses it: a graph slot and a fader.

    ``normalize_config_raw`` and ``get_active_config_raw`` both answer the text
    last loaded, so the REAL ``confirm_graph_is_live`` proof runs and passes — a
    double that skipped the proof would let a graph nobody confirmed reach the
    session and the run would not notice.
    """

    def __init__(self, entry_path) -> None:
        self.entry_path = entry_path
        self.loaded: list[str] = []
        self.volume_db = HOUSEHOLD_DB

    async def get_config_file_path(self, best_effort: bool = True) -> str:
        return str(self.entry_path)

    async def set_active_config_raw(
        self, text: str, best_effort: bool = True, duck: bool = True,
    ) -> bool:
        self.loaded.append(text)
        return True

    async def normalize_config_raw(self, text: str, best_effort: bool = True) -> str:
        return text

    async def get_active_config_raw(self, best_effort: bool = True) -> str:
        return self.loaded[-1]

    async def get_volume_db(self, best_effort: bool = True) -> float:
        return self.volume_db

    async def set_volume_db(self, db: float, best_effort: bool = True) -> bool:
        self.volume_db = float(db)
        return True


def _args(*argv: str):
    return build_parser().parse_args(["--kind", MEASURE_KIND_CANDIDATE, *argv])


# --------------------------------------------------------------------------- #
# the flag layer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ["--polarity", POLARITY_INVERTED, "--inverted-role", "tweeter"],
        ["--delayed-role", "woofer", "--delay-us", "120"],
        ["--level-matched"],
    ],
)
def test_a_variant_take_refuses_without_a_candidate_id(argv):
    """C3: every variant axis, and each one alone is enough to require the id.

    Parametrized rather than three tests because the rule is about the SET: a
    check that named only the polarity would let a delayed or level-matched
    take bank unfindable, which is the same defect through a different flag.
    """
    with pytest.raises(MeasureFlagError) as caught:
        spec_from_args(_args(*argv))

    assert caught.value.reason == REFUSE_CANDIDATE_ID_REQUIRED


@pytest.mark.parametrize(
    "argv",
    [
        ["--polarity", POLARITY_INVERTED, "--inverted-role", "tweeter"],
        ["--delayed-role", "woofer", "--delay-us", "120"],
        ["--level-matched"],
    ],
)
def test_the_same_variant_builds_a_spec_once_it_is_named(argv):
    """The CONTROL for the refusal above: the axes are otherwise buildable.

    Without it the refusal test would pass against a flag layer that refused
    every variant outright, which is a different door than the one shipped.
    """
    spec = spec_from_args(_args(*argv, "--candidate-id", "null_a1"))

    assert spec.candidate_id == "null_a1"
    assert (
        spec.polarity == POLARITY_INVERTED
        or spec.delayed_role
        or spec.level_matched
    )


def test_an_ordinary_take_needs_no_candidate_id():
    """The unlabelled walk is the common case and pays nothing for the rule."""
    spec = spec_from_args(_args("--position", "-30"))

    assert spec.candidate_id == ""
    assert spec.positions == (-30,)


def test_a_second_position_is_refused_because_nothing_moves_the_microphone():
    """B3: N bearings from ONE placement would bank N poses nobody moved to.

    The engine walks bearings because the wizard prompts a mover between them.
    This door prompts nobody, so a second ``--position`` would play two stimuli
    back-to-back at one placement and label them −30° and +30° — the silent
    wrong measurement, and one no downstream reader could detect. A walk is N
    runs of the command.
    """
    with pytest.raises(MeasureFlagError) as caught:
        spec_from_args(_args("--position", "-30", "--position", "30"))

    assert caught.value.reason == REFUSE_ONE_POSITION_PER_RUN


def test_the_engine_s_own_refusals_reach_the_operator_as_input_errors():
    """A spec the engine will not build is a flag problem, reported as one.

    The detail is the ENGINE's sentence, not a second copy of the rule: a door
    that re-worded ``MeasureSpec``'s refusals would be free to describe a
    different rule than the one that fired.
    """
    with pytest.raises(MeasureFlagError) as caught:
        spec_from_args(
            _args("--polarity", POLARITY_INVERTED, "--candidate-id", "null_a1")
        )

    assert caught.value.reason == REFUSE_SPEC_INVALID
    assert caught.value.detail


@pytest.mark.parametrize(
    "argv,reason",
    [
        (["--level-matched"], REFUSE_CANDIDATE_ID_REQUIRED),
        (["--specs", "/nonexistent/specs.json"], REFUSE_SPECS_UNREADABLE),
    ],
)
def test_the_flag_refusal_exits_as_an_input_error(argv, reason, capsys):
    """Exit 2 and the shared refusal document, with nothing beside it.

    Ungated: the document is what a script branches on, so a flag deciding
    whether it appears would make the refusal readable only by luck.
    """
    code = measure.main(["--kind", MEASURE_KIND_CANDIDATE, *argv])

    assert code == EXIT_UNREADABLE
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "unreadable", "reason": reason, "detail": payload["detail"],
    }
    assert isinstance(payload["detail"], str) and payload["detail"]


def _stub_box_reads(monkeypatch, *, preview_status: str) -> None:
    """The global reads ``read_box_declaration`` makes before the gate."""
    from jasper import output_topology
    from jasper.active_speaker import crossover_preview, design_draft

    monkeypatch.setattr(
        output_topology, "load_output_topology", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        output_topology, "topology_is_subless_passive_mains", lambda _t: False
    )
    monkeypatch.setattr(design_draft, "load_design_draft", lambda **kw: {})
    monkeypatch.setattr(
        crossover_preview,
        "load_crossover_preview",
        lambda **kw: {"status": preview_status},
    )


def test_the_door_carries_the_conductor_gates_rather_than_a_second_opinion(
    monkeypatch,
):
    """``read_box_declaration`` consumes ``resolve_conductor_context`` — the
    gates AND the sentence. It used to re-derive a subset of them, so a box the
    wizard refused could still be measured from the command line."""
    from jasper.active_speaker.crossover_v2 import conductor_context
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused

    _stub_box_reads(monkeypatch, preview_status="ready_for_protected_staging")
    monkeypatch.setattr(conductor_context, "conductor_status", dict)

    def _refuse(_status):
        raise CrossoverV2Refused("the tweeter target is missing")

    monkeypatch.setattr(conductor_context, "resolve_conductor_context", _refuse)

    with pytest.raises(measure.BoxNotMeasurable) as excinfo:
        measure.read_box_declaration()

    assert excinfo.value.reason == measure.REFUSE_BOX_NOT_READY
    assert excinfo.value.detail == "the tweeter target is missing"


def test_a_preview_that_is_not_staged_refuses_before_the_gate_is_reached(
    monkeypatch,
):
    """The door measures the box as DECLARED. ``resolve_conductor_context``
    runs ``ensure_crossover_preview_ready``, which REGENERATES a stale preview
    — setup under a measurement's name — so an unstaged preview has to refuse
    before the gate, not be repaired by it."""
    from jasper.active_speaker.crossover_v2 import conductor_context

    _stub_box_reads(monkeypatch, preview_status="stale")
    monkeypatch.setattr(
        conductor_context,
        "conductor_status",
        lambda: pytest.fail("the session-open gate must not be reached"),
    )

    with pytest.raises(measure.BoxNotMeasurable) as excinfo:
        measure.read_box_declaration()

    assert excinfo.value.reason == measure.REFUSE_BOX_NOT_READY


# --------------------------------------------------------------------------- #
# one whole run
# --------------------------------------------------------------------------- #


def _declaration() -> BoxDeclaration:
    from jasper.active_speaker.branch_chain import sections_by_role

    preset = _preset()
    return BoxDeclaration(
        topology=mono_output_topology(),
        preset=preset,
        safety_profile={},
        role_targets={"woofer": "target-w", "tweeter": "target-t"},
        declared_sensitivities={},
        playback_device="plughw:CARD=Loopback,DEV=0",
        # Every driver role, because the emitter refuses a partial protection
        # map: a measurement graph that protected one branch and not the other
        # is exactly what the confirmed-protection input exists to prevent.
        protection_sections_by_role=sections_by_role(preset.crossover_regions),
        roles_bands=tuple(_roles()),
        caps_dbfs={"woofer": 0.0, "tweeter": -30.0},
        sweep_duration_limits_s={"woofer": 4.0, "tweeter": 4.0},
        fc_hz=1800.0,
        session_volume_db=-20.0,
    )


#: What the wired half's own minter puts on an answer, in its shape.
CAPTURE_INTEGRITY = {"encoded_frames": 192000, "zero_run_count": 0, "xruns": 0}
CAPTURE_DEVICE = {"label": "UMIK-2 (card1)", "wired": True, "channel_selected": 0}
CAPTURE_SETUP = {"calibration": {"mode": "stored", "calibration_id": "cal-1"}}


class _Answer:
    wav = b""
    capture_integrity = CAPTURE_INTEGRITY
    device = CAPTURE_DEVICE
    setup = CAPTURE_SETUP


class _Capture:
    """The host's capture half, minus the microphone.

    Rolls around the play exactly as the wired one does, so the transaction
    still decides ``played`` from what it OBSERVED, and hands back a
    bundle-relative path so the banked record carries a real pointer.
    ``take_answer`` is take-and-CLEAR like the real one, which is what the
    record annotation relies on.
    """

    def __init__(self) -> None:
        self.arounds = 0
        self._pending: list[_Answer] = []

    async def around(self, play, *, program) -> str:
        self._pending.clear()
        self.arounds += 1
        await play()
        self._pending.append(_Answer())
        return CAPTURE_RELPATH

    def take_answer(self):
        return self._pending.pop() if self._pending else None


@pytest.fixture
def speaker(tmp_path, monkeypatch):
    """A whole measurable speaker: real everything but the DSP and the audio."""
    from jasper.active_speaker import bundles
    from jasper.active_speaker.crossover_v2 import program_transaction
    from jasper import measurement_window as coordinator
    from jasper.volume_owner import VolumeOwner, install_volume_owner
    from jasper.web import correction_crossover_v2_wired as wired

    class _NoWindow:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    entry = tmp_path / "entry.yml"
    entry.write_text("devices: {}\n", encoding="utf-8")
    cam = FakeCam(entry)
    played: list[Any] = []
    capture = _Capture()

    async def _play_program(program, **_seams: Any) -> Any:
        # The seam under the seam: everything above it — the door, the plan's
        # readiness assertion, the graph install, the capture roll and the bank
        # — is the production path.
        played.append(program)
        return object()

    async def _compose(**_kwargs: Any) -> ProgramForStimulus:
        return ProgramForStimulus(program=object(), seams={})

    real_open_bundle = bundles.open_bundle

    monkeypatch.setattr(coordinator, "measurement_window", lambda **kw: _NoWindow())
    monkeypatch.setattr(program_transaction, "play_program", _play_program)
    monkeypatch.setattr(measure, "_bind_compose", lambda **kw: _compose)
    monkeypatch.setattr(measure, "read_box_declaration", _declaration)
    monkeypatch.setattr(wired, "WiredStimulusCapture", lambda **kw: capture)
    monkeypatch.setattr(
        "jasper.audio_measurement.wired_capture.resolve_wired_mic",
        lambda **kw: object(),
    )
    monkeypatch.setattr("jasper.camilla.primary_controller", lambda: cam)
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)
    monkeypatch.setattr(
        bundles,
        "open_bundle",
        lambda topology, **kw: real_open_bundle(
            topology, calibration_id="", sessions_dir=tmp_path / "sessions",
        ),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        tmp_path / "session_volume.json",
    )
    # The LOCK, not the config dir the CLI resolves: `web_commissioning`
    # re-exports `DEFAULT_CAMILLA_CONFIG_DIR` at module scope, so patching that
    # constant is captured permanently by whichever module imports it first
    # under the patch. This leaf is read at call time and leaks into nothing.
    monkeypatch.setattr(
        "jasper.dsp_apply.CANONICAL_DSP_WRITER_LOCK_PATH",
        tmp_path / ".dsp_apply.lock",
    )
    install_volume_owner(
        VolumeOwner(
            set_fader_db=lambda db: cam.set_volume_db(db, best_effort=True),
            get_fader_db=lambda: cam.get_volume_db(best_effort=True),
        )
    )
    try:
        yield {"cam": cam, "played": played, "capture": capture}
    finally:
        install_volume_owner(None)


def test_one_run_opens_measures_banks_and_puts_the_speaker_back(speaker, capsys):
    """The door's whole promise, end to end and asserted on structured fields.

    A record id proves the bank happened THROUGH the real store; the entry
    graph and the household fader prove the give-back happened after it. Both
    halves matter: a door that banked and stranded the speaker would pass a
    test that only counted records.
    """
    cam = speaker["cam"]

    code = measure.main(["--kind", MEASURE_KIND_BASELINE, "--position", "0"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == EXIT_OK
    assert payload["status"] == "measured"
    assert payload["n_takes"] == 1
    assert len(payload["record_ids"]) == 1
    assert len(payload["specs"]) == 1
    assert payload["specs"][0]["graph_fingerprint"]
    assert payload["specs"][0]["n_takes"] == 1
    assert payload["specs"][0]["incidents"] == []
    # stdout IS the answer: the bank verb, spelled with this run's own bundle.
    assert payload["next"] == f"jasper-round bank {payload['bundle_dir']}"
    assert payload["bundle_dir"] in captured.err
    assert speaker["capture"].arounds == 1
    assert len(speaker["played"]) == 1
    # Given back: the entry graph is what the DSP last loaded, and the fader is
    # off the measurement level.
    assert cam.loaded[-1] == (cam.entry_path).read_text()
    assert cam.volume_db == pytest.approx(HOUSEHOLD_DB)


def test_a_level_ladder_plays_every_rung_against_one_open_session(speaker, capsys):
    """R-4's axis: the rungs move the STIMULUS, never the claim.

    Two rungs at one pose is two banked records and ONE graph install per
    stimulus against one held claim — the property ruling S8's level recipe
    turns on, and the reason a ladder is not a second session.
    """
    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--level-dbfs", "-12", "--level-dbfs", "-18",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["n_takes"] == 2
    banked = [
        json.loads((Path(payload["bundle_dir"]) / ARTIFACTS / rid).read_text())
        for rid in payload["record_ids"]
    ]
    assert [r["stimulus_dbfs"] for r in banked] == [-12.0, -18.0]
    assert all(r["level_db"] == pytest.approx(-20.0) for r in banked)


def test_a_box_with_no_microphone_refuses_before_it_takes_the_speaker(
    speaker, monkeypatch, capsys,
):
    """Nothing would record the stimulus, so nothing plays and nothing is held.

    Refused rather than played-and-not-banked: a sweep the household hears for
    evidence nobody keeps is the dishonest half of ruling S10's shape.
    """
    monkeypatch.setattr(
        "jasper.audio_measurement.wired_capture.resolve_wired_mic", lambda **kw: None,
    )

    code = measure.main(["--kind", MEASURE_KIND_BASELINE])

    assert code == EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == REFUSE_NO_MIC
    assert speaker["cam"].loaded == []
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)


def test_a_refused_run_does_not_abandon_the_live_session_s_bundle(
    speaker, tmp_path, monkeypatch, capsys,
):
    """B2: the interlock must run BEFORE anything destructive, and open_bundle is.

    ``open_bundle``'s first act is to mark every prior ``open`` bundle
    ``abandoned`` — that is how at-most-one-open is maintained — which strips
    the retention protection off whatever session owns it. Opening the bundle
    before the door's interlock therefore did that to a LIVE wizard session and
    THEN got refused: destructive on the one path whose whole promise is that it
    changes nothing.

    Driven through the interlock rather than a unit call, because the ordering
    is the property and only the real sequence has one.
    """
    from jasper.active_speaker.bundles import open_bundle

    sessions = tmp_path / "sessions"
    live = open_bundle(
        mono_output_topology(), calibration_id="", sessions_dir=sessions,
    )
    assert live is not None
    info = Path(str(live["bundle_dir"])) / "info.json"
    assert json.loads(info.read_text())["state"] == "open"

    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.live_measurement_session",
        lambda **kw: "a measurement session is already running",
    )
    code = measure.main(["--kind", MEASURE_KIND_BASELINE])

    assert code == EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == (
        door_module.REFUSE_SESSION_LIVE
    )
    assert json.loads(info.read_text())["state"] == "open", (
        "the refused run abandoned the live session's bundle"
    )
    assert [p.name for p in sessions.iterdir()] == [Path(str(live["bundle_dir"])).name]


def test_a_walk_that_stops_part_way_still_names_what_it_banked(
    speaker, monkeypatch, capsys,
):
    """B4: ``k`` takes on disk and an exit code is not a result — the ids are.

    A mid-walk graph loss is not a programming error and it arrives after
    earlier rungs have already banked. A traceback would exit with no JSON at
    all, leaving those takes under names only a directory scan could recover —
    which is the one thing this door exists to spare a reader.
    """
    from jasper.active_speaker.crossover_v2 import session_graph as graph_mod

    real_install = graph_mod.MeasurementSessionGraph.install
    played = speaker["played"]

    async def _install(self, *args, **kwargs):
        # Keyed on "a stimulus has already played" rather than a call ordinal:
        # the door and the session each prove the graph once at open, and a
        # magic count would silently move if either stopped doing so.
        if played:
            raise graph_mod.SessionGraphError("the measurement graph was stomped")
        return await real_install(self, *args, **kwargs)

    monkeypatch.setattr(graph_mod.MeasurementSessionGraph, "install", _install)

    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--level-dbfs", "-12", "--level-dbfs", "-18",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["reason"] == "interrupted"
    detail = payload["detail"]
    assert detail["reason"] == REFUSE_GRAPH_LOST
    assert len(detail["record_ids"]) == 1, "the banked take was not reported"
    assert detail["bundle_dir"]
    assert detail["stopped_at"]["index"] == 0
    # Still given back: a partial result is not a stranded speaker.
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)


def test_a_give_back_failure_after_a_clean_batch_still_renders_json(
    speaker, monkeypatch, capsys,
):
    """The verified gap: the whole batch measured and banked, then the door's
    own exit could not put the entry graph back. ``TuningSession.close`` and
    the door's own ``finally`` both restore this SAME graph handle on a clean
    exit, OUTSIDE ``_session_scoped_aborts``'s per-spec catch — which only
    wraps the loop ``_measured`` already returned from — so this pins that the
    batch's own record ids are still reported rather than lost to a bare
    traceback.
    """
    from jasper.active_speaker.crossover_v2 import session_graph as graph_mod

    async def _restore(self):
        raise graph_mod.SessionGraphError(
            "the measurement graph was played but the entry graph could not "
            "be restored"
        )

    monkeypatch.setattr(graph_mod.MeasurementSessionGraph, "restore", _restore)

    code = measure.main(["--kind", MEASURE_KIND_BASELINE, "--position", "0"])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["reason"] == "restore_failed"
    detail = payload["detail"]
    assert detail["reason"] == REFUSE_GRAPH_LOST
    assert len(detail["record_ids"]) == 1, "the banked take was not reported"
    assert len(detail["specs"]) == 1


def test_a_banked_take_carries_what_the_microphone_reported(speaker, capsys):
    """N8: a CLI take must not be structurally poorer than a wizard one.

    The engine builds a record from what it knows and knows nothing about a
    microphone; the capture half mints the counters and hands the transaction
    only a path. Without the annotating seam the two never meet, and a grading
    reader could not tell a clean take from one with an xrun in it — so the
    assertion is on the banked BYTES, not on the payload the CLI printed.
    """
    code = measure.main(["--kind", MEASURE_KIND_BASELINE])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    banked = json.loads(
        (Path(payload["bundle_dir"]) / ARTIFACTS / payload["record_ids"][0])
        .read_text()
    )
    assert banked["capture_integrity"] == CAPTURE_INTEGRITY
    assert banked["capture_device"] == CAPTURE_DEVICE
    assert banked["capture_setup"] == CAPTURE_SETUP
    # The engine's own fields are untouched by the annotation.
    assert banked["level_db"] == pytest.approx(-20.0)
    assert banked["wav_path"] == CAPTURE_RELPATH


# --------------------------------------------------------------------------- #
# a batch: N configs against ONE microphone placement
# --------------------------------------------------------------------------- #


def _specs_file(tmp_path: Path, entries: list[dict[str, Any]]) -> str:
    path = tmp_path / "specs.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    "flag",
    [
        ["--position", "0"],
        ["--prompt", "stand at the mark"],
        ["--polarity", POLARITY_INVERTED, "--inverted-role", "tweeter"],
        ["--inverted-role", "tweeter"],
        ["--delayed-role", "woofer", "--delay-us", "120"],
        ["--delay-us", "120"],
        ["--level-matched"],
        ["--level-dbfs", "-12"],
        ["--candidate-id", "null_a1"],
    ],
)
def test_specs_and_the_per_take_flags_are_refused_together(tmp_path, flag):
    """Two sources of truth for one spec, refused rather than merged.

    Parametrized over the whole set because the rule is about the SET: a check
    that named only ``--candidate-id`` would let a polarity typed on the command
    line silently lose to, or silently override, the file. Either way one of the
    two is a lie, and a precedence rule would only decide which.
    """
    argv = ["--kind", MEASURE_KIND_BASELINE, "--specs", _specs_file(tmp_path, [{}]), *flag]

    with pytest.raises(MeasureFlagError) as caught:
        specs_from_args(build_parser().parse_args(argv))

    assert caught.value.reason == REFUSE_SPECS_WITH_TAKE_FLAGS


@pytest.mark.parametrize(
    "second",
    [
        {"positions": [30]},
        {"vertical_deg": 15},
        {"position_axis": "vertical", "positions": []},
        {"positions": [0], "pose_prompts": ["stand by the couch"]},
    ],
)
def test_a_batch_whose_specs_disagree_about_the_pose_is_refused_at_parse(
    tmp_path, second,
):
    """The WHOLE pose — bearing, prompts, axis and elevation are all placement.

    A batch exists because the microphone move is the expensive part, so every
    spec in it measures the SAME placement, and nothing between two specs moves
    or raises the microphone. A differing prompt is a differing placement too:
    the prompt is what the mover was told.
    """
    entries = [{"positions": [0]}, {**second}]

    with pytest.raises(MeasureFlagError) as caught:
        specs_from_args(build_parser().parse_args(
            ["--kind", MEASURE_KIND_BASELINE, "--specs", _specs_file(tmp_path, entries)]
        ))

    assert caught.value.reason == REFUSE_SPECS_MIXED_POSE


def test_an_omitted_bearing_and_an_explicit_zero_are_one_design_axis_pose(
    tmp_path,
):
    """``positions: []`` and ``positions: [0]`` must not read as two placements.

    MeasureSpec's own contract: both spell the design axis. A set keyed on the
    raw tuples would refuse a file whose entries mean the same pose.
    """
    entries = [{"positions": []}, {"positions": [0]}]

    specs = specs_from_args(build_parser().parse_args(
        ["--kind", MEASURE_KIND_BASELINE, "--specs", _specs_file(tmp_path, entries)]
    ))

    assert len(specs) == 2


def test_a_file_entry_that_names_two_bearings_is_refused(tmp_path):
    """One entry, one placement — the flag rule holds through the file.

    ``positions: [0, 30]`` in one entry would play two stimuli from wherever
    the microphone already is and bank a pose nothing moved to, exactly what a
    second ``--position`` is refused for.
    """
    entries = [{"positions": [0, 30]}]

    with pytest.raises(MeasureFlagError) as caught:
        specs_from_args(build_parser().parse_args(
            ["--kind", MEASURE_KIND_BASELINE, "--specs", _specs_file(tmp_path, entries)]
        ))

    assert caught.value.reason == REFUSE_ONE_POSITION_PER_RUN


@pytest.mark.parametrize(
    "unlabelled",
    [
        {},
        # Whitespace only: trimmed at parse, so it cannot pass as a truthy label.
        {"candidate_id": "   "},
    ],
)
def test_every_spec_in_the_file_needs_its_own_candidate_id(tmp_path, unlabelled):
    """The candidate-id rule holds per ENTRY, and a blank label is no label.

    The second entry is a variant with no usable label, which is the take that
    would bank unfindable — and in a batch it would sit beside a labelled
    sibling it could never be told apart from.
    """
    entries = [
        {"candidate_id": "null_a1", "polarity": POLARITY_INVERTED,
         "inverted_role": "tweeter"},
        {"polarity": POLARITY_INVERTED, "inverted_role": "woofer", **unlabelled},
    ]

    with pytest.raises(MeasureFlagError) as caught:
        specs_from_args(build_parser().parse_args(
            ["--kind", MEASURE_KIND_BASELINE, "--specs", _specs_file(tmp_path, entries)]
        ))

    assert caught.value.reason == REFUSE_CANDIDATE_ID_REQUIRED


@pytest.mark.parametrize(
    "entry",
    [
        {"positions": 30},
        {"positions": "030"},
        {"pose_prompts": "one prompt"},
        {"pose_prompts": [7]},
        {"level_matched": "false"},
        {"candidate_id": 7},
        {"delayed_role": "woofer", "delay_us": True},
        {"delayed_role": "woofer", "delay_us": "120"},
        {"level_ladder_dbfs": ["-12"]},
        {"level_ladder_dbfs": [float("nan")]},
    ],
)
def test_a_file_entry_with_untyped_fields_is_refused_at_parse(tmp_path, entry):
    """Raw JSON gets argparse's typing, as the same typed refusal.

    Each of these is a value no flag could ever produce — a bare string where
    an array belongs, a truthy string for a boolean, a numeric label, a
    non-finite rung — and each once crashed outside the typed refusal or flowed
    through as a silently different measurement.
    """
    with pytest.raises(MeasureFlagError) as caught:
        specs_from_args(build_parser().parse_args(
            ["--kind", MEASURE_KIND_BASELINE,
             "--specs", _specs_file(tmp_path, [entry])]
        ))

    assert caught.value.reason == REFUSE_SPEC_INVALID


def test_a_batch_measures_every_spec_against_one_open_session(
    speaker, tmp_path, capsys,
):
    """The capability the batch exists for: N configs, ONE microphone move.

    Three variants of one pose, banked under three labels a reader can select
    apart. The session is opened once — the entry graph goes back exactly once
    at the end — which is what makes the batch cheaper than three invocations
    rather than merely shorter to type.
    """
    entries = [
        {"candidate_id": "plain"},
        {"candidate_id": "inv_tw", "polarity": POLARITY_INVERTED,
         "inverted_role": "tweeter"},
        {"candidate_id": "dly_wf", "delayed_role": "woofer", "delay_us": 120.0},
    ]

    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--specs", _specs_file(tmp_path, entries),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert [s["candidate_id"] for s in payload["specs"]] == [
        "plain", "inv_tw", "dly_wf",
    ]
    assert payload["n_takes"] == 3
    assert all(s["n_takes"] for s in payload["specs"])
    # Three specs, three VARIANT graphs: the fingerprint is per spec, and one
    # value could not name them all.
    fingerprints = [s["graph_fingerprint"] for s in payload["specs"]]
    assert all(fingerprints)
    assert len(set(fingerprints)) == 3
    # ONE session: the entry graph is restored once, at the end.
    cam = speaker["cam"]
    entry = cam.entry_path.read_text()
    assert cam.loaded[-1] == entry
    assert cam.loaded.count(entry) == 1


def test_a_spec_scoped_refusal_discloses_and_the_batch_carries_on(
    speaker, monkeypatch, tmp_path, capsys,
):
    """Arm B of the scope split: one refused take is not the placement's fault.

    An admission refusal is a property of ONE stimulus — the play transaction
    turns it into a typed ``incident`` and never raises — so the batch measures
    the next spec. Giving the whole placement back over one refused take would
    throw away the microphone move that the batch exists to amortize.
    """
    from jasper.active_speaker.crossover_v2 import program_transaction
    from jasper.active_speaker.program_admission import ProgramAdmission
    from jasper.active_speaker.program_playback import ProgramPlaybackRefused

    played = speaker["played"]
    real_play = program_transaction.play_program

    async def _play(program, **seams: Any) -> Any:
        if not played:
            played.append(program)
            raise ProgramPlaybackRefused(ProgramAdmission(
                program_id="p", phase="measure", session_volume_db=-20.0,
                segments=(), channels=(), refusals=(),
            ))
        return await real_play(program, **seams)

    monkeypatch.setattr(program_transaction, "play_program", _play)

    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--specs", _specs_file(
            tmp_path, [{"candidate_id": "refused"}, {"candidate_id": "kept"}],
        ),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK, "a spec-scoped refusal must not end the batch"
    refused, kept = payload["specs"]
    assert refused["n_takes"] == 0
    # The one fact nothing else records: a refused stimulus banks NO record,
    # so its sentence exists only here.
    assert refused["incidents"] == [
        program_transaction.STIMULUS_ADMISSION_REFUSED
    ]
    assert kept["n_takes"], "the batch did not carry on to the next spec"


def test_a_session_scoped_failure_aborts_the_batch_and_names_where(
    speaker, monkeypatch, tmp_path, capsys,
):
    """Arm A: the speaker stopped being held, so the rest would be guesswork.

    The remaining specs would measure through a graph nobody can re-prove, so
    the batch stops — and says WHICH spec it stopped at, because in a batch the
    banked ids alone cannot locate the boundary and the next attempt needs to
    know what is still owed.
    """
    from jasper.active_speaker.crossover_v2 import session_graph as graph_mod

    real_install = graph_mod.MeasurementSessionGraph.install
    played = speaker["played"]

    async def _install(self, *args, **kwargs):
        if played:
            raise graph_mod.SessionGraphError("the measurement graph was stomped")
        return await real_install(self, *args, **kwargs)

    monkeypatch.setattr(graph_mod.MeasurementSessionGraph, "install", _install)

    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--specs", _specs_file(
            tmp_path, [{"candidate_id": "first"}, {"candidate_id": "second"}],
        ),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    detail = payload["detail"]
    assert detail["reason"] == REFUSE_GRAPH_LOST
    assert detail["stopped_at"]["candidate_id"] == "second"
    assert detail["stopped_at"]["index"] == 1
    assert len(detail["record_ids"]) == 1
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)


def test_an_evidence_store_failure_aborts_as_the_same_partial_result(
    speaker, monkeypatch, tmp_path, capsys,
):
    """The bank stopped taking writes: later specs would play and keep nothing.

    A store failure lands AFTER earlier specs banked, so a traceback would exit
    with no JSON while their takes sit on disk unnamed. It aborts through the
    same refusal document the other session-scoped failures use — one shape,
    with the banked ids and the zero-based index of the spec in flight.
    """
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
        CommissioningEvidenceStoreError,
        CommissioningEvidenceStoreErrorCode,
    )

    real_publish = CommissioningEvidenceStore.publish_json_artifact
    banked: list[str] = []

    def _publish(self, relative_path, payload):
        if banked:
            raise CommissioningEvidenceStoreError(
                CommissioningEvidenceStoreErrorCode.PERSIST_FAILED,
                "the bundle volume went away",
            )
        banked.append(relative_path)
        return real_publish(self, relative_path, payload)

    monkeypatch.setattr(
        CommissioningEvidenceStore, "publish_json_artifact", _publish,
    )

    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--specs", _specs_file(
            tmp_path, [{"candidate_id": "first"}, {"candidate_id": "second"}],
        ),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    detail = payload["detail"]
    assert detail["reason"] == measure.REFUSE_STORE_LOST
    assert detail["stopped_at"]["index"] == 1
    assert len(detail["record_ids"]) == 1
    # Still given back: a partial result is not a stranded speaker.
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)
