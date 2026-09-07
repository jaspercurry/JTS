# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synchronous STATUS transport; callers own limits and failure policy."""
from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any

from jasper.log_event import log_event


logger = logging.getLogger("jasper.platform.status_socket")

# Seconds, TOTAL deadline for connect + send + every recv. 3.0 because the
# reader used to arm 1.0 s per operation, so a boot-time caller on the
# 415 MB Pi Zero 2 W keeps the same worst-case budget it had before the
# deadline was made total.
DEFAULT_STATUS_TIMEOUT_SECONDS = 3.0
_RECV_CHUNK_BYTES = 65536
# A daemon's STATUS reply is a few KiB; the cap bounds what a wedged or
# runaway writer can make a caller buffer on a 1 GB Pi.
_RESPONSE_MAX_BYTES = 1_048_576

FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"
MUX_CONTROL_SOCKET_PATH = "/run/jasper-mux/control.sock"
OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"

# Ceilings on each owner's STATUS `watchdog.last_progress_age_ms`, in
# milliseconds — above them the transport is stale. Fan-in's is deliberately
# the LOOSER of the two: a stalled fan-in starves CamillaDSP, which empties the
# ring, so outputd latches deaf at 2 s; tripping fan-in earlier than 5 s would
# report the symptom ahead of its own cause.
FANIN_STALE_MS = 5000
OUTPUTD_STALE_MS = 3000


def read_status_socket(
    path: str,
    *,
    timeout: float = DEFAULT_STATUS_TIMEOUT_SECONDS,
    max_bytes: int = _RESPONSE_MAX_BYTES,
) -> dict[str, Any]:
    """Connect to a JTS ``STATUS\\n`` control socket and return its JSON reply.

    ``timeout`` is a TOTAL deadline across connect, send and every recv, not a
    per-operation one: a daemon dribbling a byte per timeout window must not be
    able to hold a caller open indefinitely. The reply is capped at
    ``max_bytes``, and decoded lossily so a stray byte in an
    otherwise well-formed reply does not cost a caller the counters it came for.

    Raises the underlying ``OSError`` / ``TimeoutError`` on a connect/read
    failure or an over-cap reply, ``json.JSONDecodeError`` on an unparseable
    reply, and ``ValueError`` when the reply's JSON root is not an object — so
    a caller that wants to classify or surface the specific failure can.
    Callers that prefer fail-soft ``None`` should use
    :func:`read_status_socket_or_none`.
    """

    deadline = time.monotonic() + timeout

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        def arm_remaining_timeout() -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("STATUS response deadline exceeded")
            sock.settimeout(remaining)

        arm_remaining_timeout()
        sock.connect(path)
        arm_remaining_timeout()
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        received = 0
        while True:
            arm_remaining_timeout()
            chunk = sock.recv(_RECV_CHUNK_BYTES)
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise OSError("STATUS response exceeds byte limit")
            chunks.append(chunk)
    parsed = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise ValueError(
            f"STATUS response root is {type(parsed).__name__}, not an object"
        )
    return parsed


def read_status_socket_or_none(
    path: str,
    *,
    timeout: float = DEFAULT_STATUS_TIMEOUT_SECONDS,
    event: str = "route_latency.status_socket_unavailable",
) -> dict[str, Any] | None:
    """Fail-soft wrapper around :func:`read_status_socket`.

    Returns ``None`` (logging at DEBUG under ``event=``) instead of raising
    when the socket is unreachable or its reply is malformed — an unreachable
    daemon is an expected state when snapshotting route health, not an error
    that should abort the caller.
    """

    try:
        return read_status_socket(path, timeout=timeout)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log_event(
            logger,
            event,
            source=path,
            error=str(e),
            level=logging.DEBUG,
        )
        return None


__all__ = [
    "DEFAULT_STATUS_TIMEOUT_SECONDS",
    "FANIN_STATUS_SOCKET",
    "MUX_CONTROL_SOCKET_PATH",
    "OUTPUTD_STATUS_SOCKET",
    "read_status_socket",
    "read_status_socket_or_none",
]
