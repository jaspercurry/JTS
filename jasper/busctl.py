# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded, leak-free async ``busctl --system`` subprocess boundary."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass


@dataclass(frozen=True)
class BusctlResult:
    """Completed ``busctl`` process result, including diagnostic stderr."""

    returncode: int
    stdout: bytes
    stderr: bytes


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(asyncio.TimeoutError, OSError):
        async with asyncio.timeout(1.0):
            await proc.wait()


async def run_busctl(
    *args: str,
    bus: str = "--system",
    timeout: float = 2.0,
) -> BusctlResult | None:
    """Run one bounded busctl command; return ``None`` when unavailable.

    A non-zero process is still a completed result so callers that distinguish
    a missing D-Bus name from a transport failure can inspect stderr.
    """

    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl",
            bus,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        await _kill_and_reap(proc)
        raise
    except asyncio.TimeoutError:
        await _kill_and_reap(proc)
        return None
    if proc.returncode is None:
        return None
    return BusctlResult(proc.returncode, stdout, stderr)


async def system_busctl(
    *args: str,
    timeout: float = 2.0,
) -> bytes | None:
    """Return stdout for a successful system-bus call, otherwise ``None``.

    Timeout kills and reaps the child. Spawn failures and non-zero exits are
    ordinary unavailable results so fail-soft probes and best-effort control
    adapters do not leak subprocesses or crash their owning daemons.
    """

    result = await run_busctl(*args, timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    return result.stdout
