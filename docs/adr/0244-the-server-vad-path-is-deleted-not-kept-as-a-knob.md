# ADR-0244: The server-VAD path is deleted, not kept as a knob

- **Date:** 2026-09-06
- **Status:** Accepted (supersedes the "the knob remains for experiments"
  clause of [ADR-0152](0152-local-silero-on-the-aec-stream-is-the-endpointer-server-vad-stays-off.md);
  the endpointer ruling itself stands unchanged)
- **Context:** ADR-0152 ruled local Silero the endpointer and left
  `JASPER_SERVER_VAD_ENABLED` in place "for experiments". Nothing has flipped
  it since. What it kept alive was not a knob: six `LiveTurn` /
  `LiveConnection` Protocol members, an OpenAI-only adapter implementation
  (shadow speech/commit state, `set_turn_detection`, `create_response_only`,
  a manual-VAD restore on turn release), a second end-of-utterance branch in
  `WakeLoop._handle_wake_frame`, a push-to-talk refusal latch, a background
  response-trigger task, and four config fields. It was also the only reason
  `GeminiLiveTurn` failed `isinstance(turn, LiveTurn)` — the adapter omits
  the members it cannot honour — so the contract could not be typed or
  guarded while it stood.
- **Decision:** The server-VAD path is deleted from the tree. The endpointer
  vocabulary loses `server_vad`: `/state.voice.endpointer` and the
  wake-events column are now `push_to_talk` / `silero_aec` /
  `no_speech_abort`, and `voice.input_policy`'s `endpointing` is the constant
  `manual_silero`. Re-running the A/B means restoring the path from git
  history, not setting an env var.
- **Consequences:** The turn contract is typeable — `LiveTurn` and the
  `Interruptible` seam are now both satisfied by every shipped adapter, which
  is what the conformance guard in `tests/test_voice_barge_in_contract.py`
  pins. The frame loop has one endpointer instead of two. What this gives up
  is a one-line re-enable of an experiment the 2026-05-24 A/B already lost
  0/5, 3/5 and 0/5 across three cells; ADR-0152's evidence is why that is
  cheap to give up. The raw-stream question ADR-0152 left open is unaffected
  — it was never gated on server VAD.
