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
