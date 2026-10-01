# ADR-0406: A close set is one candidate graph

- **Date:** 2026-10-01
- **Status:** Accepted. Amends [ADR-0403](0403-one-level-solver-at-every-pose-no-saved-volume.md) §3
  (which takes share a close set's level).
- **Context:** ADR-0403 §3 levels a driverless summed set closer than the mark by its first take,
  and every other take of the set (its candidates, repeats and lateral poses) plays at that take's
  level, so an A/B pair keeps one drive level. Graphs at one close spot can differ by more than any
  fixed margin: next to the rear woofer, a graph that plays the rear reads far over one that mutes
  it. In the chain run of [#5737](https://github.com/jaspercurry/JTS/issues/5737) E1 part 4b-2, a
  cardioid On/Off trial at `rear_behind`, whose base mutes the rear and whose trial plays it, read
  85 dB with the shared level, at the stop.
- **Decision:**
  1. **The set.** A driverless summed set closer than the mark is one candidate graph at one kind
     and one distance: the take's candidate, with the layers its purpose clears
     (`angle_capture.level_sets`). Each graph's first take in the set probes and levels itself to
     80 ± 2 dB, and that graph's other takes (repeats and lateral poses) carry its level. Takes of
     other graphs may come between them.
  2. **Branch sets and driver poses keep their sets.** A branch set's probes play each branch alone
     on the drivers graph (ADR-0403 §3), so a second candidate there would probe the same drivers
     and find the same levels; its candidates still share them. A driver's pose plays the drivers
     graph, with no candidate.
- **Consequences:**
  - One more probe per extra graph per close set. The preview and the catalog count it, since both
    read the capture schedule: up to about 21 s at `rear_behind`, the probe's whole staircase from
    −60 dBFS at the output; the 76 dB ramp bound stops it sooner (ADR-0405).
  - An A/B pair at a close spot no longer plays at one drive level. A reader that compares them
    reads each take's own level and stimulus: the rear view compares transfer functions, each
    deconvolved from its own stimulus at the gain it played, at one placement and one fader. ADR-0366
    §2 keeps one drive level only where the graphs share a probe.
  - A plan with one candidate per close set composes and plays as before.
  - Rejected: a fixed margin under the set's level for a graph that plays a driver the first take's
    graph mutes (6 dB, the coherent sum of two woofers). Next to the rear woofer the gap can be far
    larger than that.
