# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared runtime mechanics for the jasper-control supervisors.

Subsystem policy, event names, operator warnings, singleton ownership, and
public start wrappers stay in each supervisor module.  This module owns only
the identical execution mechanics those supervisors rely on.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
from collections.abc import Awaitable, Callable, Mapping
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


def build_asyncio_thread(
    *,
    target: Callable[[], Awaitable[None]],
    name: str,
    logger: logging.Logger,
    crash_event: str,
) -> threading.Thread:
    """Build a named daemon thread that hosts one async supervisor target."""

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(target())
        except Exception:  # noqa: BLE001 - preserve a stable crash breadcrumb
            log_event(
                logger,
                crash_event,
                level=logging.ERROR,
                exc_info=True,
            )
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass

    return threading.Thread(target=_run, name=name, daemon=True)
