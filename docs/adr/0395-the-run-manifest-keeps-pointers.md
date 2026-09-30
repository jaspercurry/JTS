# ADR-0395: The run manifest keeps pointers; each reader joins the records it reads

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial) [ADR-0383](0383-one-take-record-for-every-purpose.md)
  §3 (the rows copy the record's curves and analysis) and §7 (the incomplete override and the abort
  rows are row facts), and [ADR-0299](0299-one-evidence-manifest-per-run.md)'s per-take content list:
  the take's record holds those facts, and the row points at it. Amends ADR-0383 §4's removal
  condition: C1b removes no flat record key.
- **Context:** Under ADR-0383 §3 a round held each take's facts twice, on its record and on its
  manifest rows, and a reader could read either copy. C1b PR 1 moved every reader of a take's curves
  and analysis to the record, through a join each reader asks for
  ([#5737](https://github.com/jaspercurry/JTS/issues/5737)). C1b PR 2 cuts the copies from the rows.
- **Decision:**
  1. A take row is `{take_id, record_id, selected}` (manifest `schema_version` 3). A set keeps
     `set_id`, `capture_basis` and `base`, the executor's set identity (ADR-0299). A take's role is
     its set's, and so are the gain and fader its role played: the record's `level.stimulus_dbfs` is
     the take's loudest sweep.
  2. A reader that reads a take's facts joins its row with its record, `{**row, **record}`, through
     `round_inputs.take_records`, `with_records` or `SetTakes.with_records`. The join reads each
     record once, and only for the takes that reader reads: kept takes; or every take for the
     packet's take list, the round's coverage lines, and a set whose take a view names (an unkept one
     refuses with its verdict). `read_run_manifest` and `resolve_set` return rows; a reader of their
     takes' facts joins them. A record that cannot be read refuses that reader by name
     (`capture_unreadable_sidecar`), except in the comparand rule, which passes over the take
     (ADR-0101), and in the packet and its coverage lines, which disclose it.
  3. The record banks the three facts only a row held: `pose_index` and `stimulus_ordinal`, from the
     stop, and `level.alignment`, the alignment levels with the shortfall carried from the stop's
     earlier attempt and `capped_by`. The run's retake count is the manifest's `honoured.retakes`.
     Readers order takes by the record's `captured_at`, then its attempt; no take banks its timing.
  4. The manifest names the preset it ran `preset` (R1-20c), and so do the packet's copy of it
     (`jts_round_packet/5`) and the `jasper-round list` and `show` rows (`jts_round_list/2`,
     `jts_round_show/2`). `program` elsewhere keeps its meaning, the tuning program, and the
     `--program` filters stay.
  5. A row with no `record_id` key was banked before this, and refuses `take_curves_not_banked` with
     `detail.field: record_id` (#2902; #6059's refusal by field). `read_run_manifest` and the bank
     check every row of the manifest they read, so such a manifest refuses before a reader reads its
     `preset`. It is a `RoundSetRefused`, so the round readers' error handlers still catch it.
  6. The manifest writes no planned rows. `asked`, `not_measured` and `honoured.stops_planned` state
     the plan.
  7. A take the run did not keep is `selected: false`, beside the manifest's `not_measured`, `reason`
     and `stopped_at`. Its record holds its verdict, `incident` and `measurement_status`, and the
     packet's `fault` reads its verdict's fault, else its incident. A take whose program never played
     banks no record: its row's `record_id` is `""`, the join reads no record for it, so it never
     refuses by §5, and its stop's `not_measured` entry keeps the take's `fault` and `evidence`.
  8. The live run keeps its own rows in memory: the stop, and what it decided of each take (its
     status, fault, next action, level and alignment), with each banked record. Its level
     observation, retake count, redo and ladder stop read them, and its live level-mismatch finding
     reads `RunManifest.joined()`, the join a banked round's readers read.
  9. C1b removes no flat record key. The flat pose keys go with F1, which owns the one pose record.
     `level_db` and `stimulus_dbfs` are capture-basis identity and stay.
- **Consequences:**
  - A reader of a set's takes reads one record per take, fewer bytes than the copying rows held. A
    packet reads every take's record once; each contract-source read joins its kept takes.
  - Every round banked before this stops loading after its deploy: every round view, `jasper-round
    show` and the bank answer `take_curves_not_banked` with `field: record_id`, and `jasper-round
    list` shows its `sets` as `null`. The measurements page and `frequency <round>` read the take
    records without the manifest, so they still draw those rounds. Measuring again replaces them
    (#5926 session C).
  - The copy pin (`test_every_take_banks_one_record_shape`) asserts every row of a plain and a merged
    ladder manifest is exactly `{take_id, record_id, selected}`.
  - Rejected: a flat `takes` list with sets naming their member ids, which moves set identity off the
    executor; a `schema_version` gate or a new refusal code, since the missing field already names what
    the reader needs; the join inside `read_run_manifest` or `resolve_set`, which reads records a
    reader never reads.
