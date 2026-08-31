# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared level-correction constants for crossover-v2 driver measurement.

Nothing in production reads this module today: the ``CrossoverLevelLease``
correction methods that consumed all of it are gone.
:func:`driver_solve_requirement_db` is reached only from tests, and
:data:`MIC_TARGET_PEAK_DBFS` / :data:`CLIP_UNDERESTIMATE_ALLOWANCE_DB` have
no reader at all.
"""
from __future__ import annotations

from .quality_model import QualityModel

# The solver's own margin on top of quality_model.snr_warn_db -- insurance
# against the solver's prediction not exactly matching the room's real
# behavior (see the "Bounded correction" retry this margin exists to make
# usually unnecessary).
SOLVER_MARGIN_DB = 6.0

# Target mic peak for the bounded clip correction (W2.2, hardware run 18):
# once a sweep has ACTUALLY clipped the mic, a tone-derived gain estimate is
# proven wrong for this driver's hottest band. A clip de-escalation aims at
# this peak using the clipped attempt's OWN measured mic reading, because a
# clipped/clamped reading is a floor on the true peak, not the true peak
# itself.
MIC_TARGET_PEAK_DBFS = -12.0
# A clipped capture's peak reading is clamped at (or near) digital full
# scale, so it UNDERSTATES the true acoustic peak by an unknown amount. This
# pads the assumed true peak upward before computing how far to back off, so
# the correction errs quieter rather than risking a repeat clip.
CLIP_UNDERESTIMATE_ALLOWANCE_DB = 3.0


def driver_solve_requirement_db(
    model: QualityModel, solver_margin_db: float = SOLVER_MARGIN_DB
) -> float:
    """The worst-band SNR a solve targets: ``snr_warn_db`` + solver margin.

    The ONE owner of the "floor + margin" figure (26 dB for the DRIVER
    quality model today). The completed-insufficient completion path that
    sized its correction against this threshold is gone -- no production
    caller survives.
    """

    return model.snr_warn_db + solver_margin_db
