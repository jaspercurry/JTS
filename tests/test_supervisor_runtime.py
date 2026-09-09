# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from jasper.control import supervisor_runtime


class _StopLoop(BaseException):
    pass


async def test_run_loop_isolates_tick_crash_and_preserves_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    events: list[tuple[str, dict[str, object]]] = []
    ticks = 0

    def fake_log_event(
        _logger: logging.Logger,
        event: str,
        **fields: object,
    ) -> None:
        explicit_fields = fields.pop("fields", {})
        assert isinstance(explicit_fields, dict)
        fields = {**explicit_fields, **fields}
        events.append((event, fields))

    async def tick() -> None:
        nonlocal ticks
        ticks += 1
        calls.append(("tick", ticks))
        if ticks == 1:
            raise RuntimeError("one bad poll")

    async def sleep(delay: float) -> None:
        calls.append(("sleep", delay))
        if len([call for call in calls if call[0] == "sleep"]) == 3:
            raise _StopLoop

    def uniform(low: float, high: float) -> float:
        calls.append(("uniform", (low, high)))
        return 2.0

    monkeypatch.setattr(supervisor_runtime, "log_event", fake_log_event)
    with pytest.raises(_StopLoop):
        await supervisor_runtime.run_supervisor_loop(
            tick=tick,
            cold_start_sec=60.0,
            interval_sec=30.0,
            jitter_sec=3.0,
            logger=logging.getLogger("test.supervisor"),
            start_event="example.start",
            tick_crash_event="example.tick_crash",
            start_fields={"interval": "30s", "threshold": 3},
            sleep=sleep,
            uniform=uniform,
        )

    assert calls == [
        ("sleep", 60.0),
        ("tick", 1),
        ("uniform", (-3.0, 3.0)),
        ("sleep", 32.0),
        ("tick", 2),
        ("uniform", (-3.0, 3.0)),
        ("sleep", 32.0),
    ]
    assert events == [
        ("example.start", {"interval": "30s", "threshold": 3}),
        (
            "example.tick_crash",
            {"level": logging.ERROR, "exc_info": True},
        ),
    ]


async def test_run_loop_does_not_swallow_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    sleeps: list[float] = []

    def fake_log_event(
        _logger: logging.Logger,
        event: str,
        **_fields: object,
    ) -> None:
        events.append(event)

    async def tick() -> None:
        raise asyncio.CancelledError

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(supervisor_runtime, "log_event", fake_log_event)
    with pytest.raises(asyncio.CancelledError):
        await supervisor_runtime.run_supervisor_loop(
            tick=tick,
            cold_start_sec=12.0,
            interval_sec=30.0,
            jitter_sec=3.0,
            logger=logging.getLogger("test.supervisor"),
            start_event="example.start",
            tick_crash_event="example.tick_crash",
            start_fields={},
            sleep=sleep,
        )

    assert sleeps == [12.0]
    assert events == ["example.start"]


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, "auto"),
        ({"TEST_SUPERVISOR": "AUTO"}, "auto"),
        ({"TEST_SUPERVISOR": "DiSaBlEd"}, "disabled"),
        ({"TEST_SUPERVISOR": "ON"}, "on"),
        ({"TEST_SUPERVISOR": " disabled "}, " disabled "),
        ({"TEST_SUPERVISOR": ""}, ""),
    ],
)
def test_resolve_env_mode_preserves_existing_exact_match_contract(
    environ: dict[str, str],
    expected: str,
) -> None:
    assert supervisor_runtime.resolve_env_mode(
        "TEST_SUPERVISOR",
        environ=environ,
    ) == expected


def test_snapshot_or_disabled_returns_common_fallback() -> None:
    assert supervisor_runtime.snapshot_or_disabled(None) == {"enabled": False}


def test_snapshot_or_disabled_delegates_without_copying() -> None:
    state = {"enabled": True, "count": 4}
    assert supervisor_runtime.snapshot_or_disabled(lambda: state) is state


def test_shared_loop_hosts_every_target_on_one_daemon_thread() -> None:
    """Every jasper-control background coroutine shares one thread and one
    loop (ADR-0226)."""
    logger = logging.getLogger("test.supervisor")
    seen: list[tuple[threading.Thread, asyncio.AbstractEventLoop]] = []
    done = threading.Semaphore(0)

    async def record() -> None:
        seen.append((threading.current_thread(), asyncio.get_running_loop()))
        done.release()

    for index in range(2):
        supervisor_runtime.spawn_on_control_loop(
            target=record,
            name=f"example-{index}",
            logger=logger,
            crash_event="example.thread_crash",
        )
    assert done.acquire(timeout=5)
    assert done.acquire(timeout=5)

    assert len(seen) == 2
    assert seen[0] == seen[1]
    host, _loop = seen[0]
    assert host.daemon is True
    assert host is not threading.current_thread()


def test_target_crash_is_isolated_from_the_other_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One supervisor blowing up must not take the shared loop, or the
    supervisors sharing it, down with it."""
    logger = logging.getLogger("test.supervisor")
    events: list[str] = []
    monkeypatch.setattr(
        supervisor_runtime,
        "log_event",
        lambda _logger, event, **_fields: events.append(event),
    )

    async def resident() -> None:
        await asyncio.Event().wait()

    async def crashing() -> None:
        raise RuntimeError("one broken supervisor")

    async def probe() -> None:
        return None

    resident_future = supervisor_runtime.spawn_on_control_loop(
        target=resident,
        name="resident-supervisor",
        logger=logger,
        crash_event="resident.thread_crash",
    )
    crash_future = supervisor_runtime.spawn_on_control_loop(
        target=crashing,
        name="crashing-supervisor",
        logger=logger,
        crash_event="crashing.thread_crash",
    )
    try:
        assert crash_future.result(timeout=5) is None
        assert events == ["crashing.thread_crash"]
        probe_future = supervisor_runtime.spawn_on_control_loop(
            target=probe,
            name="probe-supervisor",
            logger=logger,
            crash_event="probe.thread_crash",
        )
        assert probe_future.result(timeout=5) is None
        assert not resident_future.done()
    finally:
        resident_future.cancel()
