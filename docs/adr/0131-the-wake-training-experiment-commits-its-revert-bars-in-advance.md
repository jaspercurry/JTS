# ADR-0131: The wake-training experiment commits its revert bars in advance

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-wake-training-experiment.md was
  trimmed to its operational spine)

## Context

This project has repeatedly shipped changes on metrics that later did not
survive listening — "the data said X, the ears said Y" happened more than
once during the AEC work. A training run makes that failure mode worse: it
produces a model with a number attached, arriving after weeks of effort and
real money, at the exact moment the temptation to reinterpret the bar is
strongest.

## Decision

**The bars are fixed before the run, and a tripped bar reverts to the
existing model rather than being renegotiated.**

Ship bar: wake rate **≥ 80 %** on a *held-out* slice of the gold corpus at
the far+music condition — the hardest cell — with no false-accept regression
on the production wake-events corpus, confirmed by both peak scores and a
listening pass on representative clips.

Revert bars — **any one** of these means the trained models do not deploy:

- recall on the held-out gold corpus drops below **60 %** in *any* cell;
- false-positive rate on the production wake-events corpus exceeds
  **0.5/hour** (the `jarvis_v2` baseline is ~0.18/hour);
- the listening checkpoint shows the new model's wins are dominated by
  artifact-matching — for example firing on TV noise carrying phonemes the
  augmentation set taught it.

## Consequences

- **Metrics rank, ears select.** Metrics narrow the candidate field; a human
  listening pass picks the winner. Nothing deploys on "metric wins but sounds
  worse". The listening checkpoints are async — a review package is handed
  off and a verdict comes back — so they never block a long experiment, and
  the package hides its own metrics behind a subdirectory you open only after
  listening, with a blind randomized set for any "is X actually better than
  Y?" call where confirmation bias would inflate the answer.
- **The gold corpus is the fixed measurement instrument**, not the production
  wake-events corpus (unknown conditions) and not fresh ad-hoc recordings per
  experiment (which lose comparability). Its held-out split stays held out.
- **Verify the instrument before trusting it.** Synthetic positives, the RIR
  set and the augmented SNR range are all validated by listening *before*
  paying for training — a wrong-sounding synthetic "Jarvis" or an unrealistic
  room is much cheaper to catch there than after.
- **One lever at a time**, everything else at production defaults. Composing
  every variable with every other produces results nobody can attribute.
  Offline first, Pi last: minutes per offline iteration against hours on
  hardware, and the Pi only ever sees candidates that already cleared the
  offline bar.
- Tripping a bar is a real outcome, not a failure of nerve: it means either
  going back to design or accepting that wake-word retraining is not the
  right lever for this setup. Retraining is cheap enough (single-digit to
  low-tens of dollars per leg per run) that iteration remains available
  either way.
