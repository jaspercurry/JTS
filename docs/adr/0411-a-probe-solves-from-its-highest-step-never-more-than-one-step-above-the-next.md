# ADR-0411: A probe solves from its highest step, never more than one step above the next

- **Date:** 2026-10-02
- **Status:** Accepted. Amends [ADR-0365](0365-a-drivers-pose-finds-its-level-with-a-probe.md) §3
  (which reading a probe solves from).
- **Context:** ADR-0365 §3 solved a probe's take from the loudest reading left after its stop. A
  reading is the loudest 21 ms period of one burst (ADR-0364), and a probe plays one burst per step,
  so one room sound sets a step's reading. On jts3, two seat probes at one spot and on one graph
  solved 6.8 dB apart ([#6113](https://github.com/jaspercurry/JTS/issues/6113)). In the first, the
  −54 dBFS burst read 65.98 dB, more than the −48 dBFS burst over it (65.2 dB); one 30 ms room sound
  in that burst reproduces it. The solve read the −54 dBFS step, one step low, and the room takes
  played about 8 dB under the 74 dB seat target.
- **Decision:**
  1. **The rule.** After the probe stops, the solve reads the highest step left, not the step with the
     loudest reading. That step must stand 10 dB over its floor, or the probe asks for the microphone
     again (ADR-0365 §3's check, now on that step).
  2. **The bound.** Its solved level is never more than one probe step (`ramp.MAX_STEP_DB`, 6 dB) above
     the level the next step down solves: the lower of the two, each through `level.solve_gain`
     (`capture_dispatch._level_retake`). A probe with one step left solves from it.
- **Consequences:**
  - A room sound can only make a reading high, and the highest step has the most signal over the
    room. A room sound in a lower step no longer moves the solve. One in the step under the top moves
    it only by what it reads beyond one step: jts3's two probes now solve −40.20 and −40.98 dBFS.
  - With every step read right, every step solves the same gain, so nothing changes.
  - A top step that reads wrong low (a missed burst, a microphone's AGC) now plays at most one step
    over what the step under it solves, not up to the 15 dB raise limit over its own reading.
    ADR-0365 §3 solved from the louder step under it. A top step within 10 dB of its floor now asks
    for the microphone again.
  - **What stays:** the probe envelope (−60 dBFS start, 6 dB steps, the 76 dB ramp bound, ADR-0405),
    the take ceiling, the 15 dB raise limit and the 85 dB stop. The bound keeps a take within one
    step of a level a heard step solved.
  - Rejected:
    - Keeping the loudest reading and playing the probe again when its two top steps do not rise. It
      adds a branch, a reason code and page copy, and costs a second play.
    - The median of the steps' solves. A low step near the floor reads high and pulls it quiet, and two
      steps have no median.
