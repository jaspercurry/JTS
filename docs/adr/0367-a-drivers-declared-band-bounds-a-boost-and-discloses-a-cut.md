# ADR-0367: A driver's declared band bounds a boost; a cut outside it is admitted and disclosed

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes (partial) [ADR-0207](0207-tier-1-prescription-bounds-demote-a-cut-is-the-prescribers-to-spend.md):
  its consequence that "a prescribed cut's DEPTH is bounded by the declared band".
- **Context:** The per-driver prescription door (`crossover_v2/driver_prescription.py`) refused a
  cut outside its role's declared band, and any cut on a speaker that declares no band
  ([#5666](https://github.com/jaspercurry/JTS/issues/5666), review finding R2-F14). The band
  guards a driver against excitation (non-negotiable 2), and only a boost adds excitation. A cut
  removes level, and a cascade of cut biquads has |H| <= 1. So the refusal blocked legitimate cuts
  for no protection: a woofer breakup above its protective low-pass, or a tweeter resonance below
  its protective high-pass. The band did one more job, unstated: it kept every cut inside the
  frequencies where `EVALUABLE_Q_MAX`'s fidelity argument holds, and below the Nyquist corner the
  emitter refuses.
- **Decision:**
  1. The declared band bounds a boost only. Every boost refusal stands byte for byte:
     - `driver_filter_outside_passband` for a boost outside its role's band;
     - `driver_passband_unavailable` for any document with a boost on a speaker that declares no
       band;
     - `driver_role_unknown` for a role with no band while other roles declare one.
  2. A cut outside its role's band, or on a speaker that declares no band, is admitted. The
     receipt counts it as `cuts_outside_passband`. It must still sit within 1-23,995.2 Hz, the
     domain every chain peak is taken over (`branch_chain._GRID_EDGE_LO_HZ` and
     `_GRID_EDGE_HI_HZ`); past that it refuses as `driver_filter_malformed`. A cut inside its band
     is judged exactly as before.
  3. When the speaker declares no band, the speaker's branches (`branch_context`) decide whether a
     cut's role, or its trim pin's, is the speaker's. A role outside them refuses as
     `driver_role_unknown`.
- **Consequences:**
  - A prescribed cut is bounded by the per-role filter slots, the evaluable Q range and the
    evaluator's domain, not by the declared band.
  - A mixed-sign document keeps every boost check:
    - `_check_bounds` applies the band and the Q ceiling to each boost.
    - `_check_composed` evaluates each named role's whole cascade, cuts included.
    - `boost_headroom_by_role`, called in `read_driver_prescription`, charges the whole proposed
      program and refuses `driver_composed_boost_exceeded` past `MAX_PROGRAM_HEADROOM_DB`.
    - A cut can only lower either figure.
  - Why the domain edges. These figures are measured with the repo's `biquad_coeffs` at Q 1e6,
    the admitted ceiling, on a -3 dB Peaking cut:
    - At the domain's edges, 1 Hz and 23,995.2 Hz, the recursion amplifies f64 round-off at most
      about 4e8 times, which is about -146 dBFS and under the 24-bit floor.
    - At 1e-4 Hz and 23,999.9999 Hz (the extremes the emitter's `%.4f` spells) it amplifies about
      4e14 times, which is about -28 dBFS. The standard stability check (|a2| < 1 and
      |a1| < 1 + a2) still passes those coefficients.
    - The emitter refuses any corner at or above Nyquist.
    - Cuts at those frequencies were refused before, as outside the band, and they still are.
  - The prescriber's contract now says this: `freq_must_be_inside`, the boost refusals, and the
    packet's note for a speaker with no declared band. The generated tuning-doc text
    (`cuts_are_free`) needed no change.
  - Rejected:
    - Admitting a cut at any frequency above 0 Hz (the figures above).
    - Keeping the band for cuts: it guards no mechanism (R2-F14).
    - Admitting a cut on a role that has a branch but no band while other roles declare bands:
      not asked for, and such a role still refuses by name for either sign.
