# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one client for JTS's line-oriented control sockets.

voice_daemon, jasper-mux, jasper-fanin and jasper-outputd all speak the same
shape: one ASCII command line in, one JSON-object line back, ``{"error": ...}``
for a refusal. :func:`daemon_command` is that exchange; the named wrappers below
only carry each daemon's default socket path and timeout. Every wire-level
failure raises ``RuntimeError``; transport failures raise the underlying
``OSError``/``TimeoutError``.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from . import wire
from .status_socket import FANIN_STATUS_SOCKET, MUX_CONTROL_SOCKET_PATH

# The one ceiling every local STATUS reader in jasper-control shares.  It is a
# safety bound on a hostile or wedged local daemon, not a size estimate for any
# particular payload — set it far above what any daemon actually answers so
# that growing a diagnostic surface can never quietly blind a reader.
# jasper-outputd's own STATUS is tens of KiB on a chip-AEC box.
MAX_STATUS_BYTES = 256 * 1024

# Seconds. voice_daemon creates its control socket last during startup (~2s
# after the process itself starts). A connect landing in that window would
# otherwise surface as a hard "not running" 503 for a daemon that is merely
# still coming up. Bounded well under the bridge's 2.0s per-request HTTP
# timeout (jasper/platform/control_client.py DEFAULT_TIMEOUT) so a caller sees
# one clean 503 rather than its own request timing out mid-retry.
_CONNECT_RETRY_INTERVAL_SEC = 0.25
_CONNECT_RETRY_BUDGET_SEC = 1.2

async def _connect(
    socket_path: str, retry_budget_sec: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open the UDS, optionally retrying a not-yet-created/not-yet-bound
    socket for up to ``retry_budget_sec``."""
    deadline = time.monotonic() + retry_budget_sec
    while True:
        try:
            return await asyncio.open_unix_connection(socket_path)
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(_CONNECT_RETRY_INTERVAL_SEC)


async def daemon_command(
    socket_path: str,
    command: str,
    *,
    timeout: float,
    daemon: str,
    connect_retry_budget_sec: float = 0.0,
) -> dict[str, Any]:
    """Send one ASCII command line and return the daemon's JSON object.

    ``timeout`` is seconds, and is ONE deadline covering connect, send,
    response and close: separate per-stage timeouts multiply the advertised
    bound, and an unbounded close wait could wedge a caller holding a
    transition lock.
    """
    if not command or "\n" in command or "\r" in command:
        raise ValueError(f"{daemon} command must be one non-empty line")
    if timeout <= 0:
        raise ValueError(f"{daemon} command timeout must be positive")

    async def exchange() -> bytes:
        reader, writer = await _connect(socket_path, connect_retry_budget_sec)
        try:
            writer.write((command + "\n").encode("ascii"))
            await writer.drain()
            return await reader.readline()
        finally:
            # ``close()`` initiates transport teardown without another await.
            # Awaiting ``wait_closed()`` here would let a broken transport
            # suppress cancellation and outlive the caller's total deadline.
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass

    # asyncio.timeout(), NOT asyncio.wait_for(): on CPython <= 3.11 wait_for
    # SWALLOWS a CancelledError that arrives in the same tick its awaited
    # future completes (Lib/asyncio/tasks.py: `except CancelledError: if
    # fut.done(): return fut.result()`). Callers on cancellation-only
    # `while True:` loops -- measurement_window's lease refreshers (#1952),
    # VolumeObserver._run through renderer.selected_source (#2003), Mux.run()'s
    # patrol wait (#1935) -- would become immortal and wedge their owner's
    # teardown. Do not "simplify" this back to wait_for while 3.11 is supported.
    async with asyncio.timeout(timeout):
        line = await exchange()
    if not line:
        raise RuntimeError(f"{daemon} returned no response")
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{daemon} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{daemon} returned non-object JSON")
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload


async def voice_socket_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict[str, Any]:
    """One command to voice_daemon's control socket.

    ``timeout`` is seconds; the default covers session-state commands.
    Cue playback takes longer
    (~6s for a 5s cue plus duck/restore plus drain) and bumps ``timeout``.
    """
    return await daemon_command(
        socket_path,
        cmd,
        timeout=timeout,
        daemon="voice_daemon",
        connect_retry_budget_sec=_CONNECT_RETRY_BUDGET_SEC,
    )


async def mux_socket_command(
    cmd: str,
    *,
    socket_path: str = MUX_CONTROL_SOCKET_PATH,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """One command to jasper-mux's local control socket. ``timeout`` is seconds.

    The web frontend should not talk to fan-in directly: mux owns the
    manual-vs-auto source policy and uses fan-in only as the low-level
    audio gate.
    """
    return await daemon_command(
        socket_path, cmd, timeout=timeout, daemon="jasper-mux",
    )


async def fanin_command(
    command: str,
    *,
    socket_path: str = FANIN_STATUS_SOCKET,
    timeout_sec: float = 2.0,
) -> dict[str, Any]:
    """One command to jasper-fanin's control socket (mux's source gate)."""
    return await daemon_command(
        socket_path, command, timeout=timeout_sec, daemon="jasper-fanin",
    )


async def local_status_json(
    socket_path: str,
    *,
    timeout: float = 2.0,
    max_bytes: int = MAX_STATUS_BYTES,
) -> dict | None:
    """Read STATUS JSON to EOF within one deadline; return None on failure."""
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(socket_path)
            try:
                writer.write(wire.encode(wire.STATUS))
                await writer.drain()
                chunks: list[bytes] = []
                total = 0
                while chunk := await reader.read(65_536):
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
            finally:
                # Transport teardown must not extend the deadline or consume cancellation.
                try:
                    writer.close()
                except (OSError, RuntimeError):
                    pass
        payload = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    except (TimeoutError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
