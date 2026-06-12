"""Home Assistant control tool.

One tool, `home_assistant(query: str)`, that delegates the user's
utterance to HA's conversation pipeline. HA owns NLU, entity resolution,
sentence triggers, custom intents, and (if the household configured one)
its own LLM-backed agent. JTS is a relay.

Architecture rationale is in `docs/HANDOFF-homeassistant.md`. Summary:

  - Sentence triggers (the household's "bedroom medium" → automation
    wirings) only fire through HA's conversation pipeline. They are
    invisible to HA's MCP server.
  - Automations cannot be triggered via MCP at all (HA's MCP server has
    no `automation.trigger` tool surface). The conversation API is the
    only path that reaches them.
  - "Works with whatever HA is set up for" therefore requires the
    conversation API. MCP is a richer surface for a narrower problem
    (cross-tool orchestration); we'll revisit in v1.1+ when a concrete
    use case demands it.

The tool's description teaches the model both WHEN to call and WHEN NOT
to call — conditional rules per the OpenAI Realtime Prompting Guide
pattern that CLAUDE.md already cites. Absolute bans get partially
ignored; conditional rules stick.
"""
from __future__ import annotations

from . import tool

from ..home_assistant import DEFAULT_READ_TIMEOUT_SEC, HAClient


# LLM-backed HA conversation agents (OpenAI Conversation, Anthropic,
# Google Generative AI inside HA) legitimately take 30-60s for a
# tool-using turn, so HAClient's read timeout is 90s. The dispatch-seam
# budget must outlive that read timeout — otherwise every slow HA turn
# trips the generic 12s tool timeout and the model never sees HA's real
# answer (the bug this fixes). +5s margin lets the HTTP layer surface
# its own timeout/error first. Derived from the client so the 90s number
# stays single-sourced in home_assistant.py.
_HA_TOOL_TIMEOUT_SEC = DEFAULT_READ_TIMEOUT_SEC + 5.0


def make_home_assistant_tools(ha: HAClient | None):
    """Build the home_assistant tool factory.

    Returns an empty list when `ha` is None (HA not configured) so the
    model never sees a tool whose every call would fail. Mirrors the
    gating pattern of `make_bus_tools` and `make_subway_tools`."""
    if ha is None:
        return []

    @tool(timeout=_HA_TOOL_TIMEOUT_SEC, log_payload=False, log_args=False)
    async def home_assistant(query: str) -> dict:
        """Send a natural-language smart-home request to Home Assistant.

        Call this for ANY of these:
          - Device control: lights, switches, plugs, locks, blinds,
            covers, fans, thermostats, climate, appliances.
            ('turn on the bedroom lights', 'lock the front door',
             'set the thermostat to 70').
          - Area-scoped commands: 'turn off all the lights in the
            kitchen', 'is anything on upstairs'.
          - Scenes and scripts the household has set up: 'movie time',
            'good night', 'bedtime'.
          - Custom phrases the household has configured as sentence
            triggers, no matter how they sound: 'bedroom medium',
            'I'm leaving', 'kids are asleep'. Pass these through
            verbatim — Home Assistant owns the routing.
          - State queries about devices in the home: 'is the front
            door locked?', 'what's the bedroom temperature?',
            'are any lights on?'.

        Do NOT call home_assistant for:
          - Weather → call get_weather.
          - Music control / playback → call the spotify and transport
            tools.
          - Time / day / date → call get_current_time.
          - Subway or bus arrivals → call get_subway_arrivals /
            get_bus_arrivals.
          - Timers, calendar, email — those have dedicated tools.
          - General conversation, world-knowledge questions, jokes,
            chitchat.

        Pass the user's request close to verbatim. Home Assistant has
        its own language understanding; don't translate spoken phrasing
        into structured form. Pass the literal phrase the user said.

        Response shape:
          spoken_response: str  — speak this verbatim (or close to it)
          success: bool         — true if HA returned usable text
          response_type: str    — 'action_done' | 'query_answer' | 'error' | ''
          error_code: str|null  — 'no_intent_match' | 'no_valid_targets'
                                  | 'failed_to_handle' | 'unknown' | null
          error_detail: str     — human-readable detail on failure
                                  (empty when success=true)

        Voice answer style:
          On success, speak `spoken_response` exactly — Home
          Assistant phrased its own response. Don't add 'OK' or
          'Done' on top of it; if HA said 'Turned on the bedroom
          lights' that's the full answer.
          On failure (success=false), speak the `error_detail`
          briefly in your own words ('Home Assistant couldn't find
          that' / 'I can't reach Home Assistant right now'). Don't
          apologize at length and don't offer to try again unless
          the user asks.

        Skip the preamble — Home Assistant typically responds in
        well under a second on the rule-based path, and the user
        gains nothing from a status update.
        """
        result = await ha.process(query)
        return result.as_tool_result()

    return [home_assistant]
