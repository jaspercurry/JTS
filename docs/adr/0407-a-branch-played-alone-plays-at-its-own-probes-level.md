# ADR-0407: A branch played alone plays at its own probe's level

- **Date:** 2026-10-02
- **Status:** Accepted. Amends [ADR-0403](0403-one-level-solver-at-every-pose-no-saved-volume.md) §3
  (the level a branch take plays).
- **Context:** ADR-0403 §3 plays a branch take at one level for every segment: 6 dB under the lower
  of the levels its two branch probes find. That margin is for the sum, since two branches in phase
  read at most 6 dB over the louder one alone. At the front mark, a cardioid's rear woofer faces
  away and reads about 18.6 dB less than the front for the same drive. With one level for every
  segment, the rear-alone segment played about 25 dB under its own probe and could not be located
  (`locate_failed`; smoke test [#6113](https://github.com/jaspercurry/JTS/issues/6113), round
  `53eeb3df9b94`).
- **Decision:**
  1. **Each branch alone.** Each branch-alone segment of a branch take (each branch's sweep and its
     repeat) plays at the level its own probe solved, never above the take's ceiling: the composer
     clamps it where it clamps the take's sum. The take's spec carries both levels
     (`MeasureSpec.branch_levels_dbfs`, which only the executor sets).
  2. **The sum.** Only the summed segment, with the pilots before the sweeps, plays 6 dB under the
     lower of the two levels, so the in-phase sum still reads at or under 80 dB.
  3. **Retakes and the set.** A level retake moves every segment by the same amount: the take's
     assessment names its new peak, and the sum and each branch move by what that peak moves. The
     rest of the set carries the branch levels with the sum. A set left unmeasured carries the
     levels its take last played, turned down with its sum to the last level solved for it. A new
     placement or a Redo probes both branches again.
- **Consequences:**
  - Each branch alone now lands near its own probe's 80 ± 2 dB: 6 dB louder than before where the
    two probes agree, and up to the gap between them plus 6 dB louder where they do not. No segment
    plays above the take's ceiling.
  - Each segment's level is in the banked program, and the analysis deconvolves each segment at its
    own gain (`segment_stimulus`); the locate's matched filter is scale-invariant.
  - A branch take's banked spec states its branch levels, so every banked spec, and so every request
    fingerprint, gains that field.
  - What stays: each level comes from a probe (ADR-0361 §3), and the sum's coherent bound, the 85 dB
    stop and the driver caps do not change.
  - Rejected: a smaller margin for the whole take. The in-phase sum would read over 80 dB, and the
    rear alone would still play under its probe by the gap between the two probes.
