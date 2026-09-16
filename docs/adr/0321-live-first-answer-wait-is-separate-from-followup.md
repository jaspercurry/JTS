# ADR-0321: Live first-answer wait is separate from follow-up

- **Date:** 2026-09-16
- **Status:** Accepted
- **Context:** On jts.local at 18:00 EDT, build `a71550239`, answered Live
  utterances took 0.0, 0.6, 0.8, and 1.4 s from the user's last word to the
  first answer on the provider's transcript timeline. Two fully transcribed
  utterances produced no answer before `followup_timeout` closed them at
  about 2.0 s. The wire-time anchor
  ([PR #5258](https://github.com/jaspercurry/JTS/pull/5258)) measured only
  2–5 ms on every turn, so it did not address this wait.
- **Decision:** Use two waits for Live. After each user utterance, including
  a follow-up question, allow 5 s for the model to start answering. Audio
  accepted by playout or a backend delegation counts as a start. After the
  answer and playout drain, wait for a follow-up for 2 s by default, using
  the unchanged `JASPER_FOLLOWUP_TIMEOUT_SEC`. Both waits end with
  `followup_timeout` and the clean closing chirp, without a nudge or tool.
  Keep the 30 s backend hold and the post-completion grace unchanged.
  Withdraw the wire-time anchor and its queue timestamps and lag field.
  This refines [ADR-0320](0320-live-hangup-is-one-silence-window.md).
- **Consequences:** The first-answer wait covers the observed tail beyond
  2 s without extending the follow-up window. A silent dismissal such as
  "cancel" now chirps after about 5 s. A model that never starts costs 5 s
  of session time. The first-answer wait is a constant, with no new config
  knob.
