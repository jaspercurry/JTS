# ADR-0152: Local Silero on the AEC stream is the endpointer; provider server VAD stays off

- **Date:** 2026-08-26
- **Status:** Accepted (records the 2026-05-24 A/B verdict; written when
  HANDOFF-vad-experiments.md was trimmed to its operational spine). The
  "knob remains for experiments" clause is superseded by
  [ADR-0244](0244-the-server-vad-path-is-deleted-not-kept-as-a-knob.md).

## Context

Something has to decide when the user has stopped talking. Two candidates were
available: the realtime provider's own server-side VAD (OpenAI/Grok expose it
via `session.update`), or a local Silero VAD scoring the same audio on the Pi.

Server VAD is the tempting option — it is free, it is the provider's own tuning,
and it removes a model from the Pi's memory budget. A 2026-05-24 A/B across five
stream/VAD configurations, five utterances per cell, same phrase, user across
the room, said otherwise.

| Cell | Stream | VAD | Result |
|---|---|---|---|
| **0** | AEC | local Silero | **4/4 transcripts perfect** |
| 1 | AEC | server VAD, 350 ms silence | 0/5 — cuts mid-sentence |
| 1b | AEC | server VAD, 800 ms silence | 3/5 — threshold flakiness |
| 1c | AEC | server VAD, threshold 0.3 | 0/5 — fires on the wake word, commits before the command |
| 3 | raw + SimpleAGC | local Silero | 1/7 — AGC clipping and hallucinations |

## Decision

**Local Silero on the AEC stream is the endpointer. `JASPER_SERVER_VAD_ENABLED`
defaults to `0` and stays there.**

The knob remains for experiments (OpenAI/Grok only); `create_response` and
`interrupt_response` stay false regardless of it.

## Consequences

- **Borderline audio still gets heard.** The decisive failure was cell 1b: two
  of five attempts timed out with `event=server_vad.no_speech` on audio our
  local Silero scored at 0.98. Across-the-room voice sits right on the
  provider's 0.5 cliff, and the provider's threshold is not ours to move.
- **The wake word cannot end the turn.** Server VAD sees the wake word in the
  pre-roll and, at a permissive threshold, fires `speech_started` on it and
  commits before the command arrives. The local endpointer only arms after
  sustained speech, so the wake tail cannot commit a turn.
- **Loosening the provider's threshold makes it worse, not better.** Cell 1c
  established that the failure is not "too strict" — a lower threshold made it
  fire on transients. There is no server-VAD setting that fixes this shape.
- **The endpointer's constants are ours to tune, and they are load-bearing.**
  `END_OF_UTTERANCE_SPEECH_THRESHOLD`, `SUSTAINED_SPEECH_TO_ARM_SEC` and the
  arming peak gate in `jasper/voice_daemon.py` each carry their own recorded
  evidence. Changing one is a measured change, not a preference.
- **The cost is a VAD model resident on a 1 GB Pi**, and an endpointer we own
  the bugs in. Both were judged cheaper than a turn that ends mid-sentence.
- **The raw-stream alternative is unresolved, not rejected.** Cell 3 failed on a
  homegrown AGC's clipping, not on the premise that a well-normalized raw stream
  could work. That experiment is archived, not closed.
