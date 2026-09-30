# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the rear-branch fit guarantees about the document it writes.

The fit is a bounded LOCAL least squares, so these pin the properties it owes,
not a parameter vector: a target its own structure can realize is reproduced,
a measured sensitivity mismatch is undone instead of fitted into, and whatever
the target asks for, the document is inside ``rear_calibration``'s bounds.
Truth targets are sampled at 1/12 octave --- a 1/3-octave table cannot
represent a two-branch ratio (measured: 11 dB of interpolation error against
the very document that produced it). Grids and multi-start counts here are
coarser than the fit's own defaults, for speed.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker import rear_fit
from jasper.active_speaker.branch_chain import rear_stage_response
from jasper.active_speaker.crossover_v2.pose_curve import LateralPoseCurve, lateral_evidence_grid_hz, pose_curve_record
from jasper.active_speaker.crossover_v2.rear_views import PAIR_ROLES
from jasper.active_speaker.rear_calibration import (
    MAX_ALLPASS_Q,
    MAX_COMBO_ORDER,
    MIN_CHAIN_GAIN_DB,
    diagnostic_seed,
    read_rear_calibration,
)
from jasper.active_speaker.round_view_artifacts import CATALOG
from jasper.audio_measurement.evidence_reasons import REASON_SEGMENT_MISSING
from jasper.cli import crossover_prescriber, round_views
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED, EXIT_UNREADABLE
from tests import test_round_views_rear
from tests.crossover_v2_banked_round import SEAT_GRID_HZ
from tests.test_rear_preview import cardioid_box
from tests.test_round_views_rear import _PAIR_GAP_MS, _PAIR_LEVEL_GAP_DB, _PAIR_SET_ID, _PARENT, pair_round

# Re-exported so pytest resolves the judge's cardioid box by name here.
__all__ = ["cardioid_box"]

SAMPLE_RATE_HZ = 48000
# A two-branch document the fit's own structure can express, in bounds.
TRUTH = np.array([90.0, -3.0, 60.0, 250.0, -2.0, -2.0, -4.0])
TABLE_HZ = np.geomspace(*rear_fit.FIT_BAND_HZ, 53)
GRID_HZ = np.geomspace(*rear_fit.FIT_BAND_HZ, 60)
MISMATCH_DB = 6.0
# The report's own figure of merit: complex RMS error over the priority band,
# linear, on a ~unity target. Its best fit against the CAD model scored 0.164.
SCORED = (GRID_HZ >= 100.0) & (GRID_HZ <= 400.0)


def _validated(params, allpass=False):
    return read_rear_calibration(rear_fit.build_document(params, allpass=allpass), sample_rate=SAMPLE_RATE_HZ)


def _ratio(document, freqs):
    """The rear/front ratio a document realizes, mute aside."""
    summed, front = rear_stage_response({**document, "rear_muted": False}, freqs)
    return summed / front


def _rms(got, want):
    return float(np.sqrt(np.mean(np.abs(got[SCORED] - want[SCORED]) ** 2)))


def _fitted(target, allpass=False):
    return _validated(rear_fit.fit(GRID_HZ, target, allpass=allpass), allpass=allpass)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Coarser than the fit's own defaults, for speed.

    4 corners per axis is the coarsest sweep that still brackets the truth
    below; 2 leaves only the ends of each axis and finds a different basin.
    """
    monkeypatch.setattr(rear_fit, "SEED_POINTS", 4)


@pytest.fixture
def cheap(monkeypatch):
    """Fewest starts the fit will take, for tests that do not score it."""
    monkeypatch.setattr(rear_fit, "SEED_STARTS", 1)
    monkeypatch.setattr(rear_fit, "ALLPASS_STARTS", 2)


@pytest.fixture(scope="module")
def truth():
    """``(the truth ratio on the fit grid, the table the fit is handed)``."""
    document = _validated(TRUTH)
    return _ratio(document, GRID_HZ), (TABLE_HZ, _ratio(document, TABLE_HZ))


def test_fits_a_target_its_own_structure_produced(truth):
    """A target generated from an in-bounds document comes back as that ratio."""
    ratio, table = truth
    document = _fitted(rear_fit.interpolate(table, GRID_HZ))
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
    target = rear_fit.electrical_target(table, (flat, hot), GRID_HZ)
    # The electrical target is the acoustic one less the rear's extra output.
    offset_db = 20.0 * np.log10(np.abs(target[SCORED] / ratio[SCORED]))
    assert float(np.median(offset_db)) == pytest.approx(-MISMATCH_DB, abs=0.5)
    # Driven through that measured rear the fitted ratio lands on the acoustic
    # target; mistaking the electrical ratio for the acoustic one would not.
    electrical = _ratio(_fitted(target), GRID_HZ)
    assert _rms(electrical * mismatch, ratio) < 0.05
    assert _rms(electrical, ratio) > 0.2


def test_a_target_wanting_a_boost_stays_in_bounds(cheap):
    """No target talks the fit into a chain gain, or a filter, above unity.

    The common shift is what makes a branch the fit wants above unity legal, and
    it can only shift DOWN, so a branch the fit turned off must not fall through
    the document's own floor either.
    """
    table = (TABLE_HZ, np.full(TABLE_HZ.size, 10.0 ** (20.0 / 20.0), dtype=complex))
    document = _fitted(rear_fit.interpolate(table, GRID_HZ), allpass=True)
    chains = (document["front"], document["rear"]["bass"], document["rear"]["cancellation"])
    assert max(chain["gain_db"] for chain in chains) <= 0.0
    assert min(chain["gain_db"] for chain in chains) >= MIN_CHAIN_GAIN_DB
    for chain in chains:
        for parameters in (item["parameters"] for item in chain["filters"]):
            assert parameters.get("q", 0.0) <= MAX_ALLPASS_Q
            assert parameters.get("order", 1) <= MAX_COMBO_ORDER


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
        np.geomspace(*rear_fit.FIT_BAND_HZ, points)
        if recovers
        else np.array([50., 63., 80., 100., 125., 160., 200., 250., 315., 400., 500., 630., 800.])
    )
    front = (freqs, np.ones(freqs.size, dtype=complex))
    rear = (freqs, 2.0 * np.exp(-2j * np.pi * freqs * delay_s))
    flat = (freqs, np.ones(freqs.size, dtype=complex))
    if not recovers:
        with pytest.raises(ValueError):
            rear_fit.electrical_target(flat, (front, rear), GRID_HZ)
        return
    got = np.abs(rear_fit.electrical_target(flat, (front, rear), GRID_HZ))
    assert got == pytest.approx(np.full(GRID_HZ.size, 0.5), abs=0.01)


def _wall(freqs, excess_m):
    """One strong wall image that far behind the direct sound: a comb of deep nulls."""
    return 1.0 + 0.9 * np.exp(-2j * np.pi * freqs * excess_m / 343.0)


def test_a_well_sampled_pair_with_room_nulls_reads_as_its_own_ratio():
    """At a deep null a well-sampled phase still turns past a quarter turn per
    row, which is no sign of a coarse grid (#5404 comment 5746999024, item 6)."""
    freqs = lateral_evidence_grid_hz()
    front = _wall(freqs, 1.9)
    rear = 2.0 * np.exp(-2j * np.pi * freqs * 3.0e-3) * _wall(freqs, 1.6)
    rows = (freqs >= rear_fit.FIT_BAND_HZ[0]) & (freqs <= rear_fit.FIT_BAND_HZ[1])
    got = rear_fit.electrical_target((freqs, np.ones(freqs.size, dtype=complex)),
                                     ((freqs, front), (freqs, rear)), freqs[rows])
    assert got == pytest.approx(front[rows] / rear[rows])


def _target(freqs, ratio):
    """An ``acoustic_targets`` document (ADR-0318) asking the rear for ``ratio`` of the front."""
    document = diagnostic_seed(SAMPLE_RATE_HZ)
    for key in ("front", "rear", "boundary", "common_delay_ms", "rear_muted"):
        del document[key]
    document["reference"].update(quantity="acoustic_motion", units="unitless")
    document.update(case="acoustic_targets", valid_band_hz=[float(freqs[0]), float(freqs[-1])], targets={
        "frequency_hz": [float(hz) for hz in freqs], "front": [[1.0, 0.0]] * len(freqs),
        "rear": [[float(value.real), float(value.imag)] for value in ratio]})
    return document


def _truth_target(path: Path) -> Path:
    """The truth ratio over the fit band, bracketed by one row below it and one
    silent row above: neither is a residual row."""
    ratio = _ratio(_validated(TRUTH), TABLE_HZ)
    path.write_text(json.dumps(_target(np.array([20.0, *TABLE_HZ, 1000.0]), np.array([1.0, *ratio, 0.0]))))
    return path


def _walled_pair_curves(band_hz):
    """The pair fixture's woofers, each with its own wall image: the nulls a measured pair has."""
    front = _wall(SEAT_GRID_HZ, 1.9)
    rear = (-10.0 ** (_PAIR_LEVEL_GAP_DB / 20.0) * np.exp(-2j * np.pi * SEAT_GRID_HZ * _PAIR_GAP_MS / 1000.0)
            * _wall(SEAT_GRID_HZ, 1.6))
    return [pose_curve_record(LateralPoseCurve(role=role, freqs_hz=SEAT_GRID_HZ, complex_tf=transfer,
                                               band_hz=(band_hz[0], band_hz[1])))
            for role, transfer in zip(PAIR_ROLES, (front, rear, front + rear))]


@pytest.fixture(params=["ideal", "walled"])
def fitted(request, tmp_path, monkeypatch, capsys, cheap):
    """``(round, exit code, answer)`` of rear-fit on a banked rear/pair round's
    first on-axis take, against the truth target; ``walled`` gives the pair the
    nulls a real room puts in it."""
    if request.param == "walled":
        monkeypatch.setattr(test_round_views_rear, "_pair_curves", _walled_pair_curves)
    root = pair_round(tmp_path)
    code = round_views.main(["rear-fit", str(root), "--set", _PAIR_SET_ID, "--take", f"{_PARENT}-0-1",
                             "--target", str(_truth_target(tmp_path / "target.json"))])
    return root, code, json.loads(capsys.readouterr().out)


def test_the_fitted_document_is_one_judge_previews(fitted, tmp_path, capsys):
    """As written, and with its rear unmuted: the fitted branches compile at the
    declared cabinet and preview on the pair round they were fitted on."""
    root, code, answer = fitted
    assert code == EXIT_OK
    assert crossover_prescriber.main(["judge", "--preview", answer["document"], "--round", str(root)]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["section"] == "rear_calibration"
    assert crossover_prescriber.main([
        "judge", "--preview", answer["document"], "--round", str(root),
        "--vary", "rear_calibration.rear_muted=false", "--out-dir", str(tmp_path / "unmuted")]) == EXIT_OK
    variant, = json.loads(capsys.readouterr().out)["variants"]
    assert variant["out"] and variant["positions"]


def test_the_answer_names_its_document_beside_the_round_and_a_row_per_target_point(fitted):
    root, _, answer = fitted
    row = CATALOG[f"{round_views.PROG} rear-fit"]
    artifact = json.loads(Path(answer["out"]).read_text())
    assert (answer["view"], answer["schema"], artifact["schema"]) == ("rear-fit", row.schema, row.schema)
    assert answer.keys() - {"view", "schema", "subject", "parameters", "out", "bytes"} == set(row.answer_fields)
    assert (answer["subject"]["set_id"], answer["subject"]["take_ids"]) == (_PAIR_SET_ID, [f"{_PARENT}-0-1"])
    assert {Path(answer["out"]).parent, Path(answer["document"]).parent} == {root}
    assert [residual["hz"] for residual in artifact["residuals"]] == pytest.approx(list(TABLE_HZ), rel=1e-4)


def test_a_take_without_both_woofers_alone_refuses_by_name(tmp_path, capsys):
    root = pair_round(tmp_path, missing=(0,))
    assert round_views.main(["rear-fit", str(root), "--set", _PAIR_SET_ID, "--take", f"{_PARENT}-0-1",
                             "--target", str(_truth_target(tmp_path / "target.json"))]) == EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == REASON_SEGMENT_MISSING


def _moving_rear_over_a_still_front():
    document = _target(np.array([100.0, 200.0]), np.array([1.0, 1.0]))
    document["targets"]["front"][0] = [0.0, 0.0]
    return document


@pytest.mark.parametrize("target", [
    pytest.param("not json", id="not_json"),
    pytest.param(json.dumps(diagnostic_seed(SAMPLE_RATE_HZ)), id="an_electrical_document"),
    pytest.param(json.dumps(_moving_rear_over_a_still_front()), id="a_moving_rear_over_a_still_front"),
])
def test_a_target_that_states_no_rear_front_ratio_is_unreadable(target, tmp_path, capsys):
    path = tmp_path / "target.json"
    path.write_text(target)
    assert round_views.main(["rear-fit", str(tmp_path), "--target", str(path)]) == EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["reason"] == round_views.REASON_UNREADABLE
