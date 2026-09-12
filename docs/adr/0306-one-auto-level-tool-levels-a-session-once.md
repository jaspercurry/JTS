# ADR-0306: One auto-level tool levels a session once

- **Date:** 2026-09-12
- **Status:** Accepted; ratified by the owner on 2026-09-12 in session.
- **Context:** Amplifiers and hardware have different responses. A predicted SPL
  cannot replace a microphone reading. Re-leveling between DSP trials would hide
  the loudness change being measured.
- **Decision:** `jasper-seat-level` is the one leveling verb. It calls `level_to`
  in a closed loop at the first mark, targeting 75.0 ± 1.0 dB SPL, and banks a
  session gain with microphone identity. Runs require this record and hold its
  gain through measure, judge, trial, and repeat rounds. Old records require a
  new leveling pass. Hardware driver caps remain in force.
  Bass windows are non-positive offsets from that gain; zero is the default.
  Every take records the watch's maximum SPL window. Drift compares the median
  of accepted takes at the same gain: same-pose takes first, otherwise all poses.
  Differences through 2 dB at the same pose or 6 dB across poses are evidence;
  larger differences request a retake without charging either party.
  The typed per-run SPL ceiling and anchor-based SPL prediction retire.
  The commissioning stop is the only hard SPL limit. The microphone checks it
  throughout leveling and every take; the 0 dB fader clamp remains unchanged.
- **Consequences:** DSP loudness changes remain visible across rounds. The operator
  levels once for a microphone placement and repeats that verb after changing
  the microphone or calibration. Band-limited white noise remains the leveling
  stimulus; sweep readings are never compared against its different statistic.
  This amends [ADR-0304](0304-the-bass-level-axis-is-fixed-level-windows.md) on window
  representation; its research authorization up to 80 dB SPL is moot under the
  commissioning stop. It supersedes issue #4942's D4 typed-ceiling decision.
