# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from jasper.control import supervisor_runtime


class _StopLoop(BaseException):
    pass


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


class _InlineThread:
    created: list[_InlineThread] = []

    def __init__(
        self,
        *,
        target: Callable[[], None],
        name: str,
        daemon: bool,
    ) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.created.append(self)

    def start(self) -> None:
        self.started = True
        self.target()


def test_build_asyncio_thread_hosts_target_in_named_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _InlineThread.created.clear()
    running_loops: list[asyncio.AbstractEventLoop] = []

    async def target() -> None:
        running_loops.append(asyncio.get_running_loop())

    monkeypatch.setattr(supervisor_runtime.threading, "Thread", _InlineThread)
    try:
        thread = supervisor_runtime.build_asyncio_thread(
            target=target,
            name="example-supervisor",
            logger=logging.getLogger("test.supervisor"),
            crash_event="example.thread_crash",
        )
        assert thread.started is False
        thread.start()
    finally:
        asyncio.set_event_loop(None)

    assert thread is _InlineThread.created[0]
    assert thread.name == "example-supervisor"
    assert thread.daemon is True
    assert thread.started is True
    assert len(running_loops) == 1
    assert running_loops[0].is_closed()


def test_build_asyncio_thread_logs_target_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _InlineThread.created.clear()
    events: list[tuple[str, dict[str, object]]] = []

    async def target() -> None:
        raise RuntimeError("supervisor escaped")

    def fake_log_event(
        _logger: logging.Logger,
        event: str,
        **fields: object,
    ) -> None:
        events.append((event, fields))

    monkeypatch.setattr(supervisor_runtime.threading, "Thread", _InlineThread)
    monkeypatch.setattr(supervisor_runtime, "log_event", fake_log_event)
    try:
        thread = supervisor_runtime.build_asyncio_thread(
            target=target,
            name="example-supervisor",
            logger=logging.getLogger("test.supervisor"),
            crash_event="example.thread_crash",
        )
        thread.start()
    finally:
        asyncio.set_event_loop(None)

    assert events == [
        (
            "example.thread_crash",
            {"level": logging.ERROR, "exc_info": True},
        ),
    ]


def test_build_asyncio_thread_tolerates_loop_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _InlineThread.created.clear()
    set_loops: list[object] = []

    class BrokenCloseLoop:
        def run_until_complete(
            self,
            awaitable: Coroutine[Any, Any, None],
        ) -> None:
            awaitable.close()

        def close(self) -> None:
            raise RuntimeError("already broken")

    loop = BrokenCloseLoop()

    async def target() -> None:
        return None

    monkeypatch.setattr(supervisor_runtime.threading, "Thread", _InlineThread)
    monkeypatch.setattr(supervisor_runtime.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(
        supervisor_runtime.asyncio,
        "set_event_loop",
        set_loops.append,
    )

    thread = supervisor_runtime.build_asyncio_thread(
        target=target,
        name="example-supervisor",
        logger=logging.getLogger("test.supervisor"),
        crash_event="example.thread_crash",
    )
    assert thread.started is False
    thread.start()

    assert thread.started is True
    assert set_loops == [loop]
