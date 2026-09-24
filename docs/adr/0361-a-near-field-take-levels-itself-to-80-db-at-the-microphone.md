# ADR-0361: A near-field take levels itself to 80 dB at the microphone

- **Date:** 2026-09-24
- **Status:** Accepted. Supersedes the per-pose fader solve the
  [#5684](https://github.com/jaspercurry/JTS/issues/5684) plan proposed.

## Context

A pose at one driver sits 15–30 mm from the cone (ADR-0360), where the
seat-level fader reads about 96 dB, over the 85 dB commissioning stop. The
seat anchor predicts the seat, not the cone. The executor holds one fader for a
whole run and moves a retake's level only in the stimulus's digital gain, and
the composer enforces every driver's cap against that fader. The owner set the
near-field level: 80 dB peak in the loudest 21 ms window, ±2 dB (#5684).

## Decision

1. **The fader stays the run's own** (the seat reference by default). A
   near-field take's level moves only in its digital gain, never above the
   seat-equivalent level: the peak a far-field take of that driver plays at.
2. **Its first attempt at each pose plays 30 dB under that level**, about
   66 dB at 15 mm. The pose's later takes start at the level its take landed,
   and a pose placed again after a refused take or a redo starts quiet again.
3. **A take at one driver's pose is judged against its level target, not its
   repeats.** Its loudest 21 ms window must read 80 ± 2 dB, and the target
   never sits above the admission bound under the take's own stop. A take
   the microphone heard is levelled before its recording is judged: outside
   the band it is retaken at the peak that lands the target, raised at most
   15 dB a step, as an automatic retry charged to the speaker, and the
   out-of-band attempt stays banked, unkept. A take it did not hear is judged
   by its recording (a locate failure, or the muted-output stop), never
   levelled blind, and no other retake raises a take past its target.
4. **The 85 dB live stop, its single source and the seat reference are
   unchanged**; no near-field take writes the seat reference.

## Consequences

- A pose costs its opener and its levelled take, about twice one take's play
  time; its later takes reuse the landed level.
- A target the seat-equivalent level cannot reach spends the pose's retries,
  and that pose, not the run, is left unmeasured.
- Rejected: a per-pose fader solve (the plan's S5), because the executor holds
  one fader per run and the digital path already bounds every take by the
  driver caps; a separate level-only probe program, because the opener is the
  take itself, so its bytes and crest are the take's.
