# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin that the published example documents still validate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor

ROOT = Path(__file__).resolve().parents[1] / "docs" / "examples"


@pytest.mark.parametrize("filename,expected_case", [
    ("rear_calibration_handoff.json", "acoustic_targets"),
    ("rear_calibration_electrical_example.json", "electrical_dsp"),
])
def test_example_document_validates(filename, expected_case):
    raw = json.loads((ROOT / filename).read_text(encoding="utf-8"))
    document = read_rear_calibration(raw, sample_rate=raw["sample_rate_hz"])
    assert document["case"] == expected_case


def test_bass_example_document_is_a_new_section():
    raw = json.loads((ROOT / "bass_prescription_example.json").read_text(encoding="utf-8"))
    read_prescription_document(raw)
    validate_dynamic_bass_descriptor(raw["sections"]["bass"], new_section=True)
