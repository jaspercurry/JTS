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

These are hardware-free and do NOT import ``jasper.capture_relay`` (blocked in
some CI containers by a broken ``cryptography``/``_cffi_backend``).
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


def test_max_reservations_matches_capture_plan_attempt_ceiling():
    # Pin the lockstep the rationale comments claim: the durable reservation
    # attempt also indexes the relay's per-plan blob table. Read the source
    # rather than import capture_relay (crypto import blocked in some CI).
    source = Path("jasper/capture_relay/spec.py").read_text(encoding="utf-8")
    match = re.search(
        r"^MAX_CAPTURE_PLAN_ATTEMPTS\s*=\s*(\d+)", source, re.MULTILINE
    )
    assert match is not None, "MAX_CAPTURE_PLAN_ATTEMPTS not found in spec.py"
    assert int(match.group(1)) == MAX_RESERVATIONS
