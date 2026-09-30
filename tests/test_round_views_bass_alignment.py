# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The sealed-box alignment fit (#5928 TB9): each driver's near-field curve, or
one bass take as played, read as the corner and Q a Linkwitz transform starts from."""

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.round_view_artifacts import PROG
from jasper.cli import round_views
from tests.run_manifest_fixture import write_manifest
from tests.test_crossover_v2_nearfield_view import FREQS, _graph, _take


def _box_db(corner_hz: float, q: float) -> np.ndarray:
    """A sealed box's 2nd-order high-pass over the banked grid."""
    s = 1j * FREQS / corner_hz
    return 20.0 * np.log10(np.abs(s * s / (s * s + s / q + 1.0)))


def _sealed(take: dict, corner_hz: float, q: float) -> dict:
    """``take`` as a sealed box of this corner and Q radiates it, played through a unity graph."""
    curve, = take["curves"]
    for sweep in (curve, *curve["repeat_curves"]):
        sweep["magnitude_db"] = (np.asarray(sweep["magnitude_db"]) + _box_db(corner_hz, q)).tolist()
    return {**take, "provenance": {"graph": {"config": _graph(0.0)}}}


def _bundle(root: Path, program: str, takes: list[dict], basis: dict) -> Path:
    bundle = root / "sessions" / program.replace("/", "-")
    bundle.mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": bundle.name}))
    write_manifest(bundle, program=program, groups=[{"set_id": program.split("/")[0], "capture_basis": basis,
                                                     "takes": takes}])
    return bundle


def nearfield_round(root: Path) -> Path:
    """jts3's woofers as the cabinet-model README fits them: the front one at 15
    and 30 mm, the rear one at 15 mm; and a tweeter that swept 700 Hz-2 kHz only."""
    return _bundle(root, "nearfield/each", [
        _sealed(_take("w15", "woofer", 15, 90.0), 84.1, 1.02),
        _sealed(_take("w30", "woofer", 30, 87.9, seed=1), 84.1, 1.02),
        _sealed(_take("r15", "woofer:rear", 15, 84.0, seed=2), 89.0, 1.09),
        _sealed(_take("t15", "tweeter", 15, 80.0, seed=3, band_hz=(700.0, 2000.0)), 900.0, 0.7),
    ], {})


def test_each_driver_fits_at_its_nearest_placement_and_a_curve_that_cannot_place_its_corner_is_a_gap(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert round_views.main(["bass-alignment", str(nearfield_round(tmp_path))]) == round_views.EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    fits = {fit["role"]: fit for fit in answer["fits"]}
    assert {role: (fit["distance_mm"], fit["take_ids"], fit["source_hz"], fit["source_q"])
            for role, fit in fits.items() if fit["status"] == "available"} == {
        "woofer": (15.0, ["w15"], pytest.approx(84.1, abs=0.2), pytest.approx(1.02, abs=0.02)),
        "woofer:rear": (15.0, ["r15"], pytest.approx(89.0, abs=0.2), pytest.approx(1.09, abs=0.02))}
    assert (fits["tweeter"]["status"], fits["tweeter"]["reason"]) == ("unavailable", "coverage_short")
    filed = {fit["role"]: fit for fit in json.loads(Path(answer["out"]).read_text())["fits"]}
    assert len(filed["woofer"]["model_db"]) == len(filed["woofer"]["measured_db"]) > 3


def test_a_bass_takes_catalog_call_fits_its_curve_as_played_and_files_where_the_catalog_says(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    curve = {"role": "summed", "freqs_hz": FREQS.tolist(), "band_hz": [20.0, 20000.0],
             "magnitude_db": (80.0 + _box_db(55.0, 0.8)).tolist()}
    bundle = _bundle(tmp_path, "bass/axis", [{"take_id": "b0", "selected": True, "curves": [curve],
                                              "pose": {"kind": "bearing", "deg": 0, "elevation_deg": 0}}],
                     {"role": "summed"})
    assert round_views.main(["catalog", str(bundle)]) == round_views.EXIT_OK
    call, = (call for tool in json.loads(capsys.readouterr().out)["tools"]
             if tool["tool"] == f"{PROG} bass-alignment --take" for call in tool["calls"])

    assert round_views.main(call["argv"][1:]) == round_views.EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    fit, = answer["fits"]
    assert (fit["role"], fit["take_ids"], fit["source_hz"], fit["source_q"]) == (
        "summed", ["b0"], pytest.approx(55.0, abs=0.2), pytest.approx(0.8, abs=0.02))
    assert (answer["subject"]["set_id"], answer["out"]) == ("bass", call["out"])
