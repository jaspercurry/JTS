# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-seat sign and magnitude agreement for each feature."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from jasper.active_speaker.crossover_v2.feature_optics import detrend
from jasper.active_speaker.crossover_v2.round_inputs import RoundViewsError
from jasper.active_speaker.flat_spec import REFERENCE_BAND_HZ
from jasper.audio_measurement.excess_phase import local_features

from .banked import BankedRound
from .seats import SeatCurve

#: The campaign's own LITERAL agreement thresholds (``agreement.py``:
#: ``test >= 3 and diss <= 1``), never a seat-count-relative generalisation —
#: see :func:`agreement_table` for why a generalisation is the wrong port.
AGREEMENT_TESTIFY_MIN = 3
AGREEMENT_DISSENT_MAX = 1


@dataclass(frozen=True)
class AgreementFeature:
    """One local excursion in the pooled curve, and how every seat testifies
    to it — sign agreement and magnitude agreement, reported separately
    (the campaign's own finding: a feature can agree in sign everywhere and
    still split badly in size, which a single collapsed verdict would hide).
    """

    center_hz: float
    band_hz: tuple[float, float]
    pooled_db: float
    seat_values_db: dict[str, float]
    n_testify: int
    n_dissent: int
    spread_db: float
    ratio: float
    #: ``True``/``False`` when ``len(seats) >= AGREEMENT_TESTIFY_MIN``; ``None``
    #: below it, where that threshold cannot be satisfied by construction — a
    #: NAMED not-evaluable state, never a vacuous boolean. See
    #: :func:`agreement_table`.
    common_mode: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_hz": self.center_hz,
            "band_hz": list(self.band_hz),
            "pooled_db": self.pooled_db,
            "seat_values_db": self.seat_values_db,
            "n_testify": self.n_testify,
            "n_dissent": self.n_dissent,
            "spread_db": self.spread_db,
            "ratio": self.ratio,
            "common_mode": self.common_mode,
        }


def default_agreement_lo_hz(banked: BankedRound) -> float:
    """The trusted sweep's low edge when a caller does not name one.

    The round's OWN trusted floor when it recorded one — sweeping below a
    session's own honesty floor grades bins that session could not vouch for.
    Falls back to :data:`~jasper.active_speaker.flat_spec.REFERENCE_BAND_HZ`'s
    edge rather than a campaign-specific literal (the previous default, 357.14
    Hz, was one session's floor at its particular 7 ms gate window).
    """
    floor = banked.graded_report.trusted_floor_hz
    return float(floor) if floor is not None else float(REFERENCE_BAND_HZ[0])


def agreement_table(
    seats: Sequence[SeatCurve],
    grid: np.ndarray,
    *,
    lo_hz: float,
    hi_hz: float,
    feature_db: float = 0.4,
    testify_db: float = 0.4,
    magnitude_ratio_ok: float = 3.0,
) -> tuple[AgreementFeature, ...]:
    """Every feature in ``[lo_hz, hi_hz]``, with per-seat testify/dissent counts
    and a magnitude-agreement ratio.

    ``testify`` = same sign as the pooled curve AND ``|seat| >= testify_db``;
    ``dissent`` = opposite sign AND ``|seat| >= testify_db``. ``common_mode``
    requires BOTH sign agreement (``n_testify >= AGREEMENT_TESTIFY_MIN`` and
    ``n_dissent <= AGREEMENT_DISSENT_MAX``) AND magnitude agreement
    (``ratio <= magnitude_ratio_ok``).

    The sign-agreement counts are the campaign's own LITERAL thresholds, not a
    seat-count-relative generalisation: scaling testify to ``len(seats) - 1``
    demands 4 at the 5-seat default where the measurement-validated frame demands
    3, and returns a vacuous ``True`` at 1-2 seats. Below
    :data:`AGREEMENT_TESTIFY_MIN` seats ``common_mode`` is ``None``, while
    ``n_testify``, ``n_dissent``, ``spread_db`` and ``ratio`` are still reported
    at any seat count — they are measurements, not verdicts.
    """
    grid = np.asarray(grid, dtype=float)
    if not seats:
        raise RoundViewsError("agreement_table: no seats supplied")
    # Power-mean, unlike the campaign's dB mean: compare its published
    # tables by verdict, never cell for cell.
    detrended = np.vstack([detrend(seat.normalized_db, grid) for seat in seats])
    pooled = detrended.mean(axis=0)
    features = []
    n_seats = len(seats)
    for i, a, b in local_features(grid, pooled, lo_hz=lo_hz, hi_hz=hi_hz, feature_db=feature_db):
        seat_values = detrended[:, a : b + 1].mean(axis=1)
        p = float(pooled[a : b + 1].mean())
        sign = np.sign(p) if p != 0 else 1.0
        testify = int(np.sum((np.sign(seat_values) == sign) & (np.abs(seat_values) >= testify_db)))
        dissent = int(np.sum((np.sign(seat_values) != sign) & (np.abs(seat_values) >= testify_db)))
        spread = float(seat_values.max() - seat_values.min())
        ratio = float(np.abs(seat_values).max() / max(np.abs(seat_values).min(), 0.01))
        common_mode: bool | None
        if n_seats < AGREEMENT_TESTIFY_MIN:
            common_mode = None
        else:
            sign_ok = testify >= AGREEMENT_TESTIFY_MIN and dissent <= AGREEMENT_DISSENT_MAX
            common_mode = bool(sign_ok and ratio <= magnitude_ratio_ok)
        features.append(
            AgreementFeature(
                center_hz=float(grid[i]),
                band_hz=(float(grid[a]), float(grid[b])),
                pooled_db=p,
                seat_values_db={seat.position_id: float(v) for seat, v in zip(seats, seat_values)},
                n_testify=testify,
                n_dissent=dissent,
                spread_db=spread,
                ratio=ratio,
                common_mode=common_mode,
            )
        )
    return tuple(features)
