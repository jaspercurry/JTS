# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A repeat's reservation attempt number fits every downstream attempt budget."""

from __future__ import annotations

from jasper.active_speaker.repeat_admission import MAX_RESERVATIONS
from jasper.playback_state.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS


def test_max_reservations_fits_every_budget_the_attempt_number_must_pass():
    assert MAX_RESERVATIONS <= MAX_CAPTURE_PLAN_ATTEMPTS
