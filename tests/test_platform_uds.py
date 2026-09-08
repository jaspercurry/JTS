# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jasper import mux
from jasper.control import grouping_supervisor
from jasper.platform import uds
from tests._async_wait import wait_signalled
from tests._socket_paths import short_socket_path_fixture as _short_sock_path_fixture
from tests.fake_clock_fixtures import FakeClock

_IMPORTED_FIXTURES = (_short_sock_path_fixture,)


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


async def test_voice_socket_command_retries_connect_until_socket_appears(
    monkeypatch,
):
    """voice_daemon creates its control socket last during startup. A
    connect landing just before that must not surface as a hard 503 --
    it should retry within the bounded budget and succeed once the
    socket appears."""
    clock = FakeClock()
    monkeypatch.setattr(uds.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(uds.asyncio, "sleep", clock.sleep)

    reader, writer = _connection(b'{"result":"OK"}\n')
    attempts = 0

    async def flaky_connect(_path):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise FileNotFoundError(_path)
        return reader, writer

    monkeypatch.setattr(uds.asyncio, "open_unix_connection", flaky_connect)

    result = await uds.voice_socket_command("/run/jasper/voice.sock", "START")

    assert result == {"result": "OK"}
    assert attempts == 3
    assert clock.now == pytest.approx(2 * uds._CONNECT_RETRY_INTERVAL_SEC)


async def test_voice_socket_command_gives_up_after_retry_budget(monkeypatch):
    """A socket that never appears (daemon genuinely down, not merely
    restarting) still fails -- but only after the bounded budget, not on
    the first connect."""
    clock = FakeClock()
    monkeypatch.setattr(uds.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(uds.asyncio, "sleep", clock.sleep)

    attempts = 0

    async def always_missing(_path):
        nonlocal attempts
        attempts += 1
        raise FileNotFoundError(_path)

    monkeypatch.setattr(uds.asyncio, "open_unix_connection", always_missing)

    with pytest.raises(FileNotFoundError):
        await uds.voice_socket_command("/run/jasper/voice.sock", "START")

    assert attempts > 1, "gave up on the first attempt instead of retrying"
    assert clock.now >= uds._CONNECT_RETRY_BUDGET_SEC
    # Bounded: doesn't retry forever past the budget.
    assert clock.now < uds._CONNECT_RETRY_BUDGET_SEC + uds._CONNECT_RETRY_INTERVAL_SEC


async def test_voice_socket_command_retries_connection_refused_too(monkeypatch):
    """A stale socket file mid-teardown/startup race refuses the connect
    (ECONNREFUSED) rather than ENOENT -- same transient condition, same
    retry."""
    clock = FakeClock()
    monkeypatch.setattr(uds.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(uds.asyncio, "sleep", clock.sleep)

    reader, writer = _connection(b'{"result":"OK"}\n')
    attempts = 0

    async def flaky_connect(_path):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise ConnectionRefusedError(_path)
        return reader, writer

    monkeypatch.setattr(uds.asyncio, "open_unix_connection", flaky_connect)

    result = await uds.voice_socket_command("/run/jasper/voice.sock", "START")

    assert result == {"result": "OK"}
    assert attempts == 2


async def test_mux_command_is_one_bounded_json_exchange(monkeypatch):
    reader, writer = _connection(b'{"active_source":"idle"}\n')
    opener = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(uds.asyncio, "open_unix_connection", opener)

    result = await uds.mux_socket_command(
        "STATUS",
        socket_path="/tmp/mux.sock",
        timeout=0.25,
    )

    assert result == {"active_source": "idle"}
    opener.assert_awaited_once_with("/tmp/mux.sock")
    writer.write.assert_called_once_with(b"STATUS\n")
    writer.drain.assert_awaited_once()
    writer.close.assert_called_once()


async def test_mux_command_deadline_includes_connect(monkeypatch):
    connect_started = asyncio.Event()

    async def stalled_connect(_path):
        connect_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(uds.asyncio, "open_unix_connection", stalled_connect)

    with pytest.raises(asyncio.TimeoutError):
        await uds.mux_socket_command("STATUS", timeout=0.01)
    assert connect_started.is_set()


async def test_mux_command_wedged_close_cannot_extend_deadline(monkeypatch):
    reader, writer = _connection(b'{"active_source":"idle"}\n')
    writer.wait_closed.side_effect = lambda: asyncio.Event().wait()
    monkeypatch.setattr(
        uds.asyncio,
        "open_unix_connection",
        AsyncMock(return_value=(reader, writer)),
    )

    result = await uds.mux_socket_command("STATUS", timeout=0.01)

    assert result == {"active_source": "idle"}
    writer.close.assert_called_once()
    writer.wait_closed.assert_not_awaited()


async def test_mux_command_validates_request_and_response(monkeypatch):
    with pytest.raises(ValueError, match="one non-empty line"):
        await uds.mux_socket_command("STATUS\nAUTO")
    with pytest.raises(ValueError, match="positive"):
        await uds.mux_socket_command("STATUS", timeout=0)

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
            await uds.mux_socket_command("STATUS")


async def test_mux_command_answers_cancellation_racing_the_reply(monkeypatch):
    """mux_socket_command must terminate its caller when cancelled, even
    when jasper-mux's reply lands in the very same event-loop tick as the
    cancellation.

    Regression for #1952 (the #1935 class). CPython <= 3.11's
    asyncio.wait_for swallows a CancelledError that arrives in the tick its
    awaited future completes (Lib/asyncio/tasks.py: ``except
    CancelledError: if fut.done(): return fut.result()``). This call sits
    on measurement_window.py's _refresh_measurement_gate_lease, a
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
        uds.mux_socket_command(
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
        "mux_socket_command ignored cancellation and is still running -- "
        "a swallowed CancelledError makes "
        "_refresh_measurement_gate_lease's task immortal and wedges "
        "measurement_window() teardown (#1952)"
    )
    assert task.cancelled()


_RING_ENTRIES = 256


def _outputd_status_payload() -> bytes:
    """A STATUS body the size a chip-AEC box actually answers with."""

    ring = [
        {
            "frames_written": 1_073_741_824 + index * 341,
            "snd_pcm_delay_frames": 362 + index % 87,
            "reference_sequence": 4_193_847 + index,
            "age_ms": (_RING_ENTRIES - index) * 21,
        }
        for index in range(_RING_ENTRIES)
    ]
    return json.dumps(
        {
            "backend": "alsa",
            "dac_content": {"serving_fifo": True},
            "reference_outputs": {
                "chip_ref_writer": {
                    "active": True,
                    "recent_writes_capacity": _RING_ENTRIES,
                    "recent_writes": ring,
                }
            },
        },
        separators=(",", ":"),
    ).encode()


async def _serve_once(path: str, payload: bytes):
    """Split even a small response across separate event-loop turns."""

    async def handle(reader, writer):
        # A client that refuses an over-cap reply hangs up mid-send, so every
        # write here has to tolerate the peer going away — otherwise the
        # handler parks on drain() and Server.wait_closed() never returns.
        try:
            await reader.readline()
            chunk_size = min(4096, max(1, len(payload) // 2))
            for start in range(0, len(payload), chunk_size):
                writer.write(payload[start : start + chunk_size])
                await writer.drain()
                await asyncio.sleep(0.001)
            writer.write(b"\n")
            await writer.drain()
            writer.write_eof()
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                writer.close()

    return await asyncio.start_unix_server(handle, path=path)


@pytest.mark.parametrize("payload_kind", ["small", "outputd", "exact-cap", "over-cap"])
@pytest.mark.parametrize("consumer", ["state", "grouping", "mux"])
async def test_status_consumers_reassemble_fragmented_json(
    short_sock_path, tmp_path, monkeypatch, consumer, payload_kind,
):
    monkeypatch.setattr(grouping_supervisor, "OUTPUTD_CONTROL_SOCKET", short_sock_path)
    monkeypatch.setattr(mux, "FANIN_CONTROL_SOCKET", short_sock_path)
    calls = {
        "state": lambda: uds.local_status_json(short_sock_path),
        "grouping": grouping_supervisor.GroupingSupervisor().outputd_status,
        "mux": mux.Mux(mode_state_path=str(tmp_path / "mode"))._fanin_status_best_effort,
    }
    cap = 65_536 if consumer == "mux" else 262_144
    payload = {
        "small": b'{"inputs":[]}',
        "outputd": _outputd_status_payload(),
        "exact-cap": b"{}" + b" " * (cap - 3),
        "over-cap": b"{}" + b" " * (cap - 2),
    }[payload_kind]
    server = await _serve_once(short_sock_path, payload)
    try:
        assert await calls[consumer]() == (None if payload_kind == "over-cap" else json.loads(payload))
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("payload, expected", [
    (b"{}" + b" " * 13, {}),
    (b"{}" + b" " * 14, {}),
    (b"{}" + b" " * 15, None),
    (b"[]", None),
    (b"?", None),
    (b"", None),
])
async def test_status_limits_and_failure_policy(monkeypatch, payload, expected):
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    _, writer = _connection(b"")
    monkeypatch.setattr(
        uds.asyncio, "open_unix_connection", AsyncMock(return_value=(reader, writer)),
    )

    assert await uds.local_status_json("/tmp/status.sock", max_bytes=16) == expected
    writer.close.assert_called_once()
    writer.wait_closed.assert_not_awaited()


@pytest.mark.parametrize("phase", ["connect", "write", "drain", "read"])
async def test_status_transport_failures_return_none(monkeypatch, phase):
    reader, writer = _connection(b"")
    opener = AsyncMock(return_value=(reader, writer))
    operation = {"connect": opener, "write": writer.write, "drain": writer.drain, "read": reader.read}
    operation[phase].side_effect = OSError
    monkeypatch.setattr(uds.asyncio, "open_unix_connection", opener)

    assert await uds.local_status_json("/tmp/status.sock") is None
    assert writer.close.call_count == (0 if phase == "connect" else 1)


@pytest.mark.parametrize("phase", ["connect", "drain", "read"])
async def test_status_deadline_bounds_every_phase(monkeypatch, phase):
    started = asyncio.Event()

    async def stalled(*_args):
        started.set()
        await asyncio.Event().wait()

    reader, writer = _connection(b"")
    reader.read = AsyncMock(return_value=b"")
    opener = AsyncMock(return_value=(reader, writer))
    if phase == "connect":
        opener.side_effect = stalled
    elif phase == "drain":
        writer.drain.side_effect = stalled
    else:
        chunks = iter([b"{}"])

        async def missing_eof(_size):
            chunk = next(chunks, None)
            return chunk if chunk is not None else await stalled()

        reader.read.side_effect = missing_eof
    monkeypatch.setattr(uds.asyncio, "open_unix_connection", opener)

    async with asyncio.timeout(1.0):
        assert await uds.local_status_json("/tmp/status.sock", timeout=0.01) is None
    assert started.is_set()
    assert writer.close.call_count == (0 if phase == "connect" else 1)
    writer.wait_closed.assert_not_awaited()


async def test_status_deadline_is_shared_by_connect_send_and_read(monkeypatch):
    reader, writer = _connection(b"")
    chunks = iter([b"{}", b""])

    async def connect(_path):
        await asyncio.sleep(0.03)
        return reader, writer

    async def drain():
        await asyncio.sleep(0.03)

    async def read(_size):
        await asyncio.sleep(0.03)
        return next(chunks)

    writer.drain.side_effect = drain
    reader.read = read
    monkeypatch.setattr(uds.asyncio, "open_unix_connection", connect)

    assert await uds.local_status_json("/tmp/status.sock", timeout=0.07) is None
    writer.close.assert_called_once()


async def test_status_cancellation_racing_reply_is_preserved(monkeypatch):
    started = asyncio.Event()
    reply = asyncio.get_running_loop().create_future()

    async def read(_size):
        started.set()
        return await reply

    reader, writer = _connection(b"")
    reader.read = read
    monkeypatch.setattr(
        uds.asyncio, "open_unix_connection", AsyncMock(return_value=(reader, writer)),
    )
    task = asyncio.create_task(uds.local_status_json("/tmp/status.sock"))
    await wait_signalled(started, "STATUS reply read started", producer=task)
    reply.set_result(b"{}")
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    writer.close.assert_called_once()
    writer.wait_closed.assert_not_awaited()
