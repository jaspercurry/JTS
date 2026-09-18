# ADR-0326: The rear stage may boost, bounded, because the headroom charge pays for it

- **Date:** 2026-09-18
- **Status:** Accepted
- **Extends:** ADR-0322, ADR-0324, ADR-0325

## Context

The rear calibration vocabulary kept every chain at |H| ≤ 1: chain gain was an
attenuation only and Peaking/Lowshelf/Highshelf could only cut. That rule
predates ADR-0324, which made the stage's headroom charge its **realised
peak** — every chain evaluated as a complex response, the two rear branches
summed, the louder of that sum and the front chain taken across the evaluation
grid — and charged it pre-split into program headroom, capped by
`MAX_PROGRAM_HEADROOM_DB` (ADR-0219: never silently mute). After ADR-0324 the
cut-only rule guards nothing the charge does not already price.

Eight measurement rounds on jts3 (2026-09-18, #5330) showed why it costs: a
delay-and-invert pair loses forward output below about c/(4·spacing) ≈ 150 Hz
on this cabinet (the gradient loss). Cancelling the wall bounce and keeping
the forward level are the same act with different signs, and every rear-on
document sat 2 dB under the rear-muted trend near 100 Hz; driving the rear
harder (front branch −2/−4 dB) behaved like more cardioid — late energy and
the cardioid check improved — while the whole 90–350 Hz band lost 2–4 dB.
Commercial cardioid monitors pay this back with amplifier power. The owner
ruled (2026-09-18) that the experimental phase carries no rule that puts a
physically necessary control off the table.

## Decision

1. A rear chain's `gain_db` and a shelving or peaking filter's `gain` may be
   positive up to `rear_calibration.MAX_CHAIN_BOOST_DB` (6 dB). Below that the
   old floor stays (`MIN_CHAIN_GAIN_DB`); above it the document is refused.
2. The **only** guard is the existing headroom charge: the realised peak of
   the boosted stage is charged to program headroom by `program_headroom_db`
   (ADR-0324) and capped by `MAX_PROGRAM_HEADROOM_DB`. No second cap, no
   per-band rule, no "boost budget" vocabulary.
3. The published `rear` contract states the new bounds from the same
   constants (single source), and the playbook's first-tune recipe names the
   gradient loss and the boost that pays it back. The declared driver caps
   are respected where they always were: in the role chain's protection and
   limiter, downstream of the stage.

## Consequences

- A cardioid document can now be authored to keep the forward level it
  cancels away; the cost is loudness (program headroom), which the charge
  makes visible in `headroom_charge_db`, never safety.
- The `|H| ≤ 1` sentence in `rear_calibration.py` is gone; the vocabulary
  bounds that remain (resonant Q, all-pass Q, filter count, combo order) keep
  the stage evaluable on the headroom grid, which is their real job.
- Rejected: a separate boost budget or per-band cap (a second source of truth
  beside the charge); allowing boost only on the front chain (the pair needs
  it on both sides to keep the ratio); leaving the rule and compensating by
  attenuating every other band (it moves the woofer/tweeter blend).
- If a boosted document ever clips in practice, the incident goes to
  ADR-0324's grid or margin, not to a new cut-only rule.
