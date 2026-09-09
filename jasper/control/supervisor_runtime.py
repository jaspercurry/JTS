# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared runtime mechanics for jasper-control's background coroutines.

Subsystem policy, event names, operator warnings, singleton ownership, and
public start wrappers stay in each supervisor module.  This module owns only
the identical execution mechanics those supervisors rely on, and the one
thread and event loop that host every one of them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future
from typing import Any

from jasper.log_event import log_event


async def run_supervisor_loop(
    *,
    tick: Callable[[], Awaitable[None]],
    cold_start_sec: float,
    interval_sec: float,
    jitter_sec: float,
    logger: logging.Logger,
    start_event: str,
    tick_crash_event: str,
    start_fields: Mapping[str, Any],
    sleep: Callable[[float], Awaitable[None]] | None = None,
    uniform: Callable[[float, float], float] | None = None,
) -> None:
    """Run one supervisor's cold-start and isolated polling loop."""
    sleep_fn = asyncio.sleep if sleep is None else sleep
    uniform_fn = random.uniform if uniform is None else uniform
    log_event(logger, start_event, fields=dict(start_fields))
    await sleep_fn(cold_start_sec)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001 - one broken tick must not kill liveness
            log_event(
                logger,
                tick_crash_event,
                level=logging.ERROR,
                exc_info=True,
            )
        await sleep_fn(
            interval_sec + uniform_fn(-jitter_sec, jitter_sec),
        )


def resolve_env_mode(
    env_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the case-normalized mode, preserving invalid values for logs."""
    source = os.environ if environ is None else environ
    return source.get(env_name, "auto").lower()


def snapshot_or_disabled(
    snapshot_fn: Callable[[], dict[str, Any]] | None,
) -> dict[str, Any]:
    """Return live supervisor state or the common not-running shape."""
    if snapshot_fn is None:
        return {"enabled": False}
    return snapshot_fn()


_HOST_THREAD_NAME = "control-loop"
_host_lock = threading.Lock()
_host_loop: asyncio.AbstractEventLoop | None = None


def _control_loop() -> asyncio.AbstractEventLoop:
    """Return the one background loop, starting its thread on first use."""
    global _host_loop
    with _host_lock:
        if _host_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=_host_thread,
                args=(loop,),
                name=_HOST_THREAD_NAME,
                daemon=True,
            ).start()
            _host_loop = loop
        return _host_loop


def _host_thread(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        loop.close()


def spawn_on_control_loop(
    *,
    target: Callable[[], Awaitable[None]],
    name: str,
    logger: logging.Logger,
    crash_event: str,
) -> Future[None]:
    """Run one long-lived coroutine on jasper-control's single background
    thread and event loop (ADR-0226: the Pi Zero 2 W has 415 MB, so the
    supervisors and the peering daemon share one loop). A target that raises
    logs `crash_event` and takes down only itself."""

    async def _guarded() -> None:
        task = asyncio.current_task()
        if task is not None:
            task.set_name(name)
        try:
            await target()
        except Exception:  # noqa: BLE001 - preserve a stable crash breadcrumb
            log_event(
                logger,
                crash_event,
                level=logging.ERROR,
                exc_info=True,
            )

    return asyncio.run_coroutine_threadsafe(_guarded(), _control_loop())
