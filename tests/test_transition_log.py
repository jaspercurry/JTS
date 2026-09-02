# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The shared transition-or-reminder disclosure gate (ADR-0196)."""

from __future__ import annotations

from jasper.transition_log import TransitionLog


def _log(clock, **kwargs):
    return TransitionLog(reminder_sec=100.0, clock=lambda: clock[0], **kwargs)


def test_a_persistent_fault_logs_once_then_only_on_a_due_reminder():
    clock = [0.0]
    gate = _log(clock)

    assert gate.should_log("k", "sig") is True
    assert gate.should_log("k", "sig") is False
    clock[0] += 99.0
    assert gate.should_log("k", "sig") is False
    clock[0] += 2.0
    assert gate.should_log("k", "sig") is True


def test_a_changed_signature_is_a_fresh_transition():
    clock = [0.0]
    gate = _log(clock)

    assert gate.should_log("k", "sig-a") is True
    assert gate.should_log("k", "sig-b") is True
    assert gate.should_log("k", "sig-b") is False


def test_a_cleared_key_rediscloses_and_reports_recovery():
    clock = [0.0]
    gate = _log(clock)

    gate.should_log("k", "sig")
    assert gate.cleared("k") is True
    assert gate.cleared("k") is False
    assert gate.should_log("k", "sig") is True


def test_the_gate_is_bounded_by_max_keys():
    clock = [0.0]
    gate = _log(clock, max_keys=3)

    for index in range(10):
        gate.should_log(f"k{index}", "sig")

    assert gate.tracked() == 3
