# ADR-0326: A rear-stage filter may boost, bounded, because the headroom charge pays for it

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

1. A Peaking, Lowshelf or Highshelf filter's `gain` in any rear-stage chain
   may be positive up to `rear_calibration.MAX_CHAIN_BOOST_DB` (6 dB); above
   it the document is refused. A chain's flat `gain_db` stays an attenuation
   (`MIN_CHAIN_GAIN_DB` … 0): it is emitted as a post-split `Gain` step, and
   the runtime door refuses any positive post-split `Gain`
   (`runtime_contract._unsafe_post_split_gains`, blocker
   `active_output_gain_positive`), so a flat boost would be authored, banked
   and then refused on the speaker. A rear weight above 1 is written as front
   attenuation plus the band boost — the ratio is what matters.
2. Two guards, with a stated boundary. The headroom charge prices the
   realised peak of the whole stage pre-split (`program_headroom_db`,
   ADR-0324, capped by `MAX_PROGRAM_HEADROOM_DB`) and is the guard the filter
   boost sits under: the post-split rail inspects `Gain` steps only, never a
   `Biquad`'s gain, so a band boost is priced by the charge alone. The rail
   keeps every post-split `Gain` non-positive; this ADR does not touch it. No
   third cap, no per-band rule, no "boost budget" vocabulary.
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
- Positive filter gains sharpen the branch sum's extrema, so the charge's
  grid residual grows a little: measured worst case 0.22 dB under the truth
  on 500 random in-bounds documents (0.016 dB on targeted worst cases),
  inside the −1 dBFS soft-clip margin of the emitted graph. If a boosted
  document ever clips in practice, the incident goes to ADR-0324's grid or
  margin, not to a new cut-only rule.
- `scripts/fit-rear-branches.py` still caps its own search at +3 dB and
  shifts every chain to ≤ 0: the fitter is a separate lane and does not yet
  author what this ADR allows.
