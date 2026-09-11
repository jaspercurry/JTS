# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the delay-landscape door onto a banked round.

Three questions, one altitude each: does the door read the bank the store
actually wrote, does it hand the operator a line they can run, and does a
refusal reach them as the refusing module's own sentence.
"""

import json
import math
import shlex
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.delay_landscape import (
    REFUSAL_FC_OUTSIDE_OVERLAP,
    DelayLandscapeError,
    compute_landscape,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.position_cycle import read_take_curves
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.audio_measurement.analysis import ShoulderSpan
from jasper.cli import null_door
from jasper.cli.angle_capture import build_parser as angle_capture_parser
from jasper.cli.round_views import main

FC_HZ = 1800.0


def _lr4(freqs, *, highpass: bool):
    """One LR4 branch: an inverted, aligned pair cancels hard at Fc."""

    s = 1j * (np.asarray(freqs, dtype=float) / FC_HZ)
    butter2 = (s**2 if highpass else 1.0) / (s**2 + math.sqrt(2.0) * s + 1.0)
    return butter2**2


def _curve(role: str, *, arrival_us: float = 0.0, band=(200.0, 12000.0)):
    """One curve in `spatial.pose_curve_record`'s exact banked shape."""

    freqs = np.linspace(band[0], band[1], 512)
    tf = _lr4(freqs, highpass=(role == "tweeter")) * np.exp(
        -2j * np.pi * freqs * arrival_us * 1e-6
    )
    return {
        "role": role,
        "band_hz": [float(band[0]), float(band[1])],
        "freqs_hz": [float(hz) for hz in freqs],
        "magnitude_db": [float(db) for db in 20.0 * np.log10(np.abs(tf))],
        "phase_deg": [float(deg) for deg in np.degrees(np.angle(tf))],
    }


def _bank(
    tmp_path: Path,
    *,
    curves,
    phase: str = PHASE_MEASURE,
    position_deg: int = 0,
    kind: str = POSITION_EVIDENCE_KIND,
    take_id: str = "p0_a01",
    composition: str | None = None,
) -> Path:
    """A bundle carrying one banked take, at the path the store writes.

    No index file needed: `bundle_measurements` always rescans the corpus from
    the take files on disk, which is what a hand-built fixture like this one
    relies on.
    """

    positions = (
        tmp_path / EVIDENCE_ROOT / "artifacts" / "crossover_v2" / "capture-1" / "positions"
    )
    positions.mkdir(parents=True, exist_ok=True)
    (positions / f"{take_id}.json").write_text(
        json.dumps({
            "schema_version": 1,
            "kind": kind,
            "phase": phase,
            "take_id": take_id,
            "position_deg": position_deg,
            **({"phase_composition": composition} if composition else {}),
            "curves": curves,
        }),
        encoding="utf-8",
    )
    return tmp_path


def _propose(bundle: Path, capsys, *extra):
    code = main(["delay-landscape", str(bundle), "--fc-hz", str(FC_HZ), *extra])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def _banked(answer) -> dict:
    """The artifact the answer on stdout points at: the grid lives there, and
    the answer names its path (ADR-0237)."""
    return json.loads(Path(answer["out"]).read_text())


def test_the_door_reads_the_bank_the_store_wrote_and_finds_the_offset(
    tmp_path, capsys,
) -> None:

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", arrival_us=200.0), _curve("tweeter"),
    ])
    code, payload, err = _propose(bundle, capsys)

    assert code == 0
    assert payload["take_path"].endswith("positions/p0_a01.json")
    assert payload["best_coordinate_us"] == pytest.approx(200.0, abs=50.0)
    landscape = _banked(payload)["landscape"]
    assert landscape["best_coordinate_us"] == pytest.approx(200.0, abs=50.0)
    assert landscape["kind"] == "jts_inter_driver_delay_landscape"
    # Two or three: the optimum and its immediate neighbours.
    assert 2 <= len(landscape["confirmation_coordinates_us"]) <= 3
    # The span every printed depth was read at rides with them, and an operator
    # reading the coordinate by hand gets it beside the answer.
    assert landscape["shoulders"]["used_hz"] == [FC_HZ / 2.0, FC_HZ * 2.0]
    assert landscape["shoulders"]["lower_clamped"] is False
    assert err.strip()


def test_the_door_hands_back_a_line_the_operator_can_run(tmp_path, capsys) -> None:

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", arrival_us=200.0), _curve("tweeter"),
    ])
    _code, payload, err = _propose(bundle, capsys)

    commands = payload["next"]
    assert len(commands) == len(payload["confirmation_coordinates_us"])

    # Parsed by the REAL parser, not matched against a copy of its wording: a
    # printed line is only "ready to run" if the tool it names accepts it, and
    # a vocabulary change must fail here rather than rot silently.
    for line in commands:
        argv = shlex.split(line)
        assert argv[0] == "jasper-angle-capture"
        parsed = angle_capture_parser().parse_args(argv[1:])
        assert parsed.angles == "0"
        assert parsed.inverted_role == "tweeter"
        # The signed coordinate reaches the flags as an executable (role,
        # delay) pair, never as a negative microsecond count.
        assert parsed.delay_us >= 0.0
        # EVERY line asks for the level match, the zero coordinate included: a
        # null between branches ~10 dB apart in sensitivity is bounded by that
        # gap however well the coordinate is chosen, so a confirm line without
        # it would be staging a measurement whose answer was already decided.
        assert parsed.level_matched is True


def test_the_zero_coordinate_stages_no_delay_at_all(tmp_path, capsys) -> None:

    bundle = _bank(tmp_path, curves=[_curve("woofer"), _curve("tweeter")])
    _code, payload, err = _propose(bundle, capsys)

    zero = [
        line for line, coordinate in zip(
            payload["next"], payload["confirmation_coordinates_us"],
        )
        if coordinate == 0.0
    ]
    assert zero, "the aligned fixture puts a zero coordinate in the set"
    for line in zero:
        assert "--delayed-role" not in line
        assert "--delay-us" not in line


def test_curves_that_cannot_span_the_shoulders_refuse_verbatim(
    tmp_path, capsys,
) -> None:

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", band=(2000.0, 12000.0)),
        _curve("tweeter", band=(2000.0, 12000.0)),
    ])
    code, payload, err = _propose(bundle, capsys)

    assert code == 1
    assert payload["status"] == "refused"
    assert payload["reason"] == REFUSAL_FC_OUTSIDE_OVERLAP

    # Verbatim, pinned against the module that owns the sentence rather than
    # against a copy of its wording: re-word the refusal there and this still
    # holds, but swallow or paraphrase it here and it does not.
    with pytest.raises(DelayLandscapeError) as refusal:
        compute_landscape(
            _curve("woofer", band=(2000.0, 12000.0)),
            _curve("tweeter", band=(2000.0, 12000.0)),
            spec=sweep_spec(
                crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
                signed_acoustic_path_difference_m=0.0,
            ),
            inverted_role="tweeter",
        )
    assert payload["detail"] == str(refusal.value)
    assert err.strip(), "an operator running this by hand gets a line on stderr"


def test_a_bundle_with_no_round_refuses_before_it_reads_anything(
    tmp_path, capsys,
) -> None:
    code, payload, err = _propose(tmp_path, capsys)
    assert code == 1
    assert payload["reason"] == "delay_landscape_no_round"


def test_a_take_carrying_one_role_is_not_half_an_answer(tmp_path, capsys) -> None:

    bundle = _bank(tmp_path, curves=[_curve("woofer")])
    code, payload, err = _propose(bundle, capsys)
    assert code == 1
    assert payload["reason"] == "delay_landscape_no_banked_curves"


@pytest.mark.parametrize(
    ("phase", "kind", "curves"),
    [
        pytest.param(PHASE_LATERAL, POSITION_EVIDENCE_KIND, "ok", id="wrong_phase"),
        pytest.param(PHASE_MEASURE, "something_else", "ok", id="not_a_take"),
        pytest.param(PHASE_MEASURE, POSITION_EVIDENCE_KIND, [], id="no_curves"),
    ],
)
def test_the_curve_reader_answers_none_and_never_raises(
    tmp_path, phase, kind, curves,
) -> None:

    payload = [_curve("woofer"), _curve("tweeter")] if curves == "ok" else curves
    bundle = _bank(tmp_path, curves=payload, phase=phase, kind=kind)
    take = (
        bundle / EVIDENCE_ROOT / "artifacts" / "crossover_v2" / "capture-1"
        / "positions" / "p0_a01.json"
    )
    assert read_take_curves(take, phase=PHASE_MEASURE) is None
    assert read_take_curves(tmp_path / "nope.json", phase=PHASE_MEASURE) is None


def test_a_lateral_pose_answers_when_the_caller_asks_for_one(
    tmp_path, capsys,
) -> None:

    bundle = _bank(
        tmp_path,
        curves=[_curve("woofer", arrival_us=200.0), _curve("tweeter")],
        phase=PHASE_LATERAL,
    )
    code, payload, err = _propose(bundle, capsys, "--phase", PHASE_LATERAL)
    assert code == 0
    assert payload["best_coordinate_us"] == pytest.approx(200.0, abs=50.0)


def test_the_proposal_echoes_the_composition_its_take_was_banked_under(
    tmp_path, capsys,
) -> None:

    curves = [_curve("woofer", arrival_us=200.0), _curve("tweeter")]
    stated = _bank(
        tmp_path / "stated", curves=curves, composition="crossover_composed",
    )
    legacy = _bank(tmp_path / "legacy", curves=curves)

    _code, proposed, _err = _propose(stated, capsys)
    _code, unstamped, _err = _propose(legacy, capsys)

    assert proposed["phase"] == PHASE_MEASURE
    assert proposed["phase_composition"] == "crossover_composed"
    assert unstamped["phase_composition"] is None


def test_the_spec_the_door_builds_is_the_shared_one(tmp_path) -> None:

    spec = sweep_spec(
        crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )
    assert spec.positive_delay_target == "tweeter"
    assert spec.negative_delay_target == "woofer"


def test_a_retaken_pose_reads_the_retake_not_the_take_it_replaced(
    tmp_path, capsys,
) -> None:

    _bank(tmp_path, curves=[_curve("woofer"), _curve("tweeter")], take_id="p0_a01")
    _bank(
        tmp_path,
        curves=[_curve("woofer", arrival_us=200.0), _curve("tweeter")],
        take_id="p0_a02",
    )
    code, payload, _err = _propose(tmp_path, capsys)

    assert code == 0
    assert payload["take_path"].endswith("p0_a02.json")
    assert payload["best_coordinate_us"] == pytest.approx(200.0, abs=50.0)


def _null_row(bundle: Path, *, delay_us: float, depth_db: float | None = None,
              inverted: bool = True, refusal=None):
    """One `<bundle>/null_runs/` row, written by the door that owns their shape.

    Built through `null_door`'s own writer rather than a hand copy, so a change
    to the banked row fails here instead of being read past.
    """

    spec = sweep_spec(
        crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )
    span = ShoulderSpan(
        crossover_fc_hz=FC_HZ, overlap_hz=(FC_HZ / 2.0, FC_HZ * 2.0),
        used_hz=(FC_HZ / 2.0, FC_HZ * 2.0), samples_below_fc=64, samples_above_fc=64,
    )
    row = null_door._row(
        fc_hz=FC_HZ,
        candidate=spec.dsp_candidate(delay_us),
        inverted=inverted,
        inverted_role="tweeter",
        position_deg=0,
        trims_db={"woofer": 0.0, "tweeter": -10.0},
        trims_source="banked_base_trim",
        gap_ceiling_db=3.3,
        graph_fingerprint="abc123",
        wav_sha256="0" * 64,
        depth_db=depth_db,
        span=None if refusal else span,
        refusal=refusal,
    )
    return null_door._write_row(bundle / "null_runs", row)


def _refused():
    return null_door.NullDoorRefused(
        "null_no_shoulders", "the band cannot place a shoulder"
    )


def _confirm(bundle: Path, capsys, *extra):
    code = main(["delay-confirm", str(bundle), "--fc-hz", str(FC_HZ), *extra])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_delay_landscape_banks_itself_beside_the_round(tmp_path, capsys) -> None:

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", arrival_us=200.0), _curve("tweeter"),
    ])
    code, payload, _err = _propose(bundle, capsys)

    banked = bundle / "delay_landscape.json"
    assert code == 0
    assert payload["out"] == str(banked)
    landscape = json.loads(banked.read_text())["landscape"]
    assert landscape["best_coordinate_us"] == payload["best_coordinate_us"]
    assert payload["bytes"] == banked.stat().st_size


def test_confirm_grades_the_played_rows_against_the_computed_optimum(
    tmp_path, capsys,
) -> None:

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", arrival_us=200.0), _curve("tweeter"),
    ])
    _code, proposed, _err = _propose(bundle, capsys)
    optimum = proposed["best_coordinate_us"]
    for coordinate in proposed["confirmation_coordinates_us"]:
        _null_row(
            bundle, delay_us=coordinate,
            depth_db=26.0 if coordinate == optimum else 6.0,
        )

    code, payload, err = _confirm(bundle, capsys)

    assert code == 0
    assert payload["verdict"] == "delay_resolved_robust"
    assert payload["prescribable_delay_us"] == pytest.approx(optimum)
    banked = json.loads((bundle / "delay_confirmation.json").read_text())
    assert banked["verdict"]["verdict"] == payload["verdict"]
    assert banked["verdict"]["computed_optimum_us"] == pytest.approx(optimum)
    # Every graded row is named with its coordinate and depth, and nothing
    # from the capture rides along.
    assert len(banked["graded_rows"]) == len(
        proposed["confirmation_coordinates_us"]
    )
    assert all(
        "wav_sha256" not in row and {"delay_us", "depth_db"} <= set(row)
        for row in banked["graded_rows"]
    )
    assert err.strip()


def test_confirm_refuses_rows_it_cannot_compare(tmp_path, capsys) -> None:

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", arrival_us=200.0), _curve("tweeter"),
    ])
    _null_row(bundle, delay_us=0.0, depth_db=22.0, inverted=False)
    _null_row(bundle, delay_us=200.0, refusal=_refused())

    code, payload, _err = _confirm(bundle, capsys)

    assert code == 1
    assert payload["status"] == "refused"
    assert payload["reason"] == "delay_confirm_no_measured_rows"
    assert not (bundle / "delay_confirmation.json").exists()


def test_complete_tune_delay_proposal_selects_one_take_and_cannot_grade_neutral_rows(tmp_path, capsys):
    _bank(tmp_path, curves=[_curve("woofer", arrival_us=100), _curve("tweeter")],
          phase="lateral", composition="complete_tune_measured", take_id="p0_a01")
    _bank(tmp_path, curves=[_curve("woofer", arrival_us=-100), _curve("tweeter")],
          phase="lateral", composition="complete_tune_measured", take_id="p0_a02")
    take = "crossover_v2/capture-1/positions/p0_a01.json"
    code, payload, _err = _propose(tmp_path, capsys, "--phase", "lateral", "--take-path", take)
    assert code == 0
    assert payload["take_path"] == take
    assert payload["best_coordinate_us"] == pytest.approx(100)
    assert _banked(payload)["delay_coordinates"] == "residual addition to measured tune"
    code, payload, _err = _confirm(tmp_path, capsys, "--phase", "lateral", "--take-path", take)
    assert code == 1
    assert payload["reason"] == "delay_confirm_graph_mismatch"


def test_delay_landscape_reads_only_the_common_gate_coverage(tmp_path, capsys):
    lower, upper = _curve("woofer"), _curve("tweeter")
    lower["validity_floor_hz"] = FC_HZ + 100
    _bank(tmp_path, curves=[lower, upper])
    code, payload, _err = _propose(tmp_path, capsys)
    assert code == 1
    assert payload["reason"] == "shoulder_overlap_excludes_fc"


@pytest.mark.parametrize("override", [False, True])
def test_delay_defaults_come_from_the_selected_banked_take(tmp_path, capsys, override):
    from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION
    from tests.test_active_speaker_audition import _applied_profile
    from tests.test_active_speaker_runtime_contract import _active_topology

    bundle = _bank(tmp_path / "round" / "bundle" / "session", curves=[_curve("woofer"), _curve("tweeter")],
                   phase=PHASE_LATERAL, position_deg=15, take_id="lateral")
    _bank(bundle, curves=[_curve("woofer"), _curve("tweeter")], phase=PHASE_MEASURE, position_deg=0, take_id="measure")
    (bundle / "info.json").write_text(json.dumps({"session_id": "session"}))
    take = next(bundle.glob("evidence/v1/artifacts/**/positions/measure.json"))
    document = json.loads(take.read_text())
    document["inverted_role"] = "woofer"
    take.write_text(json.dumps(document))
    profile = _applied_profile(_active_topology("mono", "active_2_way"))
    profile.update(kind=BASELINE_PROFILE_KIND, artifact_schema_version=SCHEMA_VERSION)
    profile["recomposition_snapshot"]["preset"]["crossover_regions"][0]["fc_hz"] = FC_HZ
    bank = bundle.parent.parent
    (bank / "applied-profile.json").write_text(json.dumps(profile))
    flags = ["--fc-hz", "2000", "--position-deg", "15", "--inverted-role", "tweeter", "--phase", "lateral"] if override else []
    assert main(["delay-landscape", str(bank), *flags]) == 0
    output = _banked(json.loads(capsys.readouterr().out))
    assert output["phase"] == ("lateral" if override else "measure")
    assert output["landscape"]["inverted_role"] == ("tweeter" if override else "woofer")
    assert output["landscape"]["spec"]["crossover_fc_hz"] == (2000 if override else FC_HZ)
    assert output["take_path"].endswith("lateral.json" if override else "measure.json")
