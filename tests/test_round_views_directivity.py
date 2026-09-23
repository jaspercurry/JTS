# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import json

import numpy as np
import pytest

from jasper.audio_measurement.evidence_reasons import REASON_NO_REFERENCE_TAKE, REASON_TOO_FEW_POSITIONS
from jasper.cli import round_views
from tests.crossover_v2_banked_round import bank_measure_round
from tests.run_manifest_fixture import write_manifest

GRID_HZ = np.geomspace(100.0, 20000.0, 200)

#: ``baseline/express``'s poses, each take a flat curve at this level: four
#: 0°/0° repeats, the horizontal pair, then the vertical pair.
BASELINE = (*[((0, 0), 0.0)] * 4, ((-20, 0), -2.0), ((20, 0), -2.0), ((0, -10), -4.0), ((0, 10), -5.0))


def _take(index: int, pose: tuple[int, int], level_db: float, *, selected: bool = True) -> dict:
    return {"take_id": f"take-{index}", "phase": "measure", "role": "woofer", "selected": selected,
            "pose": {"kind": "bearing", "deg": pose[0], "elevation_deg": pose[1]},
            "curve": {"role": "woofer", "band_hz": [200.0, 12000.0], "freqs_hz": GRID_HZ.tolist(),
                      "magnitude_db": [level_db] * GRID_HZ.size, "trusted_floor_hz": 300.0}}


@pytest.mark.parametrize("layout,refusal", [
    (BASELINE, None),
    (BASELINE[:4], REASON_TOO_FEW_POSITIONS),
    (BASELINE[4:], REASON_NO_REFERENCE_TAKE),
])
def test_directivity_grades_each_bearing_against_the_0_0_takes(tmp_path, capsys, layout, refusal):
    root = bank_measure_round(tmp_path)
    takes = [_take(index, pose, level) for index, (pose, level) in enumerate(layout)]
    # A superseded 0°/0° attempt is evidence, never part of the reference.
    takes.append(_take(len(takes), (0, 0), 20.0, selected=False))
    write_manifest(root, groups=[{"set_id": "woofer", "capture_basis": {"role": "woofer"}, "takes": takes}])

    code = round_views.main(["directivity", str(root), "--set", "woofer"])
    answer = json.loads(capsys.readouterr().out)
    if refusal:
        assert (code, answer["reason"]) == (round_views.EXIT_REFUSED, refusal)
        return
    assert code == round_views.EXIT_OK
    assert answer["set_id"] == "woofer"
    assert answer["parameters"] == {
        "reference_pose": {"horizontal_deg": 0, "vertical_deg": 0}, "ladder": "speaker_spec",
        "smoothing": "none", "band_hz": [300.0, 12000.0],
        "grid": "linearization_envelope.DEFAULT_ENVELOPE_GRID_HZ",
    }
    assert answer["reference_take_ids"] == ["take-0", "take-1", "take-2", "take-3"]
    assert answer["omitted_take_ids"] == []
    expected = {(pose, f"take-{index}"): level for index, (pose, level) in enumerate(layout) if pose != (0, 0)}
    assert {((row["horizontal_deg"], row["vertical_deg"]), row["take_id"]): row["level_offset_db"]
            for row in answer["poses"]} == pytest.approx(expected)
    for row in answer["poses"]:
        assert [band["band_hz"] for band in row["bands"]] == [[250.0, 2000.0], [2000.0, 8000.0], [8000.0, 16000.0]]
        assert [band["level_offset_db"] for band in row["bands"]] == pytest.approx([row["level_offset_db"]] * 3)
        assert [band["shape_rms_db"] for band in row["bands"]] == pytest.approx([0.0] * 3, abs=1e-9)
    table = json.loads((root / "directivity-woofer.json").read_text())["directivity"]
    assert table["reference_position_ids"] == answer["reference_take_ids"]
