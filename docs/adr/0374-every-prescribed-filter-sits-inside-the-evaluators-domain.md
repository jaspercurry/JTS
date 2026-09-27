# ADR-0374: Every prescribed filter, in band or not, sits inside the evaluator's domain

- **Date:** 2026-09-27
- **Status:** Accepted. Supersedes (partial) [ADR-0367](0367-a-drivers-declared-band-bounds-a-boost-and-discloses-a-cut.md):
  its Decision 2 sentence "A cut inside its band is judged exactly as before", its pointer to
  `branch_chain._GRID_EDGE_*`, and its consequence that cuts at the emitter's extremes "were
  refused before, as outside the band, and they still are".
- **Context:** ADR-0367 held an out-of-band cut to 1-23,995.2 Hz and left every other filter to
  its role's band. A band's top is clamped at Nyquist (24,000 Hz), so filters at the emitter's
  extremes passed the per-driver door ([#5795](https://github.com/jaspercurry/JTS/issues/5795)):
  - an in-band cut at 23,999.9999 Hz and Q 1e6 amplifies f64 round-off to about -28 dBFS
    (ADR-0367's own figure);
  - an in-band boost about 1 mHz below Nyquist is not the filter the f64 evaluator reads. At
    +12 dB and Q 8 the evaluator reads 6.1 dB, while the same coefficients realize a 20 dB
    peak about 1e-5 Hz wide. The door's composed cap and the emitter's headroom charge then
    under-count it. `devices.volume_limit` stays 0.0 and the per-driver limiter still caps
    the output, so this is an accounting hole, not a level path.
- **Decision:** Every filter the per-driver door reads, cut or boost, in band or not, sits
  within `jasper.biquad.EVALUABLE_HZ_MIN`-`EVALUABLE_HZ_MAX` (1-23,995.2 Hz). Past that it
  refuses as `driver_filter_malformed`, the code the out-of-band cut already used. The two
  edges and `RESPONSE_NYQUIST_HZ` are public in `jasper/biquad.py`, beside the evaluator and
  `EVALUABLE_Q_*`; the door and `branch_chain` read them there. The response format's
  `freq_must_be_inside` states the rule. The band rules for a boost are unchanged.
- **Consequences:**
  - The door only refuses more. Old and new doors were run on 14,080 edge cases and 4,000
    two-filter documents: nothing is newly admitted, and every new refusal is a filter outside
    1-23,995.2 Hz. No filter record in the repo fixtures or the laptop's `captures/` corpus
    (291 records in 91 driver-prescription files) sits outside it.
  - The served contract text changed, so the contract digests and the fingerprint of a packet
    built on read move. A banked round keeps the fingerprint its bank stored (ADR-0371).
  - Not converged: the `((), 0.0)` default for a role with no crossover region, in three
    places. With the real emitter, a one-way preset with a +4 dB boost charges -5.0 dB of
    headroom at any trim today; converging would charge -5.0, -3.0 and 0.0 dB at trims of
    0, -2 and -6 dB, and would add `full_range` to every one-way contract's `boost_headroom`.
    Today's charge errs on the safe side, so the copies stay. The door and the emitter
    also disagree on a one-way pinned trim; that choice is
    [#5909](https://github.com/jaspercurry/JTS/issues/5909).
  - Rejected: clamping each band's top at 23,995.2 Hz instead. It closes the same hole but
    moves every published band, and a declared band is a datasheet fact, not an evaluator
    limit.
