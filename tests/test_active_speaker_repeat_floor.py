# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The banked repeat floor: the record's math, its writer/loader round trip,
and the stopping thresholds derived from it (issue #3488).

The ``derive_repeat_floor`` vectors ride the REAL
:func:`~jasper.active_speaker.crossover_v2.round_views.repeatability_spread`
over rounds built by the round-views suite's own ``_make_round_dir`` builder,
so this file never hand-types a packet the product would have to keep in step.
"""

from __future__ import annotations

import json

import pytest

from jasper.active_speaker.attempts_loop import CLAIM_FLOOR_P95_MULTIPLE
from jasper.active_speaker.crossover_v2.round_views import (
    load_banked_round,
    repeatability_spread,
)
from jasper.active_speaker.repeat_floor import (
    REPEAT_FLOOR_KIND,
    SCHEMA_VERSION,
    SHIPPED_POOL_METRIC,
    derive_repeat_floor,
    load_repeat_floor,
    pairwise_abs_delta_p95,
    stopping_thresholds,
    write_repeat_floor,
)

from tests.test_active_speaker_crossover_v2_round_views import (
    _flat_curve,
    _make_round_dir,
)


def _record(p95: float) -> dict:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": REPEAT_FLOOR_KIND,
        "measured_at": "2026-09-01T00:00:00Z",
        "n_repeats": 4,
        "aggregate_metric": SHIPPED_POOL_METRIC,
        "rounds": [],
        "metrics": {
            SHIPPED_POOL_METRIC: {
                "n": 4, "mean_db": 1.5, "sd_db": 1.29, "range_db": 3.0,
                "min_db": 0.0, "max_db": 3.0,
                "pairwise_abs_delta_p95_db": p95,
            },
        },
        "note": "",
    }


# --------------------------------------------------------------------------- #
# pairwise_abs_delta_p95
# --------------------------------------------------------------------------- #


def test_pairwise_p95_over_a_hand_derivable_set():
    """[0, 1, 2, 3] -> |delta| = {1,1,1,2,2,3}; the linear-interpolated 95th
    percentile of that sorted set is 2.75."""
    assert pairwise_abs_delta_p95([0.0, 1.0, 2.0, 3.0]) == pytest.approx(2.75)


@pytest.mark.parametrize("values", [[], [1.0]])
def test_pairwise_p95_needs_two_values_to_have_a_difference(values):
    assert pairwise_abs_delta_p95(values) is None


# --------------------------------------------------------------------------- #
# stopping_thresholds
# --------------------------------------------------------------------------- #


def test_stopping_thresholds_derive_plateau_and_margin_from_the_aggregate_p95():
    thresholds = stopping_thresholds(_record(2.75))
    assert thresholds is not None
    assert thresholds["noise_p95_db"] == pytest.approx(2.75)
    assert thresholds["plateau_db"] == pytest.approx(2.75)
    assert thresholds["margin_db"] == pytest.approx(CLAIM_FLOOR_P95_MULTIPLE * 2.75)
    # round_evidence calls plateau = margin/2 load-bearing; the derivation
    # must not invert it.
    assert thresholds["plateau_db"] < thresholds["margin_db"]
    assert thresholds["n_repeats"] == 4


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "0.4", True])
def test_stopping_thresholds_refuse_a_row_that_is_not_a_finite_number(bad):
    record = _record(0.4)
    record["metrics"][SHIPPED_POOL_METRIC]["pairwise_abs_delta_p95_db"] = bad
    assert stopping_thresholds(record) is None


def test_stopping_thresholds_refuse_a_record_with_no_aggregate_row():
    record = _record(0.4)
    record["metrics"] = {}
    assert stopping_thresholds(record) is None


# --------------------------------------------------------------------------- #
# write / load
# --------------------------------------------------------------------------- #


def test_write_then_load_round_trips_the_record(tmp_path):
    path = tmp_path / "repeat-floor.json"
    written = write_repeat_floor(_record(0.4), state_path=path)
    assert written["state_path"] == str(path)
    loaded = load_repeat_floor(state_path=path)
    assert loaded == written


def test_load_returns_none_for_a_missing_file(tmp_path):
    assert load_repeat_floor(state_path=tmp_path / "nope.json") is None


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda r: r.update(kind="something_else"), id="wrong-kind"),
    pytest.param(
        lambda r: r.update(artifact_schema_version=SCHEMA_VERSION + 1),
        id="wrong-schema",
    ),
])
def test_load_returns_none_for_a_record_it_does_not_own(tmp_path, mutate):
    path = tmp_path / "repeat-floor.json"
    record = _record(0.4)
    mutate(record)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert load_repeat_floor(state_path=path) is None


@pytest.mark.parametrize("blob", ["{not json", "[]", ""])
def test_load_returns_none_for_an_unparseable_file(tmp_path, blob):
    path = tmp_path / "repeat-floor.json"
    path.write_text(blob, encoding="utf-8")
    assert load_repeat_floor(state_path=path) is None


# --------------------------------------------------------------------------- #
# derive_repeat_floor — over the REAL repeatability view
# --------------------------------------------------------------------------- #


def _rounds(tmp_path, names):
    return [
        (
            name,
            load_banked_round(
                _make_round_dir(
                    tmp_path, name,
                    position_curves={"cloud_verify_02": ("onax", _flat_curve(ripple_db=ripple))},
                )
            ),
        )
        for name, ripple in names
    ]


def test_derive_reads_every_metric_the_repeatability_view_graded(tmp_path):
    rounds = _rounds(tmp_path, [("r1", 0.0), ("r2", 0.6), ("r3", 1.2)])
    result = repeatability_spread(rounds)
    payload = derive_repeat_floor(
        result,
        rounds=[
            {"label": label, "bundle_session_id": banked.packet["session"]["bundle_session_id"],
             "graph_fingerprint": None, "mic_calibration_id": "", "started_at": 1.0}
            for label, banked in rounds
        ],
    )

    assert payload["kind"] == REPEAT_FLOOR_KIND
    assert payload["artifact_schema_version"] == SCHEMA_VERSION
    assert payload["n_repeats"] == len(rounds)
    assert payload["aggregate_metric"] == SHIPPED_POOL_METRIC
    assert [row["label"] for row in payload["rounds"]] == ["r1", "r2", "r3"]
    assert SHIPPED_POOL_METRIC in payload["metrics"]
    for row in payload["metrics"].values():
        assert set(row) >= {"n", "sd_db", "pairwise_abs_delta_p95_db"}
    assert stopping_thresholds(payload) is not None


def test_derive_refuses_a_single_round_which_has_no_spread(tmp_path):
    rounds = _rounds(tmp_path, [("only", 0.0)])
    result = repeatability_spread(rounds)
    with pytest.raises(ValueError):
        derive_repeat_floor(result, rounds=[])
