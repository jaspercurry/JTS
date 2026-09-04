# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Local UDS client helpers used by jasper-control endpoints."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

MUX_CONTROL_SOCKET_PATH = "/run/jasper-mux/control.sock"
# The one ceiling every local STATUS reader in jasper-control shares.  It is a
# safety bound on a hostile or wedged local daemon, not a size estimate for any
# particular payload — set it far above what any daemon actually answers so
# that growing a diagnostic surface can never quietly blind a reader.
# jasper-outputd's own STATUS is tens of KiB on a chip-AEC box.
MAX_STATUS_BYTES = 256 * 1024

# voice_daemon creates its control socket last during startup (~2s after the
# process itself starts). A connect landing in that window would otherwise
# surface as a hard "not running" 503 for a daemon that is merely still
# coming up. Bounded well under the bridge's 2.0s per-request HTTP timeout
# (jasper/control/client.py DEFAULT_TIMEOUT) so a caller sees one clean 503
# rather than its own request timing out mid-retry.
_CONNECT_RETRY_INTERVAL_SEC = 0.25
_CONNECT_RETRY_BUDGET_SEC = 1.2


async def _connect_voice_socket(
    socket_path: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open the voice_daemon UDS, retrying a not-yet-created/not-yet-bound
    socket for up to ``_CONNECT_RETRY_BUDGET_SEC``."""
    deadline = time.monotonic() + _CONNECT_RETRY_BUDGET_SEC
    while True:
        try:
            return await asyncio.open_unix_connection(socket_path)
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(_CONNECT_RETRY_INTERVAL_SEC)


async def _voice_socket_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict:
    """Send one ASCII line to voice_daemon's control socket and return
    the parsed JSON response. Used by /session/start, /session/end,
    and /cue/play. The default 5s timeout covers session-state
    commands; cue playback takes longer (~6s for a 5s cue plus
    duck/restore plus drain) and bumps timeout explicitly."""
    reader, writer = await _connect_voice_socket(socket_path)
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
    #
    # asyncio.timeout(), NOT asyncio.wait_for(): on CPython <= 3.11 wait_for
    # SWALLOWS a CancelledError that arrives in the same tick its awaited
    # future completes (Lib/asyncio/tasks.py: `except CancelledError: if
    # fut.done(): return fut.result()`). measurement_window.py's
    # _refresh_measurement_gate_lease calls this from a cancellation-only
    # `while True:` that measurement_window()'s finally cancels and then
    # awaits unboundedly -- a swallowed cancel here makes that task immortal
    # and wedges the whole window teardown (#1952, same class as #1935's
    # Mux.run() patrol wait). Do not "simplify" this back to wait_for while
    # 3.11 is supported.
    async with asyncio.timeout(timeout):
        line = await exchange()
    if not line:
        raise RuntimeError("jasper-mux returned no response")
    payload = json.loads(line.decode("utf-8"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-mux returned non-object JSON")
    return payload


async def read_status_body(
    reader: asyncio.StreamReader,
    *,
    timeout: float,
    max_bytes: int = MAX_STATUS_BYTES,
) -> bytes | None:
    """Read one STATUS reply to EOF, byte- and time-bounded. None if over cap.

    The mechanic, not the policy: each caller keeps its own timeout and its own
    meaning for None.  It is shared because a SINGLE bounded ``read(n)`` is a
    silent-truncation trap — it returns a prefix, `json.loads` raises, and a
    fail-soft caller reports "daemon unreachable" for a daemon that answered
    perfectly.  jasper-outputd's STATUS crossed 8 KiB when the chip-reference
    writer's sample ring landed (#2253) and blinded exactly that way, so the
    ceiling now lives in one place with one reader.

    A reply genuinely larger than the cap returns None rather than a prefix:
    a truncated object is not a smaller answer, it is a wrong one.
    """

    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError("STATUS reply did not complete in time")
        chunk = await asyncio.wait_for(reader.read(65_536), timeout=remaining)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)


async def _local_status_json(
    socket_path: str,
    *,
    timeout: float = 2.0,
    max_bytes: int = MAX_STATUS_BYTES,
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
        body = await read_status_body(reader, timeout=timeout, max_bytes=max_bytes)
    except (asyncio.TimeoutError, ConnectionResetError, OSError):
        writer.close()
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, AssertionError):
            pass
    if body is None:
        return None
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
