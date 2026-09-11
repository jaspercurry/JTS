# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CLI results through the real session and record store, with fake hardware."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from jasper.active_speaker.crossover_v2 import door as door_module
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec

from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    POLARITY_INVERTED,
)
from jasper.active_speaker.crossover_v2.program_transaction import (
    ProgramForStimulus, StimulusCaptureStopped,
)
from jasper.audio_measurement.playback import PlaybackObservation
from jasper.audio_measurement.calibration import resolve_mic_sensitivity
from jasper.audio_measurement.wired_capture import WiredSplMonitor
from jasper.active_speaker.round_bank import bank_round
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
from tests.crossover_v2_fixtures import FakeCam, _preset, _roles

ARTIFACTS = "evidence/v1/artifacts"
CAPTURE_RELPATH = "captures/summed/take.wav"
HOUSEHOLD_DB = -14.0


def _args(*argv: str):
    return build_parser().parse_args(["--kind", MEASURE_KIND_CANDIDATE, *argv])


def test_explicit_lf_band_and_spl_ceiling_are_part_of_measure_spec():
    spec = spec_from_args(_args(
        "--graph-scope", "speaker_tune",
        "--sweep-band-hz", "20", "20000",
        "--sweep-s", "1.5",
        "--spl-ceiling-db-spl", "80",
    ))
    assert spec.sweep_band_hz == (20.0, 20_000.0)
    assert spec.sweep_s == 1.5
    assert spec.spl_ceiling_db_spl == 80.0
    action, = [a for a in build_parser()._actions if a.dest == "spl_ceiling_db_spl"]
    assert action.help


def test_direct_driver_capture_refuses_lf_summed_band_override():
    with pytest.raises(MeasureFlagError) as caught:
        spec_from_args(_args("--sweep-band-hz", "20", "20000"))
    assert caught.value.reason == REFUSE_SPEC_INVALID


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
    wav_path = CAPTURE_RELPATH
    wav_sha256 = "test-capture-digest"
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
    from jasper.active_speaker.crossover_v2 import wired_stimulus as wired

    class _NoWindow:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    entry = tmp_path / "entry.yml"
    entry.write_text("devices: {}\n", encoding="utf-8")
    cam = FakeCam(entry, volume_db=HOUSEHOLD_DB)
    played: list[Any] = []
    capture = _Capture()
    capture_factory = Mock(return_value=capture)

    async def _play_program(program, **_seams: Any) -> Any:
        # The seam under the seam: everything above it — the door, the plan's
        # readiness assertion, the graph install, the capture roll and the bank
        # — is the production path.
        played.append(program)
        return SimpleNamespace(playback=SimpleNamespace(cleanup_state="not_needed", returncode=0))

    async def _compose(**_kwargs: Any) -> ProgramForStimulus:
        return ProgramForStimulus(program=object(), seams={})

    real_open_bundle = bundles.open_bundle

    def _capture(**kwargs):
        capture.read_loudness_volume_db = kwargs["read_loudness_volume_db"]
        return capture

    monkeypatch.setattr(coordinator, "measurement_window", lambda **kw: _NoWindow())
    monkeypatch.setattr(program_transaction, "play_program", _play_program)
    monkeypatch.setattr(measure, "_bind_compose", lambda **kw: _compose)
    monkeypatch.setattr(measure, "read_box_declaration", _declaration)
    capture_factory.side_effect = _capture
    monkeypatch.setattr(wired, "WiredStimulusCapture", capture_factory)
    monkeypatch.setattr(
        "jasper.audio_measurement.wired_capture.resolve_wired_mic",
        lambda **kw: SimpleNamespace(model_key="minidsp_umik2", model_label="UMIK-2"),
    )
    # This speaker's microphone is a stand-in, so it carries no calibration a
    # run could scale dB SPL by. Stated, not inherited from whatever the
    # developer's own box has stored.
    monkeypatch.setattr("jasper.audio_measurement.household_mic.resolved_household_mic", lambda: None)
    monkeypatch.setattr(
        "jasper.audio_measurement.calibration.resolve_mic_sensitivity",
        lambda **kw: None,
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
        yield {"cam": cam, "played": played, "capture": capture, "capture_factory": capture_factory}
    finally:
        install_volume_owner(None)


def test_one_run_opens_measures_banks_and_puts_the_speaker_back(speaker, capsys, tmp_path):
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
    assert payload["specs"][0]["playback"][0] == {
        "emission": "completed", "failure_code": None,
        "cleanup_state": "not_needed", "returncode": 0,
    }
    # stdout IS the answer: the bank verb, spelled with this run's own bundle.
    assert payload["next"] == f"jasper-round bank {payload['bundle_dir']}"
    assert payload["bundle_dir"] in captured.err
    assert speaker["capture"].arounds == 1
    assert len(speaker["played"]) == 1
    # Given back: the entry graph is what the DSP last loaded, and the fader is
    # off the measurement level.
    assert cam.loaded[-1] == (cam.entry_path).read_text()
    assert cam.volume_db == pytest.approx(HOUSEHOLD_DB)
    bundle = Path(payload["bundle_dir"])
    assert json.loads((bundle / "info.json").read_text())["state"] == "closed"
    banked = bank_round(bundle, campaign_root=tmp_path / "campaigns")
    record = Path(ARTIFACTS) / payload["record_ids"][0]
    assert (banked.path / "bundle" / bundle.name / record).read_bytes() == (bundle / record).read_bytes()


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
    assert detail["stopped_at"]["index"] == 1
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


@pytest.mark.parametrize("argv", [
    [],
    ["--graph-scope", "speaker_tune", "--sweep-band-hz", "20", "20000",
     "--sweep-s", "1.5", "--spl-ceiling-db-spl", "80", "--level-dbfs", "-12"],
    ["--polarity", POLARITY_INVERTED, "--inverted-role", "tweeter",
     "--delayed-role", "woofer", "--delay-us", "120", "--position", "-30",
     "--prompt", "stand left", "--candidate-id", "null_a1"],
])
def test_a_spec_survives_its_own_json_shape_unchanged(argv: list[str]) -> None:
    """``MeasureSpec`` owns the document both the batch file and the angle-walk
    spool are written in, so every field it has round-trips through JSON: a
    field added to the class travels without a second door learning its name.
    """
    spec = spec_from_args(_args(*argv))
    document = json.loads(json.dumps(spec.to_dict()))

    assert MeasureSpec.from_mapping(document) == spec


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
        ["--spl-ceiling-db-spl", "70"],
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
        {"vertical_deg": 2.5},
        {"positoins": [0]},
    ],
)
def test_a_file_entry_with_untyped_fields_is_refused_at_parse(tmp_path, entry):
    """Raw JSON gets argparse's typing, as the same typed refusal.

    Each of these is a value no flag could ever produce — a bare string where
    an array belongs, a truthy string for a boolean, a numeric label, a
    non-finite rung, a misspelled key — and each once crashed outside the typed
    refusal or flowed through as a silently different measurement.
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


@pytest.mark.parametrize("spec_count", [1, 2])
def test_incomplete_measurements_refuse_and_preserve_partial_results(
    speaker, monkeypatch, tmp_path, capsys, spec_count,
):
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
            tmp_path, [{"candidate_id": "refused"}, {"candidate_id": "kept"}][:spec_count],
        ),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["reason"] == measure.REFUSE_INCOMPLETE
    report = payload["detail"]
    bundle = Path(report["bundle_dir"])
    assert json.loads((bundle / "info.json").read_text())["state"] == "closed"
    assert report["n_takes"] == spec_count - 1
    assert len(report["record_ids"]) == spec_count - 1
    assert len(report["specs"]) == spec_count
    refused = report["specs"][0]
    assert refused["n_takes"] == 0
    assert refused["incidents"] == [
        program_transaction.STIMULUS_ADMISSION_REFUSED
    ]
    assert refused["playback"][0]["emission"] == "not_started"
    if spec_count == 2:
        assert report["specs"][1]["n_takes"] == 1
        assert report["specs"][1]["incidents"] == []
        banked = bank_round(bundle, campaign_root=tmp_path / "campaigns")
        assert (banked.path / "bundle" / bundle.name / ARTIFACTS / report["record_ids"][0]).is_file()
    cam = speaker["cam"]
    assert cam.loaded[-1] == cam.entry_path.read_text()
    assert cam.volume_db == pytest.approx(HOUSEHOLD_DB)


@pytest.mark.parametrize("setup", ["graph", "compose"])
def test_a_session_scoped_failure_aborts_the_batch_and_names_where(
    speaker, monkeypatch, tmp_path, capsys, setup,
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
        if played and setup == "graph":
            raise graph_mod.SessionGraphError("the measurement graph was stomped")
        return await real_install(self, *args, **kwargs)

    monkeypatch.setattr(graph_mod.MeasurementSessionGraph, "install", _install)

    bind = measure._bind_compose

    def bind_compose(**kwargs):
        original = bind(**kwargs)

        async def compose(**fields):
            if played and setup == "compose":
                import asyncio
                raise asyncio.CancelledError()
            return await original(**fields)

        return compose

    monkeypatch.setattr(measure, "_bind_compose", bind_compose)

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
    assert detail["reason"] == (REFUSE_GRAPH_LOST if setup == "graph" else measure.REFUSE_CANCELLED)
    assert detail["playback"]["emission"] == "not_started"
    assert detail["stopped_at"]["candidate_id"] == "second"
    assert detail["stopped_at"]["index"] == 2
    assert len(detail["record_ids"]) == 1
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)


@pytest.mark.parametrize("reason", ["spl_ceiling_exceeded", "wired_capture_failed"])
def test_capture_stop_ends_ladder_and_batch_and_restores_tune(
    speaker, monkeypatch, tmp_path, capsys, reason,
):
    capture = speaker["capture"]
    original = capture.around
    stopped_playback = PlaybackObservation(
        emission="possible", cleanup_state="killed_and_reaped", returncode=-9,
    )

    async def around(play, *, program):
        if not speaker["played"]:
            return await original(play, program=program)
        await play()
        raise StimulusCaptureStopped(reason, "capture stopped", stopped_playback)

    monkeypatch.setattr(capture, "around", around)
    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--specs", _specs_file(tmp_path, [
            {"level_ladder_dbfs": [-40, -30, -20]},
            {"level_ladder_dbfs": [-10]},
        ]),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert payload["detail"]["reason"] == reason
    assert payload["detail"]["playback"] == stopped_playback.as_dict()
    assert payload["detail"]["stopped_at"]["index"] == 1
    assert len(payload["detail"]["record_ids"]) == 1
    assert len(speaker["played"]) == 2
    assert speaker["cam"].loaded[-1] == speaker["cam"].entry_path.read_text()
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
    assert detail["stopped_at"]["index"] == 2
    assert len(detail["record_ids"]) == 1
    # Still given back: a partial result is not a stranded speaker.
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)


@pytest.mark.parametrize("scope", ["drivers", "base", "speaker_tune", "candidate"])
def test_flags_and_batch_defaults_select_the_same_graph_scope(tmp_path, scope):
    args = ["--graph-scope", scope]
    if scope == "candidate":
        args += ["--candidate-id", "banked-candidate"]
    spec = spec_from_args(_args(*args))
    assert spec.graph_scope == scope
    batch = tmp_path / "specs.json"
    batch.write_text(json.dumps([{"candidate_id": spec.candidate_id}]))
    batch_args = _args("--graph-scope", scope, "--specs", str(batch))
    assert specs_from_args(batch_args)[0].graph_scope == scope


@pytest.mark.parametrize("available", [True, False])
def test_cli_carries_only_a_resolved_stored_microphone_reference(monkeypatch, available):
    from types import SimpleNamespace
    from jasper.audio_measurement import household_mic

    household = SimpleNamespace(model_key="umik-2", provider="manual_upload")
    resolved = (
        (household, SimpleNamespace(calibration_id="stored-calibration"))
        if available else None
    )
    monkeypatch.setattr(household_mic, "resolved_household_mic", lambda: resolved)
    setup = measure._wired_setup_reference()
    if available:
        assert setup == {"calibration": {
            "mode": "stored", "calibration_id": "stored-calibration", "model": "umik-2",
        }}
    else:
        assert setup is None


@pytest.mark.parametrize("source,volume,reason", [
    ("household", None, ""),
    ("household", -30, ""), ("household", -12, ""), ("household", 0, ""),
    ("serial", -12, ""),
    ("household_serial", -12, ""), ("curve_only_serial", -12, ""),
    ("unresolved_serial", -12, measure.REFUSE_VOLUME_REQUIRES_SPL_WATCH),
    ("missing", -30, measure.REFUSE_VOLUME_REQUIRES_SPL_WATCH),
    ("missing", 0, measure.REFUSE_VOLUME_REQUIRES_SPL_WATCH),
    ("curve_only", -12, measure.REFUSE_VOLUME_REQUIRES_SPL_WATCH),
    ("wrong_mic", -12, measure.REFUSE_VOLUME_REQUIRES_SPL_WATCH),
    ("household", 1, "measurement_volume_invalid"),
    ("household", -101, "measurement_volume_invalid"),
    ("household", float("nan"), "measurement_volume_invalid"),
])
def test_batch_volume_override_is_watched_banked_and_restored_or_refused(
    speaker, monkeypatch, tmp_path, capsys, source, volume, reason,
):
    cal = tmp_path / "mic.txt"
    cal.write_text("20 0\n20000 0\n" if source.startswith("curve_only") else "Sens Factor =-12.07dB, AGain =18dB\n20 0\n20000 0\n")
    record = SimpleNamespace(raw_path=cal, sign_convention="correction",
                             model="dayton_imm6" if source == "wrong_mic" else "minidsp_umik2")
    serial_cal = tmp_path / "serial.txt"
    serial_cal.write_text("Sens Factor =-10dB, AGain =18dB\n20 0\n20000 0\n")
    serial_record = SimpleNamespace(raw_path=serial_cal, sign_convention="correction")
    monkeypatch.setattr("jasper.audio_measurement.household_mic.resolved_household_mic",
                        lambda: (None, record) if source not in ("serial", "missing") else None)
    monkeypatch.setattr("jasper.audio_measurement.calibration.resolve_mic_sensitivity", resolve_mic_sensitivity)
    monkeypatch.setattr("jasper.audio_measurement.calibration.find_stored_calibration",
                        lambda **kw: None if source == "unresolved_serial" else serial_record)
    argv = ["--kind", MEASURE_KIND_BASELINE]
    if volume is not None:
        argv += [f"--volume-db={volume}"]
    else:
        volume = _declaration().session_volume_db
    if source in ("serial", "household_serial", "curve_only_serial", "unresolved_serial"):
        argv += ["--mic-serial", "test-serial"]
    code = measure.main(argv)
    result = json.loads(capsys.readouterr().out)
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)
    if not reason:
        assert code == EXIT_OK
        monitor = speaker["capture_factory"].call_args.kwargs["spl_monitor"]
        assert isinstance(monitor, WiredSplMonitor)
        assert monitor.ceiling_db_spl == _preset().safety.max_commissioning_level_db_spl
        assert monitor.sensitivity.sens_factor_db == (-10.0 if source in ("serial", "household_serial", "curve_only_serial") else -12.07)
        assert result["spl_monitor"] == "ceiling_85_db_spl"
        assert speaker["cam"].loudness_db == pytest.approx(HOUSEHOLD_DB)
        assert result["measurement_volume_db"] == volume
        assert result["measurement_loudness_volume_db"] == volume
        record = json.loads((Path(result["bundle_dir"]) / ARTIFACTS / result["record_ids"][0]).read_text())
        assert record["level_db"] == volume
        assert record["loudness_volume_db"] == volume
    else:
        assert code == EXIT_REFUSED
        assert result["status"] == "refused"
        assert result["reason"] == reason
        assert not speaker["played"]
        speaker["capture_factory"].assert_not_called()


def test_every_measure_spec_field_is_read_back_by_exactly_one_rule() -> None:
    from jasper.active_speaker.crossover_v2 import measure_spec as ms

    groups = (ms._TRIMMED_STRINGS, ms._ARRAYS, ms._NUMBERS, ms._PASSTHROUGH)
    assert frozenset().union(*groups) == ms._FIELD_NAMES
    assert sum(len(group) for group in groups) == len(ms._FIELD_NAMES)


# --------------------------------------------------------------------------- #
# the plan: a staged walk, and the package every run leaves behind
# --------------------------------------------------------------------------- #


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A writable pending slot for the walk a run takes."""
    from jasper.active_speaker import angle_capture_spool as spool

    spool.set_angle_request_spool_path_for_tests(tmp_path / "angle_request.json")
    try:
        yield spool
    finally:
        spool.set_angle_request_spool_path_for_tests(None)


def _one_pose_walk(**fields: Any) -> Any:
    from jasper.active_speaker import angle_capture as ac

    return ac.AngleCaptureRequest(
        stops=(ac.AngleStop(12, ac.REGIME_SUMMED),), **fields,
    )


@pytest.fixture
def applied_baseline(monkeypatch):
    """An applied Layer-A record matching this speaker, so a SUMMED scope emits.

    A walk's summed stop plays a tuning-layer graph rather than the drivers one,
    and that graph is composed from the profile a human already approved for
    these drivers.
    """
    from jasper.active_speaker import baseline_profile
    from jasper.output_topology import topology_config_fingerprint
    from tests.crossover_v2_fixtures import _fixture_applied_profile

    topology = mono_output_topology()
    applied = _fixture_applied_profile()
    applied["recomposition_snapshot"].update(
        schema_version=1,
        domain="full",
        topology_id=topology.topology_id,
        topology_fingerprint=topology_config_fingerprint(topology),
        playback_device=_declaration().playback_device,
    )
    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda *a, **k: applied,
    )


@pytest.mark.parametrize("source", ["staged", "path"])
def test_a_staged_walk_measures_through_the_same_loop_as_a_spec_batch(
    speaker, staged, applied_baseline, tmp_path, capsys, source,
):
    """The door's second front: an LLM states a walk, the run plays it, and the
    answer is the same document a spec batch answers with.

    ``staged`` consumes the pending slot; a path is the operator's own file and
    is left where it was.
    """
    path = staged.stage_angle_request(_one_pose_walk())
    moved = tmp_path / "walk.json"
    if source == "path":
        moved.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()

    code = measure.main([
        "--request", "staged" if source == "staged" else str(moved),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["n_takes"] == 1
    record = json.loads(
        (Path(payload["bundle_dir"]) / ARTIFACTS / payload["record_ids"][0]).read_text()
    )
    assert record["position_deg"] == 12
    assert staged.staged_angle_request_pending() is False


def test_every_run_leaves_a_package_naming_what_it_did(speaker, capsys, tmp_path):
    """The counts a caller reads back without re-deriving them from the takes —
    written into the bundle, so the run's own answer outlives the terminal."""
    from jasper.active_speaker import plan_run

    code = measure.main([
        "--kind", MEASURE_KIND_BASELINE,
        "--specs", _specs_file(tmp_path, [{"candidate_id": "a"}, {"candidate_id": "b"}]),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    document = json.loads(
        (Path(payload["bundle_dir"]) / payload["plan_result"]).read_text()
    )
    assert document["kind"] == plan_run.PLAN_RESULT_KIND
    assert document["status"] == plan_run.RUN_MEASURED
    assert document["takes_measured"] == payload["n_takes"] == 2
    assert [take["candidate_id"] for take in document["takes"]] == ["a", "b"]
    # No calibration on this speaker's stand-in microphone, so the run says what
    # did NOT watch its level rather than claiming a bound nothing measured.
    assert payload["spl_monitor"] == plan_run.SPL_MONITOR_UNAVAILABLE


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["--request", "staged", "--kind", MEASURE_KIND_BASELINE],
         measure.REFUSE_REQUEST_UNREADABLE),
        (["--request", "staged", "--position", "7"],
         measure.REFUSE_REQUEST_UNREADABLE),
        (["--request", "staged", "--spl-ceiling-db-spl", "70"],
         measure.REFUSE_REQUEST_UNREADABLE),
        (["--request", "staged", "--specs", "unread.json"],
         measure.REFUSE_REQUEST_UNREADABLE),
        (["--request", "staged"], measure.REFUSE_NO_STAGED_REQUEST),
    ],
    ids=["kind", "position", "spl-ceiling-db-spl", "specs", "nothing-staged"],
)
def test_a_walk_states_its_own_takes_so_the_flags_are_refused_beside_it(
    staged, capsys, argv, reason,
):
    code = measure.main(argv)

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_UNREADABLE
    assert payload["reason"] == reason


def test_a_multi_pose_walk_is_refused_because_this_door_moves_nothing(
    staged, capsys,
):
    """S12 again, for the walk shape: N poses through a door that prompts nobody
    would bank N bearings nothing moved to."""
    from jasper.active_speaker import angle_capture as ac

    staged.stage_angle_request(ac.AngleCaptureRequest(stops=(
        ac.AngleStop(0, ac.REGIME_SUMMED), ac.AngleStop(20, ac.REGIME_SUMMED),
    )))

    code = measure.main(["--request", "staged"])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_UNREADABLE
    assert payload["reason"] == REFUSE_ONE_POSITION_PER_RUN


@pytest.mark.parametrize(
    ("stated", "stop", "expected"),
    [
        (None, 85.0, ""),
        (80.0, 85.0, "measure_spl_calibration_required"),
        (90.0, 85.0, "walk_ceiling_above_stop"),
        (80.0, None, "walk_commissioning_stop_unset"),
    ],
    ids=[
        "no-ceiling-disclosed", "stated-ceiling-refused", "above-the-stop",
        "no-box-stop",
    ],
)
@pytest.mark.parametrize("volume_db", [None, 0.0])
def test_the_monitor_bounds_every_run_or_says_why_it_could_not(
    monkeypatch, stated, stop, expected, volume_db,
):
    """The box's commissioning stop bounds every run now, not only one that typed
    a ceiling — and what cannot be enforced is refused or disclosed, never assumed.

    The slugs are the WALK's, shared with the wizard door: one failure reaching
    an operator under two names would send them looking in two places.
    """
    from jasper.active_speaker import plan_run

    if not expected and volume_db is not None:
        expected = measure.REFUSE_VOLUME_REQUIRES_SPL_WATCH
    monkeypatch.setattr("jasper.audio_measurement.household_mic.resolved_household_mic", lambda: None)
    monkeypatch.setattr(
        "jasper.audio_measurement.calibration.resolve_mic_sensitivity",
        lambda **kw: None,
    )
    box = _declaration() if stop is not None else replace(
        _declaration(), preset=SimpleNamespace(safety=SimpleNamespace(max_commissioning_level_db_spl=None)),
    )
    if not expected:
        assert measure._spl_monitor(
            stated, box=box, device=object(), mic_serial=None, volume_db=volume_db,
        ) == (None, plan_run.SPL_MONITOR_UNAVAILABLE)
        return
    with pytest.raises(measure.BoxNotMeasurable) as refused:
        measure._spl_monitor(stated, box=box, device=object(), mic_serial=None, volume_db=volume_db)
    assert refused.value.reason == expected


@pytest.mark.parametrize("ceilings", [(None, 80.0), (80.0, 90.0)])
def test_a_spec_batch_still_refuses_mixed_ceilings(tmp_path, ceilings):
    path = _specs_file(tmp_path, [{"spl_ceiling_db_spl": ceiling} for ceiling in ceilings])
    with pytest.raises(MeasureFlagError) as refused:
        specs_from_args(build_parser().parse_args(["--kind", MEASURE_KIND_BASELINE, "--specs", path]))
    assert refused.value.reason == measure.REFUSE_SPL_CEILINGS_MIXED


def test_a_calibrated_box_watches_the_stop_it_declares(monkeypatch):
    """The other half: a resolvable sensitivity buys a real monitor, watching the
    ceiling the run resolved."""
    from jasper.audio_measurement.wired_capture import WiredSplMonitor

    monkeypatch.setattr(
        measure, "resolved_household_sensitivity", lambda device: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.plan_run.SUPPORTED_MODELS",
        {"umik2": {"capture_channel": 0}},
    )

    monitor, note = measure._spl_monitor(
        None,
        box=_declaration(), device=SimpleNamespace(model_key="umik2"),
        mic_serial=None, volume_db=None,
    )

    assert isinstance(monitor, WiredSplMonitor)
    assert monitor.ceiling_db_spl == _preset().safety.max_commissioning_level_db_spl
    assert note == "ceiling_85_db_spl"


def test_a_graph_install_refusal_exits_with_its_code(speaker, monkeypatch, capsys):
    from jasper.active_speaker.crossover_v2.session_graph import MeasurementSessionGraph
    from jasper.active_speaker.measurement_emit import MeasurementGraphRefused

    async def refuse(*args, **kwargs):
        raise MeasurementGraphRefused("measurement_candidate_room_mismatch", {"candidate": "candidate-1"})

    monkeypatch.setattr(MeasurementSessionGraph, "install", refuse)
    code = measure.main(["--kind", MEASURE_KIND_BASELINE])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_REFUSED
    assert payload["status"] == "refused"
    assert payload["code"] == payload["reason"] == "measurement_candidate_room_mismatch"
    assert payload["detail"] == {"candidate": "candidate-1"}
    assert payload["next_action"]["id"] == "apply_matching_room_layer"
    assert not speaker["played"]
