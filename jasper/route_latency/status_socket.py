# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One shared reader for the ``STATUS\\n`` line protocol fan-in and outputd use.

Several callers need "connect to a JTS control socket, send ``STATUS\\n``, read
the JSON reply to EOF, parse it, and confirm it's an object". This module owns
that mechanic once; each caller keeps its OWN error policy on top, because they
genuinely differ:

* doctor and the AEC CLIs want the exception to propagate so they can name the
  failure in their own report; they use :func:`read_status_socket` inside a
  try/except of their own;
* the click/capture harness wants a fail-soft ``None`` per surface (an
  unreachable daemon is an expected snapshot state) and logs at DEBUG; it uses
  :func:`read_status_socket_or_none`.
* ``deploy/bin/jasper-apply-airplay-mode``'s ``ExecStartPre`` heredoc — a
  shell script, not a Python module, invoking this one via ``python -`` at
  boot to derive shairport's AirPlay latency offset — also uses
  :func:`read_status_socket_or_none`. It has no except-block of its own on
  the shell side, so it depends on every failure mode landing on ``None``
  rather than an uncaught exception reaching the interpreter's exit code.

Deliberately NOT unifying the ``coupling_reconcile`` / ``audio_validation``
copies here: those return different shapes (``(dict|None, str)``; a
``None``-on-``OSError`` variant with its own logging) and have their own
contract tests, so folding them in is a separate reviewed change, not this
one's scope.
"""
from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any

from jasper.log_event import log_event


logger = logging.getLogger("jasper.route_latency.status_socket")

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


def read_status_socket(path: str, *, timeout: float = DEFAULT_STATUS_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Connect to a JTS ``STATUS\\n`` control socket and return its JSON reply.

    ``timeout`` is a TOTAL deadline across connect, send and every recv, not a
    per-operation one: a daemon dribbling a byte per timeout window must not be
    able to hold a caller open indefinitely. The reply is capped at
    :data:`_RESPONSE_MAX_BYTES`, and decoded lossily so a stray byte in an
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
            if received > _RESPONSE_MAX_BYTES:
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
