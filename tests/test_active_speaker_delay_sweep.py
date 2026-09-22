# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read banked delay curves, publish predictions and refusal fields."""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from tests.test_active_speaker_runtime_contract import _active_topology
from tests.test_active_speaker_audition import _applied_profile
from jasper.active_speaker.baseline_profile import BASELINE_PROFILE_KIND, SCHEMA_VERSION

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
    """The woofer arrives 200 us late, so delaying the tweeter by 200 us aligns
    them — read off two curves banked exactly as `pose_curve_record` writes
    them, through the measurement index, with no audio played."""

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


def test_curves_that_cannot_span_the_shoulders_carry_refusal_fields(
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
    message = payload["detail"].pop("message")
    assert isinstance(message, str) and message
    assert payload["detail"] == refusal.value.detail
    assert err.strip(), "an operator running this by hand gets a line on stderr"


def test_a_bundle_with_no_round_refuses_before_it_reads_anything(
    tmp_path, capsys,
) -> None:
    code, payload, err = _propose(tmp_path, capsys)
    assert code == 1
    assert payload["reason"] == "delay_landscape_no_round"
    assert isinstance(payload["detail"], str) and payload["detail"]


def test_delay_landscape_counts_separate_driver_takes_without_pairing_them(tmp_path, capsys) -> None:
    """Both transfers are summed against each other, so they must ride ONE
    take: curves from two captures would be summed across whatever moved
    between them."""

    bundle = _bank(tmp_path, curves=[_curve("woofer")])
    _bank(tmp_path, curves=[_curve("tweeter")], phase=PHASE_LATERAL,
          take_id="p1_a01")
    code, payload, err = _propose(bundle, capsys)
    assert code == 1
    assert payload["reason"] == "delay_landscape_no_banked_curves"
    message = payload["detail"].pop("message")
    assert isinstance(message, str) and message
    assert payload["detail"] == {
        "bundle_dir": str(bundle),
        "phases_searched": [PHASE_MEASURE, PHASE_LATERAL],
        "roles_required": ["woofer", "tweeter"],
        "takes_seen": 2,
        "roles_per_take": {
            "crossover_v2/capture-1/positions/p0_a01.json": {"woofer": 1},
            "crossover_v2/capture-1/positions/p1_a01.json": {"tweeter": 1},
        },
        "poses": [{"position_deg": 0, "vertical_deg": 0}],
    }


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
    """One corrupt or unrelated sidecar must not cost a reader the takes that
    are fine — the same rule `read_lateral_take` follows."""

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
    """A per-driver walk pose carries the same curve shape a design-axis
    MEASURE capture does; which phase answers is the caller's to state."""

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
    """docs/tuning-methodology.md §4 step 1, stated by the tool.

    Whether the analysis divided the emitted protection out and multiplied the
    configured crossover in is a fact about the take, not about `--phase`, and
    a protection-retained optimum is contaminated evidence. So the proposal
    echoes what the take stamped — and `None` for a take banked before the
    field existed, which is unknown rather than either one.
    """

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
    """`delay-landscape` must bound its grid with the same `sweep_spec` the
    landscape reads its bars from, or the printed coordinates would not be the
    ones the verdict grades."""

    spec = sweep_spec(
        crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )
    assert spec.positive_delay_target == "tweeter"
    assert spec.negative_delay_target == "woofer"


def test_a_retaken_pose_reads_the_retake_not_the_take_it_replaced(
    tmp_path, capsys,
) -> None:
    """A superseded take stays on disk as the honest walk record, and `take_id`
    is zero-padded so the index's `ORDER BY path` is chronological. Reading the
    FIRST match would answer off the capture a retake was taken to replace."""

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


@pytest.mark.parametrize("composition,coordinate_basis", [
    ("complete_tune_measured", "residual addition to measured tune"),
    ("crossover_composed", "neutral branch delay"),
])
def test_delay_proposal_selects_one_take_and_reports_coordinate_basis(tmp_path, capsys, composition, coordinate_basis):
    _bank(tmp_path, curves=[_curve("woofer", arrival_us=100), _curve("tweeter")],
          phase="lateral", composition=composition, take_id="p0_a01")
    _bank(tmp_path, curves=[_curve("woofer", arrival_us=-100), _curve("tweeter")],
          phase="lateral", composition=composition, take_id="p0_a02")
    take = "crossover_v2/capture-1/positions/p0_a01.json"
    code, payload, _err = _propose(tmp_path, capsys, "--phase", "lateral", "--take-path", take)
    assert code == 0
    assert payload["take_path"] == take
    assert payload["best_coordinate_us"] == pytest.approx(100)
    assert _banked(payload)["delay_coordinates"] == coordinate_basis


def test_delay_landscape_reads_only_the_common_gate_coverage(tmp_path, capsys):
    lower, upper = _curve("woofer"), _curve("tweeter")
    lower["validity_floor_hz"] = FC_HZ + 100
    _bank(tmp_path, curves=[lower, upper])
    code, payload, _err = _propose(tmp_path, capsys)
    assert code == 1
    assert payload["reason"] == "shoulder_overlap_excludes_fc"


@pytest.mark.parametrize("override", [False, True])
def test_delay_defaults_come_from_the_selected_banked_take(tmp_path, capsys, override):

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
