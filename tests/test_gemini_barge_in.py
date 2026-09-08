# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio

import pytest

from tests._gemini_fakes import Response as _Resp
from tests._gemini_fakes import ServerContent as _SC
from tests._live_turn_fake import drain_audio_chunks

try:
    from google.genai import types as genai_types

    from jasper.voice.gemini_session import (
        GeminiLiveConnection,
        GeminiLiveTurn,
    )

    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

pytestmark = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


def _turn(conn: "GeminiLiveConnection") -> "GeminiLiveTurn":
    return GeminiLiveTurn(
        conn, started_at=0.0, usage_baseline=conn._cumulative_usage,
    )


async def _interrupted(turn: "GeminiLiveTurn", *, timeout: float = 0.2) -> bool:
    """Probe the public seam only: True if ``wait_for_interrupt()``
    resolves within ``timeout`` (already armed — resolves near-instantly
    on an already-set event), False if it times out (not armed yet)."""
    try:
        await asyncio.wait_for(turn.wait_for_interrupt(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_build_config_enables_manual_interruption_and_native_transcripts():
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    config = conn._build_config()
    ric = config.realtime_input_config
    assert ric.automatic_activity_detection.disabled is True
    assert ric.activity_handling == genai_types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
    assert config.input_audio_transcription is not None
    assert config.output_audio_transcription is not None


# ---------------------------------------------------------------------------
# typed SDK fields (thinking_level / response_modalities /
# function_declarations) — the enum-member + model_validate rewrite must
# produce the identical wire payload as the raw string/dict forms it
# replaced.
# ---------------------------------------------------------------------------


def test_build_config_uses_typed_enums_and_validated_tool_declarations():
    """``_build_config`` passes ``ThinkingLevel``/``Modality`` enum members
    and ``FunctionDeclaration`` models (not raw strings/dicts). Pydantic
    coerces the old raw forms into the same models, so the assertions below
    prove the typed rewrite changes nothing on the wire."""
    from jasper.tools import ToolRegistry, tool

    @tool()
    def sample_tool() -> dict:
        """A sample tool for the structured config pin."""
        return {}

    registry = ToolRegistry()
    registry.register(sample_tool)
    decls = registry.function_declarations()

    conn = GeminiLiveConnection(api_key="fake", model="fake")
    conn._registry = registry
    cfg = conn._build_config()

    assert cfg.thinking_config.thinking_level == genai_types.ThinkingLevel.LOW
    assert cfg.response_modalities == [genai_types.Modality.AUDIO]
    assert cfg.tools[0].function_declarations[0].name == decls[0]["name"]

    # Wire-shape proof: the raw string/dict forms this replaced coerce,
    # via pydantic, into the identical serialized payload.
    legacy_thinking = genai_types.ThinkingConfig(thinking_level="low")
    assert (
        legacy_thinking.model_dump(exclude_none=True)
        == cfg.thinking_config.model_dump(exclude_none=True)
    )
    assert ["AUDIO"] == [m.value for m in cfg.response_modalities]
    legacy_tool = genai_types.Tool(function_declarations=decls)
    assert (
        legacy_tool.model_dump(exclude_none=True)
        == cfg.tools[0].model_dump(exclude_none=True)
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


async def test_local_interrupt_cancels_once_and_rejects_late_audio():
    class Session:
        def __init__(self):
            self.sent = []

        async def send_realtime_input(self, **kwargs):
            self.sent.append(kwargs)

        async def close(self):
            pass

    conn = GeminiLiveConnection(api_key="fake", model="fake")
    conn._session = session = Session()
    conn._connected_event.set()
    conn._active_turn = turn = _turn(conn)
    await turn.end_input()
    await turn._on_response(_Resp(data=b"before"))
    turn.request_local_interrupt()
    await turn.cancel_response("barge_in")
    await turn.cancel_response("again")
    assert sum("activity_start" in event for event in session.sent) == 1
    turn.clear_interrupted()
    await turn._on_response(_Resp(data=b"late"))
    assert turn.audio_chunks_pending() == 0
    await turn._on_response(_Resp(server_content=_SC(turn_complete=True)))
    assert await asyncio.wait_for(drain_audio_chunks(turn), 1) == []
    conn._resumption_handle = "old-context"
    await turn.release()
    assert conn._reconnect_event.is_set()
    await conn._teardown_session()
    assert conn._resumption_handle is None


async def test_server_interrupt_drops_queued_audio_and_does_not_complete():
    """``server_content.interrupted`` flushes queued pre-interrupt audio and
    arms the interrupt event, but does NOT mark the turn complete — there is
    no generation_complete, and ``interrupted`` alone must not look like
    "model done" to the watchdog. The trailing ``turn_complete`` is the sole
    end signal (Gemini goes interrupted -> turn_complete).

"""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = _turn(conn)
    conn._active_turn = turn

    # Audio queues up for playback ahead of the barge-in point.
    await turn._on_response(_Resp(data=b"pre-1"))
    await turn._on_response(_Resp(data=b"pre-2"))
    assert turn._audio_q.qsize() == 2

    # Server reports interruption (no turn_complete in this message).
    await turn._on_response(_Resp(server_content=_SC(interrupted=True)))
    assert await _interrupted(turn) is True
    # Queued pre-interrupt audio dropped so it is NOT played post-barge.
    assert turn._audio_q.empty()
    # NOT complete yet: no generation_complete, no turn_complete.
    assert turn.server_turn_complete() is False

    # The trailing turn_complete is what actually completes the turn.
    await turn._on_response(_Resp(server_content=_SC(turn_complete=True)))
    assert turn.server_turn_complete() is True


async def test_server_interrupt_keeps_the_end_of_audio_sentinel():
    """The interrupt drain shares ``drop_pending_audio``'s sentinel rule.

    A connection drop or a release can queue the terminal sentinel before
    the server's ``interrupted`` arrives. A drain that ate it would leave
    ``audio_out_chunks()`` awaiting a chunk that can never come, and the
    turn would never end."""
    conn = GeminiLiveConnection(api_key="fake", model="fake")
    turn = _turn(conn)
    conn._active_turn = turn

    await turn._on_response(_Resp(data=b"pre-1"))
    turn._on_connection_lost()
    assert turn._audio_q.qsize() == 2

    await turn._on_response(_Resp(server_content=_SC(interrupted=True)))

    assert await asyncio.wait_for(
        drain_audio_chunks(turn), timeout=1.0,
    ) == [], "pre-interrupt audio must be dropped, the sentinel kept"
