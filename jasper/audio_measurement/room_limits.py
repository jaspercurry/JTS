# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Every code-computed limit on a Layer-3 room candidate, as pure numpy.

Four families, one owner: the per-frequency cut depth the cross-position
spread supports; the room target curves; the taper that returns the
correction to flat below the ceiling (`See ADR-0256` rules 1-2 — the ceiling
is the applied tune's trusted floor and arrives here as an argument, never
derived); and the evidence a proposed low-frequency BOOST must show before it
is admitted (`See docs/room-correction-regime-plan.md` D5: spatial
persistence, a modally plausible shape, bounded headroom).

Nothing here reads a file, knows what a candidate is, or decides policy: a
caller supplies the median, the spread and the ceiling, and gets arrays and
findings back.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "ROOM_BOOST_DEPTH_AGREEMENT_DB",
    "ROOM_BOOST_MAX_DIP_DB",
    "ROOM_BOOST_MIN_DIP_DB",
    "ROOM_BOOST_MIN_POSITIONS",
    "ROOM_BOOST_MIN_WIDTH_OCTAVES",
    "ROOM_BOOST_PRESENCE_MIN_FRACTION",
    "ROOM_F_LOW_HZ",
    "ROOM_MAX_CUT_DB",
    "ROOM_MAX_FILTERS_PER_SIDE",
    "ROOM_MAX_FILTER_BOOST_DB",
    "ROOM_MAX_TOTAL_BOOST_DB",
    "ROOM_PEQ_Q_MAX",
    "ROOM_PEQ_Q_MIN",
    "ROOM_TAPER_OCTAVES",
    "TOLERABLE_STD_DB",
    "BoostAdmission",
    "admit_boost",
    "allowed_depth_db",
    "boost_cap_db",
    "ceiling_taper",
    "cut_floor_db",
    "depth_fraction",
    "flat_target",
    "harman_target",
    "house_curve",
]

#: Design band floor, Hz — :func:`jasper.audio_measurement.peq.design_peq`'s
#: own ``f_low`` default.
ROOM_F_LOW_HZ: float = 20.0

#: The strategy's own cut floor, dB, which the spread scales per frequency.
ROOM_MAX_CUT_DB: float = -10.0

#: Q range for a room bell — ``design_peq``'s range, roughly one octave
#: wide down to 1/8 octave.
ROOM_PEQ_Q_MIN: float = 1.0
ROOM_PEQ_Q_MAX: float = 8.0

#: The cross-position sigma, dB, at or below which a bin keeps its whole cut
#: depth — the room layer's "medium repeatability" threshold. `See ADR-0256`.
TOLERABLE_STD_DB: float = 6.0

#: Guards the division for a bin whose positions agree exactly (sigma 0).
_SIGMA_EPSILON_DB: float = 1e-6

#: Span of the taper below the ceiling, in octaves (ADR-0256 rule 2).
ROOM_TAPER_OCTAVES: float = 1.0 / 3.0

# Boost admission. `See docs/room-correction-regime-plan.md` D5: one seat
# cannot establish spatial persistence, and an average dip is not permission
# to spend headroom on an unresolved cancellation.
ROOM_BOOST_MIN_POSITIONS: int = 3
ROOM_BOOST_PRESENCE_MIN_FRACTION: float = 0.7
#: A position sees the dip when its own level sits at least this far below
#: flat, dB.
ROOM_BOOST_MIN_DIP_DB: float = 3.0
#: A position agrees on the depth when its deviation from the median is
#: within this, dB.
ROOM_BOOST_DEPTH_AGREEMENT_DB: float = 3.0
ROOM_BOOST_MIN_WIDTH_OCTAVES: float = 1.0 / 6.0
#: Deeper than this is an interference null, which EQ cannot fill.
ROOM_BOOST_MAX_DIP_DB: float = 10.0
ROOM_MAX_FILTER_BOOST_DB: float = 6.0
ROOM_MAX_TOTAL_BOOST_DB: float = 6.0
ROOM_MAX_FILTERS_PER_SIDE: int = 8


def depth_fraction(std_db: Any) -> np.ndarray:
    """The fraction of a strategy's cut depth this spread supports, in [0, 1].

    ``min(1, TOLERABLE_STD_DB / max(sigma, eps))``, elementwise. Exactly
    ``1.0`` at or below the tolerance, so an ordinary room's allowed depth is
    bit-identical to the strategy's own scalar. Always an ``ndarray``,
    including 0-d for a scalar input.
    """
    sigma = np.asarray(std_db, dtype=np.float64)
    return np.asarray(
        np.minimum(1.0, TOLERABLE_STD_DB / np.maximum(sigma, _SIGMA_EPSILON_DB)),
        dtype=np.float64,
    )


def allowed_depth_db(
    std_db: Any,
    *,
    base_max_cut_db: float = ROOM_MAX_CUT_DB,
) -> np.ndarray:
    """Per-frequency cut floor, dB, non-positive, on ``std_db``'s own grid.

    The array :func:`jasper.audio_measurement.peq.design_peq` accepts as
    ``max_cut_db``.
    """
    return np.asarray(base_max_cut_db * depth_fraction(std_db), dtype=np.float64)


def flat_target(freqs: np.ndarray) -> np.ndarray:
    """Flat target — all zeros, in dB."""
    return np.zeros_like(freqs, dtype=np.float64)


def harman_target(freqs: np.ndarray) -> np.ndarray:
    """Harman in-room target curve (Olive 2013, AES 8994), in dB on ``freqs``.

    A +4 dB shelf at or below 60 Hz returning to 0 dB at 100 Hz, then a
    -1 dB/octave tilt reaching about -7.6 dB at 20 kHz.
    """
    db = np.zeros_like(freqs, dtype=np.float64)

    sub_mask = freqs <= 60.0
    db[sub_mask] = 4.0

    transition_mask = (freqs > 60.0) & (freqs < 100.0)
    if transition_mask.any():
        f = freqs[transition_mask]
        x = np.log2(f / 60.0) / np.log2(100.0 / 60.0)
        db[transition_mask] = 4.0 * (1.0 - x)

    # -1 dB/octave above 100 Hz.
    above_mask = freqs >= 100.0
    db[above_mask] = -np.log2(freqs[above_mask] / 100.0)

    return db


def house_curve(freqs: np.ndarray, warmth: float = 1.0) -> np.ndarray:
    """House curve: linear interpolant between flat and Harman.

    ``warmth`` 0 = flat, 1 = full Harman; clamped to [-1, 2].
    """
    w = float(np.clip(warmth, -1.0, 2.0))
    return harman_target(freqs) * w


def ceiling_taper(freqs_hz: Any, ceiling_hz: float) -> np.ndarray:
    """Multiplier in [0, 1] handing the band back to the direct-sound stage.

    ``1.0`` at or below ``ceiling_hz / 2**ROOM_TAPER_OCTAVES``, ``0.0`` at or
    above ``ceiling_hz``, linear in log-frequency between. Scales BOTH the cut
    floor and the boost cap, so the hand-off is continuous rather than a hard
    edge (ADR-0256 rule 2).
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    knee_hz = float(ceiling_hz) / 2.0 ** ROOM_TAPER_OCTAVES
    # A non-positive frequency has no log; it sits below the knee either way.
    safe = np.where(freqs > 0.0, freqs, knee_hz)
    fraction = 1.0 - np.log2(safe / knee_hz) / ROOM_TAPER_OCTAVES
    return np.asarray(np.clip(fraction, 0.0, 1.0), dtype=np.float64)


def cut_floor_db(
    spread_db: Any,
    freqs_hz: Any,
    ceiling_hz: float,
    *,
    base_max_cut_db: float = ROOM_MAX_CUT_DB,
) -> np.ndarray:
    """The per-bin cut floor, dB, non-positive: spread cap times the taper."""
    return np.asarray(
        allowed_depth_db(spread_db, base_max_cut_db=base_max_cut_db)
        * ceiling_taper(freqs_hz, ceiling_hz),
        dtype=np.float64,
    )


def boost_cap_db(freqs_hz: Any, ceiling_hz: float) -> np.ndarray:
    """The per-bin boost ceiling, dB, non-negative: the cap times the taper."""
    return np.asarray(
        ROOM_MAX_FILTER_BOOST_DB * ceiling_taper(freqs_hz, ceiling_hz),
        dtype=np.float64,
    )


@dataclass(frozen=True)
class BoostAdmission:
    """What the spatial evidence says about one proposed boost frequency.

    ``depth_db`` is the median's dip depth at the evaluated bin as a positive
    number, ``width_octaves`` the dip's half-depth width. ``reason`` is ``""``
    when admitted and otherwise one of ``too_few_positions``,
    ``insufficient_presence``, ``dip_too_shallow``, ``dip_too_deep``,
    ``dip_too_narrow``.
    """

    freq_hz: float
    evaluated_at_hz: float
    n_positions: int
    n_present: int
    presence_fraction: float
    depth_db: float
    width_octaves: float
    admitted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe record of the finding."""
        return {
            "freq_hz": self.freq_hz,
            "evaluated_at_hz": self.evaluated_at_hz,
            "n_positions": self.n_positions,
            "n_present": self.n_present,
            "presence_fraction": self.presence_fraction,
            "depth_db": self.depth_db,
            "width_octaves": self.width_octaves,
            "admitted": self.admitted,
            "reason": self.reason,
        }


def _refusal_reason(
    *,
    n_positions: int,
    depth_db: float,
    width_octaves: float,
    presence_fraction: float,
) -> str:
    # Negated `>=`/`<=` rather than `<`/`>`: a non-finite measurement must
    # refuse at the first check it cannot satisfy, never fall through admitted.
    if n_positions < ROOM_BOOST_MIN_POSITIONS:
        return "too_few_positions"
    if not depth_db >= ROOM_BOOST_MIN_DIP_DB:
        return "dip_too_shallow"
    if not depth_db <= ROOM_BOOST_MAX_DIP_DB:
        return "dip_too_deep"
    if not width_octaves >= ROOM_BOOST_MIN_WIDTH_OCTAVES:
        return "dip_too_narrow"
    if not presence_fraction >= ROOM_BOOST_PRESENCE_MIN_FRACTION:
        return "insufficient_presence"
    return ""


def _finding(
    *,
    freq_hz: float,
    evaluated_at_hz: float,
    n_positions: int,
    n_present: int,
    depth_db: float,
    width_octaves: float,
) -> BoostAdmission:
    presence = float(n_present) / n_positions if n_positions > 0 else 0.0
    reason = _refusal_reason(
        n_positions=n_positions,
        depth_db=depth_db,
        width_octaves=width_octaves,
        presence_fraction=presence,
    )
    return BoostAdmission(
        freq_hz=freq_hz,
        evaluated_at_hz=evaluated_at_hz,
        n_positions=n_positions,
        n_present=n_present,
        presence_fraction=presence,
        depth_db=depth_db,
        width_octaves=width_octaves,
        admitted=not reason,
        reason=reason,
    )


def admit_boost(
    freq_hz: float,
    *,
    freqs_hz: Any,
    median_db: Any,
    deviations_db: Any,
    n_positions: int,
) -> BoostAdmission:
    """Whether the cloud's evidence admits a boost at ``freq_hz``.

    ``deviations_db`` is positions x bins, each row a position's deviation
    FROM ``median_db`` on the ``freqs_hz`` grid. The dip is evaluated at the
    nearest grid bin. Refusals are ordered as D5 states the claims: enough
    positions, then a dip deep enough to matter and shallow enough to be a
    mode rather than a null, then wide enough for a Q <= 8 bell, then present
    at enough positions. Never raises: an unusable input is an un-admitted
    finding with a reason.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    median = np.asarray(median_db, dtype=np.float64)
    deviations = np.asarray(deviations_db, dtype=np.float64)
    positions = int(n_positions)
    requested = float(freq_hz)

    if (
        freqs.ndim != 1
        or freqs.size == 0
        or median.shape != freqs.shape
        or deviations.shape != (positions, freqs.size)
        or not np.isfinite(requested)
    ):
        return _finding(
            freq_hz=requested,
            evaluated_at_hz=requested,
            n_positions=positions,
            n_present=0,
            depth_db=0.0,
            width_octaves=0.0,
        )

    index = int(np.argmin(np.abs(freqs - requested)))
    depth_db = float(-median[index])

    at_bin = deviations[:, index]
    present = (median[index] + at_bin <= -ROOM_BOOST_MIN_DIP_DB) & (
        np.abs(at_bin) <= ROOM_BOOST_DEPTH_AGREEMENT_DB
    )

    in_dip = median <= -depth_db / 2.0
    low = high = index
    while low > 0 and in_dip[low - 1]:
        low -= 1
    while high < freqs.size - 1 and in_dip[high + 1]:
        high += 1
    width = (
        float(np.log2(freqs[high] / freqs[low])) if freqs[low] > 0.0 else 0.0
    )

    return _finding(
        freq_hz=requested,
        evaluated_at_hz=float(freqs[index]),
        n_positions=positions,
        n_present=int(np.count_nonzero(present)),
        depth_db=depth_db,
        width_octaves=width,
    )
