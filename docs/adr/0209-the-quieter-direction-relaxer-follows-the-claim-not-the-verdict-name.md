# ADR-0209: The quieter-direction relaxer follows the claim, not the verdict name

- **Date:** 2026-09-01
- **Status:** Accepted

## Context

The 2026-08-15 owner ruling behind `delta_probe.seam_rollback_deferral`
(#2559) is directional: a realized-vs-commanded miss whose deviation points
entirely QUIETER than commanded is a quality miss the series keeps and learns
from, not a hazard that comes off the speaker. It was implemented for
`model_error` only, and the docstring recorded a second half — that
`level_dependent_shortfall` "never defers", being "a claim about a driver's
headroom" rather than "the shape claim this ruling is about".

The day-2 recommissioning campaign measured what that split costs (#3485).
Its flattest round by a wide margin — every spec band inside tolerance, whole-
curve rms 0.587 dB, benefit improved past its margin, realization matched,
trust trusted, safety safe, `realized_louder_than_commanded: False`,
`boost_over_declared_bound: False`, `safety_anchored: true` — was restored onto
a measured-FAILING graph (two bands out of tolerance, rms 1.40) on
`row5_trusted_safe_regressed`, cause
`delta_probe_rollback_class:level_dependent_shortfall`. The vetoing axis's own
`spec_bands` evidence was empty, because there were no failing bands. The
probe's `gain_factor=0.318` was measured over 2103–10000 Hz, a band that pooled
a deliberate +2 dB realizability PROBE at 3181 Hz — whose declared purpose was
to measure realizability, so 0.318 IS its answer — with a shelf increment.

`measurement-loop-doctrine.md` §3 names this shape outright: *a class that
retreats from a measured-acceptable state on realized != commanded alone is a
bug against this principle*, and *realized-versus-predicted mismatch is a
learning signal, never by itself a reason to retreat*.

## Decision

The relaxer's membership follows the CLAIM rather than the verdict name.
`DELTA_PROBE_REALIZED_VS_COMMANDED_VERDICTS` is
`{model_error, level_dependent_shortfall}`, and `seam_rollback_deferral` tests
membership of that set where it tested one verdict.

Shape and scale are one sentence — *the emitted filters did not do what the
fit's model of them says they do* — so the direction the fences read is the
same measurement on both. `spatially_costly` stays out, and that exclusion is
now stated as its own reason rather than as a leftover: it differences two
MEASUREMENTS (the pre- and post-apply cross-position spreads) with no model
between them, which is the measured regression §3 restores ON.

This supersedes only the docstring's second half. Every fence the ruling
carries is untouched and still decides: a graded bin realized louder than
commanded past tolerance withholds the deferral, `boost_over_declared_bound`
withholds it, and an unanchored map falls back to
`model_departure_over_tolerance`.

`SEAM_DEFERRED_QUIETER_THAN_COMMANDED` changes value from
`model_error_quieter_than_commanded` to `realized_quieter_than_commanded`. The
string rides the journal and the round receipt, so it is renamed rather than
left describing one class: banking `model_error_…` on a shortfall map would put
a false sentence on a hearing-safety record, which is the objection the
function's own docstring already raises against a different absence.

## Consequences

- A round that measures acceptable and realizes quieter than commanded now
  keeps, banks the probe map, and carries the finding as a next-round target
  (`delta_probe:realized_short_of_commanded`) — §3's learning signal, working.
- The louder direction is unchanged in both classes, which is why this is a
  narrowing of a stop rather than a deletion of one: §4a's STOP-RELAXER pattern,
  applied a second time to the same stop.
- A journal or receipt sweep for `model_error_quieter_than_commanded` stops
  matching rounds graded after this. The symbol is unchanged; only its value
  moved.
- **Rejected: keying the adoption table's quality cell on `spec` passing.**
  It is what #3485's own text proposes, and it works for the campaign round,
  but it makes a spec verdict decide — which `evaluate_round_quality` refuses
  on stated evidence (today's spec verdicts are computed with no intersection
  against the session's trusted floor, a term the E4 sweep measured moving
  ~2 dB with gate length alone). It also loses the directional discriminator:
  a LOUDER-than-commanded shape miss on a spec-passing round would have kept,
  and direction is the whole of what #2537 and #2559 turn on.
- **Rejected: a second membership table in `verification`.** The directional
  fences already have one owner, and a copy in the adoption path would be a
  second place for "which way did the miss point" to be answered.
