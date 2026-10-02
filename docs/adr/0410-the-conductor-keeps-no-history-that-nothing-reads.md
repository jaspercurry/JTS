# ADR-0410: The conductor keeps no history that nothing reads

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes (partial)
  [ADR-0198](0198-the-unwired-engine-verb-half-is-deleted.md)'s two lines that keep
  `controllability_ledger` as a raw reader and publish its rows in the status `controllability` block.
- **Context:** The deletion pass on [#5925](https://github.com/jaspercurry/JTS/issues/5925) and the
  cleanup in [#6161](https://github.com/jaspercurry/JTS/issues/6161) read the crossover conductor's
  records. No product code writes a round receipt, so the ledger read nothing. The model-error
  store's only writer was a call that a help text told an operator to type. The way-back pointer's
  only reader, `rollback_candidate`, was test-only, and `review_declined` read a `review_decision`
  that nothing writes. The owner's direction (2026-10-02): no complexity that adds no value.
- **Decision:** The crossover conductor keeps no record that no product reader uses. The
  controllability ledger, the status `controllability` block, the model-error store, and the
  way-back pointer with its Undo stash (`previous_*`, `rollback_candidate`, `review_declined`, the
  `accepted_sound_*` record and the stored post-apply offset) are deleted. The status `crossover_v2`
  block keeps the four fields the envelope reads: `phase`, `needs_recovery`, `failure` and
  `round_receipt`.
- **Consequences:**
  - The applied profile and the Sound declaration are still written durably, so a power cut after
    an apply leaves the box safe and honest. Only the pointer needed the apply's fsync of the v2
    state. A power cut can now leave a state file that the apply wrote first empty: each poll logs a
    warning until the next run writes it, and nothing plays.
  - The incumbent comes from the applied profile, and a restore re-applies a banked candidate by
    its fingerprint.
  - `round_receipt` and the stored `applied` flag stay until the receipts and the stored journey go.
  - Resurrect condition: a product reader that needs the record. A test is not one.
