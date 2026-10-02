# ADR-0411: A probe solves from its highest step, never a step over its loudest reading

- **Date:** 2026-10-02
- **Status:** Accepted. Amends [ADR-0365](0365-a-drivers-pose-finds-its-level-with-a-probe.md) §3
  (which reading a probe solves from, and which burst a stopped probe leaves out) and its
  Consequences line on the probe's log ("its loudest kept reading").
- **Context:** ADR-0365 §3 solved a probe's take from the loudest reading left after its stop. A
  reading is the loudest 21 ms period of one burst (ADR-0364), and a probe plays one burst per step,
  so one room sound sets a step's reading. On jts3, two seat probes at one spot and on one graph
  solved 6.8 dB apart ([#6113](https://github.com/jaspercurry/JTS/issues/6113)). In the first, the
  −54 dBFS burst read 65.98 dB, more than the −48 dBFS burst over it (65.2 dB); one 30 ms room sound
  in that burst reproduces it. The solve read the −54 dBFS step, one step low, and the room takes
  played about 8 dB under the 74 dB seat target. Also, a stopped probe left out its last reading,
  so when the stop cut the −36 dBFS burst before it read, the full −42 dBFS burst went instead.
- **Decision:**
  1. **The rule.** After the probe stops, the solve reads the highest step left, not the step with the
     loudest reading. That step must stand 10 dB over its floor, or the probe asks for the microphone
     again (ADR-0365 §3's check, now on that step).
  2. **The bound.** Its solved level is never more than one probe step (`ramp.MAX_STEP_DB`, 6 dB) above
     the level its loudest reading solves, which is ADR-0365 §3's answer: the lower of the two, each
     through `level.solve_gain` (`capture_dispatch._level_retake`). A probe with one step left solves
     from it.
  3. **The stop.** A stopped probe leaves out only the reading of the burst it was playing as it
     stopped: the last burst to start before its capture's post-roll (`capture_dispatch._stop_gain`),
     unless that reading is the only one. Without a frame count, it leaves out its last reading.
  4. **The log.** The verdict's `level_db_spl`, and `event=active_speaker.level_probe` with it, name the
     highest step's reading. When the bound sets the gain, `level_bound_gain_db` and
     `level_bound_db_spl` name the loudest reading that set it.
- **Consequences:**
  - A room sound can only make a reading high, and the highest step has the most signal over the
    room. A room sound in a lower step moves the solve only by what it reads beyond one step. With
    jts3's numbers and the stop in the −36 dBFS burst, both probes read the −42 dBFS step and
    solve −40.20 dBFS.
  - With every step read right, the readings rise with the steps, the loudest reading is the highest
    step's, and nothing changes.
  - Steps that read wrong low (a playback or capture dropout over one burst or more) play at most one
    step louder than ADR-0365 §3 solved, for any number of such steps.
  - **What stays:** the probe envelope (−60 dBFS start, 6 dB steps, the 76 dB ramp bound, ADR-0405),
    the take ceiling, the 15 dB raise limit and the 85 dB stop.
  - Rejected:
    - The step under the top as the bound. Two steps that read wrong low raise it to the 15 dB raise
      limit, 13.2 dB over ADR-0365 §3 on jts3, which the 85 dB stop then trips.
    - Keeping the loudest reading and playing the probe again when its two top steps do not rise. It
      adds a branch, a reason code and page copy, and costs a second play.
    - The median of the steps' solves. A low step near the floor reads high and pulls it quiet, and two
      steps have no median.
