# ADR-0132: The wake OR-gate fires now; the false-positive cost is measured, not pre-empted

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-wake-telemetry.md was trimmed to
  its operational spine; the decision itself dates to the 2026-05-21 design
  conversation and shipped with the multi-leg wake loop)

## Context

Multi-leg wake detection scores the same utterance on several mic chains
(chip-direct, AEC3, DTLN, the XVF fixed ASR beams) and has to decide what
counts as a wake. The conservative option is to measure each leg's
false-accept rate first and only then combine them. The owner's complaint at
the time was the opposite of a false-accept problem: the speaker was
"triggering too little, too infrequently" — a wake-rate sweep had 14 of 20
real "Jarvis" utterances stuck at 0.001 confidence across every AEC
configuration, so the model produced no usable signal on them at all.

## Decision

**Any leg crossing its own threshold fires the wake immediately, and the
false-positive cost of that OR is measured from production telemetry rather
than estimated in advance.**

One user attempt is one event: the first leg to cross wins the fire and
records `trigger_kind`, the other legs that were simultaneously above
threshold (with a *fresh* score — a stopped stream must not vote with a stale
one) are recorded in `fired_legs`, and a refractory window closes the race.

The standing false-positive metric is the funnel itself: a wake that opens a
turn but never reaches `ts_speech_detected` is a suspected false accept.
Nothing else gates the OR.

## Consequences

- **The gate is revisited only on evidence.** If the
  `ts_turn_opened IS NOT NULL AND ts_speech_detected IS NULL` count goes bad,
  that is the trigger to add a mic/reference coherence gate or to per-leg
  threshold the OR. Until then the coherence gate stays unbuilt — see
  ADR-0130 for the rule that nothing gets veto power upstream of the wake OR.
- **Existing cost and quality guards carry the risk.** The spend cap bounds
  the money a false-accept storm can cost, and the sustained-speech VAD gate
  ends a spurious turn without reaching the model.
- **A leg only helps if it can ever be the lone trigger.** `fired_legs` is
  what answers that; a leg that never appears alone is a candidate for
  removal, and per-leg training (ADR-0129) is the response when it appears
  alone only rarely.
- **A quieter capture floor was not the answer to the missed wakes.** The 0.001
  utterances are invisible to any capture threshold because the model emits no
  signal on them; recovering those needs a retrained model
  (HANDOFF-wake-training-experiment.md), not a lower floor.
