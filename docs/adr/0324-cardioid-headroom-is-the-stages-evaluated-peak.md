# ADR-0324: The cardioid headroom charge is the stage's evaluated peak

- **Date:** 2026-09-17
- **Status:** Accepted
- **Supersedes:** the **Headroom** paragraph of
  [ADR-0322](0322-rear-calibration-is-a-candidate-section.md); the rest of
  ADR-0322 stands.
- **Context:** ADR-0322 charged the rear stage as
  `max(0, 20·log10(10^(bass_db/20) + 10^(cancel_db/20)))` — both branches
  wide open and in phase at every frequency. The compiled stage never does
  that: the bass branch low-passes, the cancellation branch high-passes and
  inverts, and their bands barely overlap. On jts3's own fitted document the
  formula charged 5.372 dB against a realised summed peak of 0.194 dB, so the
  whole program ran 5.2 dB quieter than the graph required.
- **Decision:** the charge is the compiled stage's evaluated peak, and it must
  be an honest UPPER BOUND on what the emitted graph can do. Each chain
  (front, bass, cancellation) is evaluated as the complex response of its
  gain, polarity, delay and filters; the two rear branches are summed as
  complex numbers; the charge is `max(0, front peak, rear sum peak)`. A muted
  chain contributes nothing and `rear_muted` silences the sum. The front chain
  is charged in EVERY `rear.mode` — a `fir` rear's taps are the one thing left
  unmodelled, and only because the candidate boundary refuses `fir` in v1.
  One evaluator does the filter maths (`branch_chain.camilla_filter_response`,
  over the shared RBJ biquad coefficients
  `jasper.sound.profile._biquad_coeffs`): an RBJ `Allpass` is `2·Notch − 1`
  over that same denominator, and a `Butterworth*` combo is
  `branch_chain.butterworth_response`, which a Linkwitz-Riley section already
  cascades twice. That evaluator now honours a shelf's declared `q` instead of
  forcing `SHELF_Q`, because `compile_rear_stage` emits the document's `q`
  verbatim and CamillaDSP obeys it (a q-1.0 shelf overshoots 0.782 dB, and the
  document admits sixteen per chain: 12.512 dB). The grid is
  `branch_chain.camilla_evaluation_grid` — `CHAIN_GRID_HZ` unioned with every
  filter's centre, its shelf asymptote, and neighbours at 1/48, 1/24 and 1/12
  octave either side — and `Allpass` q is bounded at
  `rear_calibration.MAX_ALLPASS_Q` (10) so no phase rotation is narrower than
  that grid resolves. The chain-gain ≤ 0 dB bound and the
  `MAX_PROGRAM_HEADROOM_DB` ceiling are unchanged.
- **Consequences:** a cardioid speaker keeps the ~5 dB of program the old
  bound spent, and a document whose branches genuinely do overlap is still
  charged what it costs — the charge now tracks the fit instead of ignoring
  it. The ≤ 6.02 dB slack ADR-0322 gave the runtime contract's linearization
  allowance no longer holds as a number (a chain may stack resonant
  high/low-passes), so that allowance is now bounded only by
  `MAX_PROGRAM_HEADROOM_DB`; it stays generous, never tight. The charge is a
  STEADY-TONE bound on a finite grid: overshoot between grid points and
  transient overshoot stay backstopped by the per-output soft-clip limiter,
  as they already are for the linearization charge. Evaluating a full
  80-filter document costs ~120 ms on the laptop, paid at emit, never in an
  audio path. The whole-sample rounding
  CamillaDSP applies to each `Delay` (≤ 10.4 µs, ADR-0322) is not modelled —
  ≤ 0.2° of phase at 45 Hz, well inside the grid residue.
