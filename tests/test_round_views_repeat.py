# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import asyncio
from dataclasses import replace
from copy import deepcopy

import pytest

from jasper.active_speaker.attempts_loop import percentile
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.repeat_floor import derive_repeat_floor, pairwise_abs_deltas, write_repeat_floor
from jasper.cli import round_views
from jasper.active_speaker import plan_run
from jasper.active_speaker.crossover_v2.planning import analysis_json
from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
from jasper.audio_measurement.program import build_measure_program, RoleBand
from jasper.audio_measurement.excitation_admission import FrequencyBand
from tests.crossover_v2_fixtures import _measure_analysis
from tests.engine_twin import FakeSeams, FakeRecords
from tests.test_plan_run import _run_gated, _walk, AnsweredGate
from tests.crossover_v2_banked_round import bank_measure_round
from tests.run_manifest_fixture import write_manifest


@pytest.fixture
def repeated_round(tmp_path):
    root = bank_measure_round(tmp_path)
    takes = [{"take_id": f"take-{i}", "selected": True, "repeat": i + 1,
              "pose": {"kind": "bearing", "deg": 0, "elevation_deg": 0, "distance_m": 1.0},
              "analysis": {"delay_us": 100.0 + i, "polarity": "normal",
                           "trim_db": {"woofer": 0.0, "tweeter": -3.0 + i * 0.1},
                           "predicted_ripple_db": 1.0 + i * 0.05}} for i in range(2)]
    group = {"set_id": "mark", "capture_basis": {"role": "woofer"}, "takes": takes}
    samples = {f"{role}_{metric}": [0.0, spread] for role in ("woofer", "tweeter")
               for metric, spread in (("delay_us", 1.0), ("trim_db", 0.1), ("ripple_db", 0.1))}
    floor = derive_repeat_floor(samples=samples, units={f"{r}_delay_us": "us" for r in ("woofer", "tweeter")},
                                rounds=[{"take_id": "prior-a"}, {"take_id": "prior-b"}])
    write_repeat_floor(floor, state_path=root / "repeat-floor.json")
    return root, group


@pytest.mark.parametrize("mutation,pair,metric", [
    ({}, "agrees", None), ({"polarity": "inverted"}, "disagrees", "polarity"),
    ({"delay_us": 200.0}, "disagrees", "delay_us"),
    ({"predicted_ripple_db": 3.0}, "disagrees", "ripple_db"),
    ({"trim_db": {"woofer": 0.0, "tweeter": -1.0}}, "disagrees", "trim_db"),
])
def test_repeat_pair_and_spread_use_selected_take_values(repeated_round, mutation, pair, metric, capsys):
    root, group = repeated_round
    group["takes"][1]["analysis"].update(mutation)
    omitted = deepcopy(group["takes"][1])
    omitted.update(take_id="replaced", selected=False, analysis={})
    group["takes"].insert(0, omitted)
    off_axis = deepcopy(group["takes"][1])
    off_axis.update(take_id="off-axis", pose={**off_axis["pose"], "deg": 20}, analysis={})
    group["takes"].append(off_axis)
    write_manifest(root, groups=[group])
    assert round_views.main(["repeat", str(root), "--set", "mark"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pair"] == pair
    assert result["take_ids"] == ["take-0", "take-1"]
    for metrics in result["roles"].values():
        for summary in metrics.values():
            assert summary["n"] == 2
            assert summary["spread"] == pytest.approx(percentile(pairwise_abs_deltas(summary["values"]), 95))
            assert summary["median"] == pytest.approx(percentile(summary["values"], 50))
    if metric:
        assert {"role": "tweeter", "metric": metric} in result["disagreements"]
    else:
        assert result["disagreements"] == []
    assert result["floor"]["n_repeats"] == 2
    assert result["floor"]["metrics"]["tweeter_trim_db"]["pairwise_abs_delta_p95_db"] == pytest.approx(
        result["roles"]["tweeter"]["trim_db"]["spread"])
    assert json.loads((root / "repeat-floor.json").read_text())["rounds"] == [
        {"take_id": "prior-a"}, {"take_id": "prior-b"}]


@pytest.mark.parametrize("bad", ["one", "no_analysis", "missing_role", "moved", "nonfinite"])
def test_repeat_refuses_unusable_takes_by_code(repeated_round, bad, capsys):
    root, group = repeated_round
    if bad == "one":
        group["takes"].pop()
    elif bad == "no_analysis":
        group["takes"][1].pop("analysis")
    elif bad == "missing_role":
        group["takes"][1]["analysis"]["trim_db"].pop("tweeter")
    elif bad == "moved":
        group["takes"][1]["pose"]["distance_m"] = 1.1
    else:
        group["takes"][1]["analysis"]["delay_us"] = float("nan")
    write_manifest(root, groups=[group])
    assert round_views.main(["repeat", str(root), "--set", "mark"]) == round_views.EXIT_REFUSED
    result = json.loads(capsys.readouterr().out)
    assert (result["status"], result["reason"]) == ("refused", round_views.REASON_REFUSED)


def test_current_takes_cannot_supply_their_own_agreement_tolerance(repeated_round, capsys):
    root, group = repeated_round
    (root / "repeat-floor.json").unlink()
    write_manifest(root, groups=[group])
    assert round_inputs(root).repeat_floor_path is None
    assert round_views.main(["repeat", str(root), "--set", "mark"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pair"] == "unmeasured"
    assert {"role": "tweeter", "metric": "delay_us"} in result["unmeasured"]


def test_executor_keeps_each_takes_scalar_analysis(monkeypatch, tmp_path, capsys):
    program = build_measure_program({"woofer": -20, "tweeter": -20}, [
        RoleBand("woofer", 0, FrequencyBand(200, 4000)), RoleBand("tweeter", 1, FrequencyBand(1000, 20000))])
    class Records(FakeRecords):
        async def bank(self, record):
            record["program"] = program.to_dict()
            return await super().bank(record)
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: TakeVerdict(True))
    expected = []
    def analyze(record, record_id):
        analysis = _measure_analysis(program, predicted_ripple_db=float(record["repeat"]))
        expected.append(analysis_json(analysis))
        return analysis
    result, _ = asyncio.run(_run_gated(replace(_walk([0]), repeats=2), analyze=analyze,
                                       seams=FakeSeams(records=Records()), gate=AnsweredGate()))
    assert result.status == "complete"
    assert len(expected) == 2
    for group in result.to_dict()["sets"]:
        assert [take["analysis"] for take in group["takes"]] == expected

    root = bank_measure_round(tmp_path)
    groups = result.to_dict()["sets"]
    write_manifest(root, groups=groups)
    assert round_views.main(["repeat", str(root), "--set", groups[0]["set_id"]]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["roles"]["woofer"]["ripple_db"]["values"] == [1.0, 2.0]


def test_repeat_spreads_all_takes_and_names_the_pair(repeated_round, capsys):
    root, group = repeated_round
    third = deepcopy(group["takes"][1])
    third.update(take_id="take-2", repeat=3)
    third["analysis"]["delay_us"] = 150.0
    group["takes"].append(third)
    write_manifest(root, groups=[group])
    assert round_views.main(["repeat", str(root), "--set", "mark"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pair"] == "agrees"
    assert result["pair_take_ids"] == ["take-0", "take-1"]
    summary = result["roles"]["woofer"]["delay_us"]
    assert summary == {"values": [100.0, 101.0, 150.0], "median": 101.0,
                       "spread": percentile(pairwise_abs_deltas([100.0, 101.0, 150.0]), 95), "n": 3}
    assert result["floor"]["n_repeats"] == 3
