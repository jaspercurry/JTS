from __future__ import annotations

from dataclasses import dataclass

import pytest

import jasper.research as research
from jasper.research import catalog as research_catalog
from jasper.research import ResearchRequest, ResearchResult
from jasper.research.catalog import TextProviderEntry, default_model, provider_by_id
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
    assert default_model("openai") == openai_research.DEFAULT_MODEL
    assert provider_by_id("missing") is None
    assert default_model("missing") == ""


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
