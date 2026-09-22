# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparable curves for cloud seats and the verify pose."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

import numpy as np

from jasper.active_speaker.crossover_v2.durable_state import (
    verify_measured_curve_from_state,
)
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, RoundViewsError
from jasper.active_speaker.flat_spec_views import PositionCurve

from .banked import BankedRound

#: The synthetic role/position-id this module mints for a VERIFY-phase
#: capture, which a round's bundle never carries a ``positions`` row for.
VERIFY_ROLE = "verify"
VERIFY_POSITION_ID = "verify"


@dataclass(frozen=True)
class VerifyPoseResult:
    """The VERIFY-phase capture's MEASURED curve, read off the round's own banked
    state and put on the round's ``curve_grid_hz`` — or the reason it could not
    be.

    ``curve`` is ``None`` exactly when ``reason`` is non-empty. Never raises: a
    round banked before the curve was persisted, or without its ``state.json``,
    is a normal shape.
    """

    curve: PositionCurve | None
    reason: str


def _banked_verify_curve(
    inputs: RoundInputs,
) -> tuple[tuple[np.ndarray, np.ndarray] | None, str]:
    """``((freqs_hz, measured_db), "")`` off the round's flow state, or
    ``(None, reason)``.
    """
    state_path = inputs.state_path
    if state_path is None or not state_path.is_file():
        # The resolver's code when it HAS one: "the speaker's state belongs to
        # another session" is a different answer from "no state was banked",
        # and only it names a round the operator could point at instead.
        return None, (
            inputs.state_reason or "the round names no readable flow state file"
        )
    try:
        state = json.loads(state_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{state_path.name} is unreadable: {type(exc).__name__}"
    if not isinstance(state, Mapping):
        return None, f"{state_path.name} is not a JSON object"
    triple = verify_measured_curve_from_state(state)
    if triple is None:
        return None, "the round's state banked no verify_priors.verify_measured curve"
    freqs_hz, measured_db, _predicted_db = triple
    return (freqs_hz, measured_db), ""


def verify_pose_curve(banked: BankedRound) -> VerifyPoseResult:
    """The VERIFY pose's measured curve, READ rather than re-derived.

    ``verify_priors.verify_measured`` holds the very pair the delta probe graded
    (``(freqs_hz, measured_db, predicted_db)``, #2522); this reads the measured
    half through :func:`_banked_verify_curve` and interpolates it onto the
    round's shared grid.

    The banked curve is block-averaged in dB to
    :data:`~.durable_state.MAX_PERSISTED_SUM_POINTS`, not smoothed at a
    fractional-octave width, so :attr:`PositionCurve.smoothing_fraction` is
    reported as ``0`` — this module's spelling for *not attested*.
    """
    banked_curve, reason = _banked_verify_curve(banked.inputs)
    if banked_curve is None:
        return VerifyPoseResult(None, reason)
    freqs_hz, measured_db = banked_curve
    grid = np.asarray(banked.curve_grid_hz, dtype=float)
    curve = PositionCurve(
        position_id=VERIFY_POSITION_ID,
        role=VERIFY_ROLE,
        freqs_hz=grid,
        magnitude_db=np.interp(grid, freqs_hz, measured_db),
        smoothing_fraction=0,
        # The VERIFY phase measures the confirmed on-axis listening position
        # by definition of the phase — this is not an angle recovered from a
        # walk log (none exists for this pose), it is what the phase means.
        degrees=0.0,
        take_id="",
    )
    return VerifyPoseResult(curve, "")


@dataclass(frozen=True)
class SeatCurve:
    """One position's (or the VERIFY pose's) curve, normalised against its
    own median level over ``norm_band_hz`` — so a level difference between
    rounds or pipelines cannot masquerade as a shape difference."""

    position_id: str
    role: str
    normalized_db: np.ndarray


def per_seat_curves(
    banked: BankedRound,
    verify: PositionCurve | None = None,
    *,
    norm_band_hz: tuple[float, float] = (400.0, 8000.0),
) -> tuple[SeatCurve, ...]:
    """Every banked position plus, when supplied, the VERIFY pose — all
    normalised onto a comparable basis.

    Each curve is expressed as its own deviation from its own median level over
    ``norm_band_hz``. That is what makes the VERIFY pose — captured through an
    entirely different DSP path — comparable to the banked cloud positions with
    no cross-calibration assumption: only SHAPE is compared, never level.
    """
    # Asked BEFORE the norm band, so a round that banked no cloud group is told
    # what it is missing rather than that its empty grid has no bins in the band.
    positions = banked.graded_positions
    grid = np.asarray(banked.curve_grid_hz, dtype=float)
    sel = (grid >= norm_band_hz[0]) & (grid <= norm_band_hz[1])
    if not np.any(sel):
        raise RoundViewsError(f"norm band {norm_band_hz} has no bins on this round's curve grid")

    def _seat(position_id: str, role: str, curve_db: np.ndarray) -> SeatCurve:
        curve_db = np.asarray(curve_db, dtype=float)
        return SeatCurve(position_id, role, curve_db - float(np.median(curve_db[sel])))

    seats = [_seat(p.position_id, p.role, p.magnitude_db) for p in positions]
    if verify is not None:
        seats.append(_seat(verify.position_id, verify.role, verify.magnitude_db))
    return tuple(seats)
