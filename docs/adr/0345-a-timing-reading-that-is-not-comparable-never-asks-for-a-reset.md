# ADR-0345: A timing reading that is not comparable never asks for a reset

- **Date:** 2026-09-23
- **Status:** Accepted. Amends ADR-0319 (the reset rule for a saved timing
  pair) and the summed timing take's graph.
- **Context:** ADR-0319 lets a later design-axis read ask, in `next_action`,
  to reset and re-measure timing when `residual_rms_db > 3 × repeat_noise_db`
  (and above a 0.5 dB floor). On jts3 (#5632 F3) a speaker round printed a
  10.6 dB residual against 0.43 dB repeat noise and asked to reset a
  known-good −186 µs timing. The reading was not comparable with its
  prediction: both roles were short on signal-to-noise, the summed take
  played the applied candidate with its rear branch while the prediction
  used front-driver takes, and the prediction mapped outputs by driver role,
  so on a cardioid preset "woofer" resolved to the rear output. The playbook
  tells the agent to act only on `next_action`, so it would have reset good
  timing.
- **Decision:**
  1. The summed timing take plays the front drivers only: it drops the bass
     layer (as before) and mutes the rear branch, while the rear section's
     front chain stays as applied. The rear headroom that is no longer
     charged moves into the trims, so muting the rear raises no level.
     Otherwise the take plays as it did before this ADR: it still drops the
     candidate's linearization, blend and room layers, so at one of their
     cuts it plays louder than the candidate by that cut's depth. The
     prediction maps only primary outputs.
  2. The timing verdict carries `status: comparable | not_comparable` with
     `reasons` (`snr_short: [roles]`, `graph_mismatch: [targets]`) beside the
     residual, the repeat noise and the floor. `snr_short` names the driver
     takes below the 35 dB alignment signal-to-noise floor. `graph_mismatch`
     names the outputs the played graph left audible that the prediction
     does not model. With the rear muted, only a timing take made before
     this ADR and analysed again can raise it; `graph_mismatch` and
     `remeasure_timing` are removed once every cardioid speaker has banked
     a speaker round after this ADR.
  3. ADR-0319's reset rule applies only to a `comparable` reading. A
     `not_comparable` reading never asks for a reset: `snr_short` asks to
     measure timing again louder or in a quieter room (`measure_timing`);
     `graph_mismatch` asks to re-measure the timing take on the matching
     graph (`remeasure_timing`) and wins when both reasons apply. A verdict
     with no `status` (analysis stored before this ADR) has unknown
     comparability: it never asks for a reset either, and asks for
     `measure_timing`.
  4. One verdict: `jasper/audio_measurement/timing_verification.py` builds it
     and picks the next action; the packet stores it; the page and the
     envelope show the packet's verdict and never judge a second time.
- **Consequences:** Weak or mismatched evidence asks for a better reading
  instead of undoing a saved value, as ADR-0319 already intends for weak
  first reads. On a cardioid speaker the timing take and its prediction now
  describe the same graph. The page shows no timing advice until the round
  is banked (the envelope's second, live judgement is gone). A take made
  before the rear mute reads `graph_mismatch` when it is analysed again; a
  banked packet keeps the verdict it stored. A first timing decision (no
  saved timing) does not read `graph_mismatch`; the mute keeps new takes
  clean of it. On a cardioid speaker the entry-baseline take is this timing
  take, so `entry_grade` and the `entry_baseline` frequency series now
  measure a sum without the rear. In the rear's band they do not compare
  with rounds banked before this ADR. Rejected: keeping the rear on and
  predicting through it (the rear chain's level and delay are what the rear
  program tunes, so the timing read would depend on a later layer).
