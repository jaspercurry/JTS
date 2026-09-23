# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared provider audio latency, queue limits and drain behaviour."""
import asyncio
import base64
import logging

import pytest

from jasper.tools import ToolRegistry
from jasper.voice import _base
from jasper.voice.session import AudioOutChunk
from tests._async_wait import wait_until
from tests._gemini_fakes import Response
from tests._live_turn_fake import drain_audio_chunks
from tests._log_events import event_fields, event_records
from tests._provider_fakes import provider as provider


@pytest.mark.parametrize("provider,end_input_sent", [
    ("openai", True), ("grok", True), ("gemini", True),
    ("gemini", False), ("openai_live", True), ("openai_live", False),
], indirect=["provider"])
async def test_first_chunk_event_reports_latency_since_end_input(provider, end_input_sent, caplog):
    caplog.set_level(logging.INFO)
    conn, _ = provider()
    await conn.start(ToolRegistry(), "system")
    try:
        turn = await conn.acquire_turn()
        if end_input_sent:
            await turn.end_input()
            if conn.PROVIDER_NAME in {"openai", "grok"}:
                await wait_until(lambda: turn._response_id is not None)
        await asyncio.sleep(0.01)
        pcm = b"\x00\x40" * 120
        if conn.PROVIDER_NAME == "gemini":
            conn._session.feed(Response(data=pcm))
        else:
            kind = "session.output_audio.delta" if conn.PROVIDER_NAME == "openai_live" else "response.output_audio.delta"
            event = {"type": kind, "delta": base64.b64encode(pcm).decode()}
            if conn.PROVIDER_NAME == "openai_live":
                conn._session.events.put_nowait(event)
            else:
                conn._session.feed(event)
        await wait_until(lambda: turn.chunks_received() >= 1)

        fields = event_fields(caplog, "turn.first_chunk")
        assert fields["provider"] == provider.args[0]
        assert int(fields["since_turn_start_ms"]) >= 10
        if end_input_sent:
            assert 0 <= int(fields["since_end_input_ms"]) <= int(fields["since_turn_start_ms"])
        else:
            assert "since_end_input_ms" not in fields
        await turn.release()
    finally:
        await conn.stop()


async def test_playout_queue_ceiling_drops_the_newest_chunk(provider, caplog, monkeypatch):
    monkeypatch.setattr(_base, "AUDIO_OUT_QUEUE_MAX_BYTES", 10)
    conn, _factory = provider()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        with caplog.at_level(logging.WARNING):
            for pcm in (b"12345", b"67890", b"X", b"YZ"):
                if conn.PROVIDER_NAME in {"openai", "grok"}:
                    await turn._on_audio_delta(base64.b64encode(pcm).decode())
                else:
                    turn._enqueue_audio(AudioOutChunk(pcm))

        assert turn.audio_chunks_pending() == 2
        assert turn.audio_dropped_bytes() == 3, (
            "both over-ceiling chunks must be counted, not just the first"
        )
        fields = event_fields(caplog, "turn.audio_overflow")
        assert fields["provider"] == provider.args[0]
        (record,) = event_records(caplog, "turn.audio_overflow")
        log_provider = "openai" if provider.args[0] == "grok" else provider.args[0]
        assert record.name == f"jasper.voice.{log_provider}_session"
        assert record.levelno == logging.WARNING
        assert int(fields["queued_bytes"]) == 10
        assert int(fields["dropped_bytes"]) == 1, (
            "only the FIRST drop of the turn logs; later drops just count"
        )

        turn._audio_q.put_nowait(None)
        played = await asyncio.wait_for(drain_audio_chunks(turn), timeout=1.0)
        assert [chunk.pcm for chunk in played] == [b"12345", b"67890"]
    finally:
        await conn.stop()


async def test_dropping_pending_audio_frees_the_ceiling(provider, monkeypatch):
    monkeypatch.setattr(_base, "AUDIO_OUT_QUEUE_MAX_BYTES", 10)
    conn, _factory = provider()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        turn._enqueue_audio(AudioOutChunk(b"0123456789"))
        turn._enqueue_audio(AudioOutChunk(b"X"))
        assert turn.audio_dropped_bytes() == 1

        turn.drop_pending_audio()
        turn._enqueue_audio(AudioOutChunk(b"after"))

        assert turn.audio_chunks_pending() == 1
        assert turn.audio_dropped_bytes() == 0
    finally:
        await conn.stop()


async def test_dropping_pending_audio_drains_behind_the_sentinel(provider):
    conn, _factory = provider()
    await conn.start(ToolRegistry(), "")
    try:
        turn = await conn.acquire_turn()
        turn._on_connection_lost()
        turn._enqueue_audio(AudioOutChunk(b"late"))
        assert turn.audio_chunks_pending() == 2

        assert turn.drop_pending_audio() == 1
        played = await asyncio.wait_for(drain_audio_chunks(turn), timeout=1.0)
        assert played == []
    finally:
        await conn.stop()
