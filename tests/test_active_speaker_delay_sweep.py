# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pins for the PROPOSE door onto a banked round.

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
    DelayLandscapeError,
    compute_landscape,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL, PHASE_MEASURE
from jasper.active_speaker.crossover_v2.position_cycle import read_take_curves
from jasper.active_speaker.delay_sweep import sweep_spec
from jasper.cli.angle_capture import build_parser as angle_capture_parser
from jasper.cli.delay_sweep import main

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
            "curves": curves,
        }),
        encoding="utf-8",
    )
    return tmp_path


def _propose(bundle: Path, capsys, *extra):
    code = main(["propose", str(bundle), "--fc-hz", str(FC_HZ), *extra])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


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
    assert payload["status"] == "proposed"
    assert payload["take_path"].endswith("positions/p0_a01.json")
    landscape = payload["landscape"]
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
    """One `confirm_with` line per coordinate, in the flags
    `jasper-angle-capture stage` actually takes — this is the whole point of
    the verb: propose, then stage, without hand-deriving which branch moves."""

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", arrival_us=200.0), _curve("tweeter"),
    ])
    _code, payload, err = _propose(bundle, capsys)

    commands = payload["confirm_with"]
    assert len(commands) == len(payload["landscape"]["confirmation_coordinates_us"])

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
    """Neither branch is delayed at 0 us, and `MeasureSpec` refuses a
    half-stated (role, delay) pair — so a line naming a role with 0 us would be
    refused at the very door it was printed for."""

    bundle = _bank(tmp_path, curves=[_curve("woofer"), _curve("tweeter")])
    _code, payload, err = _propose(bundle, capsys)

    zero = [
        line for line, coordinate in zip(
            payload["confirm_with"],
            payload["landscape"]["confirmation_coordinates_us"],
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
    """The refusal IS the output. A bank swept only above Fc cannot carry a
    null at Fc, and the sentence the operator needs is the one
    `delay_landscape` wrote — printed through, never re-spelled here."""

    bundle = _bank(tmp_path, curves=[
        _curve("woofer", band=(2000.0, 12000.0)),
        _curve("tweeter", band=(2000.0, 12000.0)),
    ])
    code, payload, err = _propose(bundle, capsys)

    assert code == 1
    assert payload["status"] == "refused"
    assert payload["reason"] == "delay_propose_landscape_unsupported"

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
    assert payload["reason"] == "delay_propose_no_round"


def test_a_take_carrying_one_role_is_not_half_an_answer(tmp_path, capsys) -> None:
    """Both transfers are summed against each other, so they must ride ONE
    take: curves from two captures would be summed across whatever moved
    between them."""

    bundle = _bank(tmp_path, curves=[_curve("woofer")])
    code, payload, err = _propose(bundle, capsys)
    assert code == 1
    assert payload["reason"] == "delay_propose_no_banked_curves"


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
    assert payload["landscape"]["best_coordinate_us"] == pytest.approx(200.0, abs=50.0)


def test_the_spec_the_door_builds_is_the_shared_one(tmp_path) -> None:
    """`propose` must bound its grid with the same `sweep_spec` the landscape
    reads its bars from, or the printed coordinates would not be the ones the
    verdict grades."""

    spec = sweep_spec(
        crossover_fc_hz=FC_HZ, upper_role="tweeter", lower_role="woofer",
        signed_acoustic_path_difference_m=0.0,
    )
    assert spec.positive_delay_target == "tweeter"
    assert spec.negative_delay_target == "woofer"


def test_a_retaken_pose_proposes_off_the_retake_not_the_take_it_replaced(
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
    assert payload["landscape"]["best_coordinate_us"] == pytest.approx(200.0, abs=50.0)
