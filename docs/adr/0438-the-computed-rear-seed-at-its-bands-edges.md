# ADR-0438: The computed rear seed at its band's edges

- **Date:** 2026-10-03
- **Status:** Accepted. Amends
  [ADR-0425](0425-the-rear-seed-is-computed-from-the-declared-geometry.md) lines 29–30, "With no pair
  view in the round, the trim is 0 dB, and the seed's `conditions` say so (`level_gap_db: null`).", and
  lines 31–33, "With `d`, the back gap, the depth or the toe-in undeclared, no seed is computed. The
  contract's `seed` is then the gap `rear_seed_geometry_undeclared`, which names each missing
  declaration."
- **Context:** ADR-0425 puts the seed's cancellation band between "a 4th-order Linkwitz-Riley high-pass
  at `c/(6D)` and an 8th-order Butterworth low-pass at `c/(4d)`" (lines 21–22). PR #6271 and its review
  (#6227, step B2) found three edges of that band that the seed and its readers did not handle:
  1. With the front panel at or nearer the wall than `2d/3`, the high-pass is at or above the low-pass.
     The band is empty, and the seed shipped a stage that cancels nothing.
  2. The trim reads the pair view's third-octave rows whose centres lie inside the band, and those
     centres stop at 200 Hz (`THIRD_OCTAVE_BASS_BANDS_HZ`). For `d` = 0.33 m, every front distance from
     0.22 m to about 0.286 m gave a band with no row, so the seed read `level_gap_db: null`, which
     ADR-0425 gives one meaning: no pair view.
  3. A pair view read its arrival gap on the applied rear stage's band, and `arrival_gap_ms` reads no
     band narrower than `ARRIVAL_GAP_MIN_BAND_HZ` = 200 Hz (`rear_evidence.py`). The jts3 seed's band is
     114–260 Hz (146 Hz wide), so every pair round banked with that seed applied would read
     `arrival_gap {ms: null, reason: coverage_short}`, with no polarity and no gradient residual.
- **Decision:**
  1. **An empty band is a named gap.** When `c/(6D)` is at or above `c/(4d)`, each rounded as the seed
     writes it (4 decimals), no seed is computed. The contract's `seed` is the gap
     `rear_seed_band_empty`, so `rear_seed_geometry_undeclared` is one of two gaps. The detail gives both
     corners (`handover_hz`, `lowpass_hz`), `front_panel_to_wall_m` (`D`) and `rear_woofer_spacing_m`
     (`d`). The household copy names both declarations, because a mistyped spacing also lands here. It
     says that the front of the cabinet must be more than two thirds of the spacing from the wall, and
     that the woofer pair must be measured again after a change: a banked round keeps the draft and the
     geometry it was measured with.
  2. **A null level gap says why.** The seed's `conditions` carry `level_gap_reason` beside
     `level_gap_db`: `no_pair_take` when no rear view of the round has a pair section,
     `no_pair_row_in_band` when a pair view has no row at the mark inside the band, and `""` when the gap
     is read. In both null cases the seed still ships, with a 0 dB trim.
  3. **The arrival gap reads a band its reader accepts.** A pair view reads its arrival gap on the
     applied rear stage's band only when that band is at least `ARRIVAL_GAP_MIN_BAND_HZ` wide. Otherwise
     it reads on `ARRIVAL_GAP_BAND_HZ` (90–315 Hz). Either band is clipped to the sweep's coverage, and
     `arrival_gap_band_source` says `rear_document` or `default`.
- **Consequences:**
  - With the jts3 seed applied, a pair round reads its arrival gap again, on 90–315 Hz, so the polarity
    and the gradient residual, which need a confident gap, can read too.
  - The rear contract changed (the seed's `conditions` have one more field), so a pair round banked
    before this change reads `contract_current: false`.
  - Rejected: refusing the seed when its pair view has no row inside the band. The seed's filters are
    valid without a trim, and 0 dB is the trim it has with no pair take.
  - Rejected: lowering `ARRIVAL_GAP_MIN_BAND_HZ` to fit the seed's band. The correlator's search window
    gets wider as the band gets narrower (2000 ms divided by the width in Hz), and the minimum keeps it at
    10 ms or less.
