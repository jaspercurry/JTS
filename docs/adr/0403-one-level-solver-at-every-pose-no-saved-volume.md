# ADR-0403: One level solver at every pose; no saved volume

- **Date:** 2026-09-30
- **Status:** Accepted: the owner's design on [#5925](https://github.com/jaspercurry/JTS/issues/5925)
  (D2 = A; D1 = no saved volume). Supersedes
  [ADR-0306](0306-one-auto-level-tool-levels-a-session-once.md). Amends
  [ADR-0366](0366-one-pose-model-a-level-found-at-the-pose-and-a-band-stated-from-it.md) §2 and
  [ADR-0361](0361-a-near-field-take-levels-itself-to-80-db-at-the-microphone.md) §1 (the
  seat-equivalent cap).

## Context

Four owners set a take's level: the saved session volume (ADR-0306), CHECK's per-driver solve,
a driver pose's probe (ADR-0365) and the bass ladder
([#5714](https://github.com/jaspercurry/JTS/issues/5714)). A driverless take closer than the
mark plays at the seat-equivalent level, so a spot near the cabinet, such as the 0.1 m pose of
the `rear_behind` layout, can read near or over the 85 dB stop. The owner decided two things:

- **D2 = A.** A bearing, behind or driverless close pose aims at a fixed 80 ± 2 dB at the
  microphone, never above the take's ceiling.
- **D1: no saved volume.** Every run levels itself at its own first spot and holds that level
  for the rest of the run. The seat-equivalent cap goes, and nothing replaces it; the hard limits
  stay.

## Decision

1. **One solver.** Every level comes from `level.solve_gain`. A probe is read from its located
   sweeps in their band over the room floor read just before them, and nothing is solved from a
   probe reading under floor + 10 dB (ADR-0365). After the probe, a take that misses 80 ± 2 dB is
   solved again from its own reading, with no floor check. Room noise can only raise that reading,
   so that solve errs quieter. CHECK keeps its checks, and the last step of its per-driver solve
   calls the same solver.
2. **One rule.** `pose_level(pose)` names the poses whose takes level themselves: one driver
   alone, at any kind and distance (ADR-0361), and a driverless spot closer than the mark, other
   than a seat. Both aim at 80 ± 2 dB at the microphone, never above the take's ceiling. A stop
   levels itself where that rule meets what it plays (`AngleStop.level`): a driver's pose, a branch
   take, whose branches each play alone (§3), or a driverless summed stop with no bass stimulus.
   `angle_capture.level_sets` groups the takes that share one level, and
   `capture_schedule.prepare_plan_captures` marks the takes that play a probe
   (`MeasureSpec.level_probe`). A plan cannot state that mark. The composer and the executor both
   read it, so they cannot disagree.
3. **A close driverless set.** Consecutive driverless summed stops at one kind and one distance
   closer than the mark are one set. Its first take plays one probe of its own summed sweep. The
   probe starts at the lower of 30 dB under the seat-equivalent level and the take's ceiling, and
   rises at most 6 dB a burst to that ceiling. The ramp bound (76 dB under an 85 dB stop) ends it.
   When a low driver cap sets the summed ceiling, the probe is one burst at that ceiling. That take
   is levelled to 80 ± 2 dB. Every other take of the set (its candidates, repeats and lateral
   poses) plays at the level that take landed and answers to its repeats, so an A/B pair and a
   lateral falloff keep one drive level. If that take is left unmeasured, the rest of the set plays
   at the last level solved for it, never above the last level it played. A close set only turns
   down: no take plays above its ceiling.

   **A branch set.** A branch take plays each of its two branches alone, then their sum, so it
   levels itself at any spot. Consecutive branch stops of one pair at one kind and one distance
   are one set. Before the set's first take, each branch plays alone the probe a driver's pose
   plays, on the drivers graph (ADR-0365). The take plays 6 dB under the lower level those two
   probes find, never above its ceiling. Two branches in phase read at most 6 dB over the louder
   one alone, so the sum reads at most 80 dB where the take plays each branch no louder than its
   probe read it. The rest of the set plays at the level that take landed, as a close set's takes
   do.
4. **No saved volume.** A run finds its fader with a probe of its first spot's own stimulus before
   its first take, and holds it.
   - The probe's first burst plays at −60 dBFS at the output (fader plus digital gain), and its
     bursts rise at most 6 dB each (ADR-0365). Every program keeps its level relative to that
     fader, so a program with no level asked never plays over the run's level.
   - Targets: 80 ± 2 dB at the first spot, and 74 ± 2 dB at a first seat spot. The other seat
     spots hold that fader. The 85 dB stop is the only hard limit on what they read. The 6 dB
     across-pose drift limit grades a take after it plays, and its retake plays at the same level,
     so it limits nothing that plays. The first seat spot reads at most 76 dB, 9 dB under the
     stop, which leaves room for a later seat spot that reads louder than the first.
   - A run whose first spot is a driver's pose opens at a fixed probe fader (0 dB, or the loudest
     driver cap if lower), and each placement keeps its own probe. It solves no fader.
   - A driver's take is no longer held under the seat-equivalent level. Its ceiling is its driver
     cap under the run's fader, and full scale.
   - The bass ladder steps down from the bass run's own first-spot level.
5. **What goes.** The saved session volume (`seat_level_reference.py` and its readers), the
   separate engine and its page (`auto_level.py`, `seat_level_sweep.py`, `jasper-seat-level`,
   `jasper/web/sound_seat_level.py`), and the step that turned the saved volume into a fader.
   Loudness between runs shows through each take's banked `level_db` and `stimulus_dbfs`.
6. **What stays.** `volume_limit` 0.0, the graph-door refusal, the `set_volume_db` clamp, the
   85 dB commissioning stop at the microphone and its single source, the declared driver caps,
   and SNR retakes. No take is levelled blind (ADR-0361 §3).

## Consequences

- [#5737](https://github.com/jaspercurry/JTS/issues/5737) E1 lands this in stages: the one rule
  and CHECK's solver (no played change), then §2 and §3's close sets (quieter only), then §3's
  branch sets (quieter only), then §4 (a run's own level), then §5 (deletion only).
- A close driverless set costs one probe, about 12 s, before its takes. A branch set costs two
  probes, one for each branch.
- A branch probe sweeps MEASURE's band, from 150 Hz, on the drivers graph. Below 150 Hz the room,
  and a candidate's dynamic bass (ADR-0359), can lift a branch take over what its probes read.
  There the take is still never above its ceiling, and the 85 dB stop is its bound.
- At a close spot, a per-driver schedule keeps its fader, and a bass take keeps its ladder.
- Rejected:
  - A branch pair that probes its summed stimulus on the candidate graph. The branch take plays
    each branch alone as well as their sum, and a summed probe plays neither branch alone, so it
    cannot find the take's level.
  - A branch probe that plays the branch take's own solo-and-sum sequence. It is exact, but it
    needs a new admitted shape, and each of its bursts is three sweeps long.
  - A probe at each lateral placement of a close set. It would level a falloff away.
  - A target taken from the newest banked far-field take at that place (D2 = B). Each round would
    re-level to the last one and hide a loudness change between rounds.
  - A saved volume found at the mark (the owner's first D1 answer, which this replaces).
