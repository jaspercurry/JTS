# ADR-0130: Nothing gets veto power upstream of the wake OR-gate

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-wake-training-experiment.md was
  trimmed to its operational spine)

## Context

Putting a voice-activity detector in front of wake-word detection is the
standard move: it is cheap, it cuts the number of frames the detectors score,
and Silero is right there — JTS already runs it downstream as a
sustained-speech gate before opening a turn.

## Decision

**No VAD, and no other gate, sits upstream of wake detection.** The fusion
rule stays a plain OR across the legs. The existing downstream Silero stays
exactly where it is: it gates *turn-opening*, not wake-firing.

## Consequences

- **A prefix gate changes `WW_raw ∨ WW_aec ∨ WW_dtln` into
  `GATE ∧ (WW_raw ∨ WW_aec ∨ WW_dtln)`.** The OR-gate exists precisely so
  each leg can compensate for the others' failures; anything ANDed in front
  re-introduces a single-point recall bottleneck, and when it misses a frame
  of real speech, **no leg can recover**. Silero does miss, most in exactly
  the music conditions the fusion was built for.
- A secondary reason points the same way: Silero V5 dropped the AANL layer,
  with documented community regressions on quiet and distant speech — the
  same physics as far-field, which is the failure surface this whole
  workstream targets. The two failure modes compound.
- This generalises past VAD. Any future proposal that filters, gates, or
  short-circuits frames *before* the detectors — an energy threshold, a
  cheap classifier, a "skip when obviously silent" fast path — is the same
  structure and re-opens this ADR. Cost savings upstream of wake are paid for
  in recall, which is the metric the product is worst at.
- **Falls out of AGENTS.md's no-silent-deafness rule** (non-negotiable 6): a
  gate that suppresses a wake produces no cue, no log of a wake that never
  happened, and no way to tell a missed utterance from silence.
- Server-side VAD was separately tested and reverted: every cell was worse
  than the pre-existing default.
