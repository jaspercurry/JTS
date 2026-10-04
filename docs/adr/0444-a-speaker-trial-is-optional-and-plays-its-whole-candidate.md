# ADR-0444: A speaker trial is optional, and it plays its whole candidate

- **Date:** 2026-10-04
- **Status:** Accepted: the owner's answers Q2 and Q3 (#5925, 2026-10-04) to the calls
  [#6227](https://github.com/jaspercurry/JTS/issues/6227) raised.

## Context

The #6227 plan's rule 1 says a program plays the layers below it, with its own layer and the layers
above it off, and [doctrine §1a](../measurement-loop-doctrine.md) says the same. A speaker trial
(`jasper-round trial` of a speaker-only document, which routes to `speaker/mark` with candidates)
plays each candidate whole: it is composed on the applied tune, so the applied rear, bass and room
layers play too (the speaker row clears nothing;
[ADR-0420](0420-a-round-goes-stale-only-when-a-layer-under-it-changes.md),
[ADR-0436](0436-the-cardioid-default-one-pair-take-then-one-seat-trial-against-the-rear-off-base.md)).
The runbook and the playbook also made that trial a default step ("Fit, trial and apply the
speaker"), five sweeps the plan's budget does not have.

## Decision

1. **The exception.** A speaker trial is the one exception to doctrine §1a: when asked for, it plays
   each candidate whole, with the applied rear, bass and room layers on. No code changes.
2. **Not a default step.** The default setup fits the speaker and applies it after its preview, with
   no trial (an apply needs none, #6227 rule 6). The copied speaker prompt's own note says so (apply
   after the preview; trial only when asked), and the line every prompt shares no longer says "Measure
   the change": the rear and in-room flows name their trials in the playbook's Seat loop and the room
   prompt's design lines. The runbook and the playbook say the same.

## Consequences

- A default plain setup is the speaker round (13 sweeps on a two-way), the in-room round (3) and its
  trial (6), about 22 sweeps; a cardioid build adds the pair take (5 sweep slots) and the seat trial
  (6), about 30. Those counts never included a speaker trial; the docs now match them.
- A speaker trial still works when asked for. Its comparison includes the applied upper layers, so a
  difference it reads is the speaker change heard through the applied stack.
- Rejected: clearing the upper layers in a speaker trial. That changes what a trial plays (the level
  path) for a trial the default setup no longer runs.
