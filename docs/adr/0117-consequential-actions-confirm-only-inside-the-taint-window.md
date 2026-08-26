# ADR-0117: A consequential Home Assistant action confirms only inside the untrusted-content window

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Home Assistant is the only tool on this speaker that performs a real-world
action, which makes it the confused-deputy target: untrusted text the model has
read — an email body, a calendar invite, a device name — could steer it into
`home_assistant("unlock the front door")` with no human ever asking. Fencing
HA's reply text does not address this; the injection arrives on the way *in*.

The durable control from the agent-security literature is least privilege plus
human confirmation on high-risk operations. Applied unconditionally it taxes
the common case — an ordinary spoken "unlock the door" in a session that has
read nothing — for a risk that exists only in a narrow window.

## Decision

`classify_consequential(query)` flags high-impact, hard-to-reverse,
security-relevant actions (unlock, disarm, turn off the alarm, open a
garage/gate/door), conservatively and in base-verb form so state queries do not
over-fire. **The confirmation gate fires only when a shared
`UntrustedContentMonitor` reports third-party text was read within
`UNTRUSTED_CONTENT_WINDOW_SEC`.** In that window the tool does not relay the
request at all: it stashes it in a single-slot, TTL-bounded, single-use store
and returns `needs_confirmation` — a yes/no question the model must speak and
then wait for. `home_assistant_confirm()` takes no arguments and runs only the
stashed action.

The window is a deliberately dumb wall clock, not the model's context window
and not any provider's session lifetime. `monitor=None` fails safe to
always-confirm.

## Consequences

- After the household reads an email, a silently injected unlock becomes an
  audible question they answer. A clean voice-only session is unchanged.
- It also catches mishears inside the tainted window, for free.
- It does not stop an obfuscated household sentence trigger — "good night"
  wired in HA to unlock a door carries no consequential keyword, and JTS cannot
  know what a household phrase *does* because HA owns NLU.
- One model mediates the whole loop, so a fully hijacked model could call the
  tool and its confirm in one breath. The gate defeats the *silent* attack and
  raises the bar; privilege separation is the complete fix and is not built.
- A pending confirmation is bounded in time, not by turns, so a long-delayed
  bare "yes" inside the TTL is the residual. A superseding `home_assistant`
  command clears the pending one.
- Observability is action-label only — `event=ha.confirm_gate`,
  `event=ha.confirm_execute`, and `event=ha.consequential_direct` at DEBUG for
  a clean-session direct run. The utterance is never logged.
- Voice and acoustic injection are out of scope by design.
