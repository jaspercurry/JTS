# ADR-0405: Every level probe starts at −60 dBFS at the output

- **Date:** 2026-09-30
- **Status:** Accepted. Amends [ADR-0365](0365-a-drivers-pose-finds-its-level-with-a-probe.md) §1 and
  [ADR-0403](0403-one-level-solver-at-every-pose-no-saved-volume.md) §3 (where a probe starts).
- **Context:** ADR-0365 §1 and ADR-0403 §3 start a probe 30 dB under the seat-equivalent level.
  ADR-0403 §4 removes that level with the saved volume, and a driver's take may now play up to full
  scale under its cap. A start tied to that ceiling would open a probe at −30 dBFS at the output at
  a 0 dB fader. The adversarial review of
  [#5737](https://github.com/jaspercurry/JTS/issues/5737) E1 part 4a also found that a plan whose
  template states `level_ladder_dbfs` played a driver's take with no probe: a staged tweeter plan
  with a −12 dBFS rung read about 90 dB at jts3's mark, over the 85 dB stop.
- **Decision:**
  1. **The start.** Every level probe (a driver's pose, a close driverless set, a branch set, and a
     run's first spot) plays its first burst at −60 dBFS at the output, its fader plus its digital
     gain, or at its ceiling when that is lower. Its bursts rise at most 6 dB each (ADR-0365)
     (`programs.LEVEL_PROBE_START_OUTPUT_DBFS`).
  2. **A plan cannot state the level of a take that levels itself.** `AngleCaptureRequest` refuses
     a template that states `level_ladder_dbfs` when any stop levels itself (`AngleStop.level` is
     set: a driver's pose, a close set or a branch set), with `walk_template_not_accepted`. A bass
     stop keeps its ladder. The page's `plan` object and `jasper-round run --plan` both enter by
     `AngleCaptureRequest.from_mapping`, so no such take plays without its probe (ADR-0361 §3).
- **Consequences:**
  - The first burst's reading depends only on the chain, what the microphone reads for full scale
    at the output. On jts3 the tweeter at the mark reads about 66 dB, and a woofer at 15 mm about
    65 dB. Every shipped pose's first burst reads 75 dB or less.
  - A chain of about 136 to 145 dB ends its probe in the first burst, at the 76 dB ramp bound. Over
    about 145 dB (a tweeter nearer than about 0.1 m, which no shipped layout places) the 85 dB stop
    ends the first burst and the run.
  - A plan cannot state a fixed series of levels at a pose that levels itself.
  - Rejected:
    - A start at −70 dBFS. It costs up to 2 more bursts per probe, and it helps only a custom close
      tweeter pose, which the 85 dB stop already bounds.
    - A start 30 dB under the take's own ceiling. With a driver's ceiling at full scale, it opens at
      −30 dBFS at the output at a 0 dB fader, about 95 dB at 15 mm from the woofer.
