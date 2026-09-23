# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One manifest set's bearing takes as departures from its 0°/0° takes.

Each band's level difference and residual shape come from
:func:`~jasper.active_speaker.flat_spec_views.directivity_table`. This is not
sound-power DI; a shared trim leaves the difference unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from jasper.active_speaker.flat_spec import evaluate_flat_spec
from jasper.active_speaker.flat_spec_views import PositionCurve, directivity_table
from jasper.active_speaker.linearization_envelope import DEFAULT_ENVELOPE_GRID_HZ
from jasper.active_speaker.measurement_programs import POSE_KIND_BEARING
from jasper.audio_measurement.band_ladders import SPEC_BAND_EDGES_HZ, band_ladder_name
from jasper.audio_measurement.evidence_reasons import REASON_NO_REFERENCE_TAKE, REASON_TOO_FEW_POSITIONS

from ..frequency_view import position_label
from ..position_cycle import measured_curve_band
from ..round_inputs import RoundSetRefused, SetTakes


def _pose(take: Mapping[str, Any]) -> dict[str, Any]:
    pose = take["pose"]
    return {"horizontal_deg": pose.get("deg"), "vertical_deg": pose.get("elevation_deg")}


def _label(pose: Mapping[str, Any]) -> str:
    """Both angles, so a raised take never shares the 0° reference's label."""
    return position_label({"position_deg": pose["horizontal_deg"], "vertical_deg": pose["vertical_deg"]})


REFERENCE_POSE = {"horizontal_deg": 0, "vertical_deg": 0}
REFERENCE_LABEL = _label(REFERENCE_POSE)


def set_directivity(selected: SetTakes) -> dict[str, Any]:
    """Every selected bearing take of ``selected`` against the power mean of its
    0°/0° takes, graded over the spec bands on the band every take measured.

    Curves are compared as banked, unsmoothed, interpolated onto the fit's grid.
    """
    measured, omitted = [], []
    for take in selected.takes:
        if not take["selected"] or take["pose"].get("kind") != POSE_KIND_BEARING:
            continue
        curve = measured_curve_band(take.get("curve") or {})
        if curve is None:
            omitted.append(take["take_id"])
        else:
            measured.append((take, _pose(take), curve))
    reference = [take["take_id"] for take, pose, _ in measured if _label(pose) == REFERENCE_LABEL]
    if not reference:
        raise RoundSetRefused(REASON_NO_REFERENCE_TAKE, set_id=selected.set_id,
                              poses=[pose for _, pose, _ in measured])
    if len(reference) == len(measured):
        raise RoundSetRefused(REASON_TOO_FEW_POSITIONS, set_id=selected.set_id, take_ids=reference)
    lo = max(band[0] for _, _, (_, _, band) in measured)
    hi = min(band[1] for _, _, (_, _, band) in measured)
    grid = DEFAULT_ENVELOPE_GRID_HZ[(DEFAULT_ENVELOPE_GRID_HZ >= lo) & (DEFAULT_ENVELOPE_GRID_HZ <= hi)]
    positions = tuple(
        PositionCurve(
            position_id=take["take_id"], role=_label(pose), freqs_hz=grid,
            magnitude_db=np.interp(grid, freqs, magnitude), smoothing_fraction=0,
            degrees=pose["horizontal_deg"], take_id=take["take_id"],
        )
        for take, pose, (freqs, magnitude, _) in measured
    )
    # The report supplies the frame only (bands, clamps); the table computes
    # its own power-mean reference from every 0°/0° take.
    report = evaluate_flat_spec(grid, positions[0].magnitude_db, smoothing_fraction=0,
                                trusted_floor_hz=lo, trusted_ceiling_hz=hi)
    table = directivity_table(report, positions, reference_role=REFERENCE_LABEL)
    return {
        "set_id": selected.set_id,
        "role": selected.capture_basis.get("role"),
        "parameters": {
            "reference_pose": REFERENCE_POSE, "ladder": band_ladder_name(SPEC_BAND_EDGES_HZ),
            "smoothing": "none", "band_hz": [lo, hi],
            "grid": "linearization_envelope.DEFAULT_ENVELOPE_GRID_HZ",
        },
        "reference_take_ids": reference,
        "omitted_take_ids": omitted,
        "poses": {take["take_id"]: pose for take, pose, _ in measured},
        "directivity": table.to_dict(),
    }
