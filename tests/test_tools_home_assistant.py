# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tool-dispatch tests for jasper.tools.home_assistant.

Confirms:
  - make_home_assistant_tools(None) returns [] (model doesn't see the
    tools when HA is unconfigured)
  - make_home_assistant_tools(<HAClient>) returns the home_assistant +
    home_assistant_confirm pair, schemas match the provider-agnostic
    registry contract
  - home_assistant dispatches to HAClient.process() and shapes the
    result for the model (unfenced — HA replies are not fenced; the
    confused-deputy risk is handled by the consequential-action gate,
    not by fencing HA's own text)
  - Consequential actions (unlock / disarm / open garage) are NOT
    executed on request — they stash + ask, and only
    home_assistant_confirm runs them after the user says yes
  - Schemas serialize for Gemini and OpenAI/Grok
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from jasper.home_assistant import (
    DEFAULT_READ_TIMEOUT_SEC,
    HAResponse,
    OUTCOME_NETWORK,
    OUTCOME_OK,
)
from jasper.tools import (
    DEFAULT_TOOL_TIMEOUT_SEC,
    ToolRegistry,
    UntrustedContentMonitor,
    build_tool,
    dispatch_tool,
)
from jasper.tools.home_assistant import classify_consequential, make_home_assistant_tools


# ---- Stub HAClient ----------------------------------------------------------

@dataclass
class _FakeHAClient:
    """Minimal duck-typed substitute for HAClient — only `process()` is
    called by the tools. We don't subclass HAClient to keep the stub
    cheap and to verify the tools only touch the documented surface."""
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


def _ha_tool(fake, **kw):
    """The home_assistant tool (the relay + gate) — index 0."""
    return make_home_assistant_tools(fake, **kw)[0]


# ---- Gating: empty list when not configured --------------------------------

def test_returns_empty_when_ha_is_none():
    assert make_home_assistant_tools(None) == []


# ---- Two tools registered when configured ----------------------------------

def test_returns_home_assistant_and_confirm_tools():
    fake = _FakeHAClient(_ok_response())
    tools = make_home_assistant_tools(fake)
    assert len(tools) == 2
    names = {build_tool(fn).name for fn in tools}
    assert names == {"home_assistant", "home_assistant_confirm"}


def test_tool_description_mentions_smart_home():
    """Sanity check that the docstring -> description is non-empty and
    covers the primary use case. This is what the model sees."""
    built = build_tool(_ha_tool(_FakeHAClient(_ok_response())))
    desc = built.description.lower()
    assert "home assistant" in desc
    assert "smart-home" in desc or "smart home" in desc


def test_tool_schema_has_query_string_param():
    built = build_tool(_ha_tool(_FakeHAClient(_ok_response())))
    assert built.parameters["type"] == "object"
    assert "query" in built.parameters["properties"]
    assert built.parameters["properties"]["query"]["type"] == "string"
    # query has no default in the signature → required
    assert built.parameters.get("required") == ["query"]


def test_both_tools_redact_argument_and_payload_previews():
    fake = _FakeHAClient(_ok_response())
    for fn in make_home_assistant_tools(fake):
        built = build_tool(fn)
        assert built.log_args is False
        assert built.log_payload is False


# ---- Dispatch: tool forwards to HAClient.process() (unfenced) ---------------

@pytest.mark.asyncio
async def test_tool_dispatches_query_to_process_verbatim():
    fake = _FakeHAClient(_ok_response("Turned on the bedroom lights."))
    fn = _ha_tool(fake)

    result = await fn("turn on the bedroom lights")

    assert fake.calls == ["turn on the bedroom lights"]
    assert result["success"] is True
    # HA replies are NOT fenced — the confused-deputy risk is handled by
    # the consequential-action gate (below), so HA's own text passes
    # through verbatim for a clean spoken answer.
    assert result["spoken_response"] == "Turned on the bedroom lights."
    assert result["response_type"] == "action_done"
    assert result["error_code"] is None
    assert result["error_detail"] == ""


@pytest.mark.asyncio
async def test_dispatch_logs_redact_household_phrase(caplog):
    fake = _FakeHAClient(_ok_response("Turned on the bedroom lights."))
    registry = ToolRegistry()
    for fn in make_home_assistant_tools(fake):
        registry.register(fn)

    with caplog.at_level(logging.INFO, logger="jasper.tools"):
        result = await dispatch_tool(
            registry,
            "home_assistant",
            {"query": "turn on the bedroom lights"},
        )

    assert result["success"] is True
    assert fake.calls == ["turn on the bedroom lights"]
    assert "args=<redacted keys=query len=" in caplog.text
    assert "payload=<redacted len=" in caplog.text
    assert "turn on the bedroom lights" not in caplog.text
    assert "Turned on the bedroom lights" not in caplog.text


@pytest.mark.asyncio
async def test_tool_surfaces_error_detail_on_failure():
    fake = _FakeHAClient(_network_error_response())
    fn = _ha_tool(fake)

    result = await fn("turn on the lights")

    assert result["success"] is False
    assert result["spoken_response"] == "I can't reach Home Assistant right now."
    assert result["error_detail"] == "Connection refused"


@pytest.mark.asyncio
async def test_tool_passes_household_specific_phrases_unchanged():
    """The tool is a relay — household sentence-trigger phrases like
    'bedroom medium' must go through verbatim so HA's NLU can match them.
    (None of these match the consequential classifier, so they relay
    straight through without a confirmation gate.)"""
    fake = _FakeHAClient(_ok_response("OK."))
    fn = _ha_tool(fake)

    await fn("bedroom medium")
    await fn("I'm leaving")
    await fn("kids are asleep")

    assert fake.calls == ["bedroom medium", "I'm leaving", "kids are asleep"]


# ---- Consequential-action classifier (pure) --------------------------------

@pytest.mark.parametrize("query", [
    "unlock the front door",
    "unlock the back door please",
    "can you unlock the door",
    "disarm the alarm",
    "turn off the alarm",
    "disable the security system",
    "deactivate the burglar alarm",
    "open the garage",
    "open the garage door",
    "open the gate",
    "open the front door",
])
def test_classify_consequential_flags_high_impact(query):
    assert classify_consequential(query) is not None


@pytest.mark.parametrize("query", [
    "turn on the bedroom lights",
    "turn off the kitchen lights",
    "lock the front door",
    "set the thermostat to 70",
    "good night",
    "movie time",
    "is the front door locked?",
    "is the door unlocked?",       # state query, base-form classifier won't fire
    "open the blinds",
    "open the curtains",
    "close the garage",            # closing grants no access
    "what's the bedroom temperature?",
])
def test_classify_consequential_ignores_safe_actions(query):
    assert classify_consequential(query) is None


# ---- Consequential gate: stash + confirm, never execute on request ---------

@pytest.mark.asyncio
async def test_consequential_request_gates_without_calling_ha():
    fake = _FakeHAClient(_ok_response("Unlocked the front door."))
    ha_tool, _confirm = make_home_assistant_tools(fake)

    out = await ha_tool("unlock the front door")

    assert out["needs_confirmation"] is True
    assert out["action"]                       # a non-empty label
    assert "?" in out["spoken_response"]        # it's a yes/no question
    # Structural gate: HA was NOT called on the request.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_confirm_executes_pending_and_relays_original_query():
    fake = _FakeHAClient(_ok_response("Unlocked the front door."))
    ha_tool, confirm = make_home_assistant_tools(fake)

    await ha_tool("unlock the front door")
    out = await confirm()

    # The ORIGINAL query is what reaches HA (not re-derived from a label).
    assert fake.calls == ["unlock the front door"]
    assert out["success"] is True
    assert out["spoken_response"] == "Unlocked the front door."   # unfenced


@pytest.mark.asyncio
async def test_confirm_with_nothing_pending_does_not_call_ha():
    fake = _FakeHAClient(_ok_response())
    _ha_tool_, confirm = make_home_assistant_tools(fake)

    out = await confirm()

    assert fake.calls == []
    assert out["success"] is False
    assert "nothing" in out["spoken_response"].lower()


@pytest.mark.asyncio
async def test_confirm_is_single_use():
    fake = _FakeHAClient(_ok_response("Unlocked."))
    ha_tool, confirm = make_home_assistant_tools(fake)

    await ha_tool("unlock the door")
    first = await confirm()       # runs it
    second = await confirm()      # nothing left to run

    assert fake.calls == ["unlock the door"]      # executed exactly once
    assert first["success"] is True
    assert second["success"] is False


@pytest.mark.asyncio
async def test_pending_expires_after_ttl():
    fake = _FakeHAClient(_ok_response("Unlocked."))
    now = {"t": 1000.0}
    ha_tool, confirm = make_home_assistant_tools(fake, clock=lambda: now["t"])

    await ha_tool("unlock the door")
    now["t"] += 1000.0            # well past the 90s TTL
    out = await confirm()

    assert fake.calls == []        # expired → not executed
    assert out["success"] is False


@pytest.mark.asyncio
async def test_latest_consequential_request_replaces_pending():
    fake = _FakeHAClient(_ok_response("Done."))
    ha_tool, confirm = make_home_assistant_tools(fake)

    await ha_tool("unlock the front door")
    await ha_tool("disarm the alarm")     # replaces the unconfirmed unlock
    await confirm()

    assert fake.calls == ["disarm the alarm"]


@pytest.mark.asyncio
async def test_intervening_command_clears_stale_pending():
    """S3: a consequential request is gated, then the user issues a DIFFERENT
    (non-confirming) command — that supersedes the pending, so a later 'yes'
    finds nothing and the abandoned action can't fire out of context. (The
    tool has no turn context in production, so this + the TTL bound staleness.)"""
    fake = _FakeHAClient(_ok_response("OK."))
    monitor = UntrustedContentMonitor()
    monitor.mark()
    ha_tool, confirm = make_home_assistant_tools(fake, monitor=monitor)

    await ha_tool("unlock the front door")       # tainted + consequential → gated
    await ha_tool("turn on the kitchen lights")  # different command → clears pending
    out = await confirm()                        # nothing left to confirm

    assert out["success"] is False
    assert "nothing" in out["spoken_response"].lower()
    assert fake.calls == ["turn on the kitchen lights"]   # the unlock never ran


@pytest.mark.asyncio
async def test_non_consequential_uses_fast_path():
    fake = _FakeHAClient(_ok_response("Turned on the lights."))
    ha_tool, _confirm = make_home_assistant_tools(fake)

    out = await ha_tool("turn on the living room lights")

    assert "needs_confirmation" not in out
    assert fake.calls == ["turn on the living room lights"]
    assert out["spoken_response"] == "Turned on the lights."


# ---- Gate is conditional on recent untrusted content (taint window) --------

@pytest.mark.asyncio
async def test_clean_session_runs_consequential_without_confirmation():
    """No untrusted content read → not tainted → a consequential voice
    command runs directly, no nag. This is the point of the taint window:
    the confirmation cost lands only in the post-email risk window, not on
    every 'unlock the door'."""
    fake = _FakeHAClient(_ok_response("Unlocked the front door."))
    monitor = UntrustedContentMonitor()                      # never marked → clean
    ha_tool, _confirm = make_home_assistant_tools(fake, monitor=monitor)

    out = await ha_tool("unlock the front door")

    assert "needs_confirmation" not in out
    assert fake.calls == ["unlock the front door"]           # executed directly
    assert out["success"] is True


@pytest.mark.asyncio
async def test_tainted_session_confirms_consequential():
    """After untrusted content was read (monitor marked), a consequential
    action gates with needs_confirmation and does NOT run."""
    fake = _FakeHAClient(_ok_response("Unlocked."))
    monitor = UntrustedContentMonitor()
    monitor.mark()                                           # "just read an email"
    ha_tool, _confirm = make_home_assistant_tools(fake, monitor=monitor)

    out = await ha_tool("unlock the front door")

    assert out["needs_confirmation"] is True
    assert fake.calls == []


@pytest.mark.asyncio
async def test_taint_expires_then_runs_directly():
    """Past the window the session is clean again → consequential runs
    directly (no lingering nag)."""
    fake = _FakeHAClient(_ok_response("Unlocked."))
    now = {"t": 1000.0}
    monitor = UntrustedContentMonitor(window_sec=600.0, clock=lambda: now["t"])
    monitor.mark()
    now["t"] += 601.0                                        # just past 10 min
    ha_tool, _confirm = make_home_assistant_tools(
        fake, monitor=monitor, clock=lambda: now["t"],
    )

    out = await ha_tool("unlock the front door")

    assert "needs_confirmation" not in out
    assert fake.calls == ["unlock the front door"]


@pytest.mark.asyncio
async def test_no_monitor_is_failsafe_always_confirms():
    """A wiring miss (monitor=None) errs toward caution: consequential
    actions always confirm. Pins the fail-safe direction."""
    fake = _FakeHAClient(_ok_response("Unlocked."))
    ha_tool, _confirm = make_home_assistant_tools(fake)      # no monitor

    out = await ha_tool("unlock the front door")

    assert out["needs_confirmation"] is True
    assert fake.calls == []


@pytest.mark.asyncio
async def test_taint_only_gates_consequential_not_normal_commands():
    """Even in a tainted session, a non-consequential command runs straight
    through — the window guards unlock/disarm, not 'turn on the lights'."""
    fake = _FakeHAClient(_ok_response("Turned on the lights."))
    monitor = UntrustedContentMonitor()
    monitor.mark()
    ha_tool, _confirm = make_home_assistant_tools(fake, monitor=monitor)

    out = await ha_tool("turn on the living room lights")

    assert "needs_confirmation" not in out
    assert fake.calls == ["turn on the living room lights"]


def test_system_instruction_teaches_needs_confirmation_flow():
    """Pin the cross-tool rule that makes the gate work end-to-end: the
    model must speak the question, wait, and only confirm on a yes. A
    careless prompt trim can't silently drop it (AGENTS.md
    'pin promises with tests')."""
    from jasper.voice.prompt import SYSTEM_INSTRUCTION as S

    assert "needs_confirmation" in S
    low = S.lower()
    assert "not acted yet" in low
    assert "confirmation tool" in low
    assert "same turn" in low


@pytest.mark.asyncio
async def test_gate_and_execute_emit_structured_logs_without_utterance(caplog):
    fake = _FakeHAClient(_ok_response("Done."))
    ha_tool, confirm = make_home_assistant_tools(fake)

    with caplog.at_level(logging.INFO, logger="jasper.tools.home_assistant"):
        await ha_tool("open the garage door for my buddy Reginald")
        await confirm()

    assert "event=ha.confirm_gate" in caplog.text
    assert "event=ha.confirm_execute" in caplog.text
    # Structured logs carry the safe category label ("open the garage"),
    # never the raw utterance — a distinctive word from the spoken request
    # is absent.
    assert "open the garage" in caplog.text
    assert "Reginald" not in caplog.text


# ---- Provider-agnostic schema serialization --------------------------------

def _names(decls):
    return {d["name"] for d in decls}


def test_schema_serializes_for_gemini():
    fake = _FakeHAClient(_ok_response())
    registry = ToolRegistry()
    for fn in make_home_assistant_tools(fake):
        registry.register(fn)

    decls = registry.function_declarations(provider="gemini")
    assert _names(decls) == {"home_assistant", "home_assistant_confirm"}
    ha = next(d for d in decls if d["name"] == "home_assistant")
    assert ha["parameters"]["properties"]["query"]["type"] == "string"


def test_schema_serializes_for_openai():
    fake = _FakeHAClient(_ok_response())
    registry = ToolRegistry()
    for fn in make_home_assistant_tools(fake):
        registry.register(fn)

    decls = registry.openai_tools(provider="openai")
    assert all(d["type"] == "function" for d in decls)
    assert _names(decls) == {"home_assistant", "home_assistant_confirm"}


def test_schema_serializes_for_grok():
    """Grok shares OpenAI's wire format per jasper/voice/grok_session.py;
    the registry.openai_tools() shape applies to both."""
    fake = _FakeHAClient(_ok_response())
    registry = ToolRegistry()
    for fn in make_home_assistant_tools(fake):
        registry.register(fn)

    decls = registry.openai_tools(provider="grok")
    assert _names(decls) == {"home_assistant", "home_assistant_confirm"}


def test_tools_are_visible_to_all_providers():
    """Neither tool has a provider restriction — HA control works the same
    way regardless of which realtime backend the household runs."""
    fake = _FakeHAClient(_ok_response())
    for fn in make_home_assistant_tools(fake):
        assert build_tool(fn).providers is None


def test_both_tools_declare_consequential_risk_flag():
    """HA is the speaker's action SINK — both tools carry the declarative
    `consequential` flag the tool store's policy layer will read (and
    neither claims to return untrusted third-party text)."""
    fake = _FakeHAClient(_ok_response())
    for fn in make_home_assistant_tools(fake):
        built = build_tool(fn)
        assert built.consequential is True
        assert built.untrusted_output is False


# ---- Dispatch timeout outlives the HA client read timeout ------------------

def test_both_tools_declare_timeout_longer_than_read_timeout():
    """LLM-backed HA agents take 30-60s; HAClient's read timeout is 90s.
    BOTH tools call ha.process(), so both must declare a dispatch budget
    longer than the generic DEFAULT_TOOL_TIMEOUT_SEC and longer than the
    client read timeout — otherwise a slow HA turn trips the 12s seam."""
    fake = _FakeHAClient(_ok_response())
    for fn in make_home_assistant_tools(fake):
        built = build_tool(fn)
        assert built.timeout > DEFAULT_TOOL_TIMEOUT_SEC
        assert built.timeout > DEFAULT_READ_TIMEOUT_SEC
