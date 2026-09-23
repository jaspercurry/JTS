# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json
import asyncio
from dataclasses import replace
from copy import deepcopy
from pathlib import Path

import pytest

from jasper.cli import round_views
from jasper.audio_measurement.evidence_reasons import REASON_NO_SHARED_MARK_TAKES
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

MARK = {"kind": "bearing", "deg": 0, "elevation_deg": 0, "distance_m": 1.0}


def _curve(level_db: float) -> dict:
    return {"role": "woofer", "band_hz": [200.0, 12000.0], "freqs_hz": [100.0, 1000.0, 20000.0],
            "magnitude_db": [level_db] * 3, "validity_floor_hz": 300.0}


def _mark_take(take_id: str, level_db: float, role: str = "woofer") -> dict:
    return {"take_id": take_id, "selected": True, "phase": "measure", "role": role, "pose": {**MARK},
            "curve": {**_curve(level_db), "role": role}}


@pytest.fixture
def repeated_round(tmp_path):
    root = bank_measure_round(tmp_path)
    takes = [{**_mark_take(f"take-{i}", 0.5 * i), "repeat": i + 1,
              "analysis": {"delay_us": 100.0 + i, "polarity": "normal",
                           "trim_db": {"woofer": 0.0, "tweeter": -3.0 + i * 0.1},
                           "predicted_ripple_db": 1.0 + i * 0.05}} for i in range(2)]
    group = {"set_id": "mark", "capture_basis": {"role": "woofer"}, "takes": takes}
    return root, group


def test_repeat_spreads_selected_take_values_and_their_mark_pairs(repeated_round, capsys):
    root, group = repeated_round
    omitted = deepcopy(group["takes"][1])
    omitted.update(take_id="replaced", selected=False, analysis={}, curve=_curve(9.0))
    group["takes"].insert(0, omitted)
    off_axis = deepcopy(group["takes"][1])
    off_axis.update(take_id="off-axis", pose={**off_axis["pose"], "deg": 20}, analysis={}, curve=_curve(9.0))
    group["takes"].append(off_axis)
    write_manifest(root, groups=[group])
    assert round_views.main(["repeat", str(root), "--set", "mark"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["take_ids"] == ["take-0", "take-1"]
    assert all(set(metrics) == {"trim_db"} for metrics in result["roles"].values())
    for metrics in [result["take"], *result["roles"].values()]:
        for summary in metrics.values():
            a, b = summary["values"]
            assert summary["n"] == 2
            assert summary["spread"] == pytest.approx(abs(a - b))
            assert summary["median"] == pytest.approx((a + b) / 2)
    assert result["floor"]["n_repeats"] == 2
    assert result["floor"]["metrics"]["tweeter_trim_db"]["pairwise_abs_delta_p95_db"] == pytest.approx(
        result["roles"]["tweeter"]["trim_db"]["spread"])
    assert result["mark_pairs"] == {"band_hz": [300.0, 12000.0], "repeat_spread_db": pytest.approx(0.5),
                                    "n_pairs": 1, "repeat_basis": "mark_pairs_max_rms", "reason": None}


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
    assert answer["take"]["ripple_db"]["values"] == [1.0, 2.0]


def test_repeat_spreads_all_takes(repeated_round, capsys):
    root, group = repeated_round
    third = deepcopy(group["takes"][1])
    third.update(take_id="take-2", repeat=3)
    third["analysis"]["delay_us"] = 150.0
    group["takes"].append(third)
    write_manifest(root, groups=[group])
    assert round_views.main(["repeat", str(root), "--set", "mark"]) == 0
    result = json.loads(capsys.readouterr().out)
    summary = result["take"]["delay_us"]
    assert summary == {"values": [100.0, 101.0, 150.0], "median": 101.0,
                       "spread": pytest.approx(49.9), "n": 3}
    assert result["floor"]["n_repeats"] == 3
    assert result["mark_pairs"]["n_pairs"] == 3


@pytest.mark.parametrize("second,drivers", [
    ({"woofer": (3.0, 3.0), "tweeter": (0.0,)},
     {"woofer": ([1.0, 0.0], [3.0]), "tweeter": ([None], [])}),
    ({"tweeter": (0.0, 0.0)}, None),
])
def test_repeat_compares_each_drivers_mark_takes_within_and_between_rounds(tmp_path, capsys, second, drivers):
    roots = []
    for name, levels in (("a", {"woofer": (0.0, 1.0)}), ("b", second)):
        root = bank_measure_round(tmp_path, name=name)
        write_manifest(root, groups=[
            {"set_id": f"{name}-{role}", "capture_basis": {"role": role},
             "takes": [_mark_take(f"{name}-{role}-{i}", level, role) for i, level in enumerate(values)]}
            for role, values in levels.items()])
        roots.append(str(root))

    code = round_views.main(["repeat", *roots])
    answer = json.loads(capsys.readouterr().out)
    if drivers is None:
        assert (code, answer["reason"]) == (round_views.EXIT_REFUSED, REASON_NO_SHARED_MARK_TAKES)
        return
    assert code == round_views.EXIT_OK
    assert answer["rounds"] == roots
    # Flat curves a whole number of dB apart: every pair RMS is exact.
    assert {row["role"]: ([spread["repeat_spread_db"] for spread in row["within"]],
                          [spread["repeat_spread_db"] for spread in row["between"]])
            for row in answer["drivers"]} == drivers
    woofer, = (row for row in answer["drivers"] if row["role"] == "woofer")
    assert woofer["band_hz"] == [300.0, 12000.0]
    assert [(row["round"], row["n_pairs"]) for row in woofer["within"]] == [(0, 1), (1, 1)]
    assert [(row["rounds"], row["n_pairs"]) for row in woofer["between"]] == [([0, 1], 4)]
    assert json.loads(Path(answer["out"]).read_text())["drivers"][0]["within"][0]["take_ids"] == ["a-woofer-0", "a-woofer-1"]
