# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `jasper.tools.timer.make_timer_tools`.

Covers the LLM-facing response shapes for `update_timer`. The
scheduler-side semantics (atomic cancel+add, task lifecycle, label
preservation) are exercised in `tests/test_timers.py`; these tests
pin the dict the model receives so a refactor of the tool wrapper
doesn't silently change the response contract.
"""
from __future__ import annotations

from contextlib import closing

import pytest

from jasper.timers import TimerScheduler, TimerStore
from jasper.tools.timer import make_timer_tools


@pytest.fixture
def sched(tmp_path):
    with closing(TimerStore(str(tmp_path / "timers.db"))) as store:
        yield TimerScheduler(store=store)


def _by_name(fns, name: str):
    for fn in fns:
        if getattr(fn, "__jasper_tool_name__", None) == name or fn.__name__ == name:
            return fn
    raise AssertionError(
        f"tool {name!r} not found in factory output; "
        f"got {[fn.__name__ for fn in fns]}"
    )


def test_factory_exposes_update_timer(sched):
    """The factory must include update_timer alongside set/list/cancel.
    If this fails, the model won't see the tool — the cancel+set
    fallback would be the only path, which is the bug we're fixing."""
    fns = make_timer_tools(sched)
    names = {fn.__name__ for fn in fns}
    assert names == {
        "set_timer", "list_timers", "cancel_timer", "update_timer",
    }


async def test_update_timer_happy_path_returns_confirm_and_new_state(sched):
    """The shape the model sees on a successful update — `ok=True`,
    `confirm` is the spoken sentence, the rest mirrors `set_timer`'s
    response so the model can ground subsequent references (id,
    label, duration, remaining)."""
    fns = make_timer_tools(sched)
    set_timer = _by_name(fns, "set_timer")
    update_timer = _by_name(fns, "update_timer")

    await set_timer(seconds=300, label="pasta")
    result = await update_timer(timer="pasta", seconds=120)

    assert result["ok"] is True
    assert result["confirm"] == "Updated the pasta timer to 2 minutes."
    assert result["label"] == "pasta"
    assert result["duration_seconds"] == 120
    assert result["duration"] == "2 minutes"
    # Previous timer info is surfaced for context.
    assert result["previous"]["duration_seconds"] == 300
    assert result["previous"]["label"] == "pasta"
    await sched.stop()


async def test_update_timer_not_found_returns_explicit_error(sched):
    """Critical: when the user asks to update a non-existent timer,
    the tool MUST NOT silently fall through to set_timer. The
    `reason='not_found'` field and verbatim error string teach the
    model to surface the mistake rather than papering over it."""
    fns = make_timer_tools(sched)
    update_timer = _by_name(fns, "update_timer")

    result = await update_timer(timer="laundry", seconds=120)
    assert result["ok"] is False
    assert result["reason"] == "not_found"
    assert result["error"] == "No timer matches 'laundry'."
    # No timer was created.
    assert sched.list_active() == []
    await sched.stop()


async def test_update_timer_ambiguous_returns_matches(sched):
    """Two timers labelled 'pasta' — update must NOT pick one. The
    response includes both candidates so the model can read durations
    aloud and ask the user to disambiguate."""
    fns = make_timer_tools(sched)
    set_timer = _by_name(fns, "set_timer")
    update_timer = _by_name(fns, "update_timer")

    await set_timer(seconds=60, label="pasta")
    await set_timer(seconds=300, label="pasta")

    result = await update_timer(timer="pasta", seconds=120)
    assert result["ok"] is False
    assert result["reason"] == "ambiguous"
    assert len(result["matches"]) == 2
    # Both originals still active and untouched.
    active = sched.list_active()
    assert len(active) == 2
    assert {t.total_seconds for t in active} == {60, 300}
    await sched.stop()


async def test_update_timer_validation_error_returns_clean_error(sched):
    """A non-positive `seconds` should never reach the scheduler's
    second-phase add; the tool catches ValueError and surfaces it as
    a tool error, not an exception that bubbles up to the LLM as a
    raw stack trace."""
    fns = make_timer_tools(sched)
    set_timer = _by_name(fns, "set_timer")
    update_timer = _by_name(fns, "update_timer")

    await set_timer(seconds=300, label="pasta")
    result = await update_timer(timer="pasta", seconds=0)
    assert result["ok"] is False
    assert "error" in result
    await sched.stop()
