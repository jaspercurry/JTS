# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for bounded event waits in concurrency tests.

Two promises are pinned here.

Behaviour: `wait_signalled` fails fast when a signal never arrives, and
names the producing task's exception as the cause. That second half is
the point — the failure mode this guards against swallows the real error
on a task nobody awaits, so a bare timeout would leave the next reader
with nothing to go on.

Structure: the race and cancellation tests listed in
`BOUNDED_WAIT_TESTS` keep their waits bounded. Those tests hang forever
on a loaded macOS box when written with a bare `await event.wait()` — a
producer that misses a lock budget dies before it signals, and nothing
ever wakes the test body. CI's Linux runners are quiet enough that they
never see it, so only a static guard keeps the fix from silently
regressing.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from ._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_signalled

#: Waits bounded because they were observed to hang. Extend this as more
#: of the same bug class is fixed; never shrink it.
BOUNDED_WAIT_TESTS: dict[str, tuple[str, ...]] = {
    "tests/test_active_speaker_commissioning_host.py": (
        "test_two_independent_hosts_allow_only_one_issuance_to_execute",
        "test_restored_in_flight_capture_cannot_be_recovered_by_peer",
    ),
    "tests/test_active_speaker_commissioning_runtime.py": (
        "test_cancellation_during_safe_volume_set_restores_without_audio",
        "test_cancellation_during_restore_continues_remaining_cleanup",
        "test_external_cancellation_stops_interruptible_capture_and_restores",
        "test_cancel_suppressed_by_late_callback_reports_completed_result",
    ),
    "tests/test_bass_extension_profile.py": (
        "test_delayed_rollback_cannot_replay_consumed_intent_over_new_commit",
        "test_delayed_rollback_refuses_different_newer_pending_intent",
        "test_repeated_cancellation_during_rollback_drains_one_restore_task",
        "test_rollback_failure_wins_over_repeated_cancellation",
    ),
    "tests/test_camilla_controller.py": (
        "test_intent_publication_wins_race_before_direct_graph_mutation",
    ),
    "tests/test_dsp_apply.py": (
        "test_cancelled_dsp_writer_waiter_cannot_acquire_late",
        "test_cancelling_contended_owner_is_not_logged_as_wait_cancellation",
        "test_dsp_writer_lock_acquires_after_contention_before_deadline",
    ),
}

#: Calls that bound whatever they enclose.
_BOUNDING_CALLS = frozenset({"wait_for", "timeout", "wait_signalled"})

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _nodes_under_nested_defs(fn: ast.AsyncFunctionDef) -> set[int]:
    """Ids of nodes belonging to functions defined inside `fn`.

    A producer coroutine's own `await release.wait()` is driven by the
    test body, so it is not the hazard this guard is about.
    """

    nested: set[int] = set()
    for node in ast.walk(fn):
        if node is fn:
            continue
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            nested.update(id(child) for child in ast.walk(node))
    return nested


def _nodes_under_bounding_calls(fn: ast.AsyncFunctionDef) -> set[int]:
    """Ids of nodes enclosed by a call that bounds them."""

    bounded: set[int] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in _BOUNDING_CALLS:
            bounded.update(id(child) for child in ast.walk(node))
    return bounded


def _unbounded_waits(fn: ast.AsyncFunctionDef) -> list[int]:
    """Line numbers of unbounded `await <event>.wait()` in `fn`'s own body."""

    nested = _nodes_under_nested_defs(fn)
    bounded = _nodes_under_bounding_calls(fn)
    lines: list[int] = []
    for node in ast.walk(fn):
        if id(node) in nested or not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "wait":
            continue
        # `await asyncio.Event().wait()` parks a task until it is
        # cancelled explicitly. That is the idiom, not the bug.
        if isinstance(func.value, ast.Call):
            continue
        if id(node) in bounded:
            continue
        lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    ("relative_path", "test_name"),
    [
        (path, name)
        for path, names in BOUNDED_WAIT_TESTS.items()
        for name in names
    ],
)
def test_race_tests_keep_their_event_waits_bounded(
    relative_path: str,
    test_name: str,
) -> None:
    source_path = _REPO_ROOT / relative_path
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == test_name
    ]
    assert matches, f"{relative_path}::{test_name} no longer exists"

    unbounded = sorted(line for fn in matches for line in _unbounded_waits(fn))
    assert not unbounded, (
        f"{relative_path}::{test_name} has unbounded `await <event>.wait()` at "
        f"line(s) {unbounded}. These hang forever when the producing task dies "
        f"before signalling. Use wait_signalled() from tests/_async_wait.py."
    )


def test_guard_detects_an_unbounded_wait() -> None:
    """The structural guard fails on the shape it is meant to catch."""

    tree = ast.parse(
        "async def test_x():\n"
        "    started = asyncio.Event()\n"
        "    await started.wait()\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _unbounded_waits(fn) == [3]


def test_guard_accepts_bounded_and_idiomatic_waits() -> None:
    """Bounded waits, park-until-cancelled, and producer waits all pass."""

    tree = ast.parse(
        "async def test_x():\n"
        "    async def producer():\n"
        "        await release.wait()\n"          # driven by the test body
        "        await asyncio.Event().wait()\n"  # parks until cancelled
        "    await wait_signalled(started, 'started')\n"
        "    await asyncio.wait_for(other.wait(), timeout=1.0)\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _unbounded_waits(fn) == []


async def test_wait_signalled_returns_once_the_event_fires() -> None:
    event = asyncio.Event()
    event.set()
    await wait_signalled(event, "already signalled", timeout=1.0)


async def test_wait_signalled_fails_fast_instead_of_hanging() -> None:
    never = asyncio.Event()

    with pytest.raises(AssertionError, match="never signalled within 0.05s"):
        await wait_signalled(never, "a signal nobody sends", timeout=0.05)


async def test_wait_signalled_names_the_dead_producer_as_the_cause() -> None:
    """The reason the fix exists: report the swallowed producer error.

    Mirrors the real failure — a producer dies on its lock budget before
    reaching `set()`, stranding the waiter. A bare timeout would hide
    why; this must surface the producer's own exception.
    """

    stranded = asyncio.Event()

    async def dies_before_signalling() -> None:
        raise TimeoutError("DSP writer lock was unavailable after 0.501s")

    producer = asyncio.create_task(dies_before_signalling())
    await asyncio.sleep(0)

    with pytest.raises(AssertionError) as raised:
        await wait_signalled(
            stranded,
            "owner acquired the lock",
            timeout=0.05,
            producer=producer,
        )

    assert "the task expected to signal it died first" in str(raised.value)
    assert "DSP writer lock was unavailable" in str(raised.value)
    assert isinstance(raised.value.__cause__, TimeoutError)


async def test_wait_signalled_reports_no_cause_for_a_cancelled_producer() -> None:
    """A cancelled producer is not a fault to blame the timeout on."""

    stranded = asyncio.Event()

    async def parked() -> None:
        await asyncio.Event().wait()

    producer = asyncio.create_task(parked())
    await asyncio.sleep(0)
    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer

    with pytest.raises(AssertionError) as raised:
        await wait_signalled(stranded, "a signal", timeout=0.05, producer=producer)

    assert "died first" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_default_signal_timeout_is_a_hang_breaker_not_a_timing_assertion() -> None:
    """Far above any budget the guarded tests actually assert (<= 1.0s)."""

    assert DEFAULT_SIGNAL_TIMEOUT_S >= 5.0
