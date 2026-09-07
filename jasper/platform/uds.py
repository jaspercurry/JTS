# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Client side of the voice-daemon and jasper-mux control sockets, shared by
control, measurement and doctor callers."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .status_socket import MUX_CONTROL_SOCKET_PATH

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
# (jasper/platform/control_client.py DEFAULT_TIMEOUT) so a caller sees one clean 503
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


async def voice_socket_command(
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


async def mux_socket_command(
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
                writer.write(b"STATUS\n")
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
