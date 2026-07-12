# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from jasper.control import (
    grouping_supervisor,
    shairport_supervisor,
    system_supervisor,
)


_CASES = [
    (
        shairport_supervisor,
        "ShairportSupervisor",
        "JASPER_SHAIRPORT_SUPERVISOR",
        "shairport.disabled",
        "shairport-supervisor",
        "shairport.thread_crash",
    ),
    (
        grouping_supervisor,
        "GroupingSupervisor",
        "JASPER_GROUPING_SUPERVISOR",
        "grouping_supervisor.disabled",
        "grouping-supervisor",
        "grouping_supervisor.thread_crash",
    ),
    (
        system_supervisor,
        "SystemSupervisor",
        "JASPER_SYSTEM_SUPERVISOR",
        "system_supervisor.disabled",
        "system-supervisor",
        "system_supervisor.thread_crash",
    ),
]

_RUN_CASES: list[
    tuple[
        ModuleType,
        Callable[[Path], object],
        str,
        str,
        dict[str, object],
    ]
] = [
    (
        shairport_supervisor,
        lambda _path: shairport_supervisor.ShairportSupervisor(
            interval_sec=31.0,
            jitter_sec=4.0,
            failure_threshold=5,
            rate_limit_sec=601.0,
            cold_start_sec=61.0,
        ),
        "shairport.start",
        "shairport.tick_crash",
        {"interval": "31s", "threshold": 5, "rate_limit": "601s"},
    ),
    (
        grouping_supervisor,
        lambda _path: grouping_supervisor.GroupingSupervisor(
            interval_sec=31.0,
            jitter_sec=4.0,
            starved_threshold=5,
            kick_rate_limit_sec=601.0,
            cold_start_sec=61.0,
        ),
        "grouping_supervisor.start",
        "grouping_supervisor.tick_crash",
        {"interval": "31s", "threshold": 5, "rate_limit": "601s"},
    ),
    (
        system_supervisor,
        lambda path: system_supervisor.SystemSupervisor(
            interval_sec=31.0,
            jitter_sec=4.0,
            failure_threshold=5,
            rate_limit_sec=601.0,
            cold_start_sec=61.0,
            reboot_state_path=path,
        ),
        "system_supervisor.start",
        "system_supervisor.tick_crash",
        {
            "interval": "31s",
            "threshold": 5,
            "rate_limit": "601s",
            "cold_start": "61s",
        },
    ),
]


@pytest.mark.parametrize(
    ("module", "factory", "start_event", "tick_crash_event", "start_fields"),
    _RUN_CASES,
)
@pytest.mark.asyncio
async def test_run_wrapper_preserves_local_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: ModuleType,
    factory: Callable[[Path], object],
    start_event: str,
    tick_crash_event: str,
    start_fields: dict[str, object],
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_supervisor_loop(**kwargs: object) -> None:
        captured.update(kwargs)

    supervisor = factory(tmp_path / "reboot-state.json")
    monkeypatch.setattr(module, "run_supervisor_loop", fake_run_supervisor_loop)

    await supervisor.run()

    assert captured["tick"].__self__ is supervisor
    assert captured["cold_start_sec"] == 61.0
    assert captured["interval_sec"] == 31.0
    assert captured["jitter_sec"] == 4.0
    assert captured["logger"] is module.logger
    assert captured["start_event"] == start_event
    assert captured["tick_crash_event"] == tick_crash_event
    assert captured["start_fields"] == start_fields


@pytest.mark.parametrize(
    ("module", "class_name", "env_name", "disabled_event", "thread_name", "crash_event"),
    _CASES,
)
def test_disabled_mode_preserves_local_event_and_does_not_construct(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    class_name: str,
    env_name: str,
    disabled_event: str,
    thread_name: str,
    crash_event: str,
) -> None:
    del thread_name, crash_event
    events: list[str] = []

    def fail_construct() -> None:
        raise AssertionError("disabled supervisor must not be constructed")

    def fail_start(**_kwargs: object) -> None:
        raise AssertionError("disabled supervisor must not start a thread")

    monkeypatch.setattr(module, "_supervisor", None)
    monkeypatch.setattr(module, "_supervisor_thread", None)
    monkeypatch.setattr(module, class_name, fail_construct)
    monkeypatch.setattr(module, "build_asyncio_thread", fail_start)
    monkeypatch.setattr(
        module,
        "log_event",
        lambda _logger, event, **_fields: events.append(event),
    )
    monkeypatch.setenv(env_name, "DiSaBlEd")

    assert module.start_supervisor() is None
    assert module._supervisor is None
    assert module._supervisor_thread is None
    assert events == [disabled_event]


@pytest.mark.parametrize(
    ("module", "class_name", "env_name", "disabled_event", "thread_name", "crash_event"),
    _CASES,
)
def test_auto_mode_starts_once_with_local_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    class_name: str,
    env_name: str,
    disabled_event: str,
    thread_name: str,
    crash_event: str,
) -> None:
    del disabled_event
    constructed: list[object] = []
    starts: list[dict[str, object]] = []
    class FakeThread:
        def __init__(self) -> None:
            self.start_calls = 0

        def start(self) -> None:
            assert module._supervisor_thread is self
            self.start_calls += 1

    thread = FakeThread()

    class FakeSupervisor:
        def __init__(self) -> None:
            constructed.append(self)

        async def run(self) -> None:
            return None

        def snapshot(self) -> dict[str, Any]:
            return {"enabled": True}

    def fake_build_asyncio_thread(**kwargs: object) -> object:
        starts.append(kwargs)
        return thread

    monkeypatch.setattr(module, "_supervisor", None)
    monkeypatch.setattr(module, "_supervisor_thread", None)
    monkeypatch.setattr(module, class_name, FakeSupervisor)
    monkeypatch.setattr(module, "build_asyncio_thread", fake_build_asyncio_thread)
    monkeypatch.setenv(env_name, "AUTO")

    first = module.start_supervisor()
    second = module.start_supervisor()

    assert first is thread
    assert second is thread
    assert thread.start_calls == 1
    assert constructed == [module._supervisor]
    assert len(starts) == 1
    assert starts[0]["target"].__self__ is module._supervisor
    assert starts[0]["name"] == thread_name
    assert starts[0]["logger"] is module.logger
    assert starts[0]["crash_event"] == crash_event


@pytest.mark.parametrize(
    ("module", "class_name", "env_name", "disabled_event", "thread_name", "crash_event"),
    _CASES,
)
def test_unrecognized_mode_warns_exactly_and_stays_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    module: ModuleType,
    class_name: str,
    env_name: str,
    disabled_event: str,
    thread_name: str,
    crash_event: str,
) -> None:
    del disabled_event, thread_name, crash_event

    class FakeSupervisor:
        async def run(self) -> None:
            return None

    class FakeThread:
        def start(self) -> None:
            return None

    monkeypatch.setattr(module, "_supervisor", None)
    monkeypatch.setattr(module, "_supervisor_thread", None)
    monkeypatch.setattr(module, class_name, FakeSupervisor)
    monkeypatch.setattr(module, "build_asyncio_thread", lambda **_kwargs: FakeThread())
    monkeypatch.setenv(env_name, "ON")

    with caplog.at_level(logging.WARNING, logger=module.__name__):
        thread = module.start_supervisor()

    assert thread is not None
    assert caplog.messages == [
        f"{env_name}='on' unrecognized; treating as 'auto'. "
        "Use 'disabled' to turn the supervisor off.",
    ]


@pytest.mark.parametrize(
    ("module", "class_name", "env_name", "disabled_event", "thread_name", "crash_event"),
    _CASES,
)
def test_snapshot_wrapper_preserves_common_disabled_and_live_shapes(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    class_name: str,
    env_name: str,
    disabled_event: str,
    thread_name: str,
    crash_event: str,
) -> None:
    del class_name, env_name, disabled_event, thread_name, crash_event
    state = {"enabled": True, "sentinel": module.__name__}

    class FakeSupervisor:
        def snapshot(self) -> dict[str, Any]:
            return state

    monkeypatch.setattr(module, "_supervisor", None)
    assert module.snapshot() == {"enabled": False}

    monkeypatch.setattr(module, "_supervisor", FakeSupervisor())
    assert module.snapshot() is state
