# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared real-server fixture for `jasper.peering.uds` tests.

tests/test_peering_uds.py and tests/test_peering_client.py both spin up a
real `uds_mod.serve(...)` server and tear it down the same way; only the
ARBITRATE handler (and, for test_peering_uds.py, the session-notice
callbacks it wants to observe) differ between them.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from jasper.peering import uds as uds_mod
from tests._socket_paths import short_unix_socket_path as _short_socket_path

ArbitrateHandler = Callable[[dict], Awaitable[dict]]
SessionStartedHandler = Callable[[str], Awaitable[None]]
SessionEndedHandler = Callable[[str, str], Awaitable[None]]


async def _noop_started(_epoch: str) -> None:
    pass


async def _noop_ended(_epoch: str, _reason: str) -> None:
    pass


@asynccontextmanager
async def peering_uds_server(
    arbitrate: ArbitrateHandler,
    *,
    notify_session_started: SessionStartedHandler = _noop_started,
    notify_session_ended: SessionEndedHandler = _noop_ended,
) -> AsyncIterator[str]:
    """Run a real peering UDS server with the given ARBITRATE handler.

    Yields the socket path; closes the server and unlinks the socket on
    exit, even if the body raises.
    """
    sock_path = _short_socket_path()
    server = await uds_mod.serve(
        path=sock_path,
        arbitrate=arbitrate,
        notify_session_started=notify_session_started,
        notify_session_ended=notify_session_ended,
    )
    try:
        yield sock_path
    finally:
        server.close()
        await server.wait_closed()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
