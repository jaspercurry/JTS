# ADR-0215: A broken cloud connection is announced once, and only when a human must act

- **Date:** 2026-09-02
- **Status:** Accepted

## Context

The xAI account behind the active voice provider hit its billing limit on
2026-09-01. Every realtime handshake was refused with HTTP 403 for ~17 hours
across 1,025 reconnect attempts. Two failures compounded. The escalation cue
was level-triggered on a one-hour rate limit, so the speaker announced the
same unchanged fact roughly eighteen times, through the night, into an empty
room. And the provider's own explanation — "used all available credits or
reached its monthly spending limit", carried in the rejected handshake's HTTP
body — was discarded before it reached a log: the journal held only
`HTTP 403`, and `/state` held nothing at all. The owner heard a vague
"trouble reaching the cloud" all night with no way to learn the cause.

## Decision

Announcements are **edge-triggered**: the speaker speaks when the failure
class changes, never on a timer.

A failure is announced only when it is **terminal** — retrying cannot fix it
and a human must act (no credit, rejected key, missing model). Transient
failures (provider 5xx, timeouts, a dropped link) retry in silence.

**Recovery is tracked but never spoken.** The broken→working edge re-arms the
alarm for the next outage; it produces no audio. Nobody wants to be told the
speaker is working.

Spoken text comes from a **fixed, pre-baked vocabulary**. Cues are synthesised
ahead of time by a cloud TTS backend, so novel wording cannot be produced at
the moment the cloud is unreachable. Provider errors are mapped onto that
closed set. A cue names the **remedy**, not the cause; the specific cause
lives in `/state`, the web UI, doctor, and the journal.

A cue names the management URL only where the page is likely to load. That
always holds for provider failures. The network-down cue names it too, by the
owner's call: phrased as "or troubleshoot at ...", so it reads as an option
rather than an instruction when the page is unreachable.

The provider's rejection body is redacted, bounded, and surfaced —
`_supervisor.failure_detail` feeds both the journal and
`/state.voice.connection_error`.

## Consequences

The incident above would produce one announcement instead of eighteen, and
that one would say what to do about it.

Redaction is now load-bearing on a path that reaches both logs and `/state`
(non-negotiable 3), so `jasper.secret_redaction` is pattern-based: it scrubs
without ever holding the secret. It masks the three live provider key
prefixes and credential-shaped `key: value` text. A credential in an
unrecognised shape would survive — bounded to 300 characters, but surviving.
That is the accepted residual; widen the patterns, do not add a second
redactor.

Given up: proactive warning for transient outages, and the timer that made
long outages keep speaking. A speaker that is broken transiently and never
woken stays quiet. That is deliberate — the wake path already tells anyone
who actually tries to use it.

Rejected: reading the provider's error text aloud. It needs live TTS (the
thing that is down), and it would speak arbitrary text from an outside
service through the household's speaker.

Also rejected: suppressing announcements during quiet hours. No quiet-hours
concept exists in the tree, and "terminal only, once per outage" already caps
a bad night at a single sentence. Revisit only if that one sentence proves
badly timed in practice.

Wired by this ADR's first change: the cause reaching the journal and
`/state`. The cue vocabulary and the edge-trigger land behind it.
