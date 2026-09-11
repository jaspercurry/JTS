# ADR-0294: The Interruptible seam is exclusive to host-reconciled adapters

- **Date:** 2026-09-11
- **Status:** Accepted; supersedes the "every shipped adapter satisfies both
  `LiveTurn` and the seam" clause of
  [ADR-0244](0244-the-server-vad-path-is-deleted-not-kept-as-a-knob.md)'s
  Consequences; the server-VAD deletion itself stands unchanged.
- **Context:** `Interruptible` (`cancel_response`, `truncate_assistant_audio`)
  split off `LiveTurn` as its own Protocol. OpenAI Live
  (`owns_interruption = True`) stops generation on the user's own voice
  server-side, leaving no response to cancel and no history to trim, so it
  implements neither member — no no-op bodies. ADR-0244's claim that every
  shipped adapter satisfies both no longer holds.
- **Decision:** `Interruptible` is satisfied only by adapters that reconcile
  a barge-in on the host — Realtime, Grok, Gemini. An adapter that owns
  interruption server-side implements no part of the seam. The host gates
  the reconcile on `isinstance(turn, Interruptible)`, never on
  `owns_interruption` directly; their exclusivity —
  `isinstance(turn, Interruptible) is not turn.owns_interruption` — is
  pinned per catalog provider by `tests/test_voice_barge_in_contract.py`.
- **Consequences:** A future server-owned adapter ships no `Interruptible`
  no-ops to maintain; a host-reconciled one must implement both members or
  fail the conformance test. Code deciding whether to call the seam must
  check `isinstance(turn, Interruptible)`, never branch on
  `owns_interruption`.
