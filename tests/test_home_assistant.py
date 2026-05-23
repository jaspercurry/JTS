"""Unit tests for jasper.home_assistant.HAClient — mock HTTP, no network.

Uses httpx.MockTransport (the same pattern as tests/test_bus.py) so the
test suite is fully hermetic and matches the repo convention.

Coverage:
  - the six outcome buckets (ok / network / timeout / auth /
    agent_error / intent_miss / parse_error)
  - response shape parsing (action_done, query_answer, error, ssml)
  - conversation_id lifecycle: reuse within TTL, drop on
    continue_conversation=False, drop on TTL expiry, accept HA's
    rotation
  - agent_id / language pass-through in the request body
  - URL normalization (trailing slash, /api suffix)
  - healthcheck (GET /api/), config (GET /api/config), list_agents
    (GET /api/states with conversation.* filter)
  - the no_valid_targets-with-speech case (success=True because the
    text is non-empty, per the multi-satellite-benign convention)
"""
from __future__ import annotations

import json

import httpx
import pytest

from jasper.home_assistant import (
    CONVERSATION_ID_TTL_SEC,
    HAClient,
    OUTCOME_AGENT_ERROR,
    OUTCOME_AUTH,
    OUTCOME_INTENT_MISS,
    OUTCOME_NETWORK,
    OUTCOME_OK,
    OUTCOME_PARSE_ERROR,
    OUTCOME_TIMEOUT,
)


# ---- System-prompt addendum -----------------------------------------------
#
# When HA isn't configured, the system prompt grows a conditional clause
# that redirects smart-home requests to the wizard URL. Without it, the
# model has been observed (May 22 2026 production log) to misroute "turn
# on the bedroom lights" to get_current_time + get_now_playing — calling
# unrelated tools rather than the documented "tool unavailable" response.
# The clause also has to interpolate cfg.hostname so multi-speaker
# households (jts2.local, jts3.local) see the right URL.

def test_system_prompt_includes_ha_nudge_when_unconfigured():
    """When ha_configured=False, the prompt grows a clause with the
    speaker's hostname and a 'do not call other tools' guard."""
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(
        location="", ha_configured=False, hostname="jts.local",
    )
    assert "Home Assistant smart-home control isn't set up" in prompt
    assert "jts.local/ha" in prompt
    # The "do not call other tools" guard prevents the misroute we saw
    # on 2026-05-22 (lights → time + now_playing).
    assert "Do not call any other tool" in prompt


def test_system_prompt_omits_ha_nudge_when_configured():
    """When ha_configured=True (the default), no nudge is added — the
    model relies on the static SYSTEM_INSTRUCTION's tool guidance."""
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(location="", ha_configured=True)
    assert "Home Assistant smart-home control isn't set up" not in prompt
    assert "/ha" not in prompt


def test_system_prompt_ha_configured_defaults_to_true():
    """Backwards-compat: callers not passing the new arg must NOT get
    the nudge. The signature default is True (assume configured)."""
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(location="")
    assert "Home Assistant smart-home control isn't set up" not in prompt


def test_system_prompt_ha_nudge_uses_configured_hostname():
    """Multi-speaker households (jts2.local, jts3.local) see the right
    URL — the speaker the user is talking to, not a hardcoded default.
    This was the staff-review-fix bug: the original prompt hardcoded
    jts.local."""
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(
        location="", ha_configured=False, hostname="jts2.local",
    )
    assert "jts2.local/ha" in prompt
    # And explicitly NOT the wrong default
    assert "jts.local/ha" not in prompt


def test_system_prompt_transit_nudge_uses_configured_hostname():
    """Same fix applies to the transit nudge — was hardcoding jts.local
    before, breaking multi-speaker setups."""
    from jasper.voice_daemon import _build_system_instruction
    prompt = _build_system_instruction(
        location="", transit_configured=False, hostname="jts2.local",
    )
    assert "jts2.local/transit" in prompt
    # The OLD hardcoded form must not appear
    assert "jts.local/transit" not in prompt


# ---- Test scaffolding -------------------------------------------------------

class _FakeClock:
    """Monotonic-clock substitute. Advance with `.tick(seconds)`."""

    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


def _conversation_response(
    speech: str = "Turned on the bedroom lights.",
    *,
    response_type: str = "action_done",
    error_code: str | None = None,
    targets_success: list | None = None,
    targets_failed: list | None = None,
    conversation_id: str = "01HX0001",
    continue_conversation: bool = False,
    use_ssml: bool = False,
) -> dict:
    """Build a realistic HA conversation/process response body. Matches
    the schema in homeassistant/helpers/intent.py (dev branch May 2026)."""
    speech_key = "ssml" if use_ssml else "plain"
    data_block: dict = {}
    if response_type == "error":
        data_block["code"] = error_code or "no_intent_match"
    if targets_success is not None:
        data_block["success"] = targets_success
    if targets_failed is not None:
        data_block["failed"] = targets_failed
    return {
        "response": {
            "response_type": response_type,
            "speech": {speech_key: {"speech": speech, "extra_data": None}},
            "card": {},
            "language": "en",
            "data": data_block,
        },
        "conversation_id": conversation_id,
        "continue_conversation": continue_conversation,
    }


def _client_with(handler, *, clock: _FakeClock | None = None, **kwargs) -> HAClient:
    """Wire an HAClient to a mocked httpx transport."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, headers={"Authorization": "Bearer test"})
    defaults = dict(
        url="http://homeassistant.local:8123",
        token="test-token",
        http=http,
        clock=clock or _FakeClock(),
    )
    defaults.update(kwargs)
    return HAClient(**defaults)


# ---- URL normalization ------------------------------------------------------

def test_url_normalization_strips_trailing_slash():
    c = HAClient(url="http://homeassistant.local:8123/", token="t")
    assert c.url == "http://homeassistant.local:8123"


def test_url_normalization_strips_api_suffix():
    c = HAClient(url="http://homeassistant.local:8123/api", token="t")
    assert c.url == "http://homeassistant.local:8123"


def test_url_normalization_strips_api_slash_suffix():
    c = HAClient(url="http://homeassistant.local:8123/api/", token="t")
    assert c.url == "http://homeassistant.local:8123"


# ---- Happy path: action_done with speech -----------------------------------

@pytest.mark.asyncio
async def test_process_action_done_returns_speech_ok():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_conversation_response())

    client = _client_with(handler)
    try:
        result = await client.process("turn on the bedroom lights")
    finally:
        await client.aclose()

    assert result.success is True
    assert result.outcome == OUTCOME_OK
    assert result.response_type == "action_done"
    assert result.speech == "Turned on the bedroom lights."
    assert result.error_code is None
    assert captured["url"] == "http://homeassistant.local:8123/api/conversation/process"
    assert captured["auth"] == "Bearer test-token"
    assert captured["body"]["text"] == "turn on the bedroom lights"
    assert captured["body"]["language"] == "en"
    # conversation_id is NOT sent on the first call (none cached yet)
    assert "conversation_id" not in captured["body"]
    # agent_id omitted by default
    assert "agent_id" not in captured["body"]


@pytest.mark.asyncio
async def test_process_query_answer_returns_speech_ok():
    def handler(request):
        return httpx.Response(200, json=_conversation_response(
            speech="The bedroom is 72 degrees.",
            response_type="query_answer",
            targets_success=[],
        ))

    client = _client_with(handler)
    try:
        result = await client.process("what's the bedroom temperature")
    finally:
        await client.aclose()

    assert result.success is True
    assert result.response_type == "query_answer"
    assert result.speech == "The bedroom is 72 degrees."


@pytest.mark.asyncio
async def test_process_reads_ssml_when_plain_missing():
    def handler(request):
        return httpx.Response(200, json=_conversation_response(
            speech="<speak>Done.</speak>", use_ssml=True,
        ))

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert result.success is True
    assert result.speech == "<speak>Done.</speak>"


# ---- Error paths from HA --------------------------------------------------

@pytest.mark.asyncio
async def test_process_no_intent_match_is_speakable_but_logged_as_intent_miss():
    """HA returns response_type=error + a useful speech string ("Sorry,
    I am not aware of …"). The outcome bucket flags it as intent_miss
    for log forensics, but `success=True` because we have text worth
    speaking. The model speaks HA's exact wording rather than a
    generic paraphrase — HA knows which device the user named, we
    don't, and "Sorry, I am not aware of any device called bedroom xyz"
    is strictly more helpful than "Home Assistant couldn't find that."
    """
    def handler(request):
        return httpx.Response(200, json=_conversation_response(
            speech="Sorry, I am not aware of any device or area called bedroom xyz.",
            response_type="error",
            error_code="no_intent_match",
        ))

    client = _client_with(handler)
    try:
        result = await client.process("turn on the bedroom xyz")
    finally:
        await client.aclose()

    # success reflects "did HA give us text to speak" — orthogonal to
    # the outcome bucket which is for log slicing.
    assert result.success is True
    assert result.outcome == OUTCOME_INTENT_MISS
    assert result.response_type == "error"
    assert result.error_code == "no_intent_match"
    assert "bedroom xyz" in result.speech
    # as_tool_result returns empty error_detail on success — the model
    # speaks `spoken_response` directly, no paraphrase fallback needed.
    tool_result = result.as_tool_result()
    assert tool_result["success"] is True
    assert tool_result["spoken_response"].startswith("Sorry, I am not aware")
    assert tool_result["error_detail"] == ""


@pytest.mark.asyncio
async def test_process_no_valid_targets_with_speech_is_intent_miss_but_speakable():
    """no_valid_targets is documented as benign in multi-satellite homes —
    HA's speech text is still useful to surface, and `success` reflects
    "is the speech speakable" not "did the intent succeed". The model
    speaks HA's text verbatim."""
    def handler(request):
        return httpx.Response(200, json=_conversation_response(
            speech="I couldn't find a device matching that.",
            response_type="error",
            error_code="no_valid_targets",
        ))

    client = _client_with(handler)
    try:
        result = await client.process("turn on the xyzzy")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_INTENT_MISS
    # Bug-fix: response_type=error with usable speech is success=True
    # so the model speaks HA's text verbatim rather than substituting a
    # generic paraphrase. See HAResponse docstring.
    assert result.success is True
    assert result.speech == "I couldn't find a device matching that."
    tool_result = result.as_tool_result()
    assert tool_result["spoken_response"] == "I couldn't find a device matching that."
    assert tool_result["success"] is True


@pytest.mark.asyncio
async def test_process_failed_to_handle_is_intent_miss():
    def handler(request):
        return httpx.Response(200, json=_conversation_response(
            speech="An unexpected error occurred while handling the intent.",
            response_type="error",
            error_code="failed_to_handle",
        ))

    client = _client_with(handler)
    try:
        result = await client.process("do something complicated")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_INTENT_MISS
    assert result.error_code == "failed_to_handle"


@pytest.mark.asyncio
async def test_process_action_done_with_no_speech_is_parse_error():
    """An action_done response with no speech is a semantic edge case —
    HA succeeded but gave us nothing to say. Tag it as parse_error so
    the model speaks our canned error rather than going silent."""
    def handler(request):
        return httpx.Response(200, json=_conversation_response(speech=""))

    client = _client_with(handler)
    try:
        result = await client.process("turn on")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_PARSE_ERROR
    assert result.success is False


@pytest.mark.asyncio
async def test_process_error_with_no_speech_is_not_success():
    """response_type=error AND empty speech → success=False because
    there's no text to speak. The model falls back to error_detail
    via as_tool_result() and paraphrases."""
    def handler(request):
        return httpx.Response(200, json=_conversation_response(
            speech="", response_type="error", error_code="failed_to_handle",
        ))

    client = _client_with(handler)
    try:
        result = await client.process("do something")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_INTENT_MISS  # still tagged by response_type
    assert result.success is False                # nothing to speak
    tool_result = result.as_tool_result()
    assert tool_result["error_detail"]            # non-empty fallback


# ---- HTTP-status error paths -----------------------------------------------

@pytest.mark.asyncio
async def test_process_401_is_auth_outcome():
    def handler(request):
        return httpx.Response(401, text="Unauthorized")

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_AUTH
    assert result.success is False
    # Speech is provider-agnostic and points at the setup page
    assert "Home Assistant" in result.speech
    assert "reconnect" in result.speech.lower() or "setup" in result.speech.lower()


@pytest.mark.asyncio
async def test_process_500_is_agent_error_outcome():
    def handler(request):
        return httpx.Response(500, text="Internal server error")

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_AGENT_ERROR
    assert result.success is False
    assert "internal error" in result.speech.lower()


@pytest.mark.asyncio
async def test_process_unexpected_status_is_parse_error():
    def handler(request):
        return httpx.Response(418, text="I'm a teapot")

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_PARSE_ERROR


@pytest.mark.asyncio
async def test_process_malformed_json_is_parse_error():
    def handler(request):
        return httpx.Response(200, text="not valid json {{{")

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_PARSE_ERROR


@pytest.mark.asyncio
async def test_process_connection_error_is_network_outcome():
    def handler(request):
        raise httpx.ConnectError("Connection refused")

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_NETWORK
    assert "can't reach Home Assistant" in result.speech


@pytest.mark.asyncio
async def test_process_timeout_is_timeout_outcome():
    def handler(request):
        raise httpx.ReadTimeout("Read timeout")

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_TIMEOUT


@pytest.mark.asyncio
async def test_empty_query_is_parse_error_without_hitting_network():
    """Empty query short-circuits — no HTTP call, returns parse_error."""
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    client = _client_with(handler)
    try:
        result = await client.process("   ")
    finally:
        await client.aclose()

    assert result.outcome == OUTCOME_PARSE_ERROR
    assert called is False


# ---- conversation_id lifecycle ---------------------------------------------

@pytest.mark.asyncio
async def test_conversation_id_not_sent_on_first_call():
    captured: list = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_conversation_response(continue_conversation=True))

    client = _client_with(handler)
    try:
        await client.process("turn on the bedroom lights")
    finally:
        await client.aclose()

    assert "conversation_id" not in captured[0]


@pytest.mark.asyncio
async def test_conversation_id_reused_when_continue_is_true():
    """After HA returns continue_conversation=True, the next call within
    TTL sends the cached conversation_id."""
    captured: list = []
    call = {"n": 0}

    def handler(request):
        call["n"] += 1
        captured.append(json.loads(request.content))
        # First call: HA says "keep this conversation going"
        if call["n"] == 1:
            return httpx.Response(200, json=_conversation_response(
                conversation_id="conv-A", continue_conversation=True,
            ))
        # Second call: HA returns a different ID (silent rotation)
        return httpx.Response(200, json=_conversation_response(
            conversation_id="conv-B", continue_conversation=False,
        ))

    clock = _FakeClock()
    client = _client_with(handler, clock=clock)
    try:
        await client.process("set a timer for 5 minutes")
        clock.tick(10.0)  # well within TTL
        await client.process("call it pasta")
    finally:
        await client.aclose()

    # First call: no conversation_id sent. Second: conv-A from the
    # cached response is sent. We do NOT send conv-B even though HA
    # rotated to it — but the cache is updated to None (because the
    # second response said continue_conversation=False).
    assert "conversation_id" not in captured[0]
    assert captured[1].get("conversation_id") == "conv-A"
    # After the second call, the cache is cleared.
    assert client.conversation_id is None


@pytest.mark.asyncio
async def test_conversation_id_dropped_when_continue_is_false():
    captured: list = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_conversation_response(
            conversation_id="conv-A", continue_conversation=False,
        ))

    clock = _FakeClock()
    client = _client_with(handler, clock=clock)
    try:
        await client.process("turn on the lights")
        clock.tick(1.0)
        await client.process("turn off the lights")
    finally:
        await client.aclose()

    # First call has no ID; second call also has no ID because HA said
    # continue=False after the first.
    assert "conversation_id" not in captured[0]
    assert "conversation_id" not in captured[1]


@pytest.mark.asyncio
async def test_conversation_id_expires_after_ttl():
    captured: list = []
    call = {"n": 0}

    def handler(request):
        call["n"] += 1
        captured.append(json.loads(request.content))
        if call["n"] == 1:
            return httpx.Response(200, json=_conversation_response(
                conversation_id="conv-A", continue_conversation=True,
            ))
        return httpx.Response(200, json=_conversation_response(
            conversation_id="conv-B", continue_conversation=True,
        ))

    clock = _FakeClock()
    client = _client_with(handler, clock=clock)
    try:
        await client.process("start a sequence")
        # Tick past the TTL. The cached conv_id should be considered stale.
        clock.tick(CONVERSATION_ID_TTL_SEC + 1.0)
        await client.process("continue the sequence")
    finally:
        await client.aclose()

    assert "conversation_id" not in captured[0]
    assert "conversation_id" not in captured[1]


@pytest.mark.asyncio
async def test_conversation_id_property_reflects_cache():
    def handler(request):
        return httpx.Response(200, json=_conversation_response(
            conversation_id="conv-X", continue_conversation=True,
        ))

    clock = _FakeClock()
    client = _client_with(handler, clock=clock)
    try:
        assert client.conversation_id is None
        await client.process("ask a question")
        assert client.conversation_id == "conv-X"
        # Past TTL → None
        clock.tick(CONVERSATION_ID_TTL_SEC + 1.0)
        assert client.conversation_id is None
    finally:
        await client.aclose()


# ---- agent_id / language pass-through --------------------------------------

@pytest.mark.asyncio
async def test_agent_id_pass_through_when_set():
    captured: list = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_conversation_response())

    client = _client_with(handler, agent_id="conversation.openai_conversation")
    try:
        await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert captured[0]["agent_id"] == "conversation.openai_conversation"


@pytest.mark.asyncio
async def test_agent_id_omitted_when_empty():
    captured: list = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_conversation_response())

    client = _client_with(handler, agent_id="")
    try:
        await client.process("turn on the lights")
    finally:
        await client.aclose()

    assert "agent_id" not in captured[0]


@pytest.mark.asyncio
async def test_language_pass_through():
    captured: list = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_conversation_response())

    client = _client_with(handler, language="es")
    try:
        await client.process("enciende las luces")
    finally:
        await client.aclose()

    assert captured[0]["language"] == "es"


# ---- healthcheck / config / list_agents ------------------------------------

@pytest.mark.asyncio
async def test_healthcheck_returns_true_on_200_api_running():
    def handler(request):
        assert request.url.path == "/api/"
        return httpx.Response(200, json={"message": "API running."})

    client = _client_with(handler)
    try:
        assert await client.healthcheck() is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_healthcheck_returns_false_on_401():
    def handler(request):
        return httpx.Response(401, text="Unauthorized")

    client = _client_with(handler)
    try:
        assert await client.healthcheck() is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_healthcheck_returns_false_on_unexpected_body():
    def handler(request):
        return httpx.Response(200, json={"message": "Something else"})

    client = _client_with(handler)
    try:
        assert await client.healthcheck() is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_healthcheck_returns_false_on_connection_error():
    def handler(request):
        raise httpx.ConnectError("Connection refused")

    client = _client_with(handler)
    try:
        assert await client.healthcheck() is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_config_returns_dict_on_success():
    def handler(request):
        assert request.url.path == "/api/config"
        return httpx.Response(200, json={
            "location_name": "Home", "version": "2026.5.1",
        })

    client = _client_with(handler)
    try:
        config = await client.config()
    finally:
        await client.aclose()

    assert config == {"location_name": "Home", "version": "2026.5.1"}


@pytest.mark.asyncio
async def test_config_returns_none_on_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = _client_with(handler)
    try:
        assert await client.config() is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_list_agents_filters_conversation_domain():
    def handler(request):
        assert request.url.path == "/api/states"
        return httpx.Response(200, json=[
            {
                "entity_id": "conversation.home_assistant",
                "state": "2026-05-21T10:00:00+00:00",
                "attributes": {"friendly_name": "Home Assistant"},
            },
            {
                "entity_id": "conversation.openai_conversation",
                "state": "2026-05-21T10:00:00+00:00",
                "attributes": {"friendly_name": "OpenAI"},
            },
            {
                "entity_id": "light.bedroom",  # filtered out
                "state": "off",
                "attributes": {"friendly_name": "Bedroom"},
            },
        ])

    client = _client_with(handler)
    try:
        agents = await client.list_agents()
    finally:
        await client.aclose()

    ids = {a["entity_id"] for a in agents}
    assert ids == {"conversation.home_assistant", "conversation.openai_conversation"}
    names = {a["name"] for a in agents}
    assert names == {"Home Assistant", "OpenAI"}


@pytest.mark.asyncio
async def test_list_agents_returns_empty_on_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    client = _client_with(handler)
    try:
        assert await client.list_agents() == []
    finally:
        await client.aclose()


# ---- as_tool_result shape (consumed by the model) --------------------------

@pytest.mark.asyncio
async def test_as_tool_result_omits_error_detail_on_success():
    def handler(request):
        return httpx.Response(200, json=_conversation_response())

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    tool_result = result.as_tool_result()
    assert tool_result["success"] is True
    assert tool_result["spoken_response"] == "Turned on the bedroom lights."
    assert tool_result["error_detail"] == ""


@pytest.mark.asyncio
async def test_as_tool_result_includes_error_detail_on_failure():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client_with(handler)
    try:
        result = await client.process("turn on the lights")
    finally:
        await client.aclose()

    tool_result = result.as_tool_result()
    assert tool_result["success"] is False
    assert tool_result["error_detail"]  # non-empty


# ---- build_ha_client factory -----------------------------------------------

def test_build_ha_client_returns_none_when_disabled():
    from jasper.home_assistant import build_ha_client

    class _Cfg:
        ha_enabled = False
        ha_url = ""
        ha_token = ""
        ha_agent_id = ""

    assert build_ha_client(_Cfg()) is None


def test_build_ha_client_returns_client_when_enabled():
    from jasper.home_assistant import build_ha_client

    class _Cfg:
        ha_enabled = True
        ha_url = "http://homeassistant.local:8123"
        ha_token = "abc"
        ha_agent_id = ""

    client = build_ha_client(_Cfg())
    assert client is not None
    assert client.url == "http://homeassistant.local:8123"
