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
     at the band centre (the geometric mean of the two corners), with the owner's ratio `r` = 0.6. A
     negative relative delay is realized with `common_delay_ms`.
  3. **Bass branch.** In phase, with a 4th-order Linkwitz-Riley low-pass at `c/(6D)`: ADR-0325's
     complementary hand-over.
  4. **Trim.** One flat trim on both rear branches, a Lowshelf at 16 kHz (`rear_calibration.flat_shelf`,
     the boost form of ADR-0327): minus the rear-minus-front level that the round's pair view reads at
     the mark over the band's third octaves, with a +6 dB cap. With no pair view in the round, the trim
     is 0 dB, and the seed's `conditions` say so (`level_gap_db: null`).
  5. **Missing declarations.** With `d`, the back gap, the depth or the toe-in undeclared, no seed is
     computed. The contract's `seed` is then the gap `rear_seed_geometry_undeclared`, which names each
     missing declaration. No default geometry is used.
  6. **The seed ships unmuted.** It is a design from this speaker's own declarations. The compose and
     apply doors are the gates every document passes; an apply needs no trial (#6227 rule 6). A muted
     seed previews nothing. A trial's muted reference is an explicit copy of the section with
     `rear_muted: true` (step B3 builds it in).
  7. **Where.** `jasper/active_speaker/rear_seed.py` computes it from the round's contract sources: its
     banked draft, its declared geometry and its rear views (the bank's copy, or the view file while the
     bank writes the packet). So `contract --round <round> --section rear`, the packet's per-set limits
     and its stored contract digest carry one seed. A contract with no round reads no declaration, so
     its seed is the gap.
- **Consequences:**
  - **This rule does not give a supercardioid in free field.** The delay subtracts only the low-pass's
    group delay, and a group delay is not a phase delay: the hand-over high-pass leads +118° at the band
    centre and stays in the branch. In a free-field two-point model with the repo's filter evaluator, on
    a jts3-like cabinet, the rear-to-front phase at 172 Hz is −65.6° where an ideal `r` = 0.6 pair needs
    +144.2°. Front minus back is −4.1, −6.2, −14.0 and −12.6 dB at 160, 172, 200 and 230 Hz (the back is
    louder), and on axis at 200 Hz the seed is 9 dB under the front woofer alone. In a one-wall image
    model at a 2 m seat, the seed lifts the wall notch from −8.0 to −5.7 dB, and its roughness
    (80–350 Hz, against a one-octave trend) is 2.2 dB against 1.4 dB with the rear muted. The seat trial
    judges the seed (#6227 decision 3), and `--vary` stays for when it misses.
  - Kept: the one-clock pair take (ADR-0386), the superposition preview (ADR-0325), the chain gain
    ≤ 0 dB and +6 dB per filter (ADR-0326, ADR-0327), the front guard and the topology gate.
  - On a jts3-like cabinet (`d` = 0.33 m, `D` = 0.5 m) the band is 114–260 Hz, the net delay at its
    centre is 0.58 ms, and the branch delay is −3.29 ms. So the seed adds 3.29 ms of common delay to the
    front, rear and tweeter outputs: latency, with the woofer/tweeter timing unchanged. Without a boost
    the stage's sum stays at unity, so the seed costs no program headroom; the charge prices a boost.
  - The seed's `geometry` banks the declared gap, so the seed draws no wall-gap warning.
  - At a wall, the trim includes both woofers' wall reflections and their path difference to the mark;
    a pair take away from walls reads the gap cleanest.
  - The rear contract changed, so a round banked on a cardioid build before this change reads
    `contract_current: false`.
  - The prompt's `--vary rear_calibration.rear_muted=false` step goes.
  - Rejected: making the branch's phase exact at the wall notch. In the same seat model it opened a
    −8.7 dB hole at 120 Hz and read 3.4 dB rough. The rule kept here matches the pattern ratio that the
    jts3 seat sweep measured (playbook, Rear).
