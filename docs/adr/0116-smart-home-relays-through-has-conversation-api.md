# ADR-0116: Smart-home control relays through Home Assistant's conversation API, not MCP

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

JTS delegates all smart-home control to whatever Home Assistant the household
already runs on the LAN. HA exposes three viable external surfaces: the REST
conversation API, the WebSocket Assist Pipeline, and the MCP server at
`/api/mcp`. Picking among them decides what a household can reach by voice.

Two properties of HA's MCP server, read out of `homeassistant/helpers/llm.py`
and `homeassistant/components/intent/`, settle it. Its tool list is a thin
passthrough over `AssistAPI.tools` — intent-handler wrappers, one tool per
exposed script, and a live-context tool. Nothing in it triggers an automation:
`HassTurnOn` against an `automation.*` entity calls `automation.turn_on`, which
*enables* a disabled automation rather than running it. And sentence triggers
(`trigger: conversation` — HA's documented mechanism for arbitrary household
phrases) are matched before the intent loop in `default_agent`, so they are not
registered as intent handlers and MCP can never see them. A household that has
wired "good morning" or "bedroom medium" to an automation would find those
commands structurally unreachable.

## Decision

The `home_assistant` voice tool POSTs the user's utterance verbatim to
`POST /api/conversation/process` and speaks HA's reply. **JTS is a relay**: it
does not know what entities exist, does not resolve them, and does not
interpret household phrases. HA owns NLU, entity resolution, automation
dispatch, and sentence triggers.

## Consequences

- Automations and sentence triggers work, which is the whole point.
- One HTTPS POST returning JSON serialises identically across every realtime
  provider JTS supports — no per-provider tool-schema translation, and no
  workaround for providers that freeze the tool list at session start.
- JTS cannot answer "is the front door locked?" without a round trip, because
  it holds no entity snapshot. Pulling MCP's context-snapshot resource purely
  for prompt augmentation stays available as a later, additive option.
- Retrying is forbidden: the conversation endpoint is not idempotent, so a
  retried "turn off the lights" could double-fire a script. Every call is
  try-once, fail-soft, and the model speaks the error.
- Rejected: `POST /api/services/conversation/process`, which returns no
  response body (home-assistant/core#93754, #104122).
- Rejected: remote MCP (a provider's edge connecting to HA directly), which
  would require the household's HA to be publicly reachable.
