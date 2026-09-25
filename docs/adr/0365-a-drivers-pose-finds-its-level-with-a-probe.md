# ADR-0365: A driver's pose finds its level with a probe

- **Date:** 2026-09-25
- **Status:** Accepted. Supersedes (partial) [ADR-0361](0361-a-near-field-take-levels-itself-to-80-db-at-the-microphone.md)
  §2 (the quiet opener) and its rejection of a level-only probe.
- **Context:** A driver's pose opened with a whole take 30 dB under the seat-equivalent level. That
  one reading often sat in the room's noise, and each take that missed cost a full take's play
  ([#5714](https://github.com/jaspercurry/JTS/issues/5714)). [ADR-0364](0364-a-takes-level-is-read-from-its-located-sweeps-in-their-band.md)
  made a take's level trustworthy. What remained was finding a pose's level in one short play at any
  distance, never within one step of the 85 dB stop.
- **Decision:**
  1. A driver pose's first play, with no level asked, is its level probe (`compose_level_probe`,
     built by `build_level_probe_program`). It plays 1 s of room for its floor, then one short sweep
     of the take's band per level. The levels rise at most 6 dB (`ramp.MAX_STEP_DB`) a burst, from
     30 dB under the seat-equivalent level to the take's own ceiling. Each burst is longer than
     the last by whole cycles at the band's floor, at least 0.125 s, so no two share a shape in any
     band. Its bursts, each at one level, are then one matched filter: only the true alignment
     lines up every burst the capture holds, however long the play took to start, whichever
     bursts the room buried or the stop cut.
  2. A probe ends its play, as a normal end, once its loudest 21 ms period reaches the ramp bound
     under the stop (`profile.ramp_bound_db_spl`: the stop less one step and the 3 dB margin, 76 dB
     under an 85 dB stop). Its capture records that level (`stopped_at_db_spl` in its SPL block).
     The 85 dB stop still guards it, unchanged.
  3. Each burst the capture holds is read as ADR-0364 reads a take
     (`ProgramAnalysis.stimulus_levels`, one reading per gain); a burst after the stop reads
     nothing. A stopped probe's last burst may have been cut short, so it is left out unless it is
     the only one. The loudest reading left must stand 10 dB over its floor, where ISO 3744's K1
     puts the room's share at 0.46 dB at most; if it does not, the probe asks for the microphone
     again (`snr_floor`). Otherwise the take's gain is solved 1:1 from it, to 1 dB under the
     target, raised at most 15 dB. The solve is `level.solve_gain`, the one a level retake also
     uses. The take plays at that gain, and a probe is never kept.
  4. The pose's takes follow ADR-0361 §3. A take outside the band is retaken. A take its ceiling
     holds under the solved gain is kept as `level_capped`, and the probe's evidence names the gap
     (`level_shortfall_db`). A redo or a re-placement starts the pose at its probe again.
- **Consequences:** Every driver pose costs its probe (about 9–10 s, less when the bound stops it) and
  one take, where it used to cost an opener take and a take. The page names the probe while it
  plays. Each probe logs `event=active_speaker.level_probe` with its loudest kept reading, that
  reading's floor, and the gain it solved. The bound is read broadband over the whole capture, so a
  room sound that reaches it before the bursts ends the probe too; the probe then asks for the
  microphone again, or its take lands quiet and is retaken louder. Not decided here: the bass level ladder, a deliberate
  series that feeds its headroom table rather than a level search. Rejected:
  - a probe that plays, reads and plays again (one play reads every level at once);
  - a fixed-level probe (no one level spans a 15 mm and a 100 mm pose inside one step of the stop);
  - locating the probe from its first burst alone (a late amplifier or a loud room buries the quiet
    first bursts, and the match then anchors on a later one);
  - matching the bursts at their own gains (a louder unplayed burst then outweighs a quieter
    played one, and the match slides a burst early).
