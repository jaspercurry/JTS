# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from jasper.fanin import control


def _connection(reply: bytes):
    reader = AsyncMock()
    reader.readline.return_value = reply
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


@pytest.mark.asyncio
async def test_fanin_command_is_one_bounded_json_exchange(monkeypatch):
    reader, writer = _connection(b'{"result":"ok"}\n')
    opener = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(control.asyncio, "open_unix_connection", opener)

    result = await control.fanin_command(
        "MUTE usbsink",
        socket_path="/tmp/fanin.sock",
        timeout_sec=0.25,
    )

    assert result == {"result": "ok"}
    opener.assert_awaited_once_with("/tmp/fanin.sock")
    writer.write.assert_called_once_with(b"MUTE usbsink\n")
    writer.drain.assert_awaited_once()
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_fanin_command_deadline_includes_connect(monkeypatch):
    connect_started = asyncio.Event()

    async def stalled_connect(_path):
        connect_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(control.asyncio, "open_unix_connection", stalled_connect)

    with pytest.raises(asyncio.TimeoutError):
        await control.fanin_command("STATUS", timeout_sec=0.01)
    assert connect_started.is_set()


@pytest.mark.asyncio
async def test_fanin_command_wedged_close_cannot_extend_deadline(monkeypatch):
    reader, writer = _connection(b'{"result":"ok"}\n')
    writer.wait_closed.side_effect = lambda: asyncio.Event().wait()
    monkeypatch.setattr(
        control.asyncio,
        "open_unix_connection",
        AsyncMock(return_value=(reader, writer)),
    )

    result = await control.fanin_command("STATUS", timeout_sec=0.01)

    assert result == {"result": "ok"}
    writer.close.assert_called_once()
    writer.wait_closed.assert_not_awaited()


@pytest.mark.asyncio
async def test_fanin_command_rejects_error_and_malformed_responses(monkeypatch):
    for reply, match in (
        (b'{"error":"bad lane"}\n', "bad lane"),
        (b"[]\n", "non-object"),
        (b"not-json\n", "invalid JSON"),
        (b"", "no response"),
    ):
        reader, writer = _connection(reply)
        monkeypatch.setattr(
            control.asyncio,
            "open_unix_connection",
            AsyncMock(return_value=(reader, writer)),
        )
        with pytest.raises(RuntimeError, match=match):
            await control.fanin_command("STATUS")


@pytest.mark.asyncio
async def test_fanin_control_rejects_multiline_command():
    with pytest.raises(ValueError, match="one non-empty line"):
        await control.fanin_command("STATUS\nNONE")
