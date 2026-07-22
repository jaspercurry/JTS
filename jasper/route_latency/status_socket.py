# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One shared reader for the ``STATUS\\n`` line protocol fan-in and outputd use.

Several route-latency surfaces need "connect to a JTS control socket, send
``STATUS\\n``, read the JSON reply to EOF, parse it, and confirm it's an
object": the artifact writer (:mod:`jasper.cli.route_latency_artifact`) and the
click/capture harness (:mod:`jasper.cli.route_latency_harness`). Both live in
this subsystem and previously carried near-identical copies of the socket
mechanic. This module owns the mechanic once; each caller keeps its OWN error
policy on top, because they genuinely differ:

* the artifact writer wants the exception to propagate so it can classify the
  failure (``live_fanin_status_unreadable:{type}``); it wraps this in a
  try/except of its own;
* the harness wants a fail-soft ``None`` per surface (an unreachable daemon is
  an expected snapshot state) and logs at DEBUG; it uses
  :func:`read_status_socket_or_none`.

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
from typing import Any

from jasper.log_event import log_event


logger = logging.getLogger("jasper.route_latency.status_socket")

DEFAULT_STATUS_TIMEOUT_SECONDS = 1.0
_RECV_CHUNK_BYTES = 65536

# Canonical control-socket paths for the two live route-health owners.
FANIN_STATUS_SOCKET = "/run/jasper-fanin/control.sock"
OUTPUTD_STATUS_SOCKET = "/run/jasper-outputd/control.sock"


def read_status_socket(path: str, *, timeout: float = DEFAULT_STATUS_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Connect to a JTS ``STATUS\\n`` control socket and return its JSON reply.

    Raises the underlying ``OSError`` / ``TimeoutError`` on a connect/read
    failure, ``json.JSONDecodeError`` on an unparseable reply, and
    ``ValueError`` when the reply's JSON root is not an object — so a caller
    that wants to classify or surface the specific failure can. Callers that
    prefer fail-soft ``None`` should use :func:`read_status_socket_or_none`.
    """

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(b"STATUS\n")
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(_RECV_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
    parsed = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("STATUS response root is not an object")
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
    "OUTPUTD_STATUS_SOCKET",
    "read_status_socket",
    "read_status_socket_or_none",
]
