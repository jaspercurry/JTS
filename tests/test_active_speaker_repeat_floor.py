# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import json

import pytest

from jasper.active_speaker.attempts_loop import CLAIM_FLOOR_P95_MULTIPLE, percentile
from jasper.active_speaker.repeat_floor import (
    REPEAT_FLOOR_KIND,
    SCHEMA_VERSION,
    derive_repeat_floor,
    load_repeat_floor,
    metric_summaries,
    pairwise_abs_deltas,
    stopping_thresholds,
)

AGGREGATE = "shipped_linear_pool_db"


def _record(p95: float) -> dict:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": REPEAT_FLOOR_KIND,
        "measured_at": "2026-09-01T00:00:00Z",
        "n_repeats": 4,
        "aggregate_metric": AGGREGATE,
        "rounds": [],
        "metrics": {
            AGGREGATE: {
                "n": 4, "mean_db": 1.5, "sd_db": 1.29, "range_db": 3.0,
                "min_db": 0.0, "max_db": 3.0,
                "pairwise_abs_delta_p95_db": p95,
                "pairwise_abs_delta_median_db": 1.0,
            },
        },
        "note": "",
    }


def test_pairwise_abs_deltas_over_a_hand_derivable_set():
    """[0, 1, 2, 3] -> |delta| = sorted [1,1,1,2,2,3]; the linear-interpolated
    95th percentile of that set is 2.75."""
    deltas = pairwise_abs_deltas([0.0, 1.0, 2.0, 3.0])
    assert sorted(deltas) == [1.0, 1.0, 1.0, 2.0, 2.0, 3.0]
    assert percentile(deltas, 95.0) == pytest.approx(2.75)


@pytest.mark.parametrize("values", [[], [1.0]])
def test_pairwise_abs_deltas_needs_two_values_to_have_a_difference(values):
    assert pairwise_abs_deltas(values) == []


@pytest.mark.parametrize("values,median,spread", [
    ([100.0, 101.0, 150.0], 101.0, pytest.approx(49.9)),
    ([1.0, 1.0], 1.0, 0.0),
])
def test_metric_summaries_pairs_each_metrics_median_with_its_pairwise_p95_spread(values, median, spread):
    summary, = metric_summaries({"metric": values}).values()
    assert summary == {"values": values, "median": median, "spread": spread, "n": len(values)}


def test_stopping_thresholds_derive_plateau_and_margin_from_the_aggregate_p95():
    thresholds = stopping_thresholds(_record(2.75))
    assert thresholds is not None
    assert thresholds["plateau_db"] == pytest.approx(2.75)
    assert thresholds["margin_db"] == pytest.approx(CLAIM_FLOOR_P95_MULTIPLE * 2.75)
    # round_evidence calls plateau = margin/2 load-bearing; the derivation
    # must not invert it.
    assert thresholds["plateau_db"] < thresholds["margin_db"]


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "0.4", True, 0.0])
def test_stopping_thresholds_refuse_a_row_that_is_not_a_usable_number(bad):
    record = _record(0.4)
    record["metrics"][AGGREGATE]["pairwise_abs_delta_p95_db"] = bad
    assert stopping_thresholds(record) is None


def test_stopping_thresholds_refuse_a_record_with_no_aggregate_row():
    record = _record(0.4)
    record["metrics"] = {}
    assert stopping_thresholds(record) is None


def test_load_reads_back_the_record_it_owns(tmp_path):
    path = tmp_path / "repeat-floor.json"
    path.write_text(json.dumps(_record(0.4)), encoding="utf-8")
    assert load_repeat_floor(state_path=path) == _record(0.4)


@pytest.mark.parametrize("on_disk", [
    pytest.param(None, id="missing"),
    pytest.param("{not json", id="not-json"),
    pytest.param("[]", id="wrong-shape"),
    pytest.param("", id="empty"),
    pytest.param(
        json.dumps({**_record(0.4), "kind": "something_else"}), id="wrong-kind",
    ),
    pytest.param(
        json.dumps({**_record(0.4), "artifact_schema_version": SCHEMA_VERSION + 1}),
        id="wrong-schema",
    ),
])
def test_load_answers_none_for_anything_it_does_not_own(tmp_path, on_disk):
    """Absent-tolerant: every way the file can fail to be this module's record
    resolves the same, because the reader's fallback is honest in all of them."""
    path = tmp_path / "repeat-floor.json"
    if on_disk is not None:
        path.write_text(on_disk, encoding="utf-8")
    assert load_repeat_floor(state_path=path) is None


def test_derive_refuses_a_single_observation_which_has_no_spread():
    with pytest.raises(ValueError):
        derive_repeat_floor(samples={"role_metric": [1.0]}, rounds=[{}])


@pytest.mark.parametrize("unit", ["db", "us"])
def test_floor_accepts_per_take_samples_in_native_units(unit):
    values = [1.0, 2.0, 4.0]
    record = derive_repeat_floor(samples={"role_metric": values}, units={"role_metric": unit},
                                 rounds=[{"take_id": str(i)} for i in range(3)])
    assert record["kind"] == REPEAT_FLOOR_KIND
    assert record["artifact_schema_version"] == SCHEMA_VERSION
    row = record["metrics"]["role_metric"]
    assert row["n"] == record["n_repeats"] == 3
    assert row[f"pairwise_abs_delta_p95_{unit}"] == percentile(pairwise_abs_deltas(values), 95)
    assert row[f"mean_{unit}"] == pytest.approx(7 / 3)
