# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-position cloud evidence — the members, not just the aggregate.

**The gap this closes** (``docs/attribution-stage-plan.md`` §6). The cloud
pipeline **already computes** every per-position value and then discards it:
it reports ``n_confident: 6``, ``clustered_fraction: 1.0``, and the household
sentence built on them, while persisting only one aggregate 512-point curve
and a ``positions`` list of ``{attempt, index, position_id}``. WO-0 had to
rebuild the feature-stability figures behind the plan's amended mechanism rows
from raw WAVs pulled off the Pi. So P2 — the position-variance classifier §5
calls a *free* probe — is not actually free today, and
``clustered_fraction: 1.0`` is the summary of a distribution nobody can
inspect.

Everything here is already in memory at group close, on
``spatial_combine.CombinedResponse``: ``per_position_db``,
``per_position_diag_db``, ``per_position_echo``, ``position_ids``. This module
is serialization, not analysis. It computes no new signal, applies no
threshold, and reaches no verdict.

**Consensus claim A7, persistence half** (§11.1, owner row "WO-1
(persistence) / WO-6 (display)"): *plot the members alongside the aggregate;
never ship the aggregate alone*. Four schools state it and the Q-D bake-off's
clearest leg depends on having members. This block is the members. Whether
they are *displayed* is WO-6's.

**Guard G9, data half** (§11.2, owner "WO-4 / WO-1"). The two-sided 6 dB
OFFAX membership gate on cloud positions — and G8's equidistance precondition
alongside it — are WO-4's to implement and route. Their *input* is
per-position level and per-position delay, which is precisely what a member
curve plus its echo scalars carry. A row here is the carrier those verdicts
attach to; adding a key to a row is a non-breaking change, since every reader
of this block is dict-based and tolerant.

**Resolution honesty.** The persisted grid is log-spaced at
:data:`CURVE_FRACTIONAL_OCTAVE` (1/12 octave, §6's floor). That is the *grid*,
not the *resolution*: the curve sampled onto it is already smoothed at the
combiner's diagnostic fraction, so the true resolution is whatever
``smoothing_fraction`` says and the grid merely oversamples it. Both numbers
are declared in the artifact for that reason. Sampling a pre-smoothed curve
also avoids the stride-decimation aliasing that #1858 found in the persisted
``predicted_sum`` prior — a log grid built by striding a linear one would
alias hardest exactly where this evidence matters least forgiving, at the
bottom.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

#: Schema tag for the persisted block.
POSITION_EVIDENCE_SCHEMA = "jts_attribution_position_evidence/1"

#: Log-grid density, in fractions of an octave. §6 asks for "the per-position
#: analysed curve (>= 1/12-octave from the validity floor up — ~1.5 k floats
#: for a 12-position cloud)"; 1/12 octave over the audio band is ~120 points
#: per position, which lands on that estimate.
CURVE_FRACTIONAL_OCTAVE = 12

#: Grid floor when the group reported no validity floor. Never a claim that
#: the measurement is valid down here — ``floor_source`` says which floor was
#: used, and a reader that needs the validated band reads ``validity_floor_hz``
#: off the group.
DEFAULT_CURVE_FLOOR_HZ = 20.0

#: Per-position echo scalars carried from ``spatial_combine.EchoDiagnostic``.
#: ``tau_us`` and ``confidence`` are §6's named pair; ``concentration`` is the
#: prominence behind that confidence, and the rest are what make a reported
#: tau auditable rather than a magic number. ``refusal`` is first because a
#: non-empty refusal means every other value is uninformative.
_ECHO_FIELDS: tuple[str, ...] = (
    "refusal",
    "tau_us",
    "confidence",
    "concentration",
    "corroboration",
    "strength_db",
    "resolution_us",
)

#: Per-position scalars carried from the session's own retained metadata —
#: the gate actually used and the ripple, both of which §6 names and both of
#: which the pipeline already had.
_RECORD_FIELDS: tuple[str, ...] = (
    "index",
    "attempt",
    "take_id",
    "role",
    "gate_window_ms",
    "gate_floor_source",
    "gate_disclosure",
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

    ``np.interp`` on ``log2(f)`` rather than on ``f``: the grid is
    geometrically spaced, so linear-in-frequency interpolation would weight
    the top of each interval far more heavily than the bottom.

    The source grid is an ``rfftfreq`` grid, so its first bin is DC and
    ``log2(0)`` is ``-inf``. **What dropping the non-positive bins actually
    buys, stated exactly:** it suppresses a ``divide by zero`` RuntimeWarning
    and removes any reliance on how ``np.interp`` treats a ``-inf`` left edge,
    which numpy does not specify. It does **not** change the sampled values —
    measured, not assumed: :func:`position_evidence_block` builds the grid at
    ``max(floor, lowest positive bin)``, so no query point can ever land in
    the ``[-inf, log2(f1)]`` segment, and the guarded and unguarded results
    are bit-identical on the shipped grid.

    The guard stays because depending on unspecified behaviour for a
    correct answer is a latent bug, not because it is currently load-bearing.
    Pinned by ``test_sampling_emits_no_divide_by_zero_warning`` — which fails
    if it is deleted, so the reason above stays honest.
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

    Args:
      combined: the ``spatial_combine.CombinedResponse`` the group produced.
      position_records: the session's own retained per-position metadata,
        joined by ``position_id``. Supplies the gate, the ripple, the
        accepted attempt, and the capture hash — values the combiner never
        sees because they are properties of the capture, not of the combine.
      validity_floor_hz: the group's gated validity floor; the grid starts
        there when it is usable.
      fractional_octave: grid density, defaulting to §6's 1/12-octave floor.

    Returns:
      A self-describing block, or ``{"available": False, "reason": ...}``
      when the group carries no per-position curves. Never raises: this rides
      the same diagnostic/disclosure path as the rest of the cloud result,
      where a failure must degrade to an honest "unavailable" rather than
      fail a capture the household already gave.
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
