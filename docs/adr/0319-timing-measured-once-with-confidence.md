# ADR-0319: Timing is measured once with confidence

- **Date:** 2026-09-15
- **Status:** Accepted

## Context

On jts3, summed-fit margins of 3.35, 1.66, 1.14 and 1.28 caused the loop to
switch to flat-sum answers of −67, −130 and +190 µs across poses. The applied
22 µs was sound. Timing describes driver geometry; a new EQ or room read must
not choose another half-period lobe. See [issue #5206](https://github.com/jaspercurry/JTS/issues/5206).

## Decision

Decide timing only when the applied profile has no `timing` record. Fit both
polarities against the design-axis sum for every paired driver repeat. The
full-set residual is the RMS across the repeat residuals at the best delay.
`margin_db` is the losing polarity's best residual minus the winning one.
`repeat_spread_db` and `repeat_spread_us` are the ranges of the winning
polarity's best residuals and delays across repeats. Commit only when
`margin_db > repeat_spread_db` and the arrival-gap anchor agrees on the lobe.
A single take has null spreads and cannot commit. An inconclusive summed fit
reports `needs_measurement`; it never falls back to flat sum. Driver-only
flat-sum results remain estimates and cannot create measured timing.

The apply writer saves signed `delay_us`, `polarity` and `provenance` once.
Measured timing also records round, take, graph, time and all four confidence
fields. Provenance is `measured`, `authored_by_model` or `set_by_user`.
Driver delay and inversion settings are projections of that record. No EQ,
room, bass, crossover or declaration change marks it stale. Only removal of
the record enables a new timing decision.

Later design-axis reads evaluate the saved pair without a search. Print its
`residual_rms_db` and `repeat_noise_db`, the range of its repeat residuals.
Only `residual_rms_db > 3 * repeat_noise_db` asks in the existing `next_action`
field to reset and measure timing again. Missing repeats cannot make that
comparison. The saved value remains in force. Capture retake policy applies
to summed fits just as it does to other reads.

For later rounds, retained timing supersedes the timing commissioning step in
[ADR-0203, The incumbent tune retires; recommissioning is structure-first](0203-the-incumbent-tune-retires-recommissioning-is-structure-first.md).
No ADR at this base states the 1.5 margin gate or flat-sum precedence; this
replaces those code and playbook rules.

## Consequences

Timing keeps one value and one provenance record until reset. Per-pose timing
and repeat noise remain visible. Weak evidence asks for a louder read in a
quieter room, without inventing a timing result or refusing other tuning work.
The LLM leaves alignment out of documents unless the user asks for a new read
or an explicit value. Geometry changes can justify a reset; other changes do
not trigger one. Fixed thresholds and flat-sum fallback were rejected because
they hide the read's own uncertainty.
