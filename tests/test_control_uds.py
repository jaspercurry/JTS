# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jasper.control import grouping_supervisor, uds
from tests._socket_paths import short_socket_path_fixture as _short_sock_path_fixture

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


class _FakeClock:
    """Deterministic stand-in for time.monotonic()/asyncio.sleep() so the
    connect-retry budget tests run instantly instead of over real
    wall-clock seconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.now += secs


async def test_voice_socket_command_retries_connect_until_socket_appears(
    monkeypatch,
):
    """voice_daemon creates its control socket last during startup. A
    connect landing just before that must not surface as a hard 503 --
    it should retry within the bounded budget and succeed once the
    socket appears."""
    clock = _FakeClock()
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

    result = await uds._voice_socket_command("/run/jasper/voice.sock", "START")

    assert result == {"result": "OK"}
    assert attempts == 3
    assert clock.now == pytest.approx(2 * uds._CONNECT_RETRY_INTERVAL_SEC)


async def test_voice_socket_command_gives_up_after_retry_budget(monkeypatch):
    """A socket that never appears (daemon genuinely down, not merely
    restarting) still fails -- but only after the bounded budget, not on
    the first connect."""
    clock = _FakeClock()
    monkeypatch.setattr(uds.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(uds.asyncio, "sleep", clock.sleep)

    attempts = 0

    async def always_missing(_path):
        nonlocal attempts
        attempts += 1
        raise FileNotFoundError(_path)

    monkeypatch.setattr(uds.asyncio, "open_unix_connection", always_missing)

    with pytest.raises(FileNotFoundError):
        await uds._voice_socket_command("/run/jasper/voice.sock", "START")

    assert attempts > 1, "gave up on the first attempt instead of retrying"
    assert clock.now >= uds._CONNECT_RETRY_BUDGET_SEC
    # Bounded: doesn't retry forever past the budget.
    assert clock.now < uds._CONNECT_RETRY_BUDGET_SEC + uds._CONNECT_RETRY_INTERVAL_SEC


async def test_voice_socket_command_retries_connection_refused_too(monkeypatch):
    """A stale socket file mid-teardown/startup race refuses the connect
    (ECONNREFUSED) rather than ENOENT -- same transient condition, same
    retry."""
    clock = _FakeClock()
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

    result = await uds._voice_socket_command("/run/jasper/voice.sock", "START")

    assert result == {"result": "OK"}
    assert attempts == 2


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


async def test_mux_command_deadline_includes_connect(monkeypatch):
    connect_started = asyncio.Event()

    async def stalled_connect(_path):
        connect_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(uds.asyncio, "open_unix_connection", stalled_connect)

    with pytest.raises(asyncio.TimeoutError):
        await uds._mux_socket_command("STATUS", timeout=0.01)
    assert connect_started.is_set()


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


async def test_mux_command_answers_cancellation_racing_the_reply(monkeypatch):
    """_mux_socket_command must terminate its caller when cancelled, even
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


# ---------------------------------------------------------------------------
# STATUS payload ceiling (#2253)
#
# jasper-outputd's STATUS crossed 8 KiB when the chip-reference writer's
# per-write sample ring landed. Every local reader in jasper-control that used
# a single bounded `read(8192)` then received a PREFIX, `json.loads` raised,
# and the fail-soft path reported "daemon unreachable" for a daemon that had
# answered perfectly — /state.outputd null on every chip-AEC box, and the
# grouping supervisor reading a healthy bonded member as starved, which it
# answers with a reconciler kick that RESTARTS outputd, every rate-limit
# window, forever.
#
# The fake below serves a realistically-sized outputd STATUS in chunks, so a
# single read cannot return the whole body and read-to-EOF is load-bearing.
# ---------------------------------------------------------------------------

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
    """Serve `payload` in 4 KiB chunks, then EOF — a single read cannot win."""

    async def handle(reader, writer):
        # A client that refuses an over-cap reply hangs up mid-send, so every
        # write here has to tolerate the peer going away — otherwise the
        # handler parks on drain() and Server.wait_closed() never returns.
        try:
            await reader.readline()
            for start in range(0, len(payload), 4096):
                writer.write(payload[start : start + 4096])
                await writer.drain()
                await asyncio.sleep(0)
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


async def test_both_local_status_readers_survive_a_real_outputd_payload(
    short_sock_path,
):
    payload = _outputd_status_payload()
    # The property that makes this test mean something: the body does not fit
    # the 8192-byte single read both consumers used before #2253. A full ring
    # of realistically-sized counters measures ~99.8 B an entry, so the array
    # alone is ~25.6 KB — three times that read.
    assert len(payload) > 8192, len(payload)
    assert len(payload) > 24_000, len(payload)

    server = await _serve_once(short_sock_path, payload)
    try:
        # Leg (a): the reader /state uses for outputd.
        state_view = await uds._local_status_json(short_sock_path, timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert state_view is not None, (
        "/state.outputd goes null on every chip-AEC box — and the documented "
        "jq .outputd.reference_outputs.chip_ref_writer diagnostics with it"
    )
    writer_view = state_view["reference_outputs"]["chip_ref_writer"]
    assert len(writer_view["recent_writes"]) == _RING_ENTRIES


async def test_the_grouping_supervisor_probe_survives_the_same_payload(
    short_sock_path, monkeypatch
):
    payload = _outputd_status_payload()
    assert len(payload) > 8192, len(payload)
    monkeypatch.setattr(
        grouping_supervisor, "OUTPUTD_CONTROL_SOCKET", short_sock_path
    )

    server = await _serve_once(short_sock_path, payload)
    try:
        supervisor = grouping_supervisor.GroupingSupervisor(probe_timeout_sec=5.0)
        starvation_view = await supervisor.outputd_status()
    finally:
        server.close()
        await server.wait_closed()

    assert starvation_view is not None, (
        "None means 'outputd unreachable', which this supervisor answers with "
        "a reconciler kick that restarts outputd — so a healthy bonded member "
        "would be restarted every rate-limit window, forever"
    )
    assert starvation_view["dac_content"]["serving_fifo"] is True


async def test_a_reply_past_the_ceiling_is_refused_rather_than_truncated(
    short_sock_path,
):
    # The cap is a safety bound on a hostile or wedged local daemon. Past it
    # the reader returns None: a truncated object is not a smaller answer, it
    # is a wrong one, and a caller that parsed a prefix would act on it.
    #
    # Asserted against `read_status_body` itself. Through `_local_status_json`
    # a returned PREFIX also comes out as None — `json.loads` refuses it — so
    # that surface cannot tell refusing from truncating, which is the whole
    # distinction here.
    payload = b'{"x":"' + b"y" * (uds.MAX_STATUS_BYTES + 1) + b'"}'
    server = await _serve_once(short_sock_path, payload)
    try:
        reader, writer = await asyncio.open_unix_connection(short_sock_path)
        try:
            writer.write(b"STATUS\n")
            await writer.drain()
            assert await uds.read_status_body(reader, timeout=5.0) is None
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        # And the caller built on it degrades the same way.
        assert await uds._local_status_json(short_sock_path, timeout=5.0) is None
    finally:
        server.close()
        await server.wait_closed()
