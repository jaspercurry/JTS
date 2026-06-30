# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import jasper.research as research
from jasper.research import catalog as research_catalog
from jasper.research import ResearchRequest, ResearchResult
from jasper.research.catalog import TextProviderEntry, provider_by_id
from jasper.research.providers import openai_research


def test_active_research_provider_returns_none_without_key():
    assert research.active_research_provider({}) is None


def test_active_research_provider_resolves_openai_with_key_and_model_override():
    active = research.active_research_provider({
        "OPENAI_API_KEY": "sk-test",
        "JASPER_RESEARCH_OPENAI_MODEL": "custom-text-model",
    })

    assert active is not None
    assert active.provider_id == "openai"
    assert isinstance(active.client, openai_research.OpenAIResearchClient)
    assert active.client.api_key == "sk-test"
    assert active.client.model == "custom-text-model"


def test_active_research_provider_fault_isolates_bad_provider(monkeypatch):
    class BadProvider:
        def build_client(self, env):
            raise RuntimeError("boom")

    class GoodProvider:
        def build_client(self, env):
            return object()

    monkeypatch.setattr(
        research,
        "PROVIDERS",
        (
            TextProviderEntry(
                id="bad",
                label="Bad",
                key_env="BAD_KEY",
                model_env="BAD_MODEL",
                default_model="bad",
                provider=BadProvider(),
            ),
            TextProviderEntry(
                id="good",
                label="Good",
                key_env="GOOD_KEY",
                model_env="GOOD_MODEL",
                default_model="good",
                provider=GoodProvider(),
            ),
        ),
    )

    active = research.active_research_provider({"GOOD_KEY": "ok"})

    assert active is not None
    assert active.provider_id == "good"


def test_catalog_helpers():
    entry = provider_by_id("openai")

    assert entry is not None
    assert entry.model_env == "JASPER_RESEARCH_OPENAI_MODEL"
    assert entry.default_model == openai_research.DEFAULT_MODEL
    assert provider_by_id("missing") is None


def test_catalog_rejects_duplicate_provider_ids():
    entry = TextProviderEntry(
        id="dup",
        label="Duplicate",
        key_env="DUP_KEY",
        model_env="DUP_MODEL",
        default_model="dup-model",
        provider=openai_research.PROVIDER,
    )

    with pytest.raises(ValueError, match="duplicate research provider id: dup"):
        research_catalog._validate_providers((entry, entry))


@dataclass
class _FakeResponse:
    id: str
    status: str
    output_text: str = ""
    error: str | None = None
    usage: object | None = None


class _FakeResponses:
    def __init__(self) -> None:
        self.create_kwargs = {}
        self.retrieve_calls = 0

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        return _FakeResponse(id="resp_1", status="in_progress")

    async def retrieve(self, response_id):
        self.retrieve_calls += 1
        assert response_id == "resp_1"
        return _FakeResponse(
            id=response_id,
            status="completed",
            output_text="Here is the short answer.",
            usage={"input_tokens": 20, "output_tokens": 7},
        )


class _FakeOpenAI:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_openai_client_uses_background_responses_and_polling():
    fake = _FakeOpenAI()
    client = openai_research.OpenAIResearchClient(
        api_key="sk-test",
        model="gpt-test",
        client=fake,
        sleep=_no_sleep,
    )

    result = await client.complete(ResearchRequest(query="research induction ranges"))

    assert result == ResearchResult(
        text="Here is the short answer.",
        input_tokens=20,
        output_tokens=7,
        usage={
            "input_tokens": 20,
            "output_tokens": 7,
            "input_token_details": {"text_tokens": 20},
            "output_token_details": {"text_tokens": 7},
        },
    )
    assert fake.responses.create_kwargs == {
        "model": "gpt-test",
        "input": "research induction ranges",
        "instructions": research.RESEARCH_ANSWER_INSTRUCTIONS,
        "background": True,
    }
    assert fake.responses.retrieve_calls == 1


@pytest.mark.asyncio
async def test_openai_client_raises_research_error_on_terminal_failure():
    class FailedResponses:
        async def create(self, **_kwargs):
            return _FakeResponse(id="resp_1", status="failed", error="bad request")

    class FailedOpenAI:
        responses = FailedResponses()

    client = openai_research.OpenAIResearchClient(
        api_key="sk-test",
        client=FailedOpenAI(),
        sleep=_no_sleep,
    )

    with pytest.raises(research.ResearchError, match="bad request"):
        await client.complete(ResearchRequest(query="x"))


@pytest.mark.asyncio
async def test_openai_client_normalizes_provider_sdk_errors():
    class FakeOpenAIError(Exception):
        pass

    class ErrorResponses:
        async def create(self, **_kwargs):
            raise FakeOpenAIError("temporary outage")

    class ErrorOpenAI:
        responses = ErrorResponses()

    client = openai_research.OpenAIResearchClient(
        api_key="sk-test",
        client=ErrorOpenAI(),
        sleep=_no_sleep,
        provider_error_classes=(FakeOpenAIError,),
    )

    with pytest.raises(research.ResearchError, match="temporary outage"):
        await client.complete(ResearchRequest(query="x"))


class _ClosableOpenAI:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_openai_client_aclose_closes_underlying_client():
    # The SDK exposes async close(); aclose() must actually drain it so the
    # httpx pool/FDs don't leak across daemon restarts (cf. transit BusClient).
    fake = _ClosableOpenAI()
    client = openai_research.OpenAIResearchClient(api_key="sk-test", client=fake)
    await client.aclose()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_active_research_provider_aclose_forwards_to_client():
    # The registry wrapper duck-types aclose; the OpenAI client fulfills it now
    # (previously a no-op because the SDK has close(), not aclose()).
    fake = _ClosableOpenAI()
    client = openai_research.OpenAIResearchClient(api_key="sk-test", client=fake)
    active = research.ActiveResearchProvider(provider_id="openai", client=client)
    await active.aclose()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_openai_client_aclose_is_noop_when_client_never_built():
    # Lazy client never constructed -> nothing to close, must not raise.
    await openai_research.OpenAIResearchClient(api_key="sk-test").aclose()


def test_default_research_model_is_priced():
    # Load-bearing: the default research model MUST have a model_pricing.json
    # row, or research cost records $0 and the daily spend cap can't see it.
    from jasper.usage import pricing_for_model

    priced = pricing_for_model(openai_research.DEFAULT_MODEL)
    assert not priced.label.startswith("unpriced:"), openai_research.DEFAULT_MODEL
    assert priced.text_input_per_million_usd > 0
    assert priced.text_output_per_million_usd > 0


@pytest.mark.asyncio
async def test_complete_cancellation_best_effort_cancels_background_job():
    # The scheduler's runtime ceiling cancels complete(); the server-side
    # background job keeps billing, so we best-effort cancel it.
    cancelled: list[str] = []
    polling = asyncio.Event()

    class _SlowResponses:
        async def create(self, **_kwargs):
            class _R:
                id = "resp_abc"
                status = "in_progress"

            return _R()

        async def retrieve(self, _rid):
            polling.set()
            await asyncio.Event().wait()  # block until cancelled

        async def cancel(self, rid):
            cancelled.append(rid)

    class _SlowOpenAI:
        responses = _SlowResponses()

    client = openai_research.OpenAIResearchClient(
        api_key="sk-test", client=_SlowOpenAI(), sleep=_no_sleep,
    )
    task = asyncio.create_task(client.complete(ResearchRequest(query="x")))
    await asyncio.wait_for(polling.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The detached best-effort cancel is tracked in _cleanup_tasks; drain it.
    await asyncio.gather(*client._cleanup_tasks, return_exceptions=True)
    assert cancelled == ["resp_abc"]


@pytest.mark.asyncio
async def test_complete_cancellation_before_create_spawns_no_cancel():
    cancelled: list[str] = []

    class _BlockingResponses:
        async def create(self, **_kwargs):
            await asyncio.Event().wait()  # cancelled before we get an id

        async def cancel(self, rid):
            cancelled.append(rid)

    class _BlockingOpenAI:
        responses = _BlockingResponses()

    client = openai_research.OpenAIResearchClient(
        api_key="sk-test", client=_BlockingOpenAI(), sleep=_no_sleep,
    )
    task = asyncio.create_task(client.complete(ResearchRequest(query="x")))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*client._cleanup_tasks, return_exceptions=True)
    assert cancelled == []  # no response id yet -> nothing to cancel
