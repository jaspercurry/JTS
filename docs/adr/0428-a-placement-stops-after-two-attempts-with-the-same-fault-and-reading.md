# ADR-0428: A placement stops after two attempts with the same fault and reading

- **Date:** 2026-10-02
- **Status:** Accepted. Completes [ADR-0422](0422-a-placement-gets-two-extra-takes-and-a-probe-at-its-ceiling-stops.md):
  the repeat stop that it left to the executor.
- **Context:** Finding F10 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): retakes that cannot
  help keep playing. A failed room round played 12 of 12 takes that failed the same way
  (`pilot_level_collapse`) at the same level. ADR-0422 cut a placement to two charged plays after
  its first. [#1873](https://github.com/jaspercurry/JTS/issues/1873) and
  [ADR-0183](0183-the-verify-repeat-floor-is-twice-a-measured-consecutive-pair-p95.md) set the idea:
  two attempts that agree are a finding, not a transient error. The owner's plan for
  [#6227](https://github.com/jaspercurry/JTS/issues/6227): take two, compare, and take a third only
  if they disagree.
- **Decision:**
  1. A placement's ledger (`SlotAttempts` in `crossover_v2/admission.py`) keeps the last take's
     refusal when the take asks for a retake at its own level (`retake_same` or `fix_and_retake`):
     its fault and its reading. The reading is the take's level in dB SPL (`level_db_spl`) when it
     has one, else the peak of its located stimuli in dBFS (`peak_dbfs`).
  2. A take refused with that fault, and with a reading within `SAME_POSE_DRIFT_DB` (2 dB, the
     same-pose level tolerance of `capture_dispatch.py`) of that reading, spends the placement. The
     run then takes the path of a spent cap, unchanged: a capture-quality fault leaves the take
     unmeasured and the run moves on, except at CHECK and at the run's probe; any other fault stops
     the run.
  3. A level retake (`retake_louder`, `retake_quieter`) changes the level on purpose. It never
     counts, and it starts a new pair. A kept take and a take with no reading also start a new
     pair. A `replay` charge, the play after a level probe, stays free and keeps the pair, so a
     placement's probe does not hide a repeat of its take.
- **Consequences:**
  - This change only removes plays. No level, ramp bound, stop, clamp or driver cap changes.
  - A placement whose first two attempts fail alike plays two attempts, not three. Where it levels
    itself, an attempt is its probe and its take.
  - The pair holds across a placement's takes and across a redo, until a kept take ends it. A redo
    at a pose that levels itself starts a new ledger (ADR-0361).
  - The reading is the signal, not the room's noise. If the household makes the room quieter but
    the signal reads the same, the second refusal still stops the placement. The owner's "take two,
    compare" accepts this.
  - A transport fault, such as `capture_overrun`, reads the same signal on each attempt. Two in a
    row also stop the placement, where a third attempt could have passed.
  - The budget payload loses `automatic_left` and `automatic_allowed`. Nothing read them.
  - Rejected:
    - Comparing with the placement's first refusal. ADR-0183 compares consecutive attempts, because
      a fixed baseline drifts.
    - A reading for each fault, such as an SNR for an SNR fault. It adds machinery, and the cap of
      two bounds what it can save.
    - Counting level retakes. Their next play is at a different level, so it can change the answer.
