# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Composition replay: score tapes prove transitions, not speech recognition."""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock

import numpy as np
import pytest
from google.genai import types
from openai.types.realtime import (
    ResponseAudioDeltaEvent,
    ResponseAudioTranscriptDoneEvent,
    ResponseDoneEvent,
)

from jasper.mic_capture import MicCapture
from jasper.tts_playout import confirmed_tts_flush
from jasper.openwakeword_guard import ensure_openwakeword_import_safe
from jasper.tools import ToolRegistry
from jasper.vad import SpeechVAD
from jasper.voice.gemini_session import GeminiLiveConnection
from jasper.voice.grok_session import GrokRealtimeConnection
from jasper.voice.openai_session import OpenAIRealtimeConnection
from jasper.voice.turn_playback import play_responses
from tests._async_wait import wait_signalled, wait_until
from tests._wake_loop import wake_loop_for_tests
from tests.test_gemini_connection import _FakeConnect
from tests.test_openai_session import _FakeConnectFactory
from tests.voice_replay import RecordingPlayout


@pytest.fixture(params=["gemini", "openai", "grok"])
async def provider(request, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = request.param
    factory = _FakeConnect() if name == "gemini" else _FakeConnectFactory()
    cls = {"gemini": GeminiLiveConnection, "openai": OpenAIRealtimeConnection,
           "grok": GrokRealtimeConnection}[name]
    connection = cls(api_key="offline-replay", model="offline-model", connect_factory=factory)
    await connection.start(ToolRegistry(), "")
    try:
        yield name, connection, factory
    finally:
        await connection.stop()


def _wire(provider):
    name, _, factory = provider
    return factory.sessions[-1] if name == "gemini" else factory.conns[-1]


def _input_events(provider):
    wire = _wire(provider)
    if provider[0] == "gemini":
        return [e["audio"].data for e in wire.sent_realtime if "audio" in e], sum(
            "activity_end" in e for e in wire.sent_realtime
        )
    return [base64.b64decode(e["audio"]) for e in wire.sent if e["type"] == "input_audio_buffer.append"], sum(
        e["type"] == "input_audio_buffer.commit" for e in wire.sent
    )


def _reply(provider, pcm, text, *, complete=True):
    wire = _wire(provider)
    if provider[0] == "gemini":
        wire.feed(types.LiveServerMessage(server_content=types.LiveServerContent(
            model_turn=types.Content(role="model", parts=[types.Part(
                inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"),
            )]),
            output_transcription=types.Transcription(text=text), turn_complete=complete,
        )))
    else:
        identity = {"response_id": wire.response_id, "item_id": wire.item_id,
                    "output_index": 0, "content_index": 0}
        for event in (
            ResponseAudioDeltaEvent.model_validate({
                **identity, "event_id": "audio", "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode(),
            }),
            ResponseAudioTranscriptDoneEvent.model_validate({
                **identity, "event_id": "text", "type": "response.output_audio_transcript.done",
                "transcript": text,
            }),
        ):
            wire._inbox.put_nowait(event)
        if complete:
            wire._inbox.put_nowait(ResponseDoneEvent.model_validate({
                "event_id": "done", "type": "response.done", "response": {
                    "id": wire.response_id, "status": "completed", "output": [],
                },
            }))


@pytest.mark.parametrize("scenario", ["quiet", "pause", "manual", "no_speech"])
async def test_input_endpoint_adapter_and_output_replay(provider, scenario):
    ensure_openwakeword_import_safe()
    vad_module = pytest.importorskip("openwakeword.vad")
    scores = [0.65, 0.2, 0.2, 0.2]
    if scenario == "pause":
        scores += [0.0] * 5 + [0.2] * 4
    scores += [0.0] * (16 if scenario == "manual" else 12)
    if scenario == "no_speech":
        scores = [0.5] * 4 + [0.0] * 12
    frame_sec = MicCapture.OUTPUT_FRAME_SAMPLES / 16000
    repeats = round(0.08 / frame_sec)
    scores = [score for score in scores for _ in range(repeats)]
    frames = [np.full(MicCapture.OUTPUT_FRAME_SAMPLES, 201 + i, dtype=np.int16)
              for i in range(len(scores))]
    uploads = []
    for split in (0, 8 * repeats):
        sink = RecordingPlayout()
        class ScoreModel:
            tag = None
            chunk = 0

            def run(self, _outputs, inputs):
                tag = round(float(inputs["input"][0, 0]) * 32767)
                if tag != self.tag:
                    self.tag, self.chunk = tag, 0
                score = scores[tag - 201]
                # At 80 ms the three subchunks differ; using their maximum would
                # falsely arm the no-speech tape whose aggregate stays at 0.5.
                offset = (0.1, 0.0, -0.1)[self.chunk % 3] if score else 0.0
                self.chunk += 1
                return np.array([[score + offset]]), inputs["h"], inputs["c"]

        # Keep the real openWakeWord chunk mean and SpeechVAD conversion. Only
        # ONNX inference is replaced; no model assets or recognition are claimed.
        model = vad_module.VAD.__new__(vad_module.VAD)
        model.model = ScoreModel()
        model.sample_rate = np.array(16000, dtype=np.int64)
        model.reset_states()
        vad = SpeechVAD.__new__(SpeechVAD)
        vad._vad = model
        wl = wake_loop_for_tests(tts=sink, vad=vad)
        wl._connection = provider[1]
        wl._begin_turn_output_episode = AsyncMock()
        wl._prepare_assistant_loudness_context = AsyncMock()
        wl._content_activity.refresh_now = AsyncMock()
        wl._arm_turn_background_end = lambda: None
        if scenario == "manual":
            wl._push_to_talk.active_source = "replay-button"
        prefix = [np.full(MicCapture.OUTPUT_FRAME_SAMPLES, tag, dtype=np.int16)
                  for tag in (101, 102)]
        wl._pre_roll.extend(prefix)
        prior_wire = _wire(provider)
        before_audio, before_closes = _input_events(provider)
        entered, proceed = asyncio.Event(), asyncio.Event()
        acquire = provider[1].acquire_turn

        async def delayed_acquire():
            entered.set()
            await proceed.wait()
            return await acquire()

        provider[1].acquire_turn = delayed_acquire
        begin = asyncio.create_task(wl._begin_turn_inner())
        await wait_signalled(entered, "replay phase")
        # Mutating the rolling buffer cannot change the prefix frozen before acquisition.
        wl._pre_roll.clear()
        anchor = wl._turn_started_at_loop
        for i in range(split):
            wl._acquire_buffer.append(frames[i], anchor + (i + 1) * (frame_sec + 1e-6))
        proceed.set()
        await begin
        provider[1].acquire_turn = acquire
        turn = wl._turn
        if _wire(provider) is not prior_wire:
            before_audio, before_closes = [], 0
        try:
            await wl._drain_acquire_audio()
            for i in range(split, len(frames)):
                await wl._handle_session_frame(frames[i], captured_at=anchor + (i + 1) * (frame_sec + 1e-6))
                if scenario == "pause" and i == 9 * repeats - 1:
                    assert not wl._input_ended
            if scenario == "manual":
                assert not wl._input_ended
                assert await wl.manual_session_end() == "OK"
                assert await wl.manual_session_end() == "OK"
            assert wl._user_speech_seen is (scenario in {"quiet", "pause"})
            audio, closes = _input_events(provider)
            assert closes - before_closes == int(scenario != "no_speech")
            uploaded = audio[len(before_audio):]
            admitted = {"quiet": 14, "pause": 23, "manual": 20, "no_speech": 16}[scenario] * repeats
            assert [int(np.median(np.frombuffer(pcm, dtype=np.int16))) for pcm in uploaded] == (
                [101, 102] + list(range(201, 201 + admitted))
            )
            uploads.append(uploaded)
            if scenario == "no_speech":
                await wl._handle_session_frame(frames[-1], captured_at=anchor + 5.1)
                assert wl._turn is None
                assert not sink.audio
                assert _input_events(provider)[1] == closes
            else:
                _reply(provider, b"\x07\x00" * 2400, "Captured reply")
                await wait_until(lambda: sink.drained)
                assert sink.audio == b"\x07\x00" * 2400
                assert turn.capture().assistant_text == "Captured reply"
        finally:
            for task in wl._bg_tasks:
                task.cancel()
            await asyncio.gather(*wl._bg_tasks, return_exceptions=True)
            await turn.release()
            await wl._cancel_fire_and_forget_tasks()
    assert uploads[0] == uploads[1]


@pytest.mark.parametrize("phase", ["packet", "write", "drain"])
async def test_interrupt_then_fresh_turn_replay(provider, phase):
    entered = asyncio.Event()

    class HeldOutput(RecordingPlayout):
        async def write_segment(self, *args, **kwargs):
            if phase == "write":
                entered.set()
                await asyncio.Event().wait()
            return await super().write_segment(*args, **kwargs)

        async def wait_drained(self):
            if phase == "drain":
                entered.set()
                await asyncio.Event().wait()
            await super().wait_drained()

    connection = provider[1]
    old = await connection.acquire_turn()
    await old.send_audio(b"\0\0" * 1600)
    await old.end_input()
    sink = HeldOutput()
    playback = asyncio.create_task(play_responses(old, sink, barge_in_enabled=True))
    try:
        _reply(provider, b"\x01\x00" * 2400, "Old answer", complete=phase == "drain")
        if phase == "packet":
            await wait_until(lambda: bool(sink.audio))
        else:
            await wait_signalled(entered, "replay phase")
        old.request_local_interrupt()
        await playback
        assert confirmed_tts_flush(sink.flush_ack)
        old_audio = sink.audio
        await old.release()
        fresh = await connection.acquire_turn()
        await fresh.send_audio(b"\0\0" * 1600)
        await fresh.end_input()
        fresh_sink = RecordingPlayout()
        _reply(provider, b"\x02\x00" * 2400, "Fresh answer")
        await play_responses(fresh, fresh_sink, barge_in_enabled=True)
        assert fresh_sink.audio == b"\x02\x00" * 2400
        assert fresh.capture().assistant_text == "Fresh answer"
        assert sink.audio == old_audio
        await fresh.release()
    finally:
        playback.cancel()
        await asyncio.gather(playback, return_exceptions=True)
        await old.release()
