# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A repeat's reservation attempt number fits every downstream attempt budget."""

from __future__ import annotations

from jasper.active_speaker.repeat_admission import MAX_RESERVATIONS
from jasper.active_speaker.crossover_v2.capture_plan import CAPTURE_PLAN_MAX_ATTEMPTS
from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS


def test_max_reservations_fits_every_budget_the_attempt_number_must_pass():
    assert MAX_RESERVATIONS <= CAPTURE_PLAN_MAX_ATTEMPTS
    assert CAPTURE_PLAN_MAX_ATTEMPTS <= MAX_CAPTURE_PLAN_ATTEMPTS
