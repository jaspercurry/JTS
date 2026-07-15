# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Local UDS client helpers used by jasper-control endpoints."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

MUX_CONTROL_SOCKET_PATH = os.environ.get(
    "JASPER_MUX_CONTROL_SOCKET",
    "/run/jasper-mux/control.sock",
)


async def _voice_socket_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict:
    """Send one ASCII line to voice_daemon's control socket and return
    the parsed JSON response. Used by /session/start, /session/end,
    and /cue/play. The default 5s timeout covers session-state
    commands; cue playback takes longer (~6s for a 5s cue plus
    duck/restore plus drain) and bumps timeout explicitly."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError("voice_daemon returned no response")
    return json.loads(line.decode("utf-8"))


async def _mux_socket_command(
    cmd: str,
    *,
    socket_path: str = MUX_CONTROL_SOCKET_PATH,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Send one ASCII command to jasper-mux's local control socket.

    The web frontend should not talk to fan-in directly: mux owns the
    manual-vs-auto source policy and uses fan-in only as the low-level
    audio gate.
    """
    if not cmd or "\n" in cmd or "\r" in cmd:
        raise ValueError("jasper-mux command must be one non-empty line")
    if timeout <= 0:
        raise ValueError("jasper-mux command timeout must be positive")

    async def exchange() -> bytes:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        try:
            writer.write((cmd + "\n").encode("ascii"))
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

    # One deadline covers connect, send, response, and close. In particular,
    # correction's lease-renewal deadline cannot be defeated by a wedged UDS
    # connect or writer drain while mux's safety lease continues to age.
    line = await asyncio.wait_for(exchange(), timeout=timeout)
    if not line:
        raise RuntimeError("jasper-mux returned no response")
    payload = json.loads(line.decode("utf-8"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-mux returned non-object JSON")
    return payload


async def _local_status_json(
    socket_path: str,
    *,
    timeout: float = 2.0,
    max_bytes: int = 8192,
) -> dict | None:
    """Best-effort one-shot STATUS probe for local daemon UDS sockets."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path),
            timeout=timeout,
        )
    except (FileNotFoundError, ConnectionRefusedError,
            asyncio.TimeoutError, OSError):
        return None
    try:
        writer.write(b"STATUS\n")
        await writer.drain()
        body = await asyncio.wait_for(reader.read(max_bytes), timeout=timeout)
    except (asyncio.TimeoutError, ConnectionResetError, OSError):
        writer.close()
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, AssertionError):
            pass
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
