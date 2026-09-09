# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two documents a bass fit reads: one seat-cube median, one design draft.

A fixture library rather than builders inside a test module, because three
suites read them — the seat fit, the bass-fit view and the exit vocabulary —
and a shared builder living in a collected module makes that module
undeletable (the same rule :mod:`tests.room_median_fixture` states).

The median here is a MEASUREMENT, not a model: a driver's second-order
roll-off under a gentle low shelf standing in for room gain, plus seeded
noise. Room gain is included on purpose (ADR-0260 section 3), so what a fit
recovers from it is the in-room plant, not the datasheet one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.crossover_v2.round_inputs import DESIGN_DRAFT_FILENAME
from jasper.active_speaker.design_draft import DESIGN_DRAFT_KIND, SCHEMA_VERSION
from jasper.bass_extension.adapters.base import CabinetInfo
from jasper.bass_extension.alignment import (
    low_shelf_response_db,
    second_order_highpass_db,
)
from jasper.cli.round_views.bass_fit import MEDIAN_FILENAME
from tests.room_median_fixture import median_document

#: 1/24 octave from the room floor to the last bin under the ceiling, as the
#: room door requires a median's grid to sit.
CEILING_HZ = 300.0
FREQS = 20.0 * 2.0 ** (np.arange(94) / 24.0)
CABINET = CabinetInfo("sealed", 1, 165.0, 220.0)

#: Room gain as a low shelf (Hz, Q, dB) and the seeded measurement noise.
ROOM_GAIN_HZ, ROOM_GAIN_Q, ROOM_GAIN_DB = 35.0, 0.5, 2.0
NOISE_SIGMA_DB = 0.4
NOISE_SEED = 20260908

SEALED_TARGET: dict[str, Any] = {
    "target_id": "main:woofer",
    "role": "woofer",
    "hard_excitation_band_hz": [30.0, 300.0],
    "cabinet": {
        "enclosure_kind": CABINET.enclosure_kind,
        "radiator_count": CABINET.radiator_count,
        "effective_radiating_diameter_mm": CABINET.effective_radiating_diameter_mm,
        "baffle_width_mm": CABINET.baffle_width_mm,
    },
}


def in_room(shape: np.ndarray) -> np.ndarray:
    """One driver shape as a seat-cube median: room gain, then noise."""
    noise = np.random.default_rng(NOISE_SEED).normal(0.0, NOISE_SIGMA_DB, FREQS.size)
    shelf = low_shelf_response_db(FREQS, ROOM_GAIN_HZ, ROOM_GAIN_Q, ROOM_GAIN_DB)
    return shape + shelf + noise


def seat_median_db(f0_hz: float, q0: float) -> np.ndarray:
    """The median a sealed plant at ``f0_hz``/``q0`` leaves at the seats."""
    return in_room(second_order_highpass_db(FREQS, f0_hz, q0))


def seat_median_json(magnitude_db: np.ndarray) -> dict[str, Any]:
    """One ``room_median.json`` payload around that median."""
    return median_document(FREQS.tolist(), magnitude_db.tolist(), ceiling_hz=CEILING_HZ)


def design_draft(**profile: Any) -> dict[str, Any]:
    """One saved design draft, carrying whatever profile the suite declares."""
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": DESIGN_DRAFT_KIND,
        "revision": 1,
        **profile,
    }


def bank_bass_fit_inputs(
    round_dir: Path,
    *,
    draft: dict[str, Any] | str | None = None,
    median: bool = True,
    **median_fields: Any,
) -> Path:
    """Both documents filed beside a banked round: the seat median the
    room-median view writes and the draft banked with the round.

    ``median_fields`` overwrite the median document's own, for a suite pinning
    what the room door will not read.
    """
    if draft is None:
        draft = design_draft(driver_safety_profile={"targets": [SEALED_TARGET]})
    (round_dir / DESIGN_DRAFT_FILENAME).write_text(
        draft if isinstance(draft, str) else json.dumps(draft)
    )
    if median:
        (round_dir / MEDIAN_FILENAME).write_text(json.dumps({
            **seat_median_json(seat_median_db(45.0, 0.707)), **median_fields,
        }))
    return round_dir
