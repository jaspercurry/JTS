# ADR-0320: Live hang-up is one silence window

- **Date:** 2026-09-16
- **Status:** Accepted
- **Context:** The model-driven dismissal path (tool, prompt phrases, nudge,
  and `unanswered_utterance` verdict, [#5208](https://github.com/jaspercurry/JTS/pull/5208))
  missed in most listening passes. In the owner's jts.local session, "cancel"
  led to a silent frontend, a nudge at 2.1 s, a backend "got it", a 5 s
  follow-up window, and 2.7 s of teardown: about 14 s to hang up. The model
  never called the tool.
- **Decision:** The client owns Live call closure. Wait while speaker audio
  is pending, playout has not drained, the backend is mid-answer, or local
  VAD detects user speech. After 2 s of silence, play the normal closing
  chirp and hang up. `JASPER_FOLLOWUP_TIMEOUT_SEC` keeps its override and
  validation, with a default of 2.0. Keep the existing no-speech, spend,
  connection-loss, and stalled-work bounds, plus the 8 s grace for the
  frontend to voice a completed backend answer. The client never asks the
  model to end a call: remove the dismissal prompts, nudge, verdict, and
  `end_conversation` tool for every provider. This supersedes the 5 s value
  and the `end_conversation` sentence in [ADR-0290](0290-followup-windows-belong-to-the-voice-host.md).
  [ADR-0292](0292-followup-windows-are-provider-owned.md) still governs
  endpointed providers; they close when their response and playout finish.
- **Consequences:** "Stop" on any provider now waits for its normal close
  window instead of a model tool ending the call. A Live frontend that
  ignores a question yields a closing chirp instead of a rescued answer;
  the owner accepts this tradeoff: less is more. On chip-AEC boxes, residual
  echo can extend the silence window. This is accepted and must be checked
  by ear.
  The client treats playout drain as the end of the answer, so a pause longer
  than `SILENCE_BRIDGE_SEC` + the window (about 2.8 s) inside the frontend's
  own answer ends the call mid-answer with a chirp; accepted for snappiness,
  verify by ear.
