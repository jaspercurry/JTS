# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Translate crossover round evidence into the neutral frequency view."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from jasper.json_fields import as_mapping
from jasper.audio_measurement.spatial_combine import DEFAULT_SPEC_FRACTION
from jasper.active_speaker.flat_spec import evaluate_flat_spec
from jasper.active_speaker.frequency_view import (
    FrequencyRun,
    FrequencySeries,
    FrequencyViewError,
    frequency_series,
)

MEASUREMENT_FAMILY = "entry_baseline"


def _whole_degrees(value: Any) -> int | None:
    """One banked angle as a whole number, or ``None`` for "not recorded".

    ``bool`` is rejected before ``int`` because it subclasses it, so a
    hand-edited ``true`` cannot be drawn on a legend as 1°.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def position_label(row: Mapping[str, Any]) -> str:
    degrees = _whole_degrees(row.get("position_deg"))
    # Absent on a row banked before the field existed, and 0 on every seat
    # taken at mark height — neither draws a raise on the legend.
    elevation = _whole_degrees(row.get("vertical_deg")) or 0
    raw_role = str(row.get("role") or "")
    role = {"onax": "On axis", "offax": "Off axis"}.get(
        raw_role, raw_role.replace("_", " ").title(),
    )
    parts: list[str] = []
    if degrees is not None:
        parts.append(f"{degrees:+d}°" if degrees else "0°")
    if elevation:
        # The word carries the sign, so the number does not repeat it.
        parts.append(f"{abs(elevation)}° {'up' if elevation > 0 else 'down'}")
    if role:
        parts.append(role)
    return " · ".join(parts) or str(row.get("position_id") or "Measurement")


def _baseline_frame(entry: Mapping[str, Any]) -> tuple[float | None, list[list[float]]]:
    """Return the baseline's own display reference and excluded intervals."""

    try:
        report = evaluate_flat_spec(
            np.asarray(entry.get("freqs_hz"), dtype=np.float64),
            np.asarray(entry.get("magnitude_db"), dtype=np.float64),
            np.asarray(entry.get("excluded"), dtype=bool),
            smoothing_fraction=DEFAULT_SPEC_FRACTION,
        )
    except (OverflowError, TypeError, ValueError):
        return None, []
    return report.reference_db, [list(interval) for interval in report.excluded_intervals]


def frequency_run(packet: Mapping[str, Any]) -> FrequencyRun:
    """Adapt a round's ``session``, ``identity`` and ``entry_baseline`` blocks; perform no file I/O."""

    session = as_mapping(packet.get("session"))
    run_id = str(session.get("bundle_session_id") or "").strip()
    if not run_id:
        raise FrequencyViewError("evidence packet has no bundle session id")

    identity = as_mapping(packet.get("identity"))

    series: list[FrequencySeries] = []
    entry = as_mapping(packet.get("entry_baseline"))
    if entry.get("available"):
        baseline_reference_db, baseline_excluded = _baseline_frame(entry)
        baseline = frequency_series(
            series_id="entry_baseline",
            label="Before correction · 0°",
            kind="entry_baseline",
            freqs_hz=entry.get("freqs_hz"),
            magnitude_db=entry.get("magnitude_db"),
            reference_db=baseline_reference_db,
            smoothing_fractional_octave=DEFAULT_SPEC_FRACTION,
            visible_by_default=False,
            role="summed",
            position={"axis": "horizontal", "deg": 0},
            captured_at=entry.get("captured_at"),
            program_id=entry.get("program_id"),
            reference_mark=entry.get("reference_mark"),
            graph_fingerprint=entry.get("graph_fingerprint"),
            excluded=entry.get("excluded") or [],
            excluded_intervals_hz=baseline_excluded,
        )
        if baseline is not None:
            series.append(baseline)

    mic = as_mapping(identity.get("mic"))
    return FrequencyRun(
        id=run_id,
        measurement_family=MEASUREMENT_FAMILY,
        started_at=session.get("started_at"),
        state=session.get("state"),
        metadata={
            "topology_id": identity.get("topology_id"),
            "topology_fingerprint": identity.get("topology_fingerprint"),
            "build_sha": identity.get("build_sha"),
            "mic_calibration_id": mic.get("calibration_id"),
        },
        series=tuple(series),
    )
