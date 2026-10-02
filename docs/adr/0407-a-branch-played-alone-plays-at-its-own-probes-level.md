# ADR-0407: A branch played alone plays at its own probe's level

- **Date:** 2026-10-02
- **Status:** Accepted. Amends [ADR-0403](0403-one-level-solver-at-every-pose-no-saved-volume.md) §3
  (the level a branch take plays, and which takes share a branch set's probes).
- **Context:** ADR-0403 §3 plays a branch take at one level for every segment: 6 dB under the lower
  of the levels its two branch probes find. That margin is for the sum, since two branches in phase
  read at most 6 dB over the louder one alone. At the front mark, a cardioid's rear woofer faces
  away and reads about 18.6 dB less than the front for the same drive. With one level for every
  segment, the rear-alone segment played about 25 dB under its own probe and could not be located
  (`locate_failed`; smoke test [#6113](https://github.com/jaspercurry/JTS/issues/6113), round
  `53eeb3df9b94`).
- **Decision:**
  1. **Each branch alone.** Each branch-alone segment of a branch take (each branch's sweep and its
     repeat) plays at the level its own probe solved. Its ceiling is the sum's stimulus rule (the
     base peak, less the take's scope backoff) and its own driver's cap under the run's fader, less
     the dynamic bass boost its output keeps (ADR-0359), so admission's gain + fader + reserve ≤
     cap holds for it. The take's spec carries the levels (`MeasureSpec.branch_levels_dbfs`, which
     only the executor sets) and each output's reserve (`MeasureSpec.bass_reserve_db`, which only
     the composition seam sets, from the take's own graph).
  2. **The sum.** Only the summed segment, with the pilots before the sweeps, plays 6 dB under the
     lower of the two levels, under the take's ceiling (the tightest cap of every driver), so the
     in-phase sum still reads at or under 80 dB.
  3. **Each placement probes what it plays.** A branch set is one placement: a branch take at
     another bearing or spot is a new set that probes both branches again, since a cardioid's rear
     reads very differently off axis. Its repeats and candidates at that placement share the probes
     (ADR-0406 §2). The preview prices each set's probes.
  4. **Retakes.** The take's assessment names its new peak, which may now be a branch alone. The
     executor moves each segment from what it played: a cut lowers every segment by what the peak
     moves, even one its ceiling held; a raise lifts each by that much but never over its own
     probe's level (the sum's: 6 dB under the lower), and lifts nothing when the peak played held at
     its ceiling. The rest of the set carries the branch levels with the sum. A set left unmeasured
     carries, for the sum and each branch, the last level solved for it, never above the last it
     played. A new placement or a Redo probes both branches again.
- **Consequences:**
  - Each branch alone now lands near its own probe's 80 ± 2 dB: 6 dB louder than before where the
    two probes agree, and up to the gap between them plus 6 dB louder where they do not. No segment
    plays above its own ceiling, and the sum keeps the take's.
  - A plan with several placements in one branch set (a custom `--poses` list, or the preset
    layouts' ±20° bearings) plays one more pair of probes per placement.
  - Each segment's level is in the banked program, and the analysis deconvolves each segment at its
    own gain (`segment_stimulus`); the locate's matched filter is scale-invariant.
  - A branch take's banked spec states its branch levels and reserves, so every banked spec, and so
    every request fingerprint, gains those fields.
  - What stays: each level comes from a probe (ADR-0361 §3), and the sum's coherent bound, the 85 dB
    stop and the driver caps do not change.
  - Rejected: a smaller margin for the whole take. The in-phase sum would read over 80 dB, and the
    rear alone would still play under its probe by the gap between the two probes. Also rejected:
    carrying one placement's branch levels to the set's other bearings; at 80° a rear that reads
    12 dB more there would play its alone segment about 11 dB over 80 dB.
