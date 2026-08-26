# ADR-0166: A session-resumption handle is dropped on the first failure of any kind

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Gemini Live hands back a session-resumption handle so a reconnect can restore
the conversation's context instead of starting cold. The supervisor cached
that handle and reused it on every reconnect, dropping it only when the
failure was a 409.

That classification assumed a failure's close code tells you whether the
handle is still good. It does not. A 1008 close carrying the reason
`"BidiGenerateContent session expired"` means the server has permanently
invalidated *this handle* — but to a dispatch keyed on 409 it looked like any
other transient close, so the supervisor reconnected with the same dead
handle, was rejected identically, and retried. One overnight wedge reached
**798 consecutive identical retries** before a human intervened.

The failure mode is structural, not specific to that close code: any
server-side handle invalidation the classifier does not recognise becomes an
infinite loop, because the retry carries the very thing being rejected.

## Decision

**On the first reconnect failure of any kind, the resumption handle is
dropped.** The next attempt opens a cold session.

The supervisor still never gives up on reconnecting — that policy is
unchanged — and the tight-retry-loop detector still escalates audibly when
five consecutive failures share a fingerprint.

## Consequences

- The cost is bounded and known: one turn of context continuity on a
  reconnect that would probably have succeeded with the handle.
- The payoff is that no failure shape, present or future, can wedge the
  supervisor into re-presenting an invalidated credential forever.
- Handle reuse is no longer a place where a new provider error code needs a
  classifier update to stay safe. Classifying failures is still useful for
  logging and escalation; it is no longer load-bearing for liveness.
- Deliberately given up: the best case, where a transient blip preserved the
  conversation. On a household speaker a cold reconnect is a lost sentence of
  context, not a lost session.
