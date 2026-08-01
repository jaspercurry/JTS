# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from jasper.control import uds


def _connection(reply: bytes):
    reader = AsyncMock()
    reader.readline.return_value = reply
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


class _PendingReader:
    """Reader whose readline() blocks on a future the test controls.

    Needed (instead of the AsyncMock-based `_connection` above) so the
    cancellation-race test can resolve the reply at an exact,
    test-chosen event-loop tick rather than immediately.
    """

    def __init__(self, reply: "asyncio.Future[bytes]") -> None:
        self._reply = reply

    async def readline(self) -> bytes:
        return await self._reply


@pytest.mark.asyncio
async def test_mux_command_is_one_bounded_json_exchange(monkeypatch):
    reader, writer = _connection(b'{"active_source":"idle"}\n')
    opener = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(uds.asyncio, "open_unix_connection", opener)

    result = await uds._mux_socket_command(
        "STATUS",
        socket_path="/tmp/mux.sock",
        timeout=0.25,
    )

    assert result == {"active_source": "idle"}
    opener.assert_awaited_once_with("/tmp/mux.sock")
    writer.write.assert_called_once_with(b"STATUS\n")
    writer.drain.assert_awaited_once()
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_mux_command_deadline_includes_connect(monkeypatch):
    connect_started = asyncio.Event()

    async def stalled_connect(_path):
        connect_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(uds.asyncio, "open_unix_connection", stalled_connect)

    with pytest.raises(asyncio.TimeoutError):
        await uds._mux_socket_command("STATUS", timeout=0.01)
    assert connect_started.is_set()


@pytest.mark.asyncio
async def test_mux_command_wedged_close_cannot_extend_deadline(monkeypatch):
    reader, writer = _connection(b'{"active_source":"idle"}\n')
    writer.wait_closed.side_effect = lambda: asyncio.Event().wait()
    monkeypatch.setattr(
        uds.asyncio,
        "open_unix_connection",
        AsyncMock(return_value=(reader, writer)),
    )

    result = await uds._mux_socket_command("STATUS", timeout=0.01)

    assert result == {"active_source": "idle"}
    writer.close.assert_called_once()
    writer.wait_closed.assert_not_awaited()


@pytest.mark.asyncio
async def test_mux_command_validates_request_and_response(monkeypatch):
    with pytest.raises(ValueError, match="one non-empty line"):
        await uds._mux_socket_command("STATUS\nAUTO")
    with pytest.raises(ValueError, match="positive"):
        await uds._mux_socket_command("STATUS", timeout=0)

    for reply, match in (
        (b'{"error":"bad owner"}\n', "bad owner"),
        (b"[]\n", "non-object"),
        (b"", "no response"),
    ):
        reader, writer = _connection(reply)
        monkeypatch.setattr(
            uds.asyncio,
            "open_unix_connection",
            AsyncMock(return_value=(reader, writer)),
        )
        with pytest.raises(RuntimeError, match=match):
            await uds._mux_socket_command("STATUS")


@pytest.mark.asyncio
async def test_mux_command_answers_cancellation_racing_the_reply(monkeypatch):
    """_mux_socket_command must terminate its caller when cancelled, even
    when jasper-mux's reply lands in the very same event-loop tick as the
    cancellation.

    Regression for #1952 (the #1935 class). CPython <= 3.11's
    asyncio.wait_for swallows a CancelledError that arrives in the tick its
    awaited future completes (Lib/asyncio/tasks.py: ``except
    CancelledError: if fut.done(): return fut.result()``). This call sits
    on correction/coordinator.py's _refresh_measurement_gate_lease, a
    cancellation-only ``while True:`` that measurement_window()'s finally
    cancels and then awaits unboundedly -- a swallowed cancel here makes
    that task immortal and wedges the whole window teardown.

    The race is constructed deterministically, not sampled: resolve the
    reply future and cancel() the task with no intervening await, so both
    wake-ups queue in the same event-loop tick. Mirrors
    test_mux.py::test_run_answers_cancellation_racing_a_wake_alert (#1935).
    """
    loop = asyncio.get_running_loop()
    reply: asyncio.Future[bytes] = loop.create_future()
    reader = _PendingReader(reply)
    writer = MagicMock()
    writer.drain = AsyncMock()
    monkeypatch.setattr(
        uds.asyncio,
        "open_unix_connection",
        AsyncMock(return_value=(reader, writer)),
    )

    task = asyncio.create_task(
        uds._mux_socket_command(
            "STATUS", socket_path="/tmp/mux.sock", timeout=30.0,
        )
    )
    # Let the task open the (fake) connection, write, and park inside the
    # bounded wait for the reply before racing it. Measured empirically for
    # this exact call shape on 3.11.15: offset 0 never swallows (the
    # wrapped exchange() task hasn't run its first step yet); offsets 1-7
    # swallow 100/100 on the pre-fix code. This is well inside that window.
    for _ in range(3):
        await asyncio.sleep(0)

    reply.set_result(b'{"active_source":"idle"}\n')
    task.cancel()

    done, pending = await asyncio.wait({task}, timeout=10.0)
    assert not pending, (
        "_mux_socket_command ignored cancellation and is still running -- "
        "a swallowed CancelledError makes "
        "_refresh_measurement_gate_lease's task immortal and wedges "
        "measurement_window() teardown (#1952)"
    )
    assert task.cancelled()
