# ADR-0425: The rear seed is computed from the declared geometry

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes in part
  [ADR-0317](0317-wall-placement-starts-at-the-cabinet-back.md) line 27, "Neither a cabinet gap nor a
  baffle estimate becomes a cancellation delay", and
  [ADR-0318](0318-rear-calibration-separates-acoustic-targets-from-electrical-settings.md) lines 22–23,
  "No gap-to-delay conversion, wall gain or cardioid bandwidth change is inferred", with ADR-0318's
  diagnostic seed (lines 58–61).
- **Context:** The contract's rear seed was one CAD snapshot: a 0.2032 m gap and a cancellation branch
  of −0.84 dB, inverted, 1.14 ms, with no band-limiting filters, shipped muted. A first rear tune had to
  vary `rear_muted=false` to hear it, and its gap, stamped as fitted, drew a wall-gap warning against a
  200 mm declaration (F12 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md)). The owner decided that
  the cardioid is computed from geometry (decision 2 of
  [#6227](https://github.com/jaspercurry/JTS/issues/6227), step B2). Step B1 declares the inputs.
- **Decision:**
  1. **Inputs.** `d` is the declared rear woofer spacing (`manual_settings.rear_woofer_spacing_mm`).
     `D` is the front panel's distance to the wall: the back gap plus the cabinet depth projected on
     the wall normal, as ADR-0317 derives it (`DeclaredGeometry.boundary_walls`). `c` is 343 m/s.
  2. **Cancellation branch.** Inverted, with a 4th-order Linkwitz-Riley high-pass at `c/(6D)` and an
     8th-order Butterworth low-pass at `c/(4d)`. Its delay is `r·d/c` minus that low-pass's group delay
     at the band centre (the geometric mean of the two corners), with `r` = 0.6, a supercardioid. A
     negative relative delay is realized with `common_delay_ms`.
  3. **Bass branch.** In phase, with a 4th-order Linkwitz-Riley low-pass at `c/(6D)`: ADR-0325's
     complementary hand-over.
  4. **Trim.** One flat trim on both rear branches, a Lowshelf at 16 kHz (`rear_calibration.flat_shelf`,
     the boost form of ADR-0327): minus the rear-minus-front level that the round's banked pair view
     reads at the mark over the band's third octaves, with a +6 dB cap. With no pair view in the round,
     the trim is 0 dB, and the seed's `conditions` say so (`pair_round: null`).
  5. **Missing declarations.** With `d`, the back gap, the depth or the toe-in undeclared, no seed is
     computed. The contract's `seed` is then the gap `rear_seed_geometry_undeclared`, which names each
     missing declaration. No default geometry is used.
  6. **The seed ships unmuted.** It is a design from this speaker's own declarations, and every document
     still goes judge → compose → trial → apply. A muted seed previews nothing. The same section with
     `rear_muted: true` stays the muted reference.
  7. **Where.** `jasper/active_speaker/rear_seed.py` computes it. `contract --round <round> --section rear`
     reads that round's banked draft, geometry and pair view. A contract with no round reads no
     declaration, so its seed is the gap.
- **Consequences:**
  - Kept: the one-clock pair take (ADR-0386), the superposition preview (ADR-0325), the chain gain
    ≤ 0 dB and +6 dB per filter (ADR-0326, ADR-0327), the front guard and the topology gate. The seed is
    a starting point that the seat trial judges (#6227 decision 3); `--vary` stays for when it misses.
  - On a jts3-like cabinet (`d` = 0.33 m, `D` = 0.5 m) the band is 114–260 Hz and the net delay at its
    centre is 0.58 ms: the branch delay is −3.29 ms and the common delay 3.29 ms. The stage's sum stays
    at unity, so the seed costs no program headroom.
  - The seed's `geometry` banks the declared gap, so the seed draws no wall-gap warning.
  - At a wall, the trim includes both woofers' wall reflections and their path difference to the mark;
    a pair take away from walls reads the gap cleanest.
  - The prompt's `--vary rear_calibration.rear_muted=false` step goes.
  - The packet's contract digests and per-set limits are built from sources that carry no geometry, so
    their seed is the gap; the contract command carries the computed seed.
  - Rejected: making the branch's phase exact at the wall notch. In a one-wall image model at a 2 m seat
    it moved a hole to the hand-over and left the seat rougher than this rule, which also matches the
    pattern ratio measured on jts3 (playbook, Rear).
