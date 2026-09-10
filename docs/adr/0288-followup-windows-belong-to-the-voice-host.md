# ADR-0288: Follow-up windows belong to the voice host

- **Date:** 2026-09-10
- **Status:** Accepted; extends ADR-0244 for continuous-audio providers.
- **Context:** The owner requested wake-triggered conversations, interruption,
  local tools, and five seconds to ask a follow-up before the closing chirp.
  GPT-Live uses a continuous stream with no authoritative spoken-answer completion
  event ([migration guide](https://developers.openai.com/api/docs/guides/live-migration)).
- **Decision:** JTS owns conversation closure and the shared
  `JASPER_FOLLOWUP_TIMEOUT_SEC` setting (default 5; 0 disables follow-ups).
  Endpointer-based providers retain local Silero endpointing and their existing
  adapters. After playback drains, the host retains output ownership while
  listening locally for the next utterance. Push-to-talk keeps its button lifecycle.
  Continuous providers keep supplying paced audio, including silence. Their
  inactivity window considers user speech, audible output, playout drain, and
  backend work. No legacy server-VAD toggle is restored.
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
