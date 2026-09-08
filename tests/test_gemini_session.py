# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for GeminiLiveConnection / GeminiLiveTurn.

These tests construct a real GeminiLiveConnection (no network calls — the
genai.Client constructor is local) and exercise the response-dispatch
pipeline with hand-built fake response objects matching the SDK's shape.
"""
from __future__ import annotations

import asyncio

import pytest

from jasper.voice._base import BaseLiveConnection
from tests._gemini_fakes import GoAway as _GoAway
from tests._gemini_fakes import Response as _GoAwayResp

try:
    from google.genai import types

    from jasper.voice.gemini_session import (
        GOAWAY_DEFER_MIN_TIME_LEFT_SEC,
        GeminiLiveConnection,
        GeminiLiveTurn,
    )
    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

pytestmark = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


@pytest.mark.parametrize("transcripts", [False, True])
async def test_sdk_combined_audio_transcripts_and_completion(transcripts):
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = GeminiLiveTurn(conn, started_at=0)
    conn._active_turn = turn
    audio = [b"\x01\x00", b"\x02\x00"]
    response = types.LiveServerMessage(server_content=types.LiveServerContent(
        model_turn=types.Content(role="model", parts=[
            types.Part(text="model text is not a native transcript"),
            *(types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))
              for pcm in audio),
        ]),
        output_transcription=types.Transcription(text="good day") if transcripts else None,
        turn_complete=True,
    ))
    await turn._on_response(response)
    chunks = await asyncio.wait_for(_collect_chunks(turn), 1)
    assert len(chunks) == 1
    assert chunks[0].pcm == b"".join(audio)
    assert chunks[0].provider_item_id is None
    assert turn.server_turn_complete()
    capture = turn.capture()
    assert capture.user_text is None
    assert capture.assistant_text == ("good day" if transcripts else None)
    assert capture.data["transcripts_available"] is transcripts


async def _collect_chunks(turn):
    return [chunk async for chunk in turn.audio_out_chunks()]


def test_secret_literals_reports_the_api_key():
    """A rejection body echoing the key in a shape `redact_secrets`'s
    prefix patterns don't know still redacts, because the connection
    hands its own key back as a literal (ADR-0243). The base class
    returns none — it holds no secret of its own."""
    conn = GeminiLiveConnection(api_key="plainvalue123", model="fake")
    assert conn._secret_literals() == ("plainvalue123",)
    assert BaseLiveConnection._secret_literals(conn) == ()


class _FakeReceiveSession:
    """Drives GeminiLiveConnection._receive_loop with a scripted sequence
    of responses, then raises CancelledError so the loop exits cleanly
    without going through any reconnect/clean-close branch."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def _receive(self):
        if self._responses:
            return self._responses.pop(0)
        raise asyncio.CancelledError


async def _run_receive_loop_with(conn, responses):
    """Bind `conn` to a scripted fake session and run one pass of the
    receive loop over the scripted responses."""
    conn._session = _FakeReceiveSession(responses)
    with pytest.raises(asyncio.CancelledError):
        await conn._receive_loop()


async def test_goaway_mid_turn_with_ample_time_defers_reconnect():
    """A GoAway arriving while a turn is in flight, with time_left
    comfortably above the deferral threshold, must NOT tear the session
    down: it sets the pending flag and leaves the reconnect event clear
    so the in-flight turn keeps running. The reconnect fires only once
    the turn is released."""
    import datetime
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = GeminiLiveTurn(conn, started_at=0.0)
    conn._active_turn = turn

    ample = datetime.timedelta(
        seconds=GOAWAY_DEFER_MIN_TIME_LEFT_SEC + 60.0
    )
    await _run_receive_loop_with(conn, [_GoAwayResp(go_away=_GoAway(ample))])

    # Deferred: pending flag set, reconnect NOT triggered, turn intact.
    assert conn._deferred_reconnect.pending is True
    assert not conn._reconnect_event.is_set()
    assert conn._active_turn is turn

    # Releasing the turn fires the deferred reconnect.
    await conn._on_turn_released(turn)
    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


async def test_goaway_with_no_active_turn_reconnects_immediately():
    """No turn in flight → reconnect promptly as before, regardless of
    time_left."""
    import datetime
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    assert conn._active_turn is None

    ample = datetime.timedelta(
        seconds=GOAWAY_DEFER_MIN_TIME_LEFT_SEC + 60.0
    )
    await _run_receive_loop_with(conn, [_GoAwayResp(go_away=_GoAway(ample))])

    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


async def test_goaway_mid_turn_with_little_time_reconnects_immediately():
    """A GoAway mid-turn but with time_left below the threshold can't
    safely defer (the server is about to drop us) — reconnect promptly,
    do not set the pending flag."""
    import datetime
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = GeminiLiveTurn(conn, started_at=0.0)
    conn._active_turn = turn

    little = datetime.timedelta(
        seconds=GOAWAY_DEFER_MIN_TIME_LEFT_SEC - 5.0
    )
    await _run_receive_loop_with(conn, [_GoAwayResp(go_away=_GoAway(little))])

    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


async def test_goaway_mid_turn_with_unparseable_time_reconnects_immediately():
    """If time_left can't be interpreted, fail safe to the existing
    reconnect-immediately behaviour rather than deferring on a value we
    can't reason about."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = GeminiLiveTurn(conn, started_at=0.0)
    conn._active_turn = turn

    await _run_receive_loop_with(
        conn, [_GoAwayResp(go_away=_GoAway(object()))]
    )

    assert conn._deferred_reconnect.pending is False
    assert conn._reconnect_event.is_set()


def test_goaway_defer_threshold_covers_hard_recording_cap():
    """Drift guard for GOAWAY_DEFER_MIN_TIME_LEFT_SEC.

    Deferring a mid-turn GoAway is only safe if the deferred window can
    actually contain a full turn — i.e. the threshold must be >= the
    longest a turn can run, which is the daemon's hard recording cap.
    If a future change raises voice_daemon.HARD_RECORDING_CAP_SEC above
    the threshold, deferral would routinely overrun `time_left`; catch
    that here rather than discovering it on a live 15-min session. (The
    overrun is itself fail-safe — the WS drops and we reconnect — but the
    threshold should still reflect the real bound it claims to cover.)"""
    from jasper.voice_daemon import HARD_RECORDING_CAP_SEC

    assert GOAWAY_DEFER_MIN_TIME_LEFT_SEC >= HARD_RECORDING_CAP_SEC


async def test_acquire_turn_rolls_back_active_turn_when_activity_start_fails():
    """A failed activity_start must not leave the turn slot occupied."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    conn._connected_event.set()

    async def _raise(*args, **kwargs):
        raise RuntimeError("ws closed mid-send")

    conn._send_realtime_input = _raise

    with pytest.raises(RuntimeError, match="ws closed mid-send"):
        await conn.acquire_turn()
    assert conn._active_turn is None

    # The next acquire surfaces the real failure again — not the
    # "a turn is already active" wedge.
    with pytest.raises(RuntimeError, match="ws closed mid-send"):
        await conn.acquire_turn()
