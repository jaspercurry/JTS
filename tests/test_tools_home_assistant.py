"""Tool-dispatch tests for jasper.tools.home_assistant.

Confirms:
  - make_home_assistant_tools(None) returns [] (model doesn't see it
    when HA is unconfigured)
  - make_home_assistant_tools(<HAClient>) returns one tool, schema
    matches the provider-agnostic registry contract
  - The tool dispatches to HAClient.process() and shapes the result
    correctly for the model
  - Schema is serializable for both Gemini and OpenAI providers
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from jasper.home_assistant import HAResponse, OUTCOME_NETWORK, OUTCOME_OK
from jasper.tools import ToolRegistry, build_tool
from jasper.tools.home_assistant import make_home_assistant_tools


# ---- Stub HAClient ----------------------------------------------------------

@dataclass
class _FakeHAClient:
    """Minimal duck-typed substitute for HAClient — only `process()` is
    called by the tool. We don't subclass HAClient to keep the stub
    cheap and to verify the tool only touches the documented surface."""
    response: HAResponse
    calls: list = None

    def __post_init__(self):
        if self.calls is None:
            self.calls = []

    async def process(self, query: str) -> HAResponse:
        self.calls.append(query)
        return self.response


def _ok_response(speech: str = "Done.") -> HAResponse:
    return HAResponse(
        speech=speech,
        success=True,
        response_type="action_done",
        error_code=None,
        outcome=OUTCOME_OK,
        conversation_id="conv-1",
        continue_conversation=False,
        latency_ms=42,
    )


def _network_error_response() -> HAResponse:
    return HAResponse(
        speech="I can't reach Home Assistant right now.",
        success=False,
        response_type="",
        error_code=None,
        outcome=OUTCOME_NETWORK,
        conversation_id=None,
        continue_conversation=False,
        latency_ms=12,
        error_detail="Connection refused",
    )


# ---- Gating: empty list when not configured --------------------------------

def test_returns_empty_when_ha_is_none():
    assert make_home_assistant_tools(None) == []


# ---- One tool registered when configured -----------------------------------

def test_returns_one_tool_when_configured():
    fake = _FakeHAClient(_ok_response())
    tools = make_home_assistant_tools(fake)
    assert len(tools) == 1


def test_tool_name_is_home_assistant():
    fake = _FakeHAClient(_ok_response())
    [fn] = make_home_assistant_tools(fake)
    # Name comes via the @tool() decorator + build_tool introspection.
    built = build_tool(fn)
    assert built.name == "home_assistant"


def test_tool_description_mentions_smart_home():
    """Sanity check that the docstring -> description is non-empty and
    covers the primary use case. This is what the model sees."""
    fake = _FakeHAClient(_ok_response())
    [fn] = make_home_assistant_tools(fake)
    built = build_tool(fn)
    desc = built.description.lower()
    assert "home assistant" in desc
    assert "smart-home" in desc or "smart home" in desc


def test_tool_schema_has_query_string_param():
    fake = _FakeHAClient(_ok_response())
    [fn] = make_home_assistant_tools(fake)
    built = build_tool(fn)
    assert built.parameters["type"] == "object"
    assert "query" in built.parameters["properties"]
    assert built.parameters["properties"]["query"]["type"] == "string"
    # query has no default in the signature → required
    assert built.parameters.get("required") == ["query"]


# ---- Dispatch: tool forwards to HAClient.process() -------------------------

@pytest.mark.asyncio
async def test_tool_dispatches_query_to_process_verbatim():
    fake = _FakeHAClient(_ok_response("Turned on the bedroom lights."))
    [fn] = make_home_assistant_tools(fake)

    result = await fn("turn on the bedroom lights")

    assert fake.calls == ["turn on the bedroom lights"]
    assert result["success"] is True
    assert result["spoken_response"] == "Turned on the bedroom lights."
    assert result["response_type"] == "action_done"
    assert result["error_code"] is None
    assert result["error_detail"] == ""


@pytest.mark.asyncio
async def test_tool_surfaces_error_detail_on_failure():
    fake = _FakeHAClient(_network_error_response())
    [fn] = make_home_assistant_tools(fake)

    result = await fn("turn on the lights")

    assert result["success"] is False
    assert result["spoken_response"] == "I can't reach Home Assistant right now."
    assert result["error_detail"] == "Connection refused"


@pytest.mark.asyncio
async def test_tool_passes_household_specific_phrases_unchanged():
    """The tool is a relay — household sentence-trigger phrases like
    'bedroom medium' must go through verbatim so HA's NLU can match them."""
    fake = _FakeHAClient(_ok_response("OK."))
    [fn] = make_home_assistant_tools(fake)

    await fn("bedroom medium")
    await fn("I'm leaving")
    await fn("kids are asleep")

    assert fake.calls == ["bedroom medium", "I'm leaving", "kids are asleep"]


# ---- Provider-agnostic schema serialization --------------------------------

def test_schema_serializes_for_gemini():
    fake = _FakeHAClient(_ok_response())
    registry = ToolRegistry()
    for fn in make_home_assistant_tools(fake):
        registry.register(fn)

    decls = registry.function_declarations(provider="gemini")
    assert len(decls) == 1
    assert decls[0]["name"] == "home_assistant"
    assert "description" in decls[0]
    assert decls[0]["parameters"]["properties"]["query"]["type"] == "string"


def test_schema_serializes_for_openai():
    fake = _FakeHAClient(_ok_response())
    registry = ToolRegistry()
    for fn in make_home_assistant_tools(fake):
        registry.register(fn)

    decls = registry.openai_tools(provider="openai")
    assert len(decls) == 1
    assert decls[0]["type"] == "function"
    assert decls[0]["name"] == "home_assistant"


def test_schema_serializes_for_grok():
    """Grok shares OpenAI's wire format per jasper/voice/grok_session.py;
    the registry.openai_tools() shape applies to both."""
    fake = _FakeHAClient(_ok_response())
    registry = ToolRegistry()
    for fn in make_home_assistant_tools(fake):
        registry.register(fn)

    decls = registry.openai_tools(provider="grok")
    assert len(decls) == 1
    assert decls[0]["name"] == "home_assistant"


def test_tool_is_visible_to_all_providers():
    """Confirm the tool has no provider restriction (no
    @tool(providers={...}) gate) — HA control works the same way
    regardless of which realtime backend the household runs."""
    fake = _FakeHAClient(_ok_response())
    [fn] = make_home_assistant_tools(fake)
    built = build_tool(fn)
    assert built.providers is None
