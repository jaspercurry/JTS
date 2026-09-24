# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the cabinet-model scripts owe (ADR-0353): they load against the repo's own evaluators,
Boundary Lab's exp(-iwt) phasors are read into the repo's exp(+iwt) convention, and the seat
model's wall notch sits where the geometry puts it. The case is two synthetic point sources."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

TOOLKIT = Path(__file__).resolve().parents[1] / "scripts" / "cabinet-model"
C = 343.0
HALF_SPACING_M = 0.1
DEPTH_M, WALL_GAP_M = 0.25, 0.2


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
             far_front=np.exp(1j * k * (10.0 - along)) / 10.0, far_rear=np.exp(1j * k * (10.0 + along)) / 10.0)
    raw = {"freqs_hz": np.geomspace(10, 5000, 2000).tolist(), "level_db": [0.0] * 2000}
    (tmp_path / "nearfield_view.json").write_text(json.dumps({"drivers": [
        {"driver": driver, "placements": [{"distance_mm": 15.0, "raw": raw}]} for driver in ("woofer", "woofer:rear")]}))
    return model, model.Cabinet(tmp_path / "transfer.npz", tmp_path / "nearfield_view.json", f)


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
