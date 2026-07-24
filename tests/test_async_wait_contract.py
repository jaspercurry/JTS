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
KNOWN_UNBOUNDED_WAITS: frozenset[tuple[str, str]] = frozenset({
    ("tests/test_audio_measurement_admitted_playback.py",
     "test_playback_cancellation_preserves_admission_when_close_fails"),
    ("tests/test_audio_measurement_admitted_playback.py",
     "test_playback_failure_and_cancellation_have_correlated_terminal_events"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_cancellation_during_capture_restores_before_propagating"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_cancellation_settles_candidate_apply_before_restore"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_preserves_apply_failure_that_finishes_after_cancellation"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_repeated_cancellation_cannot_interrupt_restore"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_cancellation_during_success_cleanup_waits_for_restore"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_preserves_cleanup_cancellation_when_restore_fails_after_success"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_preserves_body_failure_cleanup_cancellation_and_restore_failure"),
    ("tests/test_audio_measurement_null_walk.py",
     "test_runner_preserves_repeated_cancellation_and_restore_failure"),
    ("tests/test_audio_measurement_playback.py",
     "test_continuous_tone_cancel_reaps_and_logs_lifecycle"),
    ("tests/test_audio_measurement_playback.py",
     "test_continuous_tone_unconfirmed_cancel_cleanup_is_bounded"),
    ("tests/test_bass_extension_bench_activation.py",
     "test_cancellation_still_restores"),
    ("tests/test_bluetooth_engine.py",
     "test_scan_refresh_replaces_timer_without_stacking_stop_calls"),
    ("tests/test_bluetooth_engine.py",
     "test_scan_refresh_wins_exact_expiry_race_and_remains_discovering"),
    ("tests/test_bluetooth_engine.py",
     "test_concurrent_device_operations_share_one_recovered_bus"),
    ("tests/test_bluetooth_engine.py",
     "test_engine_stop_during_start_does_not_resurrect_scan_or_timer"),
    ("tests/test_camilla_controller.py",
     "test_direct_graph_mutation_wins_race_before_intent_publication"),
    ("tests/test_camilla_controller.py",
     "test_cancellation_cancels_worker_still_queued_in_executor"),
    ("tests/test_correction_coordinator.py",
     "test_window_b_blocked_while_window_a_restore_in_flight"),
    ("tests/test_correction_level_match.py",
     "test_stop_after_locked_waits_for_terminal_ack_then_restores"),
    ("tests/test_correction_level_match.py",
     "test_session_ensure_and_restore_share_one_transition_lock"),
    ("tests/test_correction_playback.py", "test_cancelled_sweep_kills_and_reaps_aplay"),
    ("tests/test_correction_playback.py", "test_repeated_cancellation_still_reaps_aplay"),
    ("tests/test_correction_session.py",
     "test_emergency_stop_reaps_audio_task_before_reset"),
    ("tests/test_correction_session.py",
     "test_emergency_stop_gracefully_reaps_active_relay_level_ramp"),
    ("tests/test_correction_session.py",
     "test_emergency_stop_reaps_level_match_before_phone_arms"),
    ("tests/test_crossover_driver_level_domain.py",
     "test_level_cancel_during_restore_drains_then_discards_identity"),
    ("tests/test_dsp_apply.py",
     "test_pending_intent_race_orders_ordinary_writer_before_recovery"),
    ("tests/test_mux.py", "test_source_observations_serialize_probe_and_record"),
    ("tests/test_peering_discovery.py",
     "test_slow_added_cannot_overwrite_later_updated_identity"),
    ("tests/test_peering_discovery.py",
     "test_removed_waits_for_slow_added_without_peer_resurrection"),
    ("tests/test_voice_daemon_manual_start_guard.py",
     "test_measurement_auto_clear_releases_reconcile_guard"),
    ("tests/test_volume_coordinator.py",
     "test_nonzero_intent_publishes_before_slow_spotify_dispatch"),
    ("tests/test_volume_coordinator.py",
     "test_mute_intent_is_local_and_published_before_slow_spotify"),
    ("tests/test_volume_coordinator.py",
     "test_mute_context_publishes_before_blocked_camilla_backstop"),
    ("tests/test_volume_coordinator.py",
     "test_overlapping_push_writes_keep_source_persistence_and_context_aligned"),
    ("tests/test_volume_coordinator.py",
     "test_context_snapshot_retries_after_concurrent_volume_change"),
    ("tests/test_volume_coordinator.py",
     "test_reconcile_in_flight_stops_when_measurement_begins"),
    ("tests/test_volume_coordinator.py",
     "test_measurement_pause_waits_for_in_flight_reconcile_write"),
    ("tests/test_wake_events.py", "test_concurrent_sweeps_do_not_double_scan"),
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
