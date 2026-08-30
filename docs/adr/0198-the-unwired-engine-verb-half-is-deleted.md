# ADR-0198: The unwired engine verb half is deleted

- **Date:** 2026-08-30
- **Status:** Accepted

## Context

`crossover_v2/session.py` shipped ruling S1's four verbs — `measure`,
`analyze`, `recommend`, `save`. One of them was ever called in production.

The wizard constructs a `TuningSession` at
`jasper/web/correction_crossover_v2.py` and calls `open`, `measure` and
`close`. Nothing called `analyze`, `recommend` or `save`. Their whole support
cluster was reachable only through them, and production never fed the
parameters they read: `TuningSession.prior`, `gain_plan_db`,
`candidate_program_id` and `analysis_declaration` were passed by tests and by
no caller, so `_baseline_for` returned `""` for every record the speaker has
ever banked.

Meanwhile the doors-and-banks tools became the way the LLM reads a round.
Analysing a bank, recommending a next move and writing what a round accounted
for are things it already does over the banked evidence, on the LLM-over-SSH
surface ADR-0188 §4 names. The engine's three verbs were a second, unwired
answer to questions the doors already answer — the shape the Right-Size Ledger
(2026-08-30) was opened to find.

Two derived layers had the same shape. `controllability_ledger` pooled banked
per-band ratios into means, spreads and four confidence labels that no
adoption row, refusal, prescription or gate read back. `record_index` wrote a
SQLite file at the bundle root whose own docstring said nothing that decides
anything may consult it, and whose reader already refused to trust it and
rescanned the take files instead.

## Decision

**The engine's unwired verb half is deleted. The doors-and-banks tools own
analyze, recommend and save as capabilities.**

- `TuningSession` keeps `open` / `measure` / `close` and nothing else.
  `analyze()`, `recommend()`, `save()` and their outcome types are gone, with
  the fields only they read.
- The support cluster reachable only through them is gone: `analysis_walk`,
  `analysis_units`, `prior_bank`, `jasper/cli/crossover_recommender.py`, the
  `Recommender` seam and `EngineSeams.recommend`.
- `controllability_ledger` is reduced to a raw reader. It returns the per-band
  rows banked rounds already carry and computes nothing — the reader asking
  for a mean, a spread or a label is the party that knows which rounds are
  comparable.
- `record_index` keeps the in-memory selection its reader already used. No
  index file is written; the take files are the index.
- `measure`'s pre-play capability abort stays. A stub that reports
  `captured=False` still stops the call before anything plays; what goes is
  the disclosure ledger accumulated for `analyze` to report.

**Resurrect condition:** a driving caller that the doors cannot serve. Not a
plan entry, not a test, not a shape someone might want — a caller.

The process these verbs described is not lost; it lives in
`docs/tuning-methodology.md`, which is where a person or an LLM reads what a
round does.

## Consequences

- `baseline_record_id` on a banked record is now the literal `""`. That is
  what every production record already carried, because `prior` was never set,
  so no banked evidence changes meaning and no reader's shape moves.
- `MeasureOutcome` loses `disclosures`. An aborted `measure` is still
  distinguishable: `stimuli` is empty and nothing played.
- The `/state` `controllability` block publishes raw per-round rows instead of
  a pooled band axis, and `scripts/run-crossover-round.py` prints those rows.
  Both state the speaker's own numbers and derive none.
- `RecordStore.read` / `read_state` / `persist` keep their Protocol slots with
  no engine caller. Collapsing the seam is deferred — a wave that owns those
  files is in flight, and doing it here would collide.

This supersedes ruling S1's four-verb vocabulary for the engine class only.
The words still name what the tuning loop does; they no longer name methods on
`TuningSession`.
