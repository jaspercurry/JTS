# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Translate crossover round evidence into the neutral frequency view."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from jasper.audio_measurement.spatial_combine import (
    DEFAULT_DIAG_FRACTION,
    DEFAULT_SPEC_FRACTION,
)
from jasper.active_speaker.flat_spec import evaluate_flat_spec
from jasper.active_speaker.frequency_view import (
    FrequencyRun,
    FrequencySeries,
    FrequencyViewError,
    build_frequency_view as _build_frequency_view,
    frequency_series,
)

MEASUREMENT_FAMILY = "summed_cloud"

def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _curve(
    *,
    series_id: str,
    label: str,
    kind: str,
    freqs_hz: Any,
    magnitude_db: Any,
    reference_db: Any,
    smoothing_fraction: int | None,
    visible: bool,
    **metadata: Any,
) -> FrequencySeries | None:
    return frequency_series(
        series_id=series_id,
        label=label,
        kind=kind,
        freqs_hz=freqs_hz,
        magnitude_db=magnitude_db,
        reference_db=reference_db,
        smoothing_fractional_octave=smoothing_fraction,
        visible_by_default=visible,
        **metadata,
    )


def _whole_degrees(value: Any) -> int | None:
    """One banked angle as a whole number, or ``None`` for "not recorded".

    ``bool`` is rejected before ``int`` because it subclasses it, so a
    hand-edited ``true`` cannot be drawn on a legend as 1°.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _position_label(row: Mapping[str, Any]) -> str:
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
    """Adapt one retained crossover evidence packet; perform no file I/O."""

    session = _mapping(packet.get("session"))
    run_id = str(session.get("bundle_session_id") or "").strip()
    if not run_id:
        raise FrequencyViewError("evidence packet has no bundle session id")

    spec = _mapping(packet.get("spec"))
    reference_db = spec.get("reference_db")
    curve = _mapping(packet.get("curve"))
    positions = _mapping(packet.get("positions"))
    grid = _mapping(positions.get("curve_grid"))
    round_block = _mapping(packet.get("round"))
    honesty = _mapping(packet.get("honesty_mask"))
    identity = _mapping(packet.get("identity"))

    series: list[FrequencySeries] = []
    average = _curve(
        series_id="average",
        label="Run average",
        kind="average",
        freqs_hz=curve.get("freqs_hz"),
        magnitude_db=curve.get("magnitude_db"),
        reference_db=reference_db,
        smoothing_fraction=DEFAULT_SPEC_FRACTION,
        visible=True,
        role="summed",
        position=None,
    )
    if average is not None:
        series.append(average)

    entry = _mapping(packet.get("entry_baseline"))
    if entry.get("available"):
        baseline_reference_db, baseline_excluded = _baseline_frame(entry)
        baseline = _curve(
            series_id="entry_baseline",
            label="Before correction · 0°",
            kind="entry_baseline",
            freqs_hz=entry.get("freqs_hz"),
            magnitude_db=entry.get("magnitude_db"),
            reference_db=baseline_reference_db,
            smoothing_fraction=DEFAULT_SPEC_FRACTION,
            visible=False,
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

    position_freqs = grid.get("freqs_hz")
    position_smoothing = grid.get("smoothing_fraction")
    if not isinstance(position_smoothing, int) or isinstance(position_smoothing, bool):
        position_smoothing = DEFAULT_DIAG_FRACTION
    for index, raw in enumerate(positions.get("positions") or []):
        row = _mapping(raw)
        position_id = str(row.get("position_id") or f"position_{index + 1}")
        member = _curve(
            series_id=position_id,
            label=_position_label(row),
            kind="position",
            freqs_hz=position_freqs,
            magnitude_db=row.get("magnitude_db"),
            reference_db=reference_db,
            smoothing_fraction=position_smoothing,
            visible=False,
            role=row.get("role"),
            position={
                "axis": row.get("position_axis"),
                "deg": row.get("position_deg"),
                # 0 for a seat at mark height AND for a row banked before the
                # field existed — the same fact, and the view needs no third
                # answer to draw a legend.
                "vertical_deg": _whole_degrees(row.get("vertical_deg")) or 0,
                "mark_distance_m": row.get("mark_distance_m"),
            },
            take_id=row.get("take_id"),
            validity_floor_hz=row.get("validity_floor_hz"),
        )
        if member is not None:
            series.append(member)

    angles = _mapping(positions.get("angle_deg"))
    mic = _mapping(identity.get("mic"))
    return FrequencyRun(
        id=run_id,
        measurement_family=MEASUREMENT_FAMILY,
        started_at=session.get("started_at"),
        round_id=session.get("round_id"),
        state=session.get("state"),
        metadata={
            "position_count": positions.get("n_positions") or 0,
            "angles_deg": angles.get("angles_deg") or [],
            "reference_db": reference_db,
            "smoothing": {
                "average_fractional_octave": DEFAULT_SPEC_FRACTION,
                "positions_fractional_octave": position_smoothing,
            },
            "validity_floor_hz": honesty.get("validity_floor_hz"),
            "trusted_floor_hz": honesty.get("trusted_floor_hz"),
            "excluded_bands_hz": honesty.get("merged_excluded_bands_hz") or [],
            "entry_graph_fingerprint": round_block.get("entry_graph_fingerprint"),
            "applied_graph_fingerprint": round_block.get("applied_graph_fingerprint"),
            "adoption": _mapping(round_block.get("adoption")),
            "verification": _mapping(round_block.get("verification")),
            "topology_id": identity.get("topology_id"),
            "topology_fingerprint": identity.get("topology_fingerprint"),
            "build_sha": identity.get("build_sha"),
            "mic_calibration_id": mic.get("calibration_id"),
        },
        series=tuple(series),
    )


def build_frequency_view(
    run_a: Mapping[str, Any],
    run_b: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible evidence-packet door over the neutral projector."""

    return _build_frequency_view(
        frequency_run(run_a),
        frequency_run(run_b) if run_b is not None else None,
    )
