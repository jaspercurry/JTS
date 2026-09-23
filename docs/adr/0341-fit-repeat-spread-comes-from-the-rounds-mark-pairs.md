# ADR-0341: Fit repeat spread comes from the round's mark pairs

- **Date:** 2026-09-22
- **Status:** Accepted

## Context

The fit verdict reads an installed repeat floor that current rounds cannot
produce: its writer needs retired cloud groups. The current
[plans](../../jasper/active_speaker/measurement_plans.json) already measure
four mark takes for a baseline and two for `speaker/mark`. The fit envelope's
`paired_repeats` describes sweep repeats inside one capture, not these takes.
The owner chose to keep useful tools and remove the verdict's floor dependency.

## Decision

Only repeated takes at the same mark with the microphone held still establish
the fit verdict's repeat spread. For each driver/set, use all unordered pairs
of selected MEASURE takes at exactly 0° horizontal and 0° vertical. Pair only
within the same recorded pose, placement (`pose_index`), and run; do not pool
sets, drivers, off-axis takes, or returns to the mark after a move.

Interpolate each banked magnitude curve onto the fitter's frequency grid within
its `fit_band_hz`, including both edges. For each pair, compute the RMS of the
per-frequency dB difference without level removal or extra smoothing, using
`seat_figures.spread_rms_db`. Report the **maximum pair RMS** as
`repeat_spread_db`, with `repeat_basis: "mark_pairs_max_rms"` and `n_pairs`,
the number of eligible pairs. Four mark takes give six pairs; two give one.
Missing curve or band coverage makes the spread null with a reason; it must
not produce a smaller spread from a partial set of pairs.

Fewer than two mark takes produce `repeat_spread_db: null`, `n_pairs: 0`, and
`repeat_basis: "no_mark_pairs"`. Missing pairs are a disclosure, not a refusal.
The verdict does not read the installed `repeat-floor.json`. This supersedes
the installed repeat-floor authority of [ADR-0192](0192-the-campaign-is-the-validation.md)
and the [ADR-0302](0302-the-speaker-program-is-explicit.md) sentence that the pair
check "does not compute a new tolerance from the current pair".

## Consequences

Each round supplies its own measured comparison scale. The maximum keeps the
worst observed pair visible; RMS across pairs was rejected because it can hide
that difference. It is evidence for judgment, not an adoption gate. Baseline
fit numbers also change when design poses retain both horizontal and vertical
angles instead of letting a vertical take replace the on-axis response. The
in-capture envelope statistic and the repeat-floor tools remain separate.
