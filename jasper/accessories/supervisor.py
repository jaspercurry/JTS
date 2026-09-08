# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-task restart supervision for the accessory bridge process.

``jasper-input`` hosts every accessory bridge in one interpreter (ADR-0225).
Each bridge restarts independently so a mic fault cannot stop the HID bridge
that carries volume and push-to-talk.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from jasper.atomic_io import atomic_write_json
from jasper.log_event import log_event

from .status import STATUS_PATH

logger = logging.getLogger(__name__)

# Long enough that a transient fault costs one gesture rather than a session.
RESTART_BACKOFF_SEC = 2.0
# Consecutive failures double the wait up to this ceiling, so a bridge that can
# never start (no BlueZ, no udev, a broken venv) idles instead of spinning the
# Pi Zero 2 W's single core and writing a journal line every 2 s forever. The
# attempt count rides along on every failure line.
MAX_RESTART_BACKOFF_SEC = 60.0

_publish_failure_warned = False

Bridge = Callable[[], Awaitable[None]]
Publish = Callable[[], None]
# The one bridge that supervises sub-tasks of its own reports them through
# this: the supervisor calls it once with its publish hook and merges the live
# mapping it gets back into that bridge's status entry, so every publish
# carries the sub-task health next to the supervisor's own restarts/last_error.
Detail = Callable[[Publish], Mapping[str, Any]]


def _publish(health: Mapping[str, Any], status_path: str | os.PathLike) -> None:
    global _publish_failure_warned
    try:
        # 0640 + the parent's group: jasper-control (Group=jasper) reads it.
        atomic_write_json(status_path, {"bridges": health}, mode=0o640)
    except OSError as exc:
        # Fail-soft — an unwritable /run costs observability, never a bridge.
        # One WARNING per process (a missing RuntimeDirectory is a deploy bug
        # worth seeing once), then quiet: this runs on every restart.
        log_event(
            logger,
            "accessory.status_publish_failed",
            level=logging.DEBUG if _publish_failure_warned else logging.WARNING,
            err=type(exc).__name__,
        )
        _publish_failure_warned = True


async def _run_forever(
    name: str,
    bridge: Bridge,
    backoff_sec: float,
    entry: dict[str, Any],
    publish: Publish,
) -> None:
    consecutive_failures = 0
    delay = min(backoff_sec, MAX_RESTART_BACKOFF_SEC)
    while True:
        if entry["last_error"] is not None:
            entry["last_error"] = None
            publish()
        try:
            await bridge()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — this breadth IS the contract
            consecutive_failures += 1
            entry["restarts"] += 1
            entry["last_error"] = type(exc).__name__
            publish()
            log_event(
                logger,
                "accessory.bridge_failed",
                level=logging.WARNING,
                bridge=name,
                attempt=consecutive_failures,
                err=f"{type(exc).__name__}: {exc}",
            )
        else:
            consecutive_failures = 0
            delay = min(backoff_sec, MAX_RESTART_BACKOFF_SEC)
            log_event(logger, "accessory.bridge_exited", bridge=name)
        await asyncio.sleep(delay)
        if consecutive_failures:
            delay = min(delay * 2, MAX_RESTART_BACKOFF_SEC)


async def supervise(
    bridges: Mapping[str, Bridge],
    *,
    backoff_sec: float = RESTART_BACKOFF_SEC,
    status_path: str | os.PathLike = STATUS_PATH,
    detail: tuple[str, Detail] | None = None,
) -> None:
    """Run every bridge until cancelled, restarting each one independently."""

    health: dict[str, dict[str, Any]] = {
        name: {"restarts": 0, "last_error": None} for name in bridges
    }

    def publish() -> None:
        _publish(health, status_path)

    if detail is not None:
        name, hook = detail
        health[name].update(hook(publish))
    publish()
    tasks = [
        asyncio.create_task(
            _run_forever(name, bridge, backoff_sec, health[name], publish),
            name=name,
        )
        for name, bridge in bridges.items()
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        # Awaited, not just cancelled: the bridges release real resources in
        # their own finally blocks (GATT StopNotify, the D-Bus and UDP
        # sockets, the udev observer thread, evdev fds). A cancelled gather
        # already drains its own children, so this covers the other exit —
        # gather propagating one bridge failure while its siblings are still
        # unwinding.
        await asyncio.gather(*tasks, return_exceptions=True)
