# 0212 — Way-1 reuses the existing layers; it does not fork them

Date: 2026-09-01. Status: accepted.

## Decision

A `full_range_passive` (way-1) speaker joining the crossover_v2
recommissioning loop fits inside a 2-way speaker's existing layers, not new
ones of its own:

- **Its linearization compiles through the baseline candidate layer** —
  headroom → linearization → gain → limiter, the same emitter and receipts a
  2-way branch gets — **never** through the `/sound` preference-EQ slots
  (user taste on top of a tuned speaker; a way-1 round's filters are the tune
  itself).
- **No per-role trim exists.** A base trim is a FRAME — one role's level
  relative to the others — and way-1 declares one role. The write path
  refuses `base_trim_no_frame` rather than bank a vacuous
  `{"full_range": 0.0}`, indistinguishable from a levelled speaker; the apply
  seam recognizes the same fact off the applied preset's own way count and
  leaves any standing record alone under that name, so a way-1 apply never
  reports a topology property as a failed write.
- **The passive network is its own protection.** `full_range` gets no active
  `min_highpass_hz`, taking the stricter of the woofer/tweeter floor-test and
  level figures. Its declared `recommended_highpass_hz` stays optional and
  bounds only the excitation sweep floor — absent, it refuses by name.

## Why

The alternative at each point was a parallel mechanism sized for one role: a
second compile lane, a synthetic two-role trim frame, a fabricated high-pass.
Each would double a surface that already generalizes (the candidate compiler
reads roles off banked state, not a literal pair) or invent a number nothing
measured. Reusing the layer, refusing by name where a step has no referent,
keeps the honesty property without a way-1-specific path.

## Consequences

- A way-1 receipt reads like a 2-way one with fewer populated fields: same
  candidate layer, same headroom/gain/limiter keys, and no trim pair or
  `level_match.base_trim` block where a 2-way receipt carries one. The reason
  is named where the decision is made — the apply seam journals
  `event=dsp.baseline_base_trim_banked result=left_standing
  reason=base_trim_no_frame` — rather than published as a receipt row, because
  nothing was written for a row to describe.
- A future single-role or degenerate-topology speaker gets the same
  treatment by construction: fit the existing layer, refuse by name.
