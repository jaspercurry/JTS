# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable repeat counts preserve refunded transport attempts."""

from __future__ import annotations

import pytest

from jasper.active_speaker.measurement import _durable_repeat_summary
from jasper.active_speaker.repeat_admission import MAX_ATTEMPTS, MAX_RESERVATIONS
from jasper.active_speaker.crossover_v2.capture_plan import CAPTURE_PLAN_MAX_ATTEMPTS
from jasper.capture_protocol import MAX_CAPTURE_PLAN_ATTEMPTS


from tests.test_active_speaker_measurement import _repeat_summary


@pytest.mark.parametrize("attempt", [5, 6, 7, 8])
def test_b2_durable_repeat_summary_persists_reservation_attempts_above_four(attempt):
    # Three accepts whose final one lands at `attempt` (two refunded transport
    # reservations preceded them) — admission_attempts stays 3, but the raw
    # per_repeat.attempt VALUE overflows past 4 and must persist.
    assert attempt > MAX_ATTEMPTS and attempt <= MAX_RESERVATIONS
    aggregate = _repeat_summary()
    for entry, reserved in zip(aggregate["per_repeat"], (attempt - 2, attempt - 1, attempt)):
        entry["attempt"] = reserved
    aggregate["admission_attempts"] = len(aggregate["per_repeat"])

    summary = _durable_repeat_summary(aggregate)

    assert summary is not None
    assert summary["admission_attempts"] == 3  # <= MAX_ATTEMPTS
    assert summary["per_repeat"][-1]["attempt"] == attempt


def test_max_reservations_fits_every_budget_the_attempt_number_must_pass():
    assert MAX_RESERVATIONS <= CAPTURE_PLAN_MAX_ATTEMPTS
    assert CAPTURE_PLAN_MAX_ATTEMPTS <= MAX_CAPTURE_PLAN_ATTEMPTS
