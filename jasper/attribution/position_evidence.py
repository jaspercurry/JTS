# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-position cloud evidence — the members, not just the aggregate.

Serialization, not analysis: everything here is already in memory at group
close on ``spatial_combine.CombinedResponse``. No new signal is computed, no
threshold applied, no verdict reached. The persisted grid is log-spaced at
:data:`CURVE_FRACTIONAL_OCTAVE`, which is the GRID and not the resolution —
the curve is already smoothed at the combiner's diagnostic fraction, and both
numbers are declared in the artifact. Sampling a pre-smoothed curve also avoids
the stride-decimation aliasing #1858 found in the persisted ``predicted_sum``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

#: Schema tag for the persisted block.
POSITION_EVIDENCE_SCHEMA = "jts_attribution_position_evidence/1"

#: Log-grid density, in fractions of an octave (§6's floor). 1/12 octave over
#: the audio band is ~120 points per position.
CURVE_FRACTIONAL_OCTAVE = 12

#: Grid floor when the group reported no validity floor. Never a claim that the
#: measurement is valid down here — ``floor_source`` says which floor was used.
DEFAULT_CURVE_FLOOR_HZ = 20.0

#: Per-position echo scalars carried from ``spatial_combine.EchoDiagnostic``.
#: ``refusal`` is first because a non-empty refusal means every other value is
#: uninformative.
_ECHO_FIELDS: tuple[str, ...] = (
    "refusal",
    "tau_us",
    "confidence",
    "concentration",
    "corroboration",
    "strength_db",
    "resolution_us",
)

#: Per-position scalars carried from the session's own retained metadata.
_RECORD_FIELDS: tuple[str, ...] = (
    "index",
    "attempt",
    "take_id",
    "role",
    # WHERE the microphone was, as numbers. ``position_deg`` is absent whenever
    # no bearing was commanded and on any round banked before the writer gained
    # the fields; ``FIELD_DESCRIPTIONS`` below enumerates the cases.
    "position_deg",
    "position_axis",
    "vertical_deg",
    "mark_distance_m",
    "gate_window_ms",
    "gate_floor_source",
    "gate_disclosure",
    # The two numbers ``gate_disclosure``'s sentence narrates, banked beside it
    # so a reader need not parse English. A row carries a key only when the
    # capture had a value for it — see ``evidence_packet._gate_numbers_reason``.
    "gate_moved_rms_db",
    "gate_reflection_delay_ms",
    # The ROOM's floor at this seat and the word saying where it came from.
    # Projected as a PAIR: a floor without its provenance reads as a
    # measurement whatever produced it (#3502).
    "gate_entanglement_floor_hz",
    "gate_entanglement_floor_source",
    "validity_floor_hz",
    "gating_applied",
    "summed_ripple_db",
    "glitch_detected",
    "wav_sha256",
)

FIELD_DESCRIPTIONS: Mapping[str, str] = {
    "curve_grid": (
        "The shared log-spaced frequency grid every position's magnitude_db "
        "is sampled onto. fractional_octave is the GRID density; "
        "smoothing_fraction is the actual resolution of the curve sampled "
        "onto it (the grid oversamples the smoothing, it does not add "
        "resolution). floor_hz/floor_source say where the grid starts and "
        "why."
    ),
    "positions": (
        "One row per accepted cloud position, in the combiner's own order. "
        "position_id names the prompted spot; attempt and take_id name WHICH "
        "take survived, so a retaken position is recoverable rather than "
        "visible only as a gap in filenames."
    ),
    "magnitude_db": (
        "This position's analysed magnitude in dB on curve_grid.freqs_hz, "
        "diagnostic-smoothed and reflection-gated, calibrated. The member "
        "curve behind the aggregate."
    ),
    "echo": (
        "This position's discrete-echo diagnostic. Read refusal first: a "
        "non-empty refusal means the detector declined and every other value "
        "is uninformative. An empty refusal with confidence 0.0 means it ran "
        "and found nothing credible."
    ),
    "tau_us": "Delay of the secondary arrival at this position, microseconds.",
    "confidence": (
        "0.0-1.0 credibility of this position's tau — the product of cepstral "
        "concentration and agreement between the two tau estimators."
    ),
    "concentration": "Cepstral concentration: the prominence behind confidence.",
    "role": (
        "The named question this position answers — onax (inside the "
        "design-axis window), offax (out at the coverage edge), or xovr (a "
        "vertical offset, the axis a two-way crossover lobes on). READ off "
        "the position's own record, never re-derived from its prompt string: "
        "the prompt is household copy and is rewritten by product rulings, "
        "which is exactly what this label exists to survive. A cloud need not "
        "carry every role — the Full tier's walk samples all three, Express's "
        "shorter walk stops before the first vertical move and carries "
        "{onax, offax} only, so an absent role is UNSAMPLED, not null "
        "evidence."
    ),
    "position_deg": (
        "This position's SIGNED whole-degree bearing from the speaker, "
        "negative LEFT of the design axis as seen from the microphone looking "
        "at the speaker, derived from the pose it was prompted at rather than "
        "parsed out of that prompt's English. Absent means no bearing was "
        "commanded, never 0: a vertical pose is raised or lowered rather than "
        "swung, a geometry-locked retake's record declares no side, and a "
        "round banked before this field existed recorded none at all — "
        "position_axis and role say which. A commanded pose recorded verbatim, "
        "not a measurement, so the walk's own pointing error is unmeasured "
        "rather than quantified here."
    ),
    "position_axis": (
        "Which axis this pose's stated move was on — horizontal (a bearing in "
        "the plane the microphone swings through) or vertical (a raise or a "
        "lower, which commands no bearing at all). Read it before reading "
        "position_deg: it is what separates 'no bearing exists for this pose' "
        "from 'this round banks none'."
    ),
    "vertical_deg": (
        "This position's SIGNED whole-degree elevation above mark height, "
        "negative BELOW, derived against mark_distance_m exactly as "
        "position_deg is and from the pose it was prompted at rather than "
        "parsed out of that prompt's English. The two angles are orthogonal: a "
        "pose states both, and 0 here means AT mark height rather than "
        "'unknown' — which is why this is absent only on a round banked before "
        "the field existed, and a reader takes that absence as 0 because "
        "nothing could state any other elevation then. A commanded pose "
        "recorded verbatim, not a measurement. NO analysis reads it: every "
        "aggregate in the round pools seats without regard to height, so a "
        "raised seat is recorded and displayed and nothing more."
    ),
    "mark_distance_m": (
        "The speaker-to-MARK distance position_deg was derived against, in "
        "metres. A reference length, never a surveyed capsule distance — "
        "nothing in a round measures where the microphone actually ended up, "
        "so this says what the bearing was taken against and makes no claim "
        "about the other."
    ),
    "gate_window_ms": "The reflection gate actually applied to this capture.",
    "gate_floor_source": (
        "WHY gate_window_ms is what it is, and the two answers are not the "
        "same claim. measured_reflection: a reflection onset was found and "
        "the window stops at it. search_span_bound: the search reached its "
        "ceiling without finding one, so the window was CAPPED there and "
        "nothing was gated out — read a window equal to the ceiling with "
        "this value as 'no reflection found', never as 'reflections "
        "removed'. null means the capture was ungateable. This field is "
        "distinct from curve_grid.floor_source, which names where the GRID "
        "starts, not what the gate found."
    ),
    "gate_disclosure": (
        "gate_window_ms, gate_floor_source, both validity floors, the "
        "classification ledger, and what the gate did to the spectrum — as "
        "one sentence, written by exactly one function so this file and the "
        "retained-capture sidecar cannot describe the same gate two "
        "different ways. Read this rather than reassembling the numbers; the "
        "numbers beside it are for machines."
    ),
    "gate_moved_rms_db": (
        "How far the gate moved this capture's response SHAPE, dB RMS over the "
        "band the comparison is honest across (the gate's trusted floor "
        "intersected with what the stimulus radiated). The machine-readable "
        "half of the last clause of gate_disclosure. It is uninterpretable "
        "alone: the same small number means 'genuinely clean' beside "
        "gate_floor_source=measured_reflection and 'the gate did nothing and "
        "nothing was proven' beside search_span_bound. Absent when no band "
        "supported a comparison — never a zero standing in for one."
    ),
    "gate_reflection_delay_ms": (
        "When the first reflection arrived AFTER the direct sound, "
        "milliseconds. A DELAY, not the gating block's absolute "
        "first_reflection_ms, whose origin is the deconvolution window's and "
        "means nothing on its own. Absent — never 0.0 — when the window was "
        "capped at the search ceiling, because nothing was found to time."
    ),
    "gate_entanglement_floor_hz": (
        "The ROOM's floor at this seat, Hz. Below it no gate window, however "
        "long, separates the speaker from the room, so nothing there is a "
        "speaker measurement — it is the floor no window choice can lower, "
        "and it is stated beside the window's own two rather than instead of "
        "them. Absent when unknown. Banked per SEAT because it is derived at "
        "that seat's own mark distance, though every pose a round walks "
        "declares the same distance today, so these rows currently carry one "
        "number — see DeclaredGeometry.first_bounce_s."
    ),
    "gate_entanglement_floor_source": (
        "Which of three things produced gate_entanglement_floor_hz: "
        "measured_reflection (a reflection was found and timed), "
        "declared_geometry (the operator's declared rig heights and this "
        "seat's distance), or unknown. Declared is not measured and never "
        "prints as if it were. unknown is the ordinary state on a rig whose "
        "first bounce arrives while the direct sound is still decaying, and "
        "is resolved by declaring the geometry rather than by measuring "
        "harder."
    ),
    "summed_ripple_db": "This capture's own summed-response ripple, dB.",
    "wav_sha256": (
        "SHA-256 of this position's captured WAV bytes. The VERIFIER for a "
        "replay, never the index — the session identity plus the take id is "
        "how the capture is found."
    ),
}


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def log_grid_hz(
    floor_hz: float, ceiling_hz: float, *, fractional_octave: int = CURVE_FRACTIONAL_OCTAVE
) -> np.ndarray:
    """``floor_hz * 2**(k/fractional_octave)`` up to ``ceiling_hz``, inclusive-ish.

    Pure geometry. Raises ``ValueError`` on a degenerate range rather than
    returning an empty grid a caller would have to special-case.
    """

    if not (math.isfinite(floor_hz) and math.isfinite(ceiling_hz)):
        raise ValueError("log grid bounds must be finite")
    if floor_hz <= 0.0 or ceiling_hz <= floor_hz:
        raise ValueError("log grid needs 0 < floor_hz < ceiling_hz")
    if fractional_octave <= 0:
        raise ValueError("fractional_octave must be positive")
    octaves = math.log2(ceiling_hz / floor_hz)
    count = int(math.floor(octaves * fractional_octave)) + 1
    steps = np.arange(count, dtype=float) / float(fractional_octave)
    return floor_hz * np.power(2.0, steps)


def _sample_onto(
    freqs_hz: np.ndarray, magnitude_db: np.ndarray, grid_hz: np.ndarray
) -> list[float]:
    """Sample a smoothed curve onto ``grid_hz``, interpolating in log-frequency.

    ``np.interp`` on ``log2(f)`` rather than on ``f``, because the grid is
    geometrically spaced. The source grid is an ``rfftfreq`` grid whose first
    bin is DC, so non-positive bins are dropped to avoid a divide-by-zero
    warning and any reliance on how ``np.interp`` treats a ``-inf`` left edge.
    That guard does not change the sampled values: the grid starts at
    ``max(floor, lowest positive bin)``, so no query point reaches that segment.
    """

    freqs = np.asarray(freqs_hz, dtype=float)
    magnitude = np.asarray(magnitude_db, dtype=float)
    positive = freqs > 0.0
    freqs, magnitude = freqs[positive], magnitude[positive]
    if freqs.size == 0:
        raise ValueError("no positive frequency bins to sample")
    sampled = np.interp(np.log2(grid_hz), np.log2(freqs), magnitude)
    return [round(float(value), 4) for value in sampled]


def position_evidence_block(
    combined: Any,
    *,
    position_records: Sequence[Mapping[str, Any]] = (),
    validity_floor_hz: float | None = None,
    fractional_octave: int = CURVE_FRACTIONAL_OCTAVE,
) -> dict[str, Any]:
    """Serialize one closed cloud group's per-position members.

    ``position_records`` is the session's own retained per-position metadata,
    joined by ``position_id``: the gate, the ripple, the accepted attempt and
    the capture hash, all properties of the capture the combiner never sees.
    Returns a self-describing block, or ``{"available": False, "reason": ...}``
    when the group carries no per-position curves. Never raises — a failure
    must degrade to an honest "unavailable".
    """

    try:
        return _block(
            combined,
            position_records=position_records,
            validity_floor_hz=validity_floor_hz,
            fractional_octave=fractional_octave,
        )
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        return {
            "schema": POSITION_EVIDENCE_SCHEMA,
            "available": False,
            "reason": f"per_position_serialization_failed: {exc}",
        }


def _block(
    combined: Any,
    *,
    position_records: Sequence[Mapping[str, Any]],
    validity_floor_hz: float | None,
    fractional_octave: int,
) -> dict[str, Any]:
    if combined is None:
        return {
            "schema": POSITION_EVIDENCE_SCHEMA,
            "available": False,
            "reason": "combine_failed",
        }
    freqs = np.asarray(getattr(combined, "freqs_hz", ()), dtype=float)
    curves = np.asarray(
        getattr(combined, "per_position_diag_db", np.empty((0, 0))), dtype=float
    )
    ids = tuple(getattr(combined, "position_ids", ()) or ())
    if freqs.size == 0 or curves.ndim != 2 or curves.shape[0] == 0:
        # A pre-PR-3b combiner, or a group that produced no members. Honest
        # unavailable, never a fabricated empty distribution.
        return {
            "schema": POSITION_EVIDENCE_SCHEMA,
            "available": False,
            "reason": "no_per_position_curves",
        }

    floor = _finite_or_none(validity_floor_hz)
    lowest = float(freqs[freqs > 0.0].min()) if np.any(freqs > 0.0) else 0.0
    if floor is not None and floor > 0.0:
        floor_hz, floor_source = max(floor, lowest), "validity_floor_hz"
    else:
        floor_hz, floor_source = max(DEFAULT_CURVE_FLOOR_HZ, lowest), "default_floor"
    grid = log_grid_hz(floor_hz, float(freqs.max()), fractional_octave=fractional_octave)

    echoes = tuple(getattr(combined, "per_position_echo", ()) or ())
    by_id = {
        str(record.get("position_id")): record
        for record in position_records
        if isinstance(record, Mapping) and record.get("position_id")
    }

    rows: list[dict[str, Any]] = []
    for i in range(curves.shape[0]):
        position_id = str(ids[i]) if i < len(ids) else f"index_{i}"
        row: dict[str, Any] = {"position_id": position_id}
        record = by_id.get(position_id)
        if record is not None:
            for key in _RECORD_FIELDS:
                if record.get(key) is not None:
                    row[key] = record[key]
        row["magnitude_db"] = _sample_onto(freqs, curves[i], grid)
        echo = echoes[i] if i < len(echoes) else None
        row["echo"] = (
            None
            if echo is None
            else {key: getattr(echo, key) for key in _ECHO_FIELDS}
        )
        rows.append(row)

    return {
        "schema": POSITION_EVIDENCE_SCHEMA,
        "available": True,
        "field_descriptions": dict(FIELD_DESCRIPTIONS),
        "curve_grid": {
            "fractional_octave": int(fractional_octave),
            "smoothing_fraction": int(getattr(combined, "diag_fraction", 0) or 0),
            "floor_hz": round(float(floor_hz), 4),
            "floor_source": floor_source,
            "freqs_hz": [round(float(f), 4) for f in grid],
        },
        "positions": rows,
    }


__all__ = [
    "CURVE_FRACTIONAL_OCTAVE",
    "DEFAULT_CURVE_FLOOR_HZ",
    "FIELD_DESCRIPTIONS",
    "POSITION_EVIDENCE_SCHEMA",
    "log_grid_hz",
    "position_evidence_block",
]
