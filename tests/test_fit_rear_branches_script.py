# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the rear-branch fitter guarantees about the document it writes.

``scripts/fit-rear-branches.py`` is a script, not a package module, and its
filename is not an identifier, so it is loaded by path.

The fit is a bounded LOCAL least squares, so these pin the properties the tool
owes, not a parameter vector: a target its own structure can realize is
reproduced, a measured sensitivity mismatch is undone instead of fitted into,
and whatever the target asks for, the document is inside
``rear_calibration``'s bounds. Truth targets are sampled at 1/12 octave --- a
1/3-octave table cannot represent a two-branch ratio (measured: 11 dB of
interpolation error against the very document that produced it). Grids and
multi-start counts here are coarser than the tool's own defaults, for speed.
"""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.rear_calibration import (
    MAX_ALLPASS_Q,
    MAX_COMBO_ORDER,
    MIN_CHAIN_GAIN_DB,
    read_rear_calibration,
)

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fit-rear-branches.py"
_spec = importlib.util.spec_from_file_location("fit_rear_branches", _SCRIPT)
fitter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fitter)

SAMPLE_RATE_HZ = 48000
# A two-branch document the tool's own structure can express, in bounds.
TRUTH = np.array([90.0, -3.0, 60.0, 250.0, -2.0, -2.0, -4.0])
TABLE_HZ = np.geomspace(*fitter.FIT_BAND_HZ, 53)
GRID_HZ = np.geomspace(*fitter.FIT_BAND_HZ, 60)
MISMATCH_DB = 6.0
# The report's own figure of merit: complex RMS error over the priority band,
# linear, on a ~unity target. Its best fit against the CAD model scored 0.164.
SCORED = (GRID_HZ >= 100.0) & (GRID_HZ <= 400.0)


def _validated(params, allpass=False):
    return read_rear_calibration(
        fitter.build_document(params, allpass=allpass), sample_rate=SAMPLE_RATE_HZ,
    )


def _ratio(document, freqs):
    """The rear/front ratio a document realizes, mute aside."""
    summed, front = rear_stage_response({**document, "rear_muted": False}, freqs)
    return summed / front


def _rms(got, want):
    return float(np.sqrt(np.mean(np.abs(got[SCORED] - want[SCORED]) ** 2)))


def _fitted(target, allpass=False):
    return _validated(fitter.fit(GRID_HZ, target, allpass=allpass), allpass=allpass)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Coarser than the tool's own defaults, for speed.

    4 corners per axis is the coarsest sweep that still brackets the truth
    below; 2 leaves only the ends of each axis and finds a different basin.
    """
    monkeypatch.setattr(fitter, "SEED_POINTS", 4)


@pytest.fixture
def cheap(monkeypatch):
    """Fewest starts the tool will take, for tests that do not score the fit."""
    monkeypatch.setattr(fitter, "SEED_STARTS", 1)
    monkeypatch.setattr(fitter, "ALLPASS_STARTS", 2)


@pytest.fixture(scope="module")
def truth():
    """``(the truth ratio on the fit grid, the table the tool is handed)``."""
    document = _validated(TRUTH)
    return _ratio(document, GRID_HZ), (TABLE_HZ, _ratio(document, TABLE_HZ))


def test_fits_a_target_its_own_structure_produced(truth):
    """A target generated from an in-bounds document comes back as that ratio."""
    ratio, table = truth
    document = _fitted(fitter.electrical_target(table, None, GRID_HZ))
    assert _rms(_ratio(document, GRID_HZ), ratio) < 0.05
    # The negative relative delay the fit wants is realized by common delay.
    shared = document["common_delay_ms"] + document["front"]["delay_ms"]
    assert min(shared + document["rear"][side]["delay_ms"] for side in ("bass", "cancellation")) >= 0.0


def test_measured_responses_undo_a_sensitivity_mismatch(truth):
    """A rear driver louder than the front is backed off, not fitted into."""
    ratio, table = truth
    mismatch = 10.0 ** (MISMATCH_DB / 20.0)
    flat = (TABLE_HZ, np.ones(TABLE_HZ.size, dtype=complex))
    hot = (TABLE_HZ, np.full(TABLE_HZ.size, mismatch, dtype=complex))
    target = fitter.electrical_target(table, (flat, hot), GRID_HZ)
    # The electrical target is the acoustic one less the rear's extra output.
    offset_db = 20.0 * np.log10(np.abs(target[SCORED] / ratio[SCORED]))
    assert float(np.median(offset_db)) == pytest.approx(-MISMATCH_DB, abs=0.5)
    # Driven through that measured rear the fitted ratio lands on the acoustic
    # target; mistaking the electrical ratio for the acoustic one would not.
    electrical = _ratio(_fitted(target), GRID_HZ)
    assert _rms(electrical * mismatch, ratio) < 0.05
    assert _rms(electrical, ratio) > 0.2


def test_a_target_wanting_a_boost_stays_in_bounds(cheap):
    """No target talks the tool into a chain gain, or a filter, above unity.

    The common shift is what makes a branch the fit wants above unity legal, and
    it can only shift DOWN, so a branch the fit turned off must not fall through
    the document's own floor either.
    """
    table = (TABLE_HZ, np.full(TABLE_HZ.size, 10.0 ** (20.0 / 20.0), dtype=complex))
    document = _fitted(fitter.electrical_target(table, None, GRID_HZ), allpass=True)
    chains = (document["front"], document["rear"]["bass"], document["rear"]["cancellation"])
    assert max(chain["gain_db"] for chain in chains) <= 0.0
    assert min(chain["gain_db"] for chain in chains) >= MIN_CHAIN_GAIN_DB
    for chain in chains:
        for parameters in (item["parameters"] for item in chain["filters"]):
            assert parameters.get("q", 0.0) <= MAX_ALLPASS_Q
            assert parameters.get("order", 1) <= MAX_COMBO_ORDER


def _table_frequencies(report: str) -> list[float]:
    """The frequency of every data row in a printed report table."""
    found = []
    for line in report.splitlines():
        if line.startswith("|"):
            try:
                found.append(float(line.split("|")[1]))
            except ValueError:
                continue
    return found


def test_main_writes_a_validating_document_and_a_row_per_target_point(tmp_path, monkeypatch, capsys, cheap):
    """The CLI's own surface: a document that validates, one row per in-band row."""
    # Increasing, and bracketing the fit band with one row below it and one
    # silent row above: neither of those is a table row.
    rows = [
        "frequency_hz,rear_front_ratio_mag_db,rear_front_ratio_phase_dsp_deg,rear_motion_zero",
        "20.0,0.0,0.0,False",
    ]
    for freq, value in zip(TABLE_HZ, _ratio(_validated(TRUTH), TABLE_HZ)):
        rows.append(f"{freq},{20 * np.log10(abs(value))},{np.degrees(np.angle(value))},False")
    rows.append("1000.0,,,True")
    target = tmp_path / "target.csv"
    target.write_text("\n".join(rows) + "\n")
    out = tmp_path / "fitted.json"
    # Deliberately not .md: the CI docs-lane classifier reads any test that
    # opens an .md path as reading documentation and demands the docs bundle
    # register it (tests/test_ci_classifier.py). --report takes any path.
    report = tmp_path / "fit_report.txt"
    monkeypatch.setattr(
        "sys.argv",
        [
            _SCRIPT.name, "--target", str(target), "--out", str(out), "--report", str(report),
        ],
    )
    # Exits 0: this target's fit clears the suppression floor. A miss still
    # writes both files, for inspection, but leaves a non-zero status.
    fitter.main()
    printed = capsys.readouterr().out
    written = read_rear_calibration(json.loads(out.read_text()), sample_rate=SAMPLE_RATE_HZ)
    assert written["case"] == "electrical_dsp"
    assert written["reference"]["quantity"] == "electrical_filter_transfer"
    assert written["rear_muted"] is True
    assert written["conditions"]["measured"] is False
    # One row per in-band target frequency, at the table's printed precision.
    assert _table_frequencies(report.read_text()) == pytest.approx(list(TABLE_HZ), rel=1e-4)
    assert _table_frequencies(printed) == pytest.approx(list(TABLE_HZ), rel=1e-4)


@pytest.mark.parametrize(
    ("points", "recovers"), [(400, True), (14, False)],
)
def test_a_measured_pair_is_only_usable_while_its_phase_can_be_unwrapped(points, recovers):
    """A rear 3 ms behind the front, both from one capture: the electrical
    target is a flat 0.5, and only a grid fine enough to carry that rotation
    between rows can say so. Interpolating the complex value read 8.05 there.
    """
    delay_s = 3.0e-3
    freqs = (
        np.geomspace(*fitter.FIT_BAND_HZ, points)
        if recovers
        else np.array([50., 63., 80., 100., 125., 160., 200., 250., 315., 400., 500., 630., 800.])
    )
    front = (freqs, np.ones(freqs.size, dtype=complex))
    rear = (freqs, 2.0 * np.exp(-2j * np.pi * freqs * delay_s))
    flat = (freqs, np.ones(freqs.size, dtype=complex))
    if not recovers:
        with pytest.raises(ValueError):
            fitter.electrical_target(flat, (front, rear), GRID_HZ)
        return
    got = np.abs(fitter.electrical_target(flat, (front, rear), GRID_HZ))
    assert got == pytest.approx(np.full(GRID_HZ.size, 0.5), abs=0.01)


@pytest.mark.parametrize(
    "rows",
    [
        ["200.0,0.0,0.0,False", "100.0,0.0,0.0,False"],  # not increasing
        ["100.0,0.0,0.0,False", "100.0,0.0,0.0,False"],  # duplicated
        ["100.0,0.0,,False", "200.0,0.0,0.0,False"],  # blank phase, not a silent row
        [",0.0,0.0,False", "200.0,0.0,0.0,False"],  # blank frequency
    ],
)
def test_a_target_table_that_would_interpolate_silently_is_refused(tmp_path, rows):
    target = tmp_path / "target.csv"
    target.write_text(
        "frequency_hz,rear_front_ratio_mag_db,rear_front_ratio_phase_dsp_deg,rear_motion_zero\n"
        + "\n".join(rows) + "\n"
    )
    with pytest.raises(ValueError):
        fitter.read_target(target)


def test_half_a_measured_pair_is_refused(tmp_path, monkeypatch):
    """The correction needs both responses; one alone would silently change nothing."""
    target = tmp_path / "target.csv"
    target.write_text(
        "frequency_hz,rear_front_ratio_mag_db,rear_front_ratio_phase_dsp_deg,rear_motion_zero\n"
        "100.0,0.0,0.0,False\n200.0,0.0,0.0,False\n"
    )
    monkeypatch.setattr(
        "sys.argv", [_SCRIPT.name, "--target", str(target), "--measured-front", "front.csv"],
    )
    with pytest.raises(SystemExit):
        fitter.main()
