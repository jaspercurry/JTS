# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The delta probe's model-departure reading on a banked round's real curves.

``tests/fixtures/crossover_v2_d1_incident_20260817/verify_curves.json`` holds
the persisted 511-bin transport grid of jts3's 2026-08-17 series-2 rounds,
lifted from ``captures/xover-series2-2026-08-17/series2-state-{r1b,r2}.json``.
The shipped probe ran 155,018 bins, so scalars reproduce to ~0.06 dB and the
structured-run booleans do not reproduce at all; the assertions below are on
quantities, never on the decimated width.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.delta_probe import VERDICT_MATCHED, classify_delta_probe

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "crossover_v2_d1_incident_20260817"
    / "verify_curves.json"
)

#: The band the shipped round graded, off its own journal line
#: (``event=correction.crossover_v2_delta_probe trusted_band_hz``).
BAND_HZ = (357.1, 20_000.0)

#: Where the standing model error peaks.
OVERSHOOT_HZ = 1384.1


def _round(tag: str) -> dict:
    """One round's curves, on one grid, with NaN restored for excluded bins."""
    data = json.loads(FIXTURE.read_text())["rounds"][tag]

    def arr(key):
        return np.asarray(
            [np.nan if v is None else v for v in data[key]], dtype=float
        )

    measured = arr("measured_db")
    predicted = arr("predicted_db")
    commanded = arr("commanded_delta_db")
    return {
        "freqs": arr("freqs_hz"),
        "commanded": commanded,
        "realized": (measured - predicted) + commanded,
        "offset": float(data["expected_offset_db"]),
    }


def _probe(tag: str):
    """The round, classified from its banked curves."""
    r = _round(tag)
    return classify_delta_probe(
        r["freqs"], r["realized"], r["commanded"],
        band_hz=BAND_HZ,
        expected_offset_db=r["offset"],
    )


def test_the_model_error_is_still_measured():
    """The +3.9 dB blend-region departure is a real defect: the two-branch
    model places its crossover summation loss near 1400 Hz where this speaker's
    is near 1650. It stays measured and on the record."""
    probe = _probe("r2")
    assert probe.verdict == VERDICT_MATCHED
    assert probe.model_departure_over_tolerance is True
    assert probe.max_signed_error_db == pytest.approx(3.891, abs=5e-3)
    assert probe.max_signed_error_hz == pytest.approx(OVERSHOOT_HZ, abs=1.0)
