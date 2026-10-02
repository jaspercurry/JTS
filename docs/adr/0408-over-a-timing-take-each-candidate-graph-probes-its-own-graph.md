# ADR-0408: Over a timing take, each candidate graph probes its own graph

- **Date:** 2026-10-02
- **Status:** Accepted. Amends [ADR-0403](0403-one-level-solver-at-every-pose-no-saved-volume.md) §4
  (which takes play at the run's fader) and [ADR-0406](0406-a-close-set-is-one-candidate-graph.md)
  (which takes make up a set).
- **Context:** In a run that takes a timing take (ADR-0319), that take finds the run's fader with its
  probe (ADR-0403 §4). A trial's candidate takes at the mark then played at that fader. The timing
  graph plays the base's front drivers only, with no bass extension, so rule A
  (`preflight.run_margins`) charged each other graph's rise over it: the dynamic bass reserve
  (ADR-0359), the rear woofer in phase, and each driver's gap under the timing graph
  ([#6154](https://github.com/jaspercurry/JTS/pull/6154)). On jts3's smoke-test tune (a 14.78 dB bass
  reserve and a rear seed) a speaker trial's margin is 20.80 dB, so the run's fader, and the timing
  take with it, comes down 16.8 dB. Its pilots then fall about 0.7 dB short of the 12.38 dB they need
  (`pilot_level_collapse`, smoke test [#6113](https://github.com/jaspercurry/JTS/issues/6113) bug 7).
- **Decision:**
  1. **The rule.** In a run that takes a timing take, the timing take still finds the run's fader with
     its own probe and plays at the level that probe solves. Each candidate graph's summed takes there
     are their own set, at one kind and one distance, as ADR-0406 makes a close set
     (`angle_capture.take_level`, `level_sets`). The base's candidate graph is never the timing graph,
     so the base is one of them. The set's first take probes its own graph from −60 dBFS at the output
     and levels itself to its pose's run level, 80 ± 2 dB at a bearing. Its repeats and other poses carry
     that level, as a close set's takes do. A plan template cannot state a ladder for a run that takes
     a timing take (ADR-0405).
  2. **What goes.** Over a timing take, no summed take on another graph plays at the run's fader. So
     rule A's timing branch has no input, and it goes: each driver's gap under the timing graph
     (`driver_excess_db`), with `PreflightFacts.driver_peaks_db`, `applied_program_charge_db` and
     `applied_timing_floor_db`, the reads in `preflight_live` that fed only it
     (`measurement_emit.timing_floor_db`, `rear_calibration.front_floor_db`,
     `program_headroom.output_peaks_db`), and rule A's own cases for a timing probe in its bass, rear
     and room-off terms.
  3. **What stays.** Rule A's bass lift, rear-woofer sum and room-off rise still bound a run whose
     probe is not a timing take. A room, rear or bass trial plays its base and its candidate at one
     fader, and the bass trial's ladder needs both graphs at one drive level (ADR-0370, ADR-0403 §4).
  4. **The sum's ceiling.** A branch take's sum plays under every driver's cap less the dynamic bass
     boost its output keeps, the rule its alone segments use (ADR-0407). So the composer never asks for
     a level that admission refuses.
- **Hearing:** every probe starts at −60 dBFS at the output, rises at most 6 dB a burst, and stops at
  the 76 dB ramp bound (ADR-0405). Every take of a run that takes a timing take plays only a graph its
  own probe read, and each set's first take lands at 80 ± 2 dB. A take that carries that level to
  another pose reads what that pose adds, bounded by the 85 dB stop, as ADR-0406 accepts for a close
  set. Every shipped timing layout starts on the axis, where the speaker reads most. The 85 dB stop,
  `volume_limit`, the graph doors, the `set_volume_db` clamp and the driver caps do not change.
- **Consequences:**
  - A speaker trial plays at the fader its timing take's probe finds, with no margin cut. On jts3's
    tune its margin goes from 20.80 dB to 0, and its timing take keeps its pilots' SNR.
  - Each candidate graph of a speaker or tournament trial plays one more probe: 28.3 s more for a
    two-graph trial at each layout of `speaker/mark` and `tournament/express` in the preview.
  - An A/B pair of a speaker trial no longer plays at one drive level. Its readers compare transfer
    functions, each deconvolved from its own stimulus at the gain it played (`segment_stimulus`), at
    one placement and one fader, as ADR-0406 accepts for a close set. So `round compare` reads such a
    pair as one basis: it compares the stimulus's shape (`program.stimulus_shape_id`), not its level.
  - A hand-staged bass stop over a timing take now levels per graph, so `bass_fit`, `bass_table` and
    `bass_comparison` refuse its pair (`*_capture_context_changed`): that is why the bass trial keeps
    one drive level (§3).
  - Rejected: each take pays its own margin under the timing take's fader. It moves the charge to the
    candidate takes instead of removing it, and on jts3 they would still play 16.8 dB down.
