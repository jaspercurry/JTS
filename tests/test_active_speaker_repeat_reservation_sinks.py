# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#1513 downstream sinks of the RAW reservation attempt.

After transport failures are refunded from the audible measurement budget
(``jasper.active_speaker.repeat_admission``), a set can reach its third accept
at a durable reservation ``attempt`` above ``MAX_ATTEMPTS`` (up to
``MAX_RESERVATIONS``). The audible budget still caps the number of stored,
audio-emitting captures at ``MAX_ATTEMPTS`` — so ``admission_attempts`` and the
``per_repeat`` length stay <= 4 — but the raw ``attempt`` VALUE overflows past
4 and flows through two sinks that used to hard-cap at 4:

- B1: ``CrossoverLevelLease.append_driver_repeat`` (the lease store).
- B2: ``measurement._durable_repeat_summary`` via ``_repeat_int`` on
  ``per_repeat.attempt`` (the durable persistence path).

These are hardware-free.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.active_speaker.commissioning_capture import (
    DEFAULT_REPEAT_TARGET,
    aggregate_driver_repeats,
)
from jasper.active_speaker.measurement import _durable_repeat_summary
from jasper.active_speaker.repeat_admission import MAX_ATTEMPTS, MAX_RESERVATIONS
from jasper.web.correction_crossover_backend import CrossoverLevelLease


def _accepting_item(attempt: int) -> dict:
    """One audio-emitting, acceptable driver repeat at a given raw attempt."""
    return {
        "attempt": attempt,
        "verdict": "present",
        "acoustic": {
            "verdict": "present",
            "capture_geometry": "near_field",
            "observed_mic_dbfs": -30.0,
            "mic_clipping": False,
            "snr": {
                "verdict": "ok",
                "worst_relevant": {
                    "band_id": "mid",
                    "estimated_snr_db": 30.0,
                    "verdict": "ok",
                },
            },
        },
        "artifact_path": f"captures/repeat-{attempt}.wav",
        "capture_admission": None,
    }


@pytest.mark.parametrize("attempt", [5, 6, 7, 8])
def test_b1_append_driver_repeat_accepts_reservation_attempts_above_four(attempt):
    # A capture that lands at a durable reservation number above MAX_ATTEMPTS
    # (after refunded transport failures) must still be storable.
    assert attempt > MAX_ATTEMPTS and attempt <= MAX_RESERVATIONS
    store = CrossoverLevelLease()
    key = store.repeat_session_key("c" * 32, "fp")
    stored = store.append_driver_repeat(
        key, target_id="mono:woofer", attempt=attempt, item=_accepting_item(attempt)
    )
    assert [item["attempt"] for item in stored] == [attempt]
    assert store.driver_repeats(key)[0]["attempt"] == attempt


def test_b1_append_driver_repeat_still_rejects_past_the_reservation_ceiling():
    store = CrossoverLevelLease()
    key = store.repeat_session_key("c" * 32, "fp")
    with pytest.raises(RuntimeError, match="out of bounds"):
        store.append_driver_repeat(
            key,
            target_id="mono:woofer",
            attempt=MAX_RESERVATIONS + 1,
            item=_accepting_item(MAX_RESERVATIONS + 1),
        )


@pytest.mark.parametrize("attempt", [5, 6, 7, 8])
def test_b2_durable_repeat_summary_persists_reservation_attempts_above_four(attempt):
    # Three accepts whose final one lands at `attempt` (two refunded transport
    # reservations preceded them) — admission_attempts stays 3, but the raw
    # per_repeat.attempt VALUE overflows past 4 and must persist.
    assert attempt > MAX_ATTEMPTS and attempt <= MAX_RESERVATIONS
    aggregate = aggregate_driver_repeats(
        [_accepting_item(a) for a in (attempt - 2, attempt - 1, attempt)],
        target=DEFAULT_REPEAT_TARGET,
    )
    aggregate["admission_attempts"] = len(aggregate["per_repeat"])
    assert aggregate["accepted"] == DEFAULT_REPEAT_TARGET

    summary = _durable_repeat_summary(aggregate)

    assert summary is not None
    assert summary["admission_attempts"] == DEFAULT_REPEAT_TARGET  # <= MAX_ATTEMPTS
    assert summary["per_repeat"][-1]["attempt"] == attempt


def test_completing_set_after_two_refunded_transports_stores_and_persists():
    # End-to-end-ish: the exact #1513 shape. Two transport failures (reservations
    # 1 and 2) were refunded and never reached the store; the three accepts land
    # at reservation attempts 3, 4, 5. Both the lease store (B1) and the durable
    # summary (B2) must accept attempt 5, while admission_attempts stays at 3.
    store = CrossoverLevelLease()
    key = store.repeat_session_key("c" * 32, "fp")
    stored: list = []
    for attempt in (3, 4, 5):
        stored = store.append_driver_repeat(
            key, target_id="mono:woofer", attempt=attempt,
            item=_accepting_item(attempt),
        )
    assert [item["attempt"] for item in stored] == [3, 4, 5]  # B1: attempt 5 stored

    aggregate = aggregate_driver_repeats(stored, target=DEFAULT_REPEAT_TARGET)
    aggregate["admission_attempts"] = len(aggregate["per_repeat"])
    assert aggregate["accepted"] == DEFAULT_REPEAT_TARGET
    # admission_attempts is the MEASUREMENT count (audio captures), still <= 4;
    # only the raw per_repeat.attempt VALUE overflows past 4.
    assert aggregate["admission_attempts"] == DEFAULT_REPEAT_TARGET
    assert max(item["attempt"] for item in aggregate["per_repeat"]) == 5

    summary = _durable_repeat_summary(aggregate)  # B2: must not raise

    assert summary["admission_attempts"] == DEFAULT_REPEAT_TARGET
    assert summary["per_repeat"][-1]["attempt"] == 5


def _constant_from_source(path: str, name: str) -> int:
    """Read an int constant textually, so the v2 plan budget above is read
    the same way for symmetry."""
    source = Path(path).read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*(\d+)", source, re.MULTILINE)
    assert match is not None, f"{name} not found in {path}"
    return int(match.group(1))


def test_max_reservations_fits_every_budget_the_attempt_number_must_pass():
    """The reservation attempt number flows into two DIFFERENT budgets.

    Both were 8 before the capture ceiling was raised to 32 for multi-position
    capture plans, which made a single equality assertion ambiguous about
    which relationship it was guarding. They are separate invariants:

    1. **Plan-budget fit (the binding one).** A reservation attempt becomes
       the capture session's ``begin_capture.attempt``, and
       ``parse_begin_capture`` rejects ``attempt > plan.max_attempts``. The v2
       flow's plan carries ``CAPTURE_PLAN_MAX_ATTEMPTS``, so the ledger must
       never hand out an attempt that plan cannot admit. Currently exactly
       tight (8 <= 8) — lowering the flow's retry budget below
       ``MAX_RESERVATIONS`` would break the flow, and this is the assertion
       that catches it.
    2. **Transport fit.** The plan's own budget must fit the capture's
       blob-index space (``capture_index = attempt - 1``, valid indexes
       ``0..MAX_CAPTURE_PLAN_ATTEMPTS-1``). With (1) this transitively keeps
       every reservation attempt inside the blob table, which is what the
       ``MAX_RESERVATIONS`` rationale comment claims.

    ``MAX_RESERVATIONS`` is an infra circuit-breaker sized against
    ``MAX_ATTEMPTS``, so it deliberately does not track the capture ceiling
    upward — only these inequalities bind it.
    """
    flow_budget = _constant_from_source(
        "jasper/active_speaker/crossover_v2/capture_plan.py",
        "CAPTURE_PLAN_MAX_ATTEMPTS",
    )
    transport_ceiling = _constant_from_source(
        "jasper/capture_protocol.py", "MAX_CAPTURE_PLAN_ATTEMPTS"
    )
    # (1) plan-budget fit — the invariant that actually binds the v2 flow.
    assert MAX_RESERVATIONS <= flow_budget
    # (2) transport fit — the plan's budget must fit the capture's blob table.
    assert flow_budget <= transport_ceiling
