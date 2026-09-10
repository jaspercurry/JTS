# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin :mod:`jasper.active_speaker.crossover_v2.measurement_plan`'s shape:
poses x takes pricing, JSON round-trip identity, every refusal code, and the
``jasper-round plan`` CLI wiring.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from jasper.active_speaker import measurement_programs
from jasper.active_speaker.candidate_bank import CandidateBankRefusal
from jasper.active_speaker.crossover_v2 import measurement_plan as mplan
from jasper.active_speaker.crossover_v2.admission import MAX_EXTRA_ATTEMPTS_PER_POSITION
from jasper.cli import round as cli
from jasper.cli._refusal import STATUS_BY_CODE

#: Every non-"base" candidate this file plans with; the scope value is
#: arbitrary (any :data:`~.measure_spec.CANDIDATE_SCOPES` member) since no
#: test here reads it back for meaning, only for identity.
_CANDIDATE_SCOPES = {"c1": "candidate", "c2": "room_candidate", "c3": "bass_candidate"}

#: candidate count (N) -> the tokens :func:`plan_for_program` takes, "base"
#: always first so every case also pins "base first when listed first".
_CANDIDATE_SETS = {
    1: ("base",),
    2: ("base", "c1"),
    4: ("base", "c1", "c2", "c3"),
}

#: (program_id, size, poses) -- the poses column is this file's own oracle,
#: independent of the plan model, from measurement_plans.json's layouts.
_PROGRAMS = (
    ("room", "quick", 3),
    ("room", "cloud", 11),
    ("tournament", "express", 1),
)


def _plan(program_id: str, size: str, candidates: tuple[str, ...]) -> mplan.MeasurementPlan:
    return mplan.plan_for_program(
        program_id, size, candidates=candidates, candidate_scopes=_CANDIDATE_SCOPES,
    )


@pytest.mark.parametrize(
    "program_id,size,poses", _PROGRAMS, ids=[f"{p}-{s}" for p, s, _ in _PROGRAMS],
)
@pytest.mark.parametrize(
    "candidates", _CANDIDATE_SETS.values(), ids=[f"n{n}" for n in _CANDIDATE_SETS],
)
def test_cost_prices_poses_times_candidates(program_id, size, poses, candidates):
    plan = _plan(program_id, size, candidates)
    cost = plan.cost()

    n = len(candidates)
    assert cost.mic_moves == poses
    assert cost.takes == poses * n
    assert cost.max_attempts == poses * n + poses * MAX_EXTRA_ATTEMPTS_PER_POSITION
    # Order is data: the takes name the same candidates, in the same order,
    # "base" spelled by its empty candidate_id.
    assert [take.candidate_id or "base" for take in plan.takes] == list(candidates)
    assert plan.takes[0].candidate_id == ""
    assert plan.takes[0].graph_scope == "base"


def test_json_round_trip_is_stable_and_order_sensitive():
    plan = _plan("room", "quick", _CANDIDATE_SETS[4])

    restored = mplan.MeasurementPlan.from_mapping(json.loads(json.dumps(plan.to_dict())))
    assert restored == plan
    assert restored.fingerprint == plan.fingerprint

    reordered = dataclasses.replace(plan, takes=tuple(reversed(plan.takes)))
    assert reordered.fingerprint != plan.fingerprint


# --------------------------------------------------------------------------- #
# every PLAN_REFUSALS code, exactly once
# --------------------------------------------------------------------------- #

_VALIDATE_CASES = [
    ("no_poses", lambda p: dataclasses.replace(p, poses=())),
    ("no_takes", lambda p: dataclasses.replace(p, takes=())),
    (
        "take_carries_positions",
        lambda p: dataclasses.replace(
            p, takes=(dataclasses.replace(p.takes[0], positions=(1,)), *p.takes[1:]),
        ),
    ),
    ("unknown_mover", lambda p: dataclasses.replace(p, mover="robot")),
    (
        "unknown_level_mode",
        lambda p: dataclasses.replace(p, level=mplan.LevelPolicy(mode="bogus")),
    ),
    (
        "series_requires_rungs",
        lambda p: dataclasses.replace(p, level=mplan.LevelPolicy(mode=mplan.LEVEL_SERIES)),
    ),
    ("ceiling_not_positive", lambda p: dataclasses.replace(p, ceiling_db_spl=-1.0)),
    ("extra_attempts_negative", lambda p: dataclasses.replace(p, extra_attempts_per_pose=-1)),
]


@pytest.mark.parametrize(
    "code,make_invalid", _VALIDATE_CASES, ids=[code for code, _ in _VALIDATE_CASES],
)
def test_validate_raises_every_shape_refusal_once(code, make_invalid):
    plan = make_invalid(_plan("room", "quick", _CANDIDATE_SETS[2]))
    with pytest.raises(mplan.PlanRefusal) as excinfo:
        plan.validate()
    assert excinfo.value.code == code


def test_plan_for_program_refuses_an_unmapped_candidate_scope():
    with pytest.raises(mplan.PlanRefusal) as excinfo:
        mplan.plan_for_program(
            "room", "quick", candidates=("base", "ghost"), candidate_scopes={},
        )
    assert excinfo.value.code == "unknown_candidate_scope"


def test_from_mapping_refuses_a_wrong_kind_or_schema_version():
    with pytest.raises(mplan.PlanRefusal) as excinfo:
        mplan.MeasurementPlan.from_mapping({"kind": "not_a_plan", "schema_version": 1})
    assert excinfo.value.code == "plan_schema_unsupported"


def test_every_plan_refusal_code_is_exercised_above():
    exercised = {code for code, _ in _VALIDATE_CASES} | {
        "unknown_candidate_scope", "plan_schema_unsupported",
    }
    assert exercised == set(mplan.PLAN_REFUSALS)


# --------------------------------------------------------------------------- #
# take_regime mirrors angle_capture.request_for_program's rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "candidates,expected",
    [((), "per_driver"), (("base",), "summed"), (("base", "c1"), "summed")],
    ids=["no-candidates", "one-candidate", "two-candidates"],
)
def test_take_regime_sums_a_per_driver_program_once_candidates_are_named(candidates, expected):
    per_driver_program = measurement_programs.program("tournament", "express")
    assert mplan.take_regime(per_driver_program, candidates) == expected


def test_take_regime_leaves_a_branches_program_alone():
    branches_program = measurement_programs.program("branches", "express")
    assert mplan.take_regime(branches_program, ("only-one",)) == "branches"


# --------------------------------------------------------------------------- #
# jasper-round plan
# --------------------------------------------------------------------------- #


class _FakeBankedCandidate:
    def __init__(self) -> None:
        self.candidate = object()


def test_plan_writes_the_file_and_prints_one_json_document(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "jasper.active_speaker.candidate_bank.find_banked_candidate",
        lambda fingerprint, root=None: _FakeBankedCandidate(),
    )
    monkeypatch.setattr(
        "jasper.active_speaker.measured_crossover_candidate.candidate_trial_scope",
        lambda candidate: "candidate",
    )
    out = tmp_path / "plan.json"

    code = cli.main([
        "plan", "--program", "room", "--size", "quick",
        "--candidates", "base,c1", "--out", str(out),
    ])

    assert code == cli.EXIT_OK
    receipt = json.loads(capsys.readouterr().out)
    assert set(receipt) == {"verb", "out", "fingerprint", "cost", "poses", "takes"}
    assert receipt["out"] == str(out)
    assert receipt["poses"] == 3
    assert receipt["takes"] == 2
    assert receipt["cost"]["takes"] == 6

    written = json.loads(out.read_text())
    assert written["fingerprint"] == receipt["fingerprint"]
    assert written["kind"] == mplan.PLAN_KIND


def test_plan_exits_refused_when_the_bank_refuses(tmp_path, monkeypatch, capsys):
    def _refuse(fingerprint, root=None):
        raise CandidateBankRefusal("not_found", "no banked candidate matches")

    monkeypatch.setattr(
        "jasper.active_speaker.candidate_bank.find_banked_candidate", _refuse,
    )

    code = cli.main([
        "plan", "--program", "room", "--size", "quick",
        "--candidates", "base,c1", "--out", str(tmp_path / "plan.json"),
    ])

    assert code == cli.EXIT_REFUSED
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == STATUS_BY_CODE[cli.EXIT_REFUSED]
    assert receipt["reason"] == "not_found"
    assert not (tmp_path / "plan.json").exists()
