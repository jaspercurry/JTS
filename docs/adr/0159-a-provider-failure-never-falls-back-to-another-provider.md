# ADR-0159: A provider failure never falls back to another provider — the speaker says it cannot reach the cloud and stays put

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

JTS supports three interchangeable realtime speech-to-speech backends behind
one env var, and each has observed failure modes: Gemini's silent-session
failures, transient reconnect storms, provider outages. With three working
adapters already loaded, automatic cross-provider failover looks free — the
daemon could reopen on OpenAI when Gemini stops answering.

It is not free. The three backends differ in voice, latency, conversational
style, tool-calling behaviour, and billing model. A household that asked a
question and got an answer in a different voice at a different speed has been
handed a bug report it cannot write. Worse, failover hides the failure: the
Gemini silent-session bug is exactly the class of defect that would have gone
undiagnosed for months behind an automatic retry on another backend.

## Decision

**When the active provider fails, the daemon plays the `cant_reach_cloud` cue
and stays on that provider.** Switching backends is an operator action —
the `/voice/` wizard, `scripts/switch-voice-provider.sh`, or the env file —
never an automatic recovery step.

The supervisor still retries the *same* provider forever with bounded
backoff, and escalates audibly when five consecutive failures share a
fingerprint. The user learns the speaker is broken; the speaker does not
quietly become a different speaker.

## Consequences

- Failures stay visible and diagnosable. A provider-specific bug presents as
  that provider failing, which is what makes it fixable.
- The household's experience of a failure is one consistent cue, not a
  silently changed assistant.
- Recovery from a genuine provider outage requires a human to switch. That is
  accepted: this is a one-household speaker with a wizard and a script, and
  the outage is visible.
- The cue text stays provider-agnostic, which follows directly: the user
  cares that the cloud is unreachable, not which cloud.
- Deliberately given up: uptime during a single-provider outage.
