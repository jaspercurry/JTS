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
    EXIT_INPUT,
    EXIT_OK,
    EXIT_REFUSED,
    REFUSE_CANDIDATE_ID_REQUIRED,
    REFUSE_GRAPH_LOST,
    REFUSE_NO_MIC,
    REFUSE_ONE_POSITION_PER_RUN,
    REFUSE_SPEC_INVALID,
    REFUSE_SPECS_MIXED_POSE,
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


def test_the_flag_refusal_exits_as_an_input_error(capsys):
    """Exit 2 and a machine-readable reason — the code a script branches on."""
    code = measure.main(
        ["--kind", MEASURE_KIND_CANDIDATE, "--level-matched", "--json"]
    )

    assert code == EXIT_INPUT
    assert json.loads(capsys.readouterr().out)["reason"] == (
        REFUSE_CANDIDATE_ID_REQUIRED
    )


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
#: The wired minter's report shape, minus any frame count: this double does not
#: encode the capture the way the minter does, and a claimed count that
#: disagreed with the bytes would make the frame ledger report a loss that
#: belongs to the double rather than to the door.
CAPTURE_INTEGRITY = {"zero_run_count": 0, "xruns": 0}
CAPTURE_DEVICE = {"label": "UMIK-2 (card1)", "wired": True, "channel_selected": 0}
CAPTURE_SETUP = {"calibration": {"mode": "stored", "calibration_id": "cal-1"}}


class _Answer:
    wav = b""
    capture_integrity = CAPTURE_INTEGRITY
    device = CAPTURE_DEVICE
    setup = CAPTURE_SETUP


def _driver_ir(seed: int = 3):
    """A short decaying impulse response — a speaker, near enough to analyse."""
    import numpy as np

    rng = np.random.default_rng(seed)
    ir = rng.normal(0.0, 1.0, 256) * np.exp(-np.arange(256) / 40.0)
    ir[0] += 1.0
    return ir / np.max(np.abs(ir))


def _synthesize(program):
    """The capture the room would have made of ``program``, offset and coloured.

    The same shape ``test_capture_frame_ledger`` synthesises: render the
    program's own PCM, convolve one driver IR through it, and start it late.
    Real enough for the analyser to locate every segment and deconvolve a
    transfer function, which is what makes the crunch assertion mean something.
    """
    import numpy as np
    from scipy.signal import fftconvolve

    from jasper.audio_measurement.program import render_program_pcm

    pcm = render_program_pcm(program)
    mono = pcm.sum(axis=1) if pcm.ndim > 1 else pcm
    body = fftconvolve(mono, _driver_ir())[: mono.size]
    out = np.zeros(3_000 + body.size)
    out[3_000:] = body * 0.4
    return out


class _Capture:
    """The host's capture half, minus the microphone.

    Rolls around the play exactly as the wired one does, so the transaction
    still decides ``played`` from what it OBSERVED, and hands back a
    bundle-relative path so the banked record carries a real pointer.
    ``take_answer`` is take-and-CLEAR like the real one, which is what the
    record annotation relies on.

    It writes a REAL capture WAV, 32-bit like the wired minter's, because the
    door crunches the bytes it just placed: a double that wrote nothing would
    let the crunch fail soft and the curve assertion would pass on an empty
    list forever.
    """

    def __init__(self, bundle_dir: Path) -> None:
        self.bundle_dir = Path(bundle_dir)
        self.arounds = 0
        self._pending: list[_Answer] = []

    async def around(self, play, *, program) -> str:
        import numpy as np
        from scipy.io import wavfile

        self._pending.clear()
        self.arounds += 1
        await play()
        path = self.bundle_dir / CAPTURE_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        captured = _synthesize(program)
        wavfile.write(
            path,
            int(program.sample_rate_hz),
            (np.clip(captured, -1.0, 1.0) * (2**31 - 1)).astype(np.int32),
        )
        self._pending.append(_Answer())
        return CAPTURE_RELPATH

    def take_answer(self):
        return self._pending.pop() if self._pending else None


@pytest.fixture
def speaker(tmp_path, monkeypatch):
    """A whole measurable speaker: real everything but the DSP and the audio."""
    from jasper.active_speaker import bundles
    from jasper.active_speaker.crossover_v2 import program_transaction
    from jasper.correction import coordinator
    from jasper.volume_owner import VolumeOwner, install_volume_owner
    from jasper.web import correction_crossover_v2_wired as wired

    class _NoWindow:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    captures: dict[str, Any] = {}
    played = measure._PlayedProgram()
    entry = tmp_path / "entry.yml"
    entry.write_text("devices: {}\n", encoding="utf-8")
    cam = FakeCam(entry)
    played_programs: list[Any] = []

    async def _play_program(program, **_seams: Any) -> Any:
        # The seam under the seam: everything above it — the door, the plan's
        # readiness assertion, the graph install, the capture roll and the bank
        # — is the production path.
        played_programs.append(program)
        return object()

    def _measure_program():
        """A REAL per-driver MEASURE program — the crunch analyses its schedule."""
        from jasper.audio_measurement.excitation_admission import FrequencyBand
        from jasper.audio_measurement.program import RoleBand, build_measure_program

        return build_measure_program(
            {"woofer": -11.0, "tweeter": -13.0},
            [
                RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
                RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
            ],
            sweep_durations={"woofer": 1.0, "tweeter": 0.8},
            leading_pilot_gains_db=(-22.0, -12.0),
            pilot_duration_s=0.5,
        )

    program = _measure_program()

    async def _compose(**_kwargs: Any) -> ProgramForStimulus:
        played.program = program
        return ProgramForStimulus(program=program, seams={})

    real_open_bundle = bundles.open_bundle

    monkeypatch.setattr(coordinator, "measurement_window", lambda **kw: _NoWindow())
    monkeypatch.setattr(program_transaction, "play_program", _play_program)
    monkeypatch.setattr(measure, "_bind_compose", lambda **kw: _compose)
    monkeypatch.setattr(measure, "_PlayedProgram", lambda: played)
    monkeypatch.setattr(measure, "read_box_declaration", _declaration)
    monkeypatch.setattr(
        wired, "WiredStimulusCapture",
        lambda **kw: captures.setdefault("one", _Capture(kw["bundle_dir"])),
    )
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
        yield {"cam": cam, "played": played_programs, "captures": captures}
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

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["status"] == "measured"
    assert len(payload["record_ids"]) == 1
    assert payload["graph_fingerprint"]
    assert len(payload["specs"]) == 1
    assert [s["position_deg"] for s in payload["specs"][0]["stimuli"]] == [0]
    assert [s["incident"] for s in payload["specs"][0]["stimuli"]] == [""]
    assert speaker["captures"]["one"].arounds == 1
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
    stimuli = payload["specs"][0]["stimuli"]
    assert [s["stimulus_dbfs"] for s in stimuli] == [-12.0, -18.0]
    assert len(payload["record_ids"]) == 2
    assert all(s["level_db"] == pytest.approx(-20.0) for s in stimuli)


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

    code = measure.main(["--kind", MEASURE_KIND_BASELINE, "--json"])

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
    code = measure.main(["--kind", MEASURE_KIND_BASELINE, "--json"])

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
    assert payload["status"] == "partial"
    assert payload["reason"] == REFUSE_GRAPH_LOST
    assert len(payload["record_ids"]) == 1, "the banked take was not reported"
    assert payload["bundle_dir"]
    # Still given back: a partial result is not a stranded speaker.
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)


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
        ["--polarity", POLARITY_INVERTED, "--inverted-role", "tweeter"],
        ["--delayed-role", "woofer", "--delay-us", "120"],
        ["--level-matched"],
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
    ],
)
def test_a_batch_whose_specs_disagree_about_the_pose_is_refused_at_parse(
    tmp_path, second,
):
    """The WHOLE pose, not just the bearing — axis and elevation are pose too.

    A batch exists because the microphone move is the expensive part, so every
    spec in it measures the SAME placement. A file mixing bearings is B3 arriving
    through a file; one mixing elevations is the same defect stood on its end,
    and nothing between two specs raises the microphone either.
    """
    entries = [{"positions": [0]}, {"positions": [0], **second}]

    with pytest.raises(MeasureFlagError) as caught:
        specs_from_args(build_parser().parse_args(
            ["--kind", MEASURE_KIND_BASELINE, "--specs", _specs_file(tmp_path, entries)]
        ))

    assert caught.value.reason == REFUSE_SPECS_MIXED_POSE


def test_every_spec_in_the_file_needs_its_own_candidate_id(tmp_path):
    """C3 holds per ENTRY: the file is preset vocabulary, not an exemption.

    The second entry is a variant with no label, which is the take that would
    bank unfindable — and in a batch it would sit beside a labelled sibling it
    could never be told apart from.
    """
    entries = [
        {"candidate_id": "null_a1", "polarity": POLARITY_INVERTED,
         "inverted_role": "tweeter"},
        {"polarity": POLARITY_INVERTED, "inverted_role": "woofer"},
    ]

    with pytest.raises(MeasureFlagError) as caught:
        specs_from_args(build_parser().parse_args(
            ["--kind", MEASURE_KIND_BASELINE, "--specs", _specs_file(tmp_path, entries)]
        ))

    assert caught.value.reason == REFUSE_CANDIDATE_ID_REQUIRED


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
    assert len(payload["record_ids"]) == 3
    assert all(s["record_ids"] for s in payload["specs"])
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
    assert refused["record_ids"] == []
    assert [s["incident"] for s in refused["stimuli"]] == [
        program_transaction.STIMULUS_ADMISSION_REFUSED
    ]
    assert kept["record_ids"], "the batch did not carry on to the next spec"


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
    assert payload["status"] == "partial"
    assert payload["reason"] == REFUSE_GRAPH_LOST
    assert payload["stopped_at"]["candidate_id"] == "second"
    assert [s["candidate_id"] for s in payload["specs"]] == ["first"]
    assert len(payload["record_ids"]) == 1
    assert speaker["cam"].volume_db == pytest.approx(HOUSEHOLD_DB)


def test_a_banked_take_carries_the_curves_it_was_crunched_into(speaker, capsys):
    """Capture and analysis are ONE act — a take arrives already crunched.

    There is no point measuring what nobody turns into numbers: a take banked as
    a WAV reference alone makes every later reader re-derive what the box
    already knew, and ruling S3's whole return on banking the complex response
    is that the bank can be re-analysed by an analysis that did not exist when
    it was captured.

    Asserted on the banked BYTES and on the CONTENT, not merely the key: an
    empty ``curves`` list is what a failed crunch banks, so a test that only
    checked the key was present would pass forever against a door that had
    stopped crunching. Both magnitude and phase are required — phase is the half
    a later alignment read cannot recompute from a magnitude trace.
    """
    code = measure.main(["--kind", MEASURE_KIND_BASELINE])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    banked = json.loads(
        (Path(payload["bundle_dir"]) / ARTIFACTS / payload["record_ids"][0])
        .read_text()
    )
    curves = banked["curves"]
    assert curves, "the take banked no curve — it was measured and not crunched"
    assert {curve["role"] for curve in curves} == {"woofer", "tweeter"}
    for curve in curves:
        assert curve["magnitude_db"], f"{curve['role']} banked no magnitude"
        assert curve["phase_deg"], f"{curve['role']} banked no phase"
        assert len(curve["magnitude_db"]) == len(curve["phase_deg"])
