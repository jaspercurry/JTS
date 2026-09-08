# Active Speaker DSP

Speaker tuning, room correction, and preference voicing answer different
questions. The [doctrine](../../../../docs/measurement-loop-doctrine.md#1a-the-layering-rule--what-a-measurement-plays-through)
owns their graph boundaries. The [runbook](../../../../docs/tuning-operator-runbook.md#entry-contract)
is the current entry; tool help owns supported calls and physical limits.

## Complementary Measurements

- A solo capture reveals one driver's response. Near-field placement can
  reduce room influence, but its useful band and relation to the full speaker
  require care; it does not by itself establish the far-field sum.
- A reverse null can test delay and polarity near a crossover. Branch level
  mismatch limits its depth. Read the [alignment guidance](../../../../docs/tuning-methodology.md#4-time-alignment)
  before treating a shallow null as a timing error or a deep null as proof of
  the best alignment.
- A gated summed capture tests how drivers combine above the window's valid
  frequency floor. Complex solo responses can predict a sum; adding only their
  magnitudes cannot establish delay or polarity effects.

These measurements are options for different questions, not required steps.
Below the gate's validity floor, disclose the unverified band. A single in-room
seat cannot separate room modes from the speaker's response. See the
[low-frequency guidance](../../../../docs/tuning-methodology.md#9-below-the-gate-floor)
for the limits of that evidence and the current toolbox scope.

## Sources

- [Active-speaker research archive](../../../../docs/research/2026-05-25-calibration-agent/README.md)
- [Alignment and structure research](../../../../docs/research/2026-08-31-tuning-methodology-deep-research/04-structure-alignment-and-automation-prior-art.md)
- [Gating and low-frequency research](../../../../docs/research/2026-08-31-tuning-methodology-deep-research/03-gating-windowing-and-low-frequency-truth.md)
