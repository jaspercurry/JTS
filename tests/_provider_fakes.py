# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""In-memory SDK transports shared by provider and base behaviour tests."""
from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

import pytest
from openai.types.live.client_event_param import ClientEventParam
from pydantic import TypeAdapter

from jasper.voice.grok_session import GrokRealtimeConnection
from jasper.voice.openai_session import OpenAIRealtimeConnection
from jasper.voice.openai_live_session import OpenAILiveConnection
from tests._async_wait import wait_until
from tests._gemini_fakes import ServerContent, Response as _Resp

try:
    from google.genai import types
    from jasper.voice.gemini_session import GeminiLiveConnection
except ImportError:
    types = None

CLIENT_EVENT = TypeAdapter(ClientEventParam)


class RealtimeSocket:

    def __init__(self) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.response_number = 0
        self.commit_number = 0
        self.response_id = "resp_1"
        self.item_id = "msg_1"

    async def send(self, event: dict) -> None:
        self.sent.append(event)
        if event["type"] == "session.update":
            self._inbox.put_nowait({"type": "session.updated", "session": event["session"]})

        elif event["type"] == "input_audio_buffer.commit":
            self.commit_number += 1
            self.feed({"type": "input_audio_buffer.committed", "item_id": f"user_{self.commit_number}"})
        elif event["type"] == "response.create":
            self.response_number += 1
            self.response_id = f"resp_{self.response_number}"
            self.item_id = f"msg_{self.response_number}"
            self.feed({"type": "response.created", "response": {"id": self.response_id}})
            self.feed({"type": "response.output_item.added", "item": {
                "id": self.item_id, "type": "message", "role": "assistant",
            }})

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if isinstance(item, _IterStop):
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def feed(self, event: dict) -> None:
        event = dict(event)
        etype = event["type"]
        if etype.startswith("response."):
            if etype in ("response.done", "response.created"):
                event["response"] = {"id": self.response_id, "status": "completed", **event["response"]}
            else:
                event.setdefault("response_id", self.response_id)
                event.setdefault("item_id", self.item_id)
                if etype == "response.output_item.added":
                    self.item_id = event["item"]["id"]
        elif etype.startswith("conversation.item.input_audio_transcription."):
            event.setdefault("item_id", "user_1")
        self._inbox.put_nowait(event)

    def feed_error(self, exc: BaseException) -> None:
        self._inbox.put_nowait(exc)

    def feed_iter_stop(self) -> None:
        self._inbox.put_nowait(_IterStop())


class _IterStop:
    pass


class RealtimeContext:
    def __init__(self, conn: RealtimeSocket) -> None:
        self._conn = conn

    async def __aenter__(self) -> RealtimeSocket:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class RealtimeConnect:

    def __init__(self) -> None:
        self.conns: list[RealtimeSocket] = []
        self.models: list[str] = []
        self.next_exceptions: list[BaseException] = []

    def __call__(self, *, model: str) -> RealtimeContext:
        if self.next_exceptions:
            exc = self.next_exceptions.pop(0)
            raise exc
        self.models.append(model)
        c = RealtimeSocket()
        self.conns.append(c)
        return RealtimeContext(c)

    @property
    def sessions(self):
        return self.conns


class GeminiSession:

    def __init__(self, fake: "GeminiConnect") -> None:
        self._fake = fake
        self._inbox: asyncio.Queue[_Resp | Exception] = asyncio.Queue()
        self.sent_realtime: list[dict] = []
        self.sent_client_content: list[dict] = []
        self.sent_tool_responses: list[Any] = []
        self.closed = False
        self.received = 0
        self.setup_complete = types.LiveServerSetupComplete()

    async def send_realtime_input(self, **kwargs) -> None:
        self.sent_realtime.append(kwargs)

    async def send_client_content(self, **kwargs) -> None:
        self.sent_client_content.append(kwargs)

    async def send_tool_response(self, function_responses=None) -> None:
        self.sent_tool_responses.append(function_responses)

    async def receive(self):
        while True:
            item = await self._inbox.get()
            if isinstance(item, Exception):
                raise item
            yield item

    async def _receive(self):
        item = await self._inbox.get()
        if isinstance(item, Exception):
            raise item
        self.received += 1
        return item

    async def close(self) -> None:
        self.closed = True


    def feed(self, resp: _Resp) -> None:
        self._inbox.put_nowait(resp)

    def feed_error(self, exc: Exception) -> None:
        self._inbox.put_nowait(exc)


class GeminiContext:

    def __init__(self, session: GeminiSession) -> None:
        self._session = session

    async def __aenter__(self) -> GeminiSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class GeminiConnect:

    def __init__(self) -> None:
        self.sessions: list[GeminiSession] = []
        self.configs: list[Any] = []
        self.next_exceptions: list[Exception] = []

    def __call__(self, *, model, config) -> GeminiContext:
        if self.next_exceptions:
            exc = self.next_exceptions.pop(0)
            raise exc
        self.configs.append(config)
        sess = GeminiSession(self)
        self.sessions.append(sess)
        return GeminiContext(sess)


class LiveSocket:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def send(self, event):
        CLIENT_EVENT.validate_python(event)
        if event["type"] == "session.start":
            tools = event["session"]["delegation"]["responses"]["tools"]
            assert tools.count({"type": "web_search"}) == 1
        self.sent.append(event)
        if event["type"] == "session.start":
            await self.events.put({"type": "session.started"})
        if event["type"] == "session.close":
            await self.events.put({"type": "session.closed", "usage": {"seconds": 12.5}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if isinstance(event, BaseException):
            raise event
        return event

    def feed_error(self, exc):
        self.events.put_nowait(exc)



def make_provider(provider, *, sleep=asyncio.sleep, watchdog_sec=None, **kwargs):
    if provider == "openai_live":
        return OpenAILiveConnection(api_key="test", connect=LiveSocket, **kwargs), None
    if watchdog_sec is not None:
        if provider == "gemini":
            kwargs["rotate_after_sec"] = watchdog_sec
        else:
            kwargs.update(session_max_sec=2 * watchdog_sec, proactive_buffer_sec=watchdog_sec)
    kwargs.setdefault("backoff_schedule", (0.0, 0.0))
    kwargs.setdefault("context_reset_sec", 9999.0)
    if provider == "gemini":
        factory = GeminiConnect()
        kwargs.setdefault("rotate_after_sec", 0.0)
        conn = GeminiLiveConnection(api_key="fake", model="fake-model", connect_factory=factory, **kwargs)
    else:
        factory = RealtimeConnect()
        cls = OpenAIRealtimeConnection if provider == "openai" else GrokRealtimeConnection
        conn = cls(api_key="fake", connect_factory=factory, **kwargs)
    conn._sleep = sleep
    return conn, factory


PERSISTENT_PROVIDERS = [
    "openai", "grok",
    pytest.param("gemini", marks=pytest.mark.skipif(types is None, reason="google-genai not installed")),
]


@pytest.fixture(params=PERSISTENT_PROVIDERS)
def persistent_provider(request):
    return partial(make_provider, request.param)


@pytest.fixture(params=[*PERSISTENT_PROVIDERS, "openai_live"])
def provider(request):
    return partial(make_provider, request.param)


async def complete_gemini_turn(turn, session):
    await turn.end_input()
    session.feed(_Resp(server_content=ServerContent(turn_complete=True)))
    await wait_until(turn.server_turn_complete)


async def release_turn(turn, session):
    if turn._conn.PROVIDER_NAME == "gemini":
        await complete_gemini_turn(turn, session)
    await turn.release()
