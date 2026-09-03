# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-task restart supervision for the accessory bridge process.

``jasper-input`` hosts every accessory bridge in one interpreter (ADR-0225).
systemd's ``Restart=`` is a *process* contract, so under one roof a fault in
the BLE mic adapter would take the HID button bridge down with it — and the
HID bridge is how volume and push-to-talk reach jasper-control. This
supervisor makes the restart unit the task instead: each bridge runs in its
own loop, a crash is logged and retried after a fixed backoff, and no bridge
can end the process or another bridge's run.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# Long enough that a transient fault costs one gesture rather than a session.
RESTART_BACKOFF_SEC = 2.0
# Consecutive failures double the wait up to this ceiling, so a bridge that can
# never start (no BlueZ, no udev, a broken venv) idles instead of spinning the
# Pi Zero 2 W's single core and writing a journal line every 2 s forever. The
# attempt count rides along on every failure line: a bridge stuck in this loop
# is invisible in the unit's state, which stays `active`.
MAX_RESTART_BACKOFF_SEC = 60.0

Bridge = Callable[[], Awaitable[None]]


async def _run_forever(name: str, bridge: Bridge, backoff_sec: float) -> None:
    consecutive_failures = 0
    while True:
        try:
            await bridge()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — this breadth IS the contract
            consecutive_failures += 1
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
            log_event(logger, "accessory.bridge_exited", bridge=name)
        await asyncio.sleep(min(
            backoff_sec * 2 ** max(consecutive_failures - 1, 0),
            MAX_RESTART_BACKOFF_SEC,
        ))


async def supervise(
    bridges: Mapping[str, Bridge],
    *,
    backoff_sec: float = RESTART_BACKOFF_SEC,
) -> None:
    """Run every bridge until cancelled, restarting each one independently."""

    tasks = [
        asyncio.create_task(_run_forever(name, bridge, backoff_sec), name=name)
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
