"""Home Assistant control tool.

`home_assistant(query)` delegates the user's utterance to HA's conversation
pipeline (HA owns NLU, entity resolution, sentence triggers, custom intents,
and any LLM-backed agent); JTS is a relay. A second tool,
`home_assistant_confirm()`, completes a consequential action the user was
asked to confirm.

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

Consequential-action confirmation (prompt-injection defense). HA is the
only tool on this speaker that performs a real-world action, which makes
it the confused-deputy target: untrusted content the model has read (an
email body, a calendar invite) could steer it into calling
`home_assistant("unlock the front door")` with no human intent. Per OWASP
LLM01 "human oversight for high-risk operations" and the design-patterns
literature (see docs/HANDOFF-prompting.md "Untrusted tool-result fencing"
for sources), high-impact actions get an explicit user confirmation:
`classify_consequential` flags them, the tool stashes the request and
returns `needs_confirmation` WITHOUT acting, and only `home_assistant_confirm`
— after the user audibly says yes — carries it out. This is a structural
gate: a consequential action is never executed in the same call that
requests it, so a silent injected unlock becomes an audible "Do you want
me to…?" the household answers.

The confirmation is **conditional on recent untrusted content** (an
`UntrustedContentMonitor`, shared with the gmail/calendar tools): it fires
only within ~10 minutes of reading email/calendar — the window where an
injected instruction could be in play. A clean voice-only session runs the
action directly, so the household isn't nagged on an ordinary "unlock the
door". This is a deliberately dumb wall-clock window, not tied to the
model's context window or per-provider session persistence; voice/acoustic
injection is out of scope by design. Residual (a fully-hijacked model that
self-confirms in one breath) needs privilege separation / dual-LLM; tracked
as future work in HANDOFF-prompting.md.

The tool's description teaches the model both WHEN to call and WHEN NOT
to call — conditional rules per the OpenAI Realtime Prompting Guide
pattern that CLAUDE.md already cites. Absolute bans get partially
ignored; conditional rules stick.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from . import tool

from ..home_assistant import DEFAULT_READ_TIMEOUT_SEC, HAClient

logger = logging.getLogger(__name__)


# LLM-backed HA conversation agents (OpenAI Conversation, Anthropic,
# Google Generative AI inside HA) legitimately take 30-60s for a
# tool-using turn, so HAClient's read timeout is 90s. The dispatch-seam
# budget must outlive that read timeout — otherwise every slow HA turn
# trips the generic 12s tool timeout and the model never sees HA's real
# answer (the bug this fixes). +5s margin lets the HTTP layer surface
# its own timeout/error first. Derived from the client so the 90s number
# stays single-sourced in home_assistant.py.
_HA_TOOL_TIMEOUT_SEC = DEFAULT_READ_TIMEOUT_SEC + 5.0

# How long a pending consequential action waits for the user's spoken
# confirmation before it expires. Long enough for "Do you want me to…?"
# → user reply, short enough that a stale pending can't execute if the
# household wandered off mid-exchange.
_CONFIRM_TTL_SEC = 90.0


# Consequential smart-home actions: high-impact, security/safety-relevant,
# hard to reverse. An injected "unlock the door" / "disarm the alarm" is the
# dangerous confused-deputy payload, so these get an explicit user
# confirmation before JTS relays them to Home Assistant.
#
# This is a best-effort English-keyword safety net, NOT a complete
# classifier — HA owns NLU and entity names, so an obfuscated household
# sentence-trigger (e.g. "good night" wired to unlock a door) can bypass it.
# It deliberately errs toward confirming: a spurious confirm is a minor
# annoyance; a silent unlock is not. Base verb forms ("unlock", not
# "unlocked") keep state queries ("is the door unlocked?") from over-firing.
# First match wins; order garage/gate before the generic door rule.
# Labels are written to read naturally in the spoken question "Do you want me
# to {label}?" — and kept generic (not echoing the raw, possibly-untrusted
# query) so the confirmation prompt never reads attacker text aloud.
_CONSEQUENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bunlock(s)?\b"), "unlock the door"),
    (re.compile(r"\bdisarm(s)?\b"), "disarm the alarm"),
    (
        re.compile(
            r"\b(turn off|shut off|switch off|disable|deactivate)\b"
            r"[^.?!]*\b(alarm|security|burglar)\b"
        ),
        "turn off the alarm",
    ),
    (re.compile(r"\bopen(s)?\b[^.?!]*\bgarage\b"), "open the garage"),
    (re.compile(r"\bopen(s)?\b[^.?!]*\bgate\b"), "open the gate"),
    (re.compile(r"\bopen(s)?\b[^.?!]*\bdoor\b"), "open the door"),
)


def classify_consequential(query: str) -> str | None:
    """Return a short human label if `query` names a consequential
    smart-home action that should be confirmed before execution, else
    None. Conservative + best-effort — see `_CONSEQUENTIAL_PATTERNS`."""
    q = (query or "").lower()
    for pat, label in _CONSEQUENTIAL_PATTERNS:
        if pat.search(q):
            return label
    return None


@dataclass
class _Pending:
    query: str          # the original request, executed verbatim on confirm
    label: str          # human category, e.g. "unlock the door" (safe to log)
    deadline: float     # monotonic clock deadline


class _ConfirmationStore:
    """Single-slot store for one consequential action awaiting the user's
    spoken confirmation.

    - single-use: `take()` clears the slot, so one "yes" runs one action;
    - TTL-bounded: an expired pending must not execute if the household
      wandered off mid-exchange;
    - latest-wins: a fresh consequential request replaces an unconfirmed
      older one rather than queuing.
    Injectable clock mirrors HAClient so TTL is testable without sleeping."""

    def __init__(self, *, ttl_sec: float = _CONFIRM_TTL_SEC, clock=time.monotonic) -> None:
        self._pending: _Pending | None = None
        self._ttl = ttl_sec
        self._clock = clock

    def arm(self, query: str, label: str) -> None:
        self._pending = _Pending(query, label, self._clock() + self._ttl)

    def take(self) -> _Pending | None:
        """Return and clear the pending action, or None if absent/expired."""
        p, self._pending = self._pending, None
        if p is None or self._clock() > p.deadline:
            return None
        return p

    def clear(self) -> None:
        """Drop any pending action — a fresh non-confirming command has
        superseded it, so a later 'yes' must not trigger the abandoned one."""
        self._pending = None


def make_home_assistant_tools(ha: HAClient | None, *, monitor=None, clock=time.monotonic):
    """Build the home_assistant tool factory.

    Returns an empty list when `ha` is None (HA not configured) so the
    model never sees a tool whose every call would fail. Mirrors the
    gating pattern of `make_bus_tools` and `make_subway_tools`.

    `monitor` (an `UntrustedContentMonitor`, optional) makes the
    consequential-action confirmation *conditional*: a consequential action
    confirms only when untrusted content (email/calendar) was read recently
    — a clean voice-only session runs it directly, no prompt. `monitor=None`
    is the fail-safe (always confirm consequential actions), so a wiring
    miss errs toward more caution, never less.

    `clock` is injectable so the confirmation TTL is testable; production
    callers use the default monotonic clock."""
    if ha is None:
        return []

    store = _ConfirmationStore(clock=clock)

    @tool(timeout=_HA_TOOL_TIMEOUT_SEC, log_payload=False, log_args=False, consequential=True)
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

        Response shape (one of two):
          Completed action / answer:
            spoken_response: str  — speak this; HA phrased its own response
            success: bool         — true if HA returned usable text
            response_type: str    — 'action_done' | 'query_answer' | 'error' | ''
            error_code: str|null  — 'no_intent_match' | 'no_valid_targets'
                                    | 'failed_to_handle' | 'unknown' | null
            error_detail: str     — human-readable detail on failure
          Confirmation needed (consequential action, NOT done yet):
            needs_confirmation: true
            action: str           — short label, e.g. 'unlock the door'
            spoken_response: str  — a yes/no question to speak

        Consequential actions (unlocking, disarming an alarm, opening a
        garage/gate/door) are NOT carried out here — the tool returns
        `needs_confirmation: true`. Speak the `spoken_response` question,
        wait for the user, and only call home_assistant_confirm if they
        say yes. This protects the household from a request that didn't
        come from them.

        Voice answer style:
          On a completed success, speak `spoken_response` exactly — Home
          Assistant phrased its own response. Don't add 'OK' or 'Done' on
          top of it. On failure (success=false), speak the `error_detail`
          briefly in your own words ('Home Assistant couldn't find that' /
          'I can't reach Home Assistant right now'). Don't apologize at
          length and don't offer to try again unless the user asks.

        Skip the preamble — Home Assistant typically responds in
        well under a second on the rule-based path, and the user
        gains nothing from a status update.
        """
        label = classify_consequential(query)
        tainted = monitor is None or monitor.is_tainted()
        if label is not None and tainted:
            # Consequential AND untrusted content was read recently (or no
            # monitor wired → fail-safe). Structural gate: never execute a
            # consequential action in the call that requests it. Stash it and
            # ask; only the user's confirmation (home_assistant_confirm) runs
            # it. A clean voice-only session (untainted) skips this and runs
            # the action directly — the cost lands only in the risk window.
            store.arm(query, label)
            logger.info("event=ha.confirm_gate action=%s", label)
            # Phrasing: a plain yes/no question, nothing more. The daemon has
            # no follow-up-listening yet (after this turn the user must
            # re-wake to answer — see docs/HANDOFF-homeassistant.md), so a
            # "say yes to confirm" suffix would imply an instant reply we
            # can't honor. The question itself elicits "yes"; the
            # needs_confirmation rule in SYSTEM_INSTRUCTION owns the wait/
            # confirm protocol. Reads the same once barge-in lands.
            return {
                "needs_confirmation": True,
                "action": label,
                "spoken_response": f"Do you want me to {label}?",
            }
        # Non-arming path: this is a fresh command, not a consequential
        # confirmation. It supersedes any unconfirmed pending — the household
        # moved on — so a later "yes" can't fire an abandoned action. The tool
        # has no turn context in production (jasper/voice/trace.py is
        # test-only), so this + the TTL are what bound a stale pending.
        store.clear()
        if label is not None:
            # Consequential, but the session was clean (untainted) so we ran it
            # without asking. DEBUG-level for forensics ("why no confirm?")
            # without journal spam on the common path.
            logger.debug("event=ha.consequential_direct action=%s", label)
        result = await ha.process(query)
        return result.as_tool_result()

    @tool(timeout=_HA_TOOL_TIMEOUT_SEC, log_payload=False, log_args=False, consequential=True)
    async def home_assistant_confirm() -> dict:
        """Carry out the consequential smart-home action that
        home_assistant just asked the user to confirm.

        Call this ONLY when both are true:
          1. home_assistant returned `needs_confirmation: true`, and
          2. the user then clearly said yes ('yes', 'go ahead', 'do it')
             in a later turn.
        Takes no arguments — it runs the exact action that was pending.
        Do NOT call it in the same turn as the request, and do NOT call
        it if the user declined, changed the subject, or said anything
        other than a clear yes — the action is cancelled. If nothing is
        pending (the request expired or was already confirmed) the tool
        says so; relay that.

        Response shape: the same completed-action shape as home_assistant
        (spoken_response / success / response_type / error_code /
        error_detail). Speak `spoken_response`.
        """
        pending = store.take()
        if pending is None:
            return {
                "success": False,
                "spoken_response": "There's nothing waiting to be confirmed.",
            }
        logger.info("event=ha.confirm_execute action=%s", pending.label)
        result = await ha.process(pending.query)
        return result.as_tool_result()

    return [home_assistant, home_assistant_confirm]
