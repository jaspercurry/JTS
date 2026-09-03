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

That isolation is also what hides a wedged bridge: the unit stays ``active``
while one bridge loops in backoff forever, and the journal is the only place
that says so. So the supervisor also publishes a per-bridge restart count and
last failure class to :data:`STATUS_PATH`, which
``/state.resilience.accessory_bridges`` reads.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from jasper.atomic_io import atomic_write_json
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# Long enough that a transient fault costs one gesture rather than a session.
RESTART_BACKOFF_SEC = 2.0
# Consecutive failures double the wait up to this ceiling, so a bridge that can
# never start (no BlueZ, no udev, a broken venv) idles instead of spinning the
# Pi Zero 2 W's single core and writing a journal line every 2 s forever. The
# attempt count rides along on every failure line.
MAX_RESTART_BACKOFF_SEC = 60.0

# jasper-input's own RuntimeDirectory (deploy/systemd/jasper-input.service).
# systemd reaps it when the unit stops, so a file that exists always describes
# the process running now — there is no staleness stamp to carry or check.
STATUS_PATH = "/run/jasper-input/status.json"
# Group-readable so jasper-control (Group=jasper, like this unit) can read it.
_STATUS_FILE_MODE = 0o640

Bridge = Callable[[], Awaitable[None]]


def _publish(
    health: Mapping[str, Any], status_path: str | os.PathLike
) -> None:
    """Fail-soft: the journal already carries the failure this file mirrors,
    so an unwritable /run costs observability, never a bridge."""
    try:
        atomic_write_json(
            status_path,
            {"bridges": dict(health)},
            mode=_STATUS_FILE_MODE,
            group_from_parent=True,
        )
    except OSError as exc:
        log_event(
            logger,
            "accessory.status_publish_failed",
            level=logging.DEBUG,
            err=type(exc).__name__,
        )


def snapshot(status_path: str | os.PathLike = STATUS_PATH) -> dict[str, Any]:
    """Read side of :data:`STATUS_PATH`, for ``/state``. Never raises.

    ``bridges`` maps each supervised bridge to ``restarts`` (failures since
    this process started; it only ever grows) and ``last_error`` (the failing
    exception's CLASS NAME only — a bridge fault's message can carry a device
    name or address). ``last_error`` clears when the bridge returns cleanly,
    so a bridge running again after a crash still shows what it crashed on.
    A missing or corrupt file reads as ``present: False``."""
    try:
        with open(status_path, encoding="utf-8") as f:
            raw = json.load(f)
        return {
            "present": True,
            "bridges": {
                str(name): {
                    "restarts": int(entry["restarts"]),
                    "last_error": entry["last_error"],
                }
                for name, entry in raw["bridges"].items()
            },
        }
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        return {"present": False, "bridges": {}}


async def _run_forever(
    name: str,
    bridge: Bridge,
    backoff_sec: float,
    health: dict[str, dict[str, Any]],
    status_path: str | os.PathLike,
) -> None:
    consecutive_failures = 0
    entry = health[name]
    while True:
        try:
            await bridge()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — this breadth IS the contract
            consecutive_failures += 1
            entry["restarts"] += 1
            entry["last_error"] = type(exc).__name__
            _publish(health, status_path)
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
            if entry["last_error"] is not None:
                entry["last_error"] = None
                _publish(health, status_path)
            log_event(logger, "accessory.bridge_exited", bridge=name)
        await asyncio.sleep(min(
            backoff_sec * 2 ** max(consecutive_failures - 1, 0),
            MAX_RESTART_BACKOFF_SEC,
        ))


async def supervise(
    bridges: Mapping[str, Bridge],
    *,
    backoff_sec: float = RESTART_BACKOFF_SEC,
    status_path: str | os.PathLike = STATUS_PATH,
) -> None:
    """Run every bridge until cancelled, restarting each one independently."""

    health: dict[str, dict[str, Any]] = {
        name: {"restarts": 0, "last_error": None} for name in bridges
    }
    _publish(health, status_path)
    tasks = [
        asyncio.create_task(
            _run_forever(name, bridge, backoff_sec, health, status_path),
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
