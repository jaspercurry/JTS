# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the cabinet-model scripts owe (ADR-0353): they load against the repo's own evaluators,
Boundary Lab's exp(-iwt) phasors are read into the repo's exp(+iwt) convention, the seat
model's wall notch sits where the geometry puts it, and the model check reads gated far-field
takes against the model at the microphone. The case is two synthetic point sources."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

TOOLKIT = Path(__file__).resolve().parents[1] / "scripts" / "cabinet-model"
C = 343.0
HALF_SPACING_M = 0.1
DEPTH_M, WALL_GAP_M = 0.25, 0.2
MIC_M = 0.5
#: Each woofer's path to each microphone by (driver, side): the sources sit HALF_SPACING_M either
#: side of the front face, the microphones MIC_M before it and MIC_M behind the back panel.
MIC_PATH_M = {("woofer", "front"): MIC_M - HALF_SPACING_M, ("woofer:rear", "front"): MIC_M + HALF_SPACING_M,
              ("woofer", "behind"): DEPTH_M + MIC_M + HALF_SPACING_M,
              ("woofer:rear", "behind"): DEPTH_M + MIC_M - HALF_SPACING_M}
SOURCE = {"woofer": "front", "woofer:rear": "rear"}
KIND = {"front": "bearing", "behind": "behind"}
#: A far-field raw curve starts at its sweep's 150 Hz; the measurement sits this far above the model.
FAR_FREQS, OFFSET_DB = np.geomspace(150.0, 2000.0, 120), 3.0
BUMP_HZ = float(FAR_FREQS[np.argmin(np.abs(FAR_FREQS - 300.0))])


def _load(filename, monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLKIT))
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py").replace("-", "_"), TOOLKIT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cabinet(tmp_path, monkeypatch):
    """Front source HALF_SPACING_M ahead of the polar origin, rear source as far behind; both
    radiate as monopoles and read 1 at their near-field spot. Far field as Boundary Lab stores it."""
    model = _load("_cabinet.py", monkeypatch)
    f = np.geomspace(20, 1000, 400)
    angles = np.arange(0, 361, 5.0)
    k = (2 * np.pi * f / C)[:, None]
    along = HALF_SPACING_M * np.cos(np.radians(angles))[None, :]
    np.savez(tmp_path / "transfer.npz", f=f, angles_deg=angles, radius_m=10.0, front_z_m=0.1, depth_m=DEPTH_M,
             nf_front=np.ones(f.size, complex), nf_rear=np.ones(f.size, complex),
             far_front=np.exp(1j * k * (10.0 - along)) / 10.0, far_rear=np.exp(1j * k * (10.0 + along)) / 10.0,
             mic_m=MIC_M, **{f"mic_{side}_{SOURCE[driver]}": np.exp(1j * k[:, 0] * path) / path
                             for (driver, side), path in MIC_PATH_M.items()})
    # The near-field view exports a raw curve over its sweep's band only; a re-run replaces the rear woofer's.
    views = {"nearfield_view.json": ("woofer", "woofer:rear"), "rerun_view.json": ("woofer:rear",)}
    for name, drivers in views.items():
        raw = {"take_ids": [name], "freqs_hz": np.geomspace(20, 2000, 2000).tolist(), "level_db": [0.0] * 2000}
        (tmp_path / name).write_text(json.dumps({"drivers": [
            {"driver": driver, "placements": [{"distance_mm": 15.0, "raw": raw}]} for driver in drivers]}))
    return model, model.Cabinet(tmp_path / "transfer.npz", [tmp_path / name for name in views], f)


def _farfield_view(window_ms=(), drop=()):
    """The nearfield view of a drivers/each round: each woofer alone at MIC_M in front and behind,
    its raw curve the monopole's level there plus OFFSET_DB, gated at 20 ms unless ``window_ms``
    names another (``None`` is ungated); ``drop`` leaves those rows' placements out."""
    takes, drivers = [], {driver: [] for driver in SOURCE}
    for (driver, side), path in MIC_PATH_M.items():
        take_id, gate_ms = f"{driver}/{side}", dict(window_ms).get((driver, side), 20.0)
        takes.append({"take_id": take_id, "gate": {"window": "ungated"} if gate_ms is None else {
            "window": "gated", "window_ms": gate_ms, "validity_floor_hz": 1000.0 / gate_ms,
            "trusted_floor_hz": 2500.0 / gate_ms, "floor_source": "search_span_bound"}})
        if (driver, side) not in drop:
            drivers[driver].append({"distance_mm": MIC_M * 1000.0, "kind": KIND[side], "take_ids": [take_id],
                                    "trusted_band": {"high_hz": 26_000.0}, "raw": {
                                        "take_ids": [take_id], "freqs_hz": FAR_FREQS.tolist(),
                                        "level_db": [20.0 * np.log10(1.0 / path) + OFFSET_DB] * FAR_FREQS.size}})
    return {"takes": takes, "drivers": [{"driver": driver, "placements": at} for driver, at in drivers.items()]}


@pytest.mark.parametrize("filename", sorted(p.name for p in TOOLKIT.glob("*.py")))
def test_script_loads(filename, monkeypatch):
    _load(filename, monkeypatch)


def test_inverted_rear_delayed_by_the_travel_time_nulls_behind(cabinet):
    _, cab = cabinet
    r = -np.exp(-2j * np.pi * cab.grid * 2 * HALF_SPACING_M / C)
    assert np.max(np.abs(cab.at_angle(r, 180))) < 1e-9 * np.max(np.abs(cab.at_angle(r, 0)))


def test_seat_notch_sits_at_the_quarter_wave_of_the_source_to_wall_trip(cabinet):
    model, cab = cabinet
    seat = np.abs(cab.seat(np.zeros(cab.grid.size), listener_m=2.0, wall_gap_m=WALL_GAP_M))
    band = (cab.grid > 100) & (cab.grid < 400)
    notch_hz = cab.grid[band][np.argmin(seat[band])]
    assert notch_hz == pytest.approx(model.C / (4 * (HALF_SPACING_M + DEPTH_M + WALL_GAP_M)), rel=0.01)


def test_the_model_is_read_through_the_takes_gate(cabinet):
    """The model is read as the analysis reads a gated take: an echo 8 ms after the arrival
    ripples a 20 ms read, and a 5 ms gate takes it out."""
    model, _ = cabinet
    echoed = 1.0 + 0.5 * np.exp(-2j * np.pi * np.fft.rfftfreq(model.NFFT, 1 / model.FS) * 0.008)
    assert [np.ptp(model.gated_db(echoed, window_ms, FAR_FREQS[FAR_FREQS <= 600.0])) > 1.0 for window_ms in (20.0, 5.0)] == [
        True, False]


@pytest.mark.parametrize("shift_db,moved", [
    ({}, {}),
    ({("woofer:rear", "behind"): 2.0 * (FAR_FREQS == BUMP_HZ)},
     {("woofer:rear", "behind"): {"max_abs_db": 2.0, "max_abs_hz": BUMP_HZ}}),
    ({("woofer:rear", side): 1.5 for side in KIND}, {("woofer:rear", side): {"level_db": 1.5, "max_abs_db": 1.5} for side in KIND}),
], ids=["matched", "bump_300_hz_behind", "rear_1_5_db_louder"])
def test_the_model_check_reads_each_woofer_alone_in_front_and_behind_on_one_anchor(cabinet, monkeypatch, shift_db, moved):
    """measured - model per woofer and side over [gate floor, 600 Hz], from the raw's own 150 Hz
    under a 20 ms gate. One offset, the front woofer's in front at 400-600 Hz, comes off every row:
    a bump moves its row alone, and a louder rear woofer keeps its level and misses in front. A
    later near-field view replaces an earlier one's curve for the woofer it has."""
    _, cab = cabinet
    view = _farfield_view()
    for (driver, side), db in shift_db.items():
        placement, = (one for entry in view["drivers"] if entry["driver"] == driver
                      for one in entry["placements"] if one["kind"] == KIND[side])
        placement["raw"]["level_db"] = (np.asarray(placement["raw"]["level_db"]) + db).tolist()

    check = _load("predict.py", monkeypatch).model_check(cab, view)

    rows = {(row["driver"], row["side"]): row for row in check["rows"]}
    expected = {key: {"level_db": 0.0, "max_abs_db": 0.0, **moved.get(key, {})} for key in MIC_PATH_M}
    assert check["nearfield_take_ids"] == {"woofer": ["nearfield_view.json"], "woofer:rear": ["rerun_view.json"]}
    assert check["anchor_offset_db"] == pytest.approx(OFFSET_DB, abs=0.05)
    assert {key: (row["missing"], row["band_hz"]) for key, row in rows.items()} == dict.fromkeys(
        MIC_PATH_M, (None, [150.0, 600.0]))
    assert {key: {field: rows[key][field] for field in want} for key, want in expected.items()} == {
        key: {field: pytest.approx(value, abs=0.2) for field, value in want.items()} for key, want in expected.items()}
    assert [rows[driver, "front"]["within_1_db"] for driver in SOURCE] == [
        expected[driver, "front"]["max_abs_db"] <= 1.0 for driver in SOURCE]


@pytest.mark.parametrize("changes,key,expected,status", [
    ({"drop": [("woofer:rear", "behind")]}, ("woofer:rear", "behind"), {"missing": "no_placement"}, 1),
    ({"window_ms": [(("woofer", "behind"), None)]}, ("woofer", "behind"), {"missing": "not_gated"}, 1),
    ({"window_ms": [(("woofer:rear", "front"), 5.0)]}, ("woofer:rear", "front"), {"missing": None, "band_hz": [200.0, 600.0]}, 0),
], ids=["no_placement", "not_gated", "short_gate"])
def test_a_row_the_round_cannot_answer_is_named_and_a_short_gate_raises_its_floor(
        tmp_path, cabinet, monkeypatch, changes, key, expected, status):
    """A row with no placement or an ungated take is named, and the check exits 1 after
    printing the rest; a 5 ms gate starts its row at 1/T, 200 Hz."""
    _, cab = cabinet
    view = _farfield_view(**changes)
    (tmp_path / "farfield_view.json").write_text(json.dumps(view))
    predict = _load("predict.py", monkeypatch)

    assert predict.main(["--transfer", str(tmp_path / "transfer.npz"), "--nearfield", str(tmp_path / "nearfield_view.json"),
                         "--farfield", str(tmp_path / "farfield_view.json")]) == status
    row, = (row for row in predict.model_check(cab, view)["rows"] if (row["driver"], row["side"]) == key)
    assert {field: row.get(field) for field in expected} == expected
