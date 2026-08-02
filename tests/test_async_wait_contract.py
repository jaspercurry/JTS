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

Structure: NO async test anywhere under tests/ may coordinate through a
bare `await <event>.wait()` in its own body, except the shrinking
allowlist in `KNOWN_UNBOUNDED_WAITS`. Such a test hangs forever when its
producer dies before signalling — a lock budget missed on a loaded box,
say — and the producer's real exception stays swallowed on a task nobody
awaits. CI's Linux runners are quiet enough that these never hang there,
so the global pytest-timeout backstop never fires on them either; this
static guard is the only thing that catches the pattern at CI time.

The allowlist is a two-sided ratchet, mirroring
`tests/test_atomic_io_conventions.py`: a new offender fails, AND a stale
entry fails, so the list can only shrink. It is the burn-down list for
the sites this bug class already has — not permission to add more.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from ._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_signalled

#: Burn-down list: async tests still coordinating through an unbounded
#: `await <event>.wait()`. Each one can hang forever if its producer dies
#: first. Fix with `wait_signalled()` and DELETE the entry — the guard
#: fails on a stale entry too, so this only ever shrinks. Never add.
#:
#: The 2026-08-02 tranche shrank this from 41 to 1. The sole survivor,
#: test_concurrent_device_operations_share_one_recovered_bus, has two
#: racing producer tasks (pair_task and connect_task) contending for one
#: shared bus-recovery connect() call — which one actually reaches
#: `connector.entered.set()` is the very race the test asserts on, so no
#: single task can be named as `wait_signalled`'s producer without
#: misattributing a future failure's cause to the wrong task.
KNOWN_UNBOUNDED_WAITS: frozenset[tuple[str, str]] = frozenset({
    ("tests/test_bluetooth_engine.py",
     "test_concurrent_device_operations_share_one_recovered_bus"),
})

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
        # A call carrying its own `timeout=` is bounded by construction —
        # `asyncio.wait({task}, timeout=...)` is the shape that matched here
        # only because it shares the name `wait`. `asyncio.Event.wait()`
        # takes no arguments at all, so this cannot excuse the real hazard.
        if any(kw.arg == "timeout" for kw in call.keywords):
            continue
        if id(node) in bounded:
            continue
        lines.append(node.lineno)
    return lines


def _offending_tests() -> dict[tuple[str, str], list[int]]:
    """Every async test under tests/ with an unbounded wait in its body."""

    offenders: dict[tuple[str, str], list[int]] = {}
    for path in sorted((_REPO_ROOT / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        relative = path.relative_to(_REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            lines = _unbounded_waits(node)
            if lines:
                offenders.setdefault((relative, node.name), []).extend(lines)
    return offenders


def test_no_new_test_coordinates_through_an_unbounded_event_wait() -> None:
    """A bare `await <event>.wait()` in a test body can hang forever.

    When the producing task dies before signalling — a lock budget missed
    on a loaded box — nothing ever wakes the test, and the producer's real
    exception stays swallowed on a task nobody awaits.
    """

    offenders = _offending_tests()
    new = sorted(key for key in offenders if key not in KNOWN_UNBOUNDED_WAITS)

    assert not new, (
        "unbounded `await <event>.wait()` in a test body — these hang forever "
        "when the producing task dies before signalling. Use wait_signalled() "
        "from tests/_async_wait.py (it also reports the producer's exception "
        "as the cause):\n"
        + "\n".join(
            f"  {path}::{name}  line(s) {offenders[(path, name)]}"
            for path, name in new
        )
    )


def test_known_unbounded_wait_allowlist_has_no_stale_entries() -> None:
    """The ratchet only tightens.

    A fixed (or deleted, or renamed) test must lose its entry, otherwise
    the list stops describing real debt and quietly re-authorizes the
    pattern for a name nobody is watching any more.
    """

    stale = sorted(KNOWN_UNBOUNDED_WAITS - set(_offending_tests()))

    assert not stale, (
        "stale KNOWN_UNBOUNDED_WAITS entries — the test was fixed, renamed, or "
        "removed. Delete these so the ratchet keeps tightening:\n"
        + "\n".join(f"  {path}::{name}" for path, name in stale)
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
        "    await asyncio.wait({task}, timeout=1.0)\n"  # bounded by its own kwarg
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _unbounded_waits(fn) == []


def test_guard_still_flags_an_unbounded_wait_named_like_asyncio_wait() -> None:
    """Relaxing for `timeout=` must not relax for a bare event wait.

    `asyncio.Event.wait()` accepts no arguments, so no true instance of the
    hazard can carry a `timeout=` keyword — but pin it rather than assume it.
    """

    tree = ast.parse(
        "async def test_x():\n"
        "    await gate.wait()\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _unbounded_waits(fn) == [2]


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
