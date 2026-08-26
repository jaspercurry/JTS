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

Structure, second half: nor may one bound such a wait so tightly that the
bound becomes a deadline the test has to beat — see
`SMALL_BOUNDED_WAIT_THRESHOLD_S`. The unbounded guard alone cannot see
that shape, because a `wait_for` bounds whatever it encloses no matter
how small its timeout is.

Both allowlists are two-sided ratchets, mirroring
`tests/test_atomic_io_conventions.py`: a new offender fails, AND a stale
entry fails, so a list can only shrink. They are the burn-down lists for
the sites each bug class already has — not permission to add more.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from ._async_wait import DEFAULT_SIGNAL_TIMEOUT_S, wait_signalled

#: Burn-down list: async tests still coordinating through an unbounded
#: `await <event>.wait()`. Each one can hang forever if its producer dies
#: first. Fix with `wait_signalled()` and DELETE the entry — the guard
#: fails on a stale entry too, so this only ever shrinks. Never add.
#:
#: Even a racing-producer
#: site with no single task nameable as `wait_signalled`'s `producer=` —
#: test_concurrent_device_operations_share_one_recovered_bus's pair_task
#: vs. connect_task — still migrates: `producer=` is optional, and the
#: bound alone (no producer attribution) still turns a hang into a fast,
#: clear failure instead of leaving the list non-empty.
KNOWN_UNBOUNDED_WAITS: frozenset[tuple[str, str]] = frozenset()

#: Floor for a `wait_for(<event>.wait(), timeout=...)` bound in a test.
#:
#: In this repo a timing promise is pinned by an explicit
#: `assert elapsed < N` — test_alert_storm_does_not_postpone_fixed_patrol
#: in tests/test_mux.py does exactly that, alongside a `wait_signalled`
#: whose own bound it calls a hang-breaker. A `wait_for` timeout never
#: pins one: nothing reads it, nothing reports it, and a test that beats
#: it learns nothing. So a small bound on an event wait is always a
#: hang-breaker in disguise — and a hang-breaker set near the coordination
#: it is breaking is a deadline the test has to beat on a loaded box.
#:
#: 1.0 is the bottom of the band the tree already sits in: 29 of the 45
#: bounded event waits on 2026-08-17 (`wait_for` sites; counting the
#: `wait_signalled` calls that name a bound too gives 49) were exactly
#: 1.0, and nothing at all fell between 0.2 and 1.0. So the ratchet starts
#: with an EMPTY allowlist and grandfathers nothing.
SMALL_BOUNDED_WAIT_THRESHOLD_S = 1.0

#: Burn-down list for the other half: async tests bounding an event wait
#: below `SMALL_BOUNDED_WAIT_THRESHOLD_S`. Empty because the last three
#: sites (all in one mux test) were fixed when this guard landed. Fix with
#: `wait_signalled()` and DELETE the entry — the guard fails on a stale
#: entry too, so this only ever shrinks. Never add.
KNOWN_SMALL_BOUNDED_WAITS: frozenset[tuple[str, str]] = frozenset()

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


def _timeout_literal(call: ast.Call) -> float | None:
    """`call`'s timeout when it is written as a plain number, else None.

    A variable or expression is not guessed at — the guard has no way to
    know what it holds. That is also why `wait_signalled`'s own internal
    `asyncio.wait_for(event.wait(), timeout)` is not a candidate.
    """

    node: ast.expr | None = None
    if len(call.args) >= 2:
        node = call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            node = keyword.value
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return float(node.value)
    return None


def _small_bounded_waits(fn: ast.AsyncFunctionDef) -> list[int]:
    """Lines where `fn`'s own body bounds an event wait too tightly."""

    nested = _nodes_under_nested_defs(fn)
    lines: list[int] = []
    for node in ast.walk(fn):
        if id(node) in nested or not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "wait_for" or not node.args:
            continue
        # Any no-arg `.wait()` receiver, not just an `asyncio.Event` —
        # `proc.wait()` and `cond.wait()` match too, and are meant to: a
        # sub-second bound is a deadline the test has to beat whatever it
        # is waiting on. `wait_for(<coroutine>, ...)` written any other
        # way is out of scope; its timeout may well be the point.
        awaited = node.args[0]
        if not isinstance(awaited, ast.Call):
            continue
        func = awaited.func
        if not isinstance(func, ast.Attribute) or func.attr != "wait":
            continue
        timeout = _timeout_literal(node)
        if timeout is not None and timeout < SMALL_BOUNDED_WAIT_THRESHOLD_S:
            lines.append(node.lineno)
    return lines


def _flagged_tests(
    detect: Callable[[ast.AsyncFunctionDef], list[int]],
) -> dict[tuple[str, str], list[int]]:
    """Every async test under tests/ whose own body `detect` flags.

    Scoped to `async def test_*` exactly as the unbounded guard is, and
    each detector drops nodes under nested defs for the same reason: a
    helper coroutine's wait is driven by the test body, not by pytest.
    Both choices are deliberate, and together they are why a helper module
    like tests/_async_wait.py is out of scope without being named here.
    """

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
            lines = detect(node)
            if lines:
                offenders.setdefault((relative, node.name), []).extend(lines)
    return offenders


def _offending_tests() -> dict[tuple[str, str], list[int]]:
    """Every async test under tests/ with an unbounded wait in its body."""

    return _flagged_tests(_unbounded_waits)


def _small_bounded_tests() -> dict[tuple[str, str], list[int]]:
    """Every async test under tests/ bounding an event wait too tightly."""

    return _flagged_tests(_small_bounded_waits)


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


def test_no_new_test_bounds_an_event_wait_below_the_hang_breaker_floor() -> None:
    """A tight `wait_for` bound on an event wait is a deadline in disguise.

    It looks bounded, so the unbounded guard passes it, but nothing reads
    the timeout as a promise: the test just has to reach its signal before
    the bound, and on a loaded box it sometimes does not.
    """

    offenders = _small_bounded_tests()
    new = sorted(key for key in offenders if key not in KNOWN_SMALL_BOUNDED_WAITS)

    assert not new, (
        f"`wait_for(<x>.wait(), timeout=<{SMALL_BOUNDED_WAIT_THRESHOLD_S}s)` "
        "in a test body — the bound is a hang-breaker, not a timing promise, so "
        "a small one only adds a deadline the test can lose. Raise it above the "
        "floor, or use wait_signalled() from tests/_async_wait.py when the wait "
        "is on an asyncio.Event; pin any real timing promise with an explicit "
        "`assert elapsed < N`:\n"
        + "\n".join(
            f"  {path}::{name}  line(s) {offenders[(path, name)]}"
            for path, name in new
        )
    )


def test_known_small_bounded_wait_allowlist_has_no_stale_entries() -> None:
    """This ratchet only tightens too, for the same reason as the other."""

    stale = sorted(KNOWN_SMALL_BOUNDED_WAITS - set(_small_bounded_tests()))

    assert not stale, (
        "stale KNOWN_SMALL_BOUNDED_WAITS entries — the test was fixed, renamed, "
        "or removed. Delete these so the ratchet keeps tightening:\n"
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


def test_guard_detects_a_bound_below_the_floor() -> None:
    """The shape the unbounded guard cannot see: bounded, but too tightly."""

    tree = ast.parse(
        "async def test_x():\n"
        "    await asyncio.wait_for(started.wait(), timeout=0.2)\n"
        "    await asyncio.wait_for(other.wait(), 0.5)\n"  # positional timeout
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _unbounded_waits(fn) == []
    assert _small_bounded_waits(fn) == [2, 3]


def test_guard_accepts_hang_breaker_bounds_and_unknowable_timeouts() -> None:
    """At the floor, above it, integral, non-literal, or not an event wait."""

    tree = ast.parse(
        "async def test_x():\n"
        "    async def producer():\n"
        "        await asyncio.wait_for(release.wait(), timeout=0.2)\n"
        "    await asyncio.wait_for(started.wait(), timeout=1.0)\n"
        "    await asyncio.wait_for(other.wait(), timeout=1)\n"
        "    await asyncio.wait_for(later.wait(), timeout=budget)\n"
        "    await asyncio.wait_for(reader.read(4), timeout=0.2)\n"
        "    await wait_signalled(done, 'done')\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _small_bounded_waits(fn) == []


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
