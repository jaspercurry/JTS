# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparing takes the way REW compares traces: one window, one smoothing, b minus a."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import capture_prediction
from jasper.active_speaker.crossover_v2.round_captures import PoseCapture
from jasper.active_speaker.crossover_v2.round_inputs import COMPARAND_EARLIER_ROUND, COMPARAND_SAME_ROUND
from jasper.audio_measurement.evidence_reasons import EvidenceUnavailable
from jasper.active_speaker.crossover_v2.take_reading import (
    REFUSE_COMPARE_NO_COMMON_BAND, REFUSE_COMPARE_NO_COMPARAND, TakeRead, compare_preview_report, compare_report,
    decay_report, group_delay_report, read_preview,
)
from jasper.cli import round_views
from jasper.cli._refusal import EXIT_REFUSED
from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.test_audio_measurement_decay import _decay

RATE = 48_000
ORIGIN = 12_000


def _take(capture_id: str, *, role: str = "summed", delay: int = 100, gain: float = 1.0,
          gate_ms: float | None = 8.0, band: tuple[float, float] = (100.0, 20_000.0),
          record: dict | None = None, echo_ms: float | None = None, ir: np.ndarray | None = None) -> TakeRead:
    if ir is None:
        ir = np.zeros(36_000)
        ir[ORIGIN + delay] = gain
        if echo_ms is not None:
            ir[ORIGIN + delay + round(echo_ms * RATE / 1000)] = gain / 2
    return TakeRead(PoseCapture(
        capture_id=capture_id, phase=None, wav=None, program=None, program_sha256="",
        azimuth_deg=0.0, vertical_deg=0.0, mark_distance_m=1.0, radiated_band_hz=band,
        sample_rate=RATE, ir=ir, peak_idx=int(np.argmax(np.abs(ir))),
        preprocessing={"impulse_source": "kept", "pre_guard_samples": ORIGIN, "clock_shift_samples": 0.0},
        curve={"gate_window_ms": gate_ms} if gate_ms else {}, record_document=record or {},
    ), role)


def test_b_minus_a_reads_a_level_change_unless_the_level_is_removed():
    louder = compare_report(_take("t1"), _take("t2", gain=2.0))
    shape_only = compare_report(_take("t1"), _take("t2", gain=2.0), remove_level=True)

    assert [band["b_minus_a_db"] for band in louder["summary"]["bands"]] == pytest.approx(
        [6.02] * len(louder["summary"]["bands"]), abs=0.01)
    assert (shape_only["summary"]["level_offset_db"], shape_only["summary"]["rms_db"]) == (
        pytest.approx(6.02, abs=0.01), pytest.approx(0.0, abs=0.01))


def test_arrival_compares_only_within_one_recording():
    woofer, tweeter = _take("t1", role="woofer", delay=100), _take("t1", role="tweeter", delay=125)
    within = compare_report(woofer, tweeter)["summary"]
    across = compare_report(woofer, _take("t2", role="tweeter", delay=125))["summary"]

    assert (within["same_recording"], within["relative_arrival_ms"]) == (True, pytest.approx(25 / 48, abs=1e-3))
    assert (across["same_recording"], across["relative_arrival_ms"]) == (False, None)


def test_a_different_microphone_is_disclosed_not_refused():
    def mic(calibration_id: str) -> dict:
        return {"capture_calibration": {"applied": True, "calibration_id": calibration_id,
                                        "curve_fingerprint": f"fp-{calibration_id}"}}

    report = compare_report(_take("t1", record=mic("umik-a")), _take("t2", record=mic("umik-b")))

    assert report["summary"]["basis"]["basis_status"] == "incompatible"
    assert "capture_calibration" in report["summary"]["basis"]["incompatible_fields"]


def test_both_sides_are_read_through_the_shorter_take_window_unless_one_is_named():
    a, b = _take("t1", gate_ms=8.0), _take("t2", gate_ms=5.0)

    assert compare_report(a, b)["parameters"]["window_ms"] == 5.0
    assert compare_report(a, b, window_ms=20.0)["parameters"]["window_ms"] == 20.0


def test_sides_that_share_no_trusted_band_are_refused_by_name():
    with pytest.raises(EvidenceUnavailable) as refused:
        compare_report(_take("t1", band=(100.0, 300.0)), _take("t2", band=(2000.0, 8000.0)))
    assert refused.value.reason == REFUSE_COMPARE_NO_COMMON_BAND


def test_a_forecast_is_compared_through_its_own_window():
    """A forecast of the take itself, gated as forecasts are, reads as no error,
    with a reflection inside the window where another taper would differ."""
    measured = _take("t1", gate_ms=None, echo_ms=4.0)
    freqs = np.fft.rfftfreq(capture_prediction.N_FFT, 1 / RATE)
    in_band = (freqs >= 300.0) & (freqs <= 18_000.0)
    segment, _ = capture_prediction.gated_segment(
        measured.capture.ir, RATE, gate_ms=7.0, peak_idx=measured.capture.peak_idx)
    grid = freqs[in_band]
    forecast = 20 * np.log10(np.abs(np.fft.rfft(segment, n=capture_prediction.N_FFT)[in_band])) - 20.0
    preview = read_preview({"section": "emitted_graph", "preview": {
        "kind": "jts_capture_prediction",
        "summary": {"window": {"window_ms": 7.0, "lead_ms": 1.0}, "candidate_id": "cand", "basis": {},
                    "prediction_fingerprint": "f" * 64},
        "prediction": {"freqs_hz": grid.tolist(), "predicted_db": forecast.tolist(),
                       "sum_band_hz": [300.0, 18_000.0]},
    }})
    report = compare_preview_report(preview, measured)

    assert report["parameters"]["window_ms"] == 7.0
    assert report["summary"]["level_offset_db"] == pytest.approx(20.0, abs=0.05)
    assert report["summary"]["rms_db"] < 0.05


def test_a_take_decays_from_its_own_onset_over_its_swept_band():
    report = decay_report(_take("t1", ir=_decay(0.4, -80.0), band=(100.0, 20_000.0)))
    bands = {band["hz"]: band for band in report["summary"]["bands"]}

    assert bands[2000.0]["t20_s"] == pytest.approx(0.4, rel=0.05)
    assert min(bands) == 125.0
    assert report["summary"]["kept_after_onset_ms"] == pytest.approx(500.0, abs=5.0)


def test_an_ungated_take_reads_no_further_than_its_impulse_holds():
    short = np.zeros(ORIGIN + 100 + 4_800)
    short[ORIGIN + 100] = 1.0

    assert _take("t1", gate_ms=None, ir=short).window() == (pytest.approx(99.98, abs=0.05), "retained")
    assert _take("t1", gate_ms=None, ir=np.pad(short, (0, 48_000))).window() == (500.0, "ungated")


def test_a_read_says_which_window_it_used_and_bands_only_what_it_read():
    short = _take("t2", ir=_take("t2").capture.ir[:ORIGIN + 100 + round(0.01 * RATE)])
    compared = compare_report(_take("t1"), short, window_ms=20.0)["parameters"]
    timing = group_delay_report(_take("t1", band=(100.0, 900.0), gate_ms=7.0))
    lo, hi = timing["parameters"]["band_hz"]

    assert (compared["window_ms"] < 20.0, compared["window_source"]) == (True, "shorter take window (argument, retained)")
    assert all(lo <= band["hz"] <= hi for band in timing["summary"]["bands"])


def _banked(store: Path, name: str, banked_at: str, sets: dict[str, list[tuple]]) -> Path:
    """One banked round of bare-delta takes under its own session: each set's
    takes as ``(take_id, bearing[, run_id, ended_s])``; a set named ``base…`` is base."""
    rows = {set_id: [(*take, None, 0)[:4] for take in takes] for set_id, takes in sets.items()}
    takes = [take for set_takes in rows.values() for take in set_takes]
    ir = np.zeros(4800)
    ir[480] = 1.0
    root = bank_capture_round(store / name, [ir] * len(takes), capture_ids=[take[0] for take in takes],
                              positions_deg=[take[1] for take in takes])
    session = (root / "bundle" / "b0").rename(root / "bundle" / name)
    docs = {doc["position_id"]: (str(path.relative_to(session)), doc)
            for path in session.glob("summed/*.json") for doc in [json.loads(path.read_text())]}
    groups = []
    for set_id, set_takes in rows.items():
        group = manifest_set([docs[take[0]] for take in set_takes], set_id=set_id)
        group["base"] = set_id.startswith("base")
        for row, (*_, run_id, ended_s) in zip(group["takes"], set_takes):
            row.update(run_id=run_id, timing={"ended_s": ended_s})
        groups.append(group)
    write_manifest(root, groups=groups)
    (root / "provenance.json").write_text(json.dumps({"banked_at_utc": banked_at}))
    return root


@pytest.mark.parametrize("rounds,flags,expected", [
    pytest.param({"r": {"base": [("r0", 0)], "cand": [("c0", 0)]}, "e": {"base": [("e0", 0)]}},
                 ["--b-set", "cand", "--b-take", "c0"], (COMPARAND_SAME_ROUND, "r", "r0"), id="same-round-base"),
    pytest.param({"r": {"base-1": [("b1", 0, "run-1", 1)], "base-2": [("b2", 0, "run-2", 3)],
                        "cand": [("c1", 0, "run-1", 2)]}},
                 ["--b-set", "cand", "--b-take", "c1"], (COMPARAND_SAME_ROUND, "r", "b1"), id="its-own-run"),
    pytest.param({"e": {"base": [("e0", 0)]}, "r": {"base": [("r30", 30)], "cand": [("c0", 0)]}},
                 ["--b-set", "cand", "--b-take", "c0"], (COMPARAND_EARLIER_ROUND, "e", "e0"), id="no-base-here"),
    pytest.param({"o": {"base": [("o0", 0)]}, "e": {"base": [("e30", 30), ("e0", 0)]},
                  "r": {"base": [("r0", 0)]}, "later": {"base": [("l0", 0)]}},
                 [], (COMPARAND_EARLIER_ROUND, "e", "e0"), id="newest-earlier"),
    pytest.param({"r": {"base": [("r0", 0)], "cand": [("c0", 0)]}},
                 ["--a-set", "cand", "--a-take", "c0", "--b-set", "base"], (None, "r", "c0"), id="side-a-named"),
    pytest.param({"e": {"base": [("e30", 30)]}, "r": {"base": [("r0", 0)]}}, [], None, id="none"),
])
def test_compare_with_one_take_reads_its_comparand_by_the_one_rule(tmp_path, capsys, rounds, flags, expected):
    """ADR-0391: side A left unnamed is B's comparand, and the answer says how it
    was found beside the capture-basis disclosure; with none, compare refuses by name."""
    store = tmp_path / "campaigns"
    paths = {name: _banked(store, name, f"2026-09-{20 + index:02d}T12:00:00Z", sets)
             for index, (name, sets) in enumerate(rounds.items())}

    code = round_views.main(["compare", str(paths["r"]), *flags])
    answer = json.loads(capsys.readouterr().out)

    if expected is None:
        assert (code, answer["reason"]) == (EXIT_REFUSED, REFUSE_COMPARE_NO_COMPARAND)
        return
    a, _b = answer["subject"]["rounds"]
    assert (code, answer["comparand"], a["round_id"], a["take_ids"]) == (0, *expected[:2], [expected[2]])
    assert set(answer["basis"]) == {"basis_status", "intervention_fields", "incompatible_fields",
                                    "mismatched_fields", "unknown_fields"}
