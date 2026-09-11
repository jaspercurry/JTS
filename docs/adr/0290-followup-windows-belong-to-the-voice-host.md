# ADR-0290: Follow-up windows belong to the voice host

- **Date:** 2026-09-10
- **Status:** Accepted; extends ADR-0244 for continuous-audio providers.
- **Context:** The owner requested wake-triggered conversations, interruption,
  local tools, and five seconds to ask a follow-up before the closing chirp.
  GPT-Live uses a continuous stream with no authoritative spoken-answer completion
  event ([migration guide](https://developers.openai.com/api/docs/guides/live-migration)).
- **Decision:** JTS owns conversation closure and the shared `JASPER_FOLLOWUP_TIMEOUT_SEC`
  setting (default 5; 0 disables follow-ups) shapes every window. Realtime, Gemini, and
  Grok are endpointer-based and close once playout drains; the host's own follow-up window
  opens after playout drains only for an adapter declaring `host_followup_window`, which
  none sets yet; each flips the flag once a hardware test proves it usable. Push-to-talk
  keeps its button lifecycle. Continuous providers instead run their own inactivity window
  inside the turn, off that same timeout, weighing user speech, audible output, playout
  drain, and backend work. No legacy server-VAD toggle is restored.
- **Consequences:** GPT-Live has a separate adapter and opens a billable session
  only on wake. Its managed Responses backend uses the existing local tool
  registry and dispatcher. The host's `end_conversation` tool handles conversational
  dismissals; cancelling a timer remains a timer action. Superseded task results
  cannot start another action or reach a later conversation; an action already
  executed is not undone. Final voice duration and backend token usage are separate
  ledger entries. Live's five-second closure is an inactivity policy, not a claim
  that audio gaps prove semantic completion; real hardware verification is still
  needed for pauses, echo, cancellation latency, and the close handshake. The
  catalog therefore labels GPT-Live experimental.
