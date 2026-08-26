# ADR-0136: Waveform fusion must beat score fusion before it is a candidate

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-wake-corpus-quality.md was
  trimmed to its operational spine)

## Context

JTS captures the same utterance on several mic legs, which invites a tempting
shortcut: instead of scoring each leg and OR-ing the decisions (ADR-0132), mix
the aligned waveforms into one better signal and score that once. It would cost
one detector instead of N, and on a first pass the mixes did produce recall
numbers in the same range as the full leg union.

## Decision

**Waveform mixing stays an offline hypothesis test. It becomes a live or
training leg only after beating score/decision fusion across multiple sessions
*and* against hard negatives.**

A single-session recall gain is evidence to investigate, never a production
recommendation.

## Consequences

- **A mix that wins on positives can still be the wrong answer.** Mixing
  destroys per-leg diversity: the clips score fusion catches because *one* leg
  saw them cleanly can disappear into an average, and the same averaging can
  raise false accepts on hard negatives. Positives-only evidence cannot see
  either failure.
- **Hard negatives are part of the bar, not a follow-up.** The wake gate's cost
  is false accepts (ADR-0132); a fusion change evaluated without them is
  measuring half the question.
- **The current architecture stays score/decision fusion** until that bar is
  cleared. The offline harness (`scripts/_waveform_fusion_experiment.py`)
  exists to try to clear it, and its output belongs in the review package, not
  in the runtime.
