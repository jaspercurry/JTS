# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparing takes the way REW compares traces: one window, one smoothing, b minus a."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import capture_prediction
from jasper.active_speaker.crossover_v2.round_captures import PoseCapture
from jasper.active_speaker.crossover_v2.round_inputs import COMPARAND_EARLIER_ROUND, COMPARAND_SAME_ROUND, comparand
from jasper.active_speaker.crossover_v2.take_impulses import write_take_impulses
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from jasper.audio_measurement.evidence_reasons import EvidenceUnavailable
from jasper.platform.json_fields import parse_utc_iso
from jasper.active_speaker.crossover_v2.take_reading import (
    REFUSE_COMPARE_NO_COMMON_BAND, REFUSE_COMPARE_NO_COMPARAND, TakeRead, compare_preview_report, compare_report,
    decay_report, group_delay_report, read_preview,
)
from jasper.cli import round_views
from jasper.cli._refusal import EXIT_REFUSED
from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.test_audio_measurement_decay import _decay
from tests.test_take_impulses import _response

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


def _banked(store: Path, name: str, banked_at: str, sets: dict) -> Path:
    """One banked round of bare-delta takes under its own session. A set is its
    takes, ``(take_id, bearing[, run_id, ended_s[, selected]])``, or :func:`_set`'s
    takes and capture-basis fields; a set named ``base…`` is base, and a driver
    ``role`` set's takes keep that role's impulse beside their recording."""
    specs = {set_id: spec if isinstance(spec, tuple) else (spec, {}) for set_id, spec in sets.items()}
    rows = {set_id: [take + (None, 0, True)[len(take) - 2:] for take in takes] for set_id, (takes, _) in specs.items()}
    takes = [take for set_takes in rows.values() for take in set_takes]
    ir = np.zeros(4800)
    ir[480] = 1.0
    root = bank_capture_round(store / name, [ir] * len(takes), capture_ids=[take[0] for take in takes],
                              positions_deg=[take[1] for take in takes])
    session = (root / "bundle" / "b0").rename(root / "bundle" / name)
    (session / "info.json").write_text(json.dumps({"bundle_schema_version": 1}))  # Kept impulses record artifacts.
    docs = {doc["position_id"]: (str(path.relative_to(session)), doc)
            for path in session.glob("summed/*.json") for doc in [json.loads(path.read_text())]}
    groups = []
    for set_id, set_takes in rows.items():
        basis = specs[set_id][1]
        for take_id, *_ in set_takes if basis.get("role") else ():
            record, doc = docs[take_id]
            doc["impulses"] = write_take_impulses(session, take_id, SimpleNamespace(
                summed_response=None, driver_responses=(_response(basis["role"], 300),)), recording=doc["wav_path"])
            (session / record).write_text(json.dumps(doc))
        group = manifest_set([docs[take[0]] for take in set_takes], set_id=set_id)
        group["base"] = set_id.startswith("base")
        group["capture_basis"].update(basis)
        for row, (*_, run_id, ended_s, selected) in zip(group["takes"], set_takes):
            row.update(run_id=run_id, timing={"ended_s": ended_s}, selected=selected)
        groups.append(group)
    write_manifest(root, groups=groups)
    (root / "provenance.json").write_text(json.dumps({"banked_at_utc": banked_at}))
    return root


def _set(*takes: tuple, **basis: str) -> tuple:
    """A set's takes and the capture-basis fields it differs by: ``side``, ``role`` or ``graph_scope``."""
    return takes, basis


def _case(case_id: str, rounds: dict, flags: list[str], expected: tuple | None, *, b: str = "bank",
          newer: int = 0, roles: tuple[str, str] = ("summed", "summed")):
    """B's round is ``r``: named by its bank, by a live copy of its bundle
    (``live``), or by a live bundle no bank holds (``unbanked``). ``newer``
    plain directories are modified after every round."""
    return pytest.param(rounds, flags, b, newer, list(roles), expected, id=case_id)


_SAME, _EARLIER = COMPARAND_SAME_ROUND, COMPARAND_EARLIER_ROUND
_AROUND_R = {"e": {"base": [("e0", 0)]}, "r": {"cand": [("c0", 0)]}, "later": {"base": [("l0", 0)]}}


@pytest.mark.parametrize("rounds,flags,b,newer,roles,expected", [
    _case("same-round-base", {"r": {"base": [("r0", 0)], "cand": [("c0", 0)]}, "e": {"base": [("e0", 0)]}},
          ["--b-set", "cand", "--b-take", "c0"], (_SAME, "r", "r0")),
    _case("its-own-run", {"r": {"base-1": [("b1", 0, "run-1", 1)], "base-2": [("b2", 0, "run-2", 3)],
                                "cand": [("c1", 0, "run-1", 2)]}},
          ["--b-set", "cand", "--b-take", "c1"], (_SAME, "r", "b1")),
    _case("no-base-here", {"e": {"base": [("e0", 0)]}, "r": {"base": [("r30", 30)], "cand": [("c0", 0)]}},
          ["--b-set", "cand", "--b-take", "c0"], (_EARLIER, "e", "e0")),
    _case("newest-earlier", {"o": {"base": [("o0", 0)]}, "e": {"base": [("e30", 30), ("e0", 0)]},
                             "r": {"base": [("r0", 0)]}, "later": {"base": [("l0", 0)]}},
          [], (_EARLIER, "e", "e0")),
    _case("side-a-named", {"r": {"base": [("r0", 0)], "cand": [("c0", 0)]}},
          ["--a-set", "cand", "--a-take", "c0", "--b-set", "base"], (None, "r", "c0")),
    _case("none", {"e": {"base": [("e30", 30)]}, "r": {"base": [("r0", 0)]}}, [], None),
    # A newer base take at B's place that differs in one key field, or was not selected, never matches.
    _case("other-side", {"r": {"base": _set(("l0", 0, None, 1), side="left"),
                               "base-right": _set(("x0", 0, None, 2), side="right"),
                               "cand": _set(("c0", 0), side="left")}},
          ["--b-set", "cand"], (_SAME, "r", "l0")),
    _case("other-role", {"r": {"base": [("b0", 0, None, 1)], "base-woofer": _set(("x0", 0, None, 2), role="woofer"),
                               "cand": [("c0", 0)]}},
          ["--b-set", "cand"], (_SAME, "r", "b0")),
    _case("other-graph-scope", {"r": {"base": [("b0", 0, None, 1)],
                                      "base-drivers": _set(("x0", 0, None, 2), graph_scope="drivers"),
                                      "cand": [("c0", 0)]}},
          ["--b-set", "cand"], (_SAME, "r", "b0")),
    _case("unselected", {"r": {"base": [("b0", 0, None, 1), ("x0", 0, None, 2, False)], "cand": [("c0", 0)]}},
          ["--b-set", "cand"], (_SAME, "r", "b0")),
    # Named by a live bundle, B's round still dates by its bank copy, or else by when it started.
    _case("live-bank-copy", _AROUND_R, [], (_EARLIER, "e", "e0"), b="live"),
    _case("live-unbanked", _AROUND_R, [], (_EARLIER, "e", "e0"), b="unbanked"),
    # With B's own round, 32 directories modified after e: the whole window.
    _case("window", {"e": {"base": [("e0", 0)]}, "r": {"cand": [("c0", 0)]}}, [], None, newer=31),
    _case("two-roles", {"r": {"cand": _set(("c0", 0), role="woofer")}}, ["--a-role", "summed"], (None, "r", "c0"),
          roles=("summed", "woofer")),
])
def test_compare_with_one_take_reads_its_comparand_by_the_one_rule(
    tmp_path, capsys, monkeypatch, rounds, flags, b, newer, roles, expected,
):
    """ADR-0391: side A left unnamed is B's comparand, and the answer says how it
    was found beside the capture-basis disclosure; with none, compare refuses by
    name. A side A named by any ``--a-*`` flag reads no comparand."""
    monkeypatch.chdir(tmp_path)  # A live round's view lands beside the caller.
    store = tmp_path / "campaigns"
    dates = {name: f"2026-09-{20 + index:02d}T12:00:00Z" for index, name in enumerate(rounds)}
    paths = {name: _banked(store, name, dates[name], sets) for name, sets in rounds.items()}
    for index in range(newer):
        (store / f"newer-{index}").mkdir()
        os.utime(store / f"newer-{index}", (time.time() + 60,) * 2)
    b_round = paths["r"]
    if b != "bank":
        b_round = tmp_path / "sessions" / "r"
        shutil.copytree(paths["r"] / "bundle" / "r", b_round)
        if b == "unbanked":
            shutil.rmtree(paths["r"])
            (b_round / "info.json").write_text(json.dumps({"started_at": parse_utc_iso(dates["r"])}))

    code = round_views.main(["compare", str(b_round), *flags])
    answer = json.loads(capsys.readouterr().out)

    if expected is None:
        assert (code, answer["reason"]) == (EXIT_REFUSED, REFUSE_COMPARE_NO_COMPARAND)
        return
    a, _b = answer["subject"]["rounds"]
    assert (code, answer["comparand"], a["round_id"], a["take_ids"], answer["parameters"]["roles"]) == (
        0, *expected[:2], [expected[2]], roles)
    assert set(answer["basis"]) == {"basis_status", "intervention_fields", "incompatible_fields",
                                    "mismatched_fields", "unknown_fields"}


def test_the_comparand_rule_reads_run_manifest_rows_only(tmp_path):
    """ADR-0391's rule reads each round's rows (pose, run, take order), so no
    earlier round's take record is read to find a comparand (#5737 C1b)."""
    store = tmp_path / "campaigns"
    earlier = _banked(store, "e", "2026-09-20T12:00:00Z", {"base": [("e0", 0)]})
    this = _banked(store, "r", "2026-09-21T12:00:00Z", {"cand": [("c0", 0)]})
    for record in (earlier / "bundle" / "e").glob("summed/*.json"):
        record.write_text("{")

    found = comparand(this, "cand", "c0", "summed")

    assert found is not None and (found.source, found.round_dir, found.set_id, found.take_id) == (
        COMPARAND_EARLIER_ROUND, earlier, "base", "e0")


@pytest.mark.parametrize("malformed", [
    lambda manifest: manifest["sets"][0].pop("capture_basis"),
    lambda manifest: manifest["sets"][0].pop("takes"),
    lambda manifest: manifest["sets"][0]["takes"][0].pop("selected"),
], ids=["set-without-basis", "set-without-takes", "take-without-selected"])
def test_an_earlier_round_with_a_malformed_row_is_passed_over(tmp_path, malformed):
    """A default is a convenience, never a refusal (ADR-0101)."""
    store = tmp_path / "campaigns"
    older = _banked(store, "o", "2026-09-19T12:00:00Z", {"base": [("o0", 0)]})
    broken = _banked(store, "e", "2026-09-20T12:00:00Z", {"base": [("e0", 0)]})
    this = _banked(store, "r", "2026-09-21T12:00:00Z", {"cand": [("c0", 0)]})
    path, = broken.rglob(RUN_MANIFEST_FILENAME)
    manifest = json.loads(path.read_text())
    malformed(manifest)
    path.write_text(json.dumps(manifest))

    found = comparand(this, "cand", "c0", "summed")

    assert found is not None and (found.round_dir, found.take_id) == (older, "o0")
