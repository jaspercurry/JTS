# ADR-0390: There is no discrete "before"

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial)
  [ADR-0345](0345-a-timing-reading-that-is-not-comparable-never-asks-for-a-reset.md)'s
  consequence note on `entry_grade` and the `entry_baseline` series.
- **Context:** A round kept a "before": the evidence packet's `entry_baseline` block, a copy in the
  flow state (`verify_priors.entry_baseline`), the `jasper-round-views entry` grade, and a "Before
  correction" series on the measurements page. The block's reader accepted only the retired engine's
  take shape, so every current round's block read `not_evaluated` and nothing drew or graded it
  ([#5919](https://github.com/jaspercurry/JTS/issues/5919)). The owner ruled that no discrete
  "before" is needed ([#5737](https://github.com/jaspercurry/JTS/issues/5737) RX-3 P4).
- **Decision:**
  1. No take, packet block, flow-state copy, view or page series is kept as a round's "before".
     [ADR-0203](0203-the-incumbent-tune-retires-recommissioning-is-structure-first.md) §4's
     campaign baseline, which a campaign opens by measuring per ADR-0192, is untouched.
  2. A comparison with an earlier take picks its comparand by #5737 P6's one comparand rule, which
     owns it.
  3. The ADR-0319 timing take stays. MEASURE reads its summed alignment from the session's timing
     take, and nothing persists that prior.
  4. ADR-0228 S3 holds: every take keeps its recording and impulses (ADR-0354), and its curves
     (ADR-0383), so any earlier take stays comparable.
- **Consequences:**
  - A packet built from now on has no `entry_baseline` block. A stored packet keeps its block, and
    nothing reads it.
  - The measurements page draws each take's own curves, and a run's metadata still names the
    bundle's build, topology and microphone calibration.
  - No `jasper-round-views` verb reads a round's packet any more; `evidence_not_banked` still
    refuses in the packet reader and the prescriber.
  - The dead benefit code of the retired engine goes with it: the benefit margin and plateau, and
    the stage-1 plan that ended on the "before".
  - Rejected: #5919 option A, a durable "before" for current rounds. Every take already banks its
    curves (ADR-0383), so a second copy of one take's curve would serve only a comparison the P6
    rule makes with any earlier take.
