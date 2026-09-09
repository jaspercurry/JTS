# ADR-0272: The DECAY_SNAP lever is removed, so the cushion hold is not hardware-provable

- **Date:** 2026-09-09
- **Status:** Accepted. Supersedes
  [ADR-0214](0214-a-raised-cushion-target-is-a-declared-window-not-a-measurement.md)'s
  `DECAY_SNAP` consequence.
- **Context:** ADR-0214 recorded a `DECAY_SNAP <label>` fan-in control verb as
  the lever that forces the still-locked snap-back on demand, and said that
  without it the hold "could never be proven on hardware" because the window
  otherwise needs a real ladder demotion. The verb had no sender anywhere in
  the repo — not in `jasper/`, `deploy/`, `scripts/`, `experiments/` or
  `tests/` — and no checked-in proof procedure ever exercised it. It also
  replied in plain text on a socket whose other replies are JSON.
- **Decision:** the verb is deleted with `TrimControl` and the rest of the
  orphan control paths. An operator lever with no sender and no proof
  procedure is not a lever; it is code that has to be kept correct for free.
- **Consequences:** ADR-0214's window itself stands — the hold, its gauges
  (`resampler.decay.refilling`, `refill_force_clears`, `host_clock.hold`) and
  its `event=fanin.decay_refill` edges are unchanged, and the sole live
  trigger is still a ladder demotion out of L0. What no longer holds is the
  hardware-provability consequence: there is now no way to open a refill
  window on demand, so the window is exercised on hardware only when a real
  demotion happens, and in the test suite by the Rust unit tests that drive
  `CushionDecay` directly.

  ADR-0214's own removal condition (the snap-back becomes a slew inside the
  demand budget) is unaffected and still unmet.

  **Removal condition:** restore a forcing lever only alongside a checked-in
  procedure that runs it; recording the loss is cheaper than keeping an
  unexercised verb alive against a proof nobody performs.
