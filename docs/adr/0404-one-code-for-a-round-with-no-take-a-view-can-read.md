# ADR-0404: One code for a round with no take a view can read

- **Date:** 2026-09-30
- **Status:** Accepted. Supersedes (partial)
  [ADR-0392](0392-classify-features-reads-each-kept-takes-impulse.md) §4's spelling
  `classification_no_admissible_captures`. The spelling in
  [ADR-0394](0394-the-distortion-reading-banks-at-capture-on-the-take.md) stands.

## Context

Two views refuse a round that holds no take they can read. `classify-features` spelled the refusal
`classification_no_admissible_captures` (ADR-0392 §4). The distortion view spelled it
`no_admissible_captures` (ADR-0394 §4). The registry held one row for each. This is the third
synonym pair that [#5928](https://github.com/jaspercurry/JTS/issues/5928) found, after
`output_unwritable` and `evidence_unreadable`. [ADR-0300](0300-one-fault-registry.md) gives one
condition one code.

## Decision

1. **The code.** `no_admissible_captures` is the one code for a round that holds no take the asking
   view can read. `evidence_reasons.NO_ADMISSIBLE_CAPTURES` takes the shorter spelling. The distortion
   reading imports that constant and keeps none of its own.
2. **What stays.** The classifier's other refusals, `classification_no_kept_takes` and
   `classification_round_shape_inadmissible`, and the gap shape do not change.
3. **The registry.** The code has one row in `REASON_REGISTRY`: one household sentence and one next
   action, which is to name a round that holds takes the view can read.

## Consequences

- `jasper-round-views classify-features` prints `no_admissible_captures` where it printed
  `classification_no_admissible_captures`. A changed code value is not a change of answer shape
  (ADR-0344 §4), so no answer schema changes.
- A round banked before this change can carry the old code in its bookkeeping. That code has no
  registry row now (#2902).
- Rejected: keep both spellings and register both. One condition would keep two codes.
