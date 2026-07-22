# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded client for jasper-fanin's one-line control protocol."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .status import FANIN_STATUS_SOCKET


async def fanin_command(
    command: str,
    *,
    socket_path: str = FANIN_STATUS_SOCKET,
    timeout_sec: float = 2.0,
) -> dict[str, Any]:
    """Send one ASCII command and return fan-in's JSON-object response."""

    if not command or "\n" in command or "\r" in command:
        raise ValueError("fan-in command must be one non-empty line")
    if timeout_sec <= 0:
        raise ValueError("fan-in command timeout must be positive")

    async def exchange() -> bytes:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        try:
            writer.write((command + "\n").encode("ascii"))
            await writer.drain()
            return await reader.readline()
        finally:
            # Initiate teardown synchronously. ``wait_closed()`` is not part of
            # the wire contract and a non-cooperative transport could suppress
            # cancellation, defeating the total deadline below.
            try:
                writer.close()
            except (OSError, RuntimeError):
                pass

    # A single deadline bounds the entire low-level gate transition. Separate
    # per-stage timeouts could multiply the advertised bound, while an unbounded
    # close wait could wedge mux behind its transition lock indefinitely.
    line = await asyncio.wait_for(exchange(), timeout=timeout_sec)
    if not line:
        raise RuntimeError("jasper-fanin returned no response")
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("jasper-fanin returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-fanin returned non-object JSON")
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload


__all__ = ["fanin_command"]
