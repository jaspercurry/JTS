# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from enum import Enum

import pytest

from jasper.correction.state_guard import SessionStateGuard


class _State(Enum):
    IDLE = "idle"
    WAITING = "waiting"
    BUSY = "busy"
    FAILED = "failed"


def _make_guard(
    *,
    state_ref: dict[str, _State],
    failed: list[str],
    timeout_sec: float = 0.02,
) -> SessionStateGuard[_State]:
    lock = asyncio.Lock()

    async def fail(message: str) -> None:
        failed.append(message)
        state_ref["state"] = _State.FAILED

    return SessionStateGuard(
        session_id="guard-test",
        capture_timeout_states={_State.WAITING},
        reset_busy_states={_State.BUSY},
        capture_timeout_sec=timeout_sec,
        get_state=lambda: state_ref["state"],
        lock_factory=lambda: lock,
        fail=fail,
        state_label=lambda state: state.value,
        logger=logging.getLogger("tests.correction.state_guard"),
    )


@pytest.mark.asyncio
async def test_state_guard_times_out_stranded_capture_state(caplog):
    state_ref = {"state": _State.WAITING}
    failed: list[str] = []
    guard = _make_guard(state_ref=state_ref, failed=failed)

    caplog.set_level(logging.WARNING, logger="tests.correction.state_guard")
    guard.on_transition(_State.WAITING)
    await asyncio.sleep(0.08)

    assert state_ref["state"] == _State.FAILED
    assert failed == ["capture never arrived — tap Start to measure again"]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=correction_capture_timeout" in msg
        and "session=guard-test" in msg
        and "state=waiting" in msg
        and "after_sec=0" in msg
        for msg in messages
    )


@pytest.mark.asyncio
async def test_state_guard_transition_cancels_pending_timeout():
    state_ref = {"state": _State.WAITING}
    failed: list[str] = []
    guard = _make_guard(state_ref=state_ref, failed=failed)

    guard.on_transition(_State.WAITING)
    state_ref["state"] = _State.IDLE
    guard.on_transition(_State.IDLE)
    await asyncio.sleep(0.08)

    assert state_ref["state"] == _State.IDLE
    assert failed == []


def test_state_guard_reports_reset_busy_states():
    state_ref = {"state": _State.IDLE}
    guard = _make_guard(state_ref=state_ref, failed=[])

    assert guard.is_reset_busy(_State.BUSY) is True
    assert guard.is_reset_busy(_State.WAITING) is False
