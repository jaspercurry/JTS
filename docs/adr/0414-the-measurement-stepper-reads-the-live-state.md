# ADR-0414: The measurement stepper reads the live state

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes (partial)
  [ADR-0410](0410-the-conductor-keeps-no-history-that-nothing-reads.md): its Decision line that the
  status `crossover_v2` block keeps four fields. It now keeps two (`failure`, `needs_recovery`).
- **Context:** No product code wrote a round receipt. Plans make only CHECK, TIMING, MEASURE and
  LATERAL takes, so the cloud, APPLYING, REVIEW and DONE phases never ran, and only a CHECK take was
  ever accepted. The stored journey (`accepted_phases`, `session_phases`, `applied`) fed only the
  stepper, through `crossover_v2_phase`. The owner picked a live stepper on
  [#5925](https://github.com/jaspercurry/JTS/issues/5925) (comment 5945693122).
- **Decision:**
  1. The measurement page's stepper is built from the setup state and the live capture status. The
     setup state places "Protected speaker setup". With no live run, "Microphone check" is the step;
     a live run is "Measure"; a run that ended (complete, stopped or failed) is "Done".
  2. The stepper follows one run. An applied tune changes no step: the `applied` chip, read from the
     setup state, shows it.
  3. No stored journey, no stored `applied` flag and no round receipt or series ordinal remain.
     Start Over deletes the v2 state file under its lock.
- **Consequences:**
  - The page reads `steps` and `screen` as before. The envelope (schema 20) drops `phase`, `progress`
    and `round_ordinal`; the status block drops `phase` and `round_receipt`.
  - A run's state carries a candidate only from its own run. A banked round is named by its session
    id.
  - Resurrect condition: a product reader that needs a stored phase or a receipt. A test is not one.
