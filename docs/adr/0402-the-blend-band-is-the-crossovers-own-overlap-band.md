# ADR-0402: The blend band is the crossover's own overlap band

- **Date:** 2026-09-30
- **Status:** Accepted.

## Context

The blend contract read its band from the round receipt, `round_measurements.blend.band_hz`.
[#5862](https://github.com/jaspercurry/JTS/pull/5862) (#5660 part 2) retired the receipt's last
writer on 2026-09-26. Since then the band was always empty, the contract said `region_unavailable`,
and `read_blend_prescription` refused every blend document. No ticket named a new source for the
band. The owner chose the crossover's own overlap band
([#5925](https://github.com/jaspercurry/JTS/issues/5925), call D4 = A).

## Decision

1. **The band.** The blend contract's `bounds.band_hz` is the candidate's crossover overlap band,
   Fc ÷ 2 to Fc × 2 (`comparison_bands.overlap_band_hz`). Fc is the corner that
   `topology.candidate_topology` reads from the candidate the contract is built for.
2. **The trusted band.** The band is clipped to the gated trusted band of every kept MEASURE take
   whose pose names no driver (`trusted_band.within_trusted`; each band is read with
   `position_cycle.curve_band`). A take whose pose names one driver is a near-field take, and the
   clip leaves it out. A round with no such take keeps the full overlap band.
3. **No band.** A candidate with no crossover region gives no band, and the contract says
   `region_unavailable`, as before. When the clip leaves no band, the contract says
   `region_unavailable` with a detail: the overlap band and the trusted band. A gated curve banked
   without its band makes only the blend section a gap, with that refusal's code
   (`take_curves_not_banked`), as the room section does with its own refusal.
4. **The receipt.** The contract does not read the round receipt. Its `receipt` input, the
   `receipt` key of `prescription_sources` and the evidence packet's receipt read are deleted
   (#2902).

## Consequences

- A blend document judges again on a round whose candidate has a crossover. The other gates of the
  blend judge do not change.
- The contract digests move, because the blend band is now a value. A packet banked before this
  change for a candidate with a crossover reads `contract_current: false`.
- The band comes from the candidate that a document composes on. A document that also pins a new
  corner in its topology section is judged against the band of its base.
- Rejected: keep blend refused until a measured band exists (#5925 D4 = B). No ticket names a
  measured band, and the overlap band is the band that the ripple figure already reads.
