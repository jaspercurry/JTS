# ADR-0423: Each candidate graph's summed set levels itself, at every spot

- **Date:** 2026-10-02
- **Status:** Accepted. Extends
  [ADR-0408](0408-over-a-timing-take-each-candidate-graph-probes-its-own-graph.md) §1 from a run
  that takes a timing take to every run. Supersedes in part
  [ADR-0403](0403-one-level-solver-at-every-pose-no-saved-volume.md) §4, its sentence "The other
  seat spots hold that fader." (lines 67–68), and ADR-0408 §3, its words "A room, rear or bass
  trial plays its base and its candidate at one fader" (lines 33–34), for a room and a rear trial.
  A bass trial's ladder keeps both (§2).
- **Context:** Outside a timing take, a trial's graphs played at the one fader that the run's probe
  found on its first summed take, the base's. That fader came down by what another graph might play
  over the probe's graph: rule A's margins (`preflight.run_margins`) for that graph's dynamic bass
  reserve and for a rear woofer that the probe's graph mutes, and the probe's own scope backoff. A
  declared reserve is a ceiling, not a measured rise. In jts3's bass trials rule A's cut was
  10.05 dB in [#6113](https://github.com/jaspercurry/JTS/issues/6113) run 2 and 14.20 dB in run 3,
  against a measured rise of at most 6.2 and 3.9 dB. So those trials played 9–13 dB too quiet, and
  the base fell under the SNR floor below 63 Hz (finding F3 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md), step A1 of
  [#6227](https://github.com/jaspercurry/JTS/issues/6227)). ADR-0408 removed the same cut over a
  timing take: each graph probes itself there.
- **Decision:**
  1. **The rule.** In every run, each candidate graph's summed takes are one set at one kind and
     one distance: its candidate and the layers its purpose clears (`angle_capture.take_level`,
     `level_sets`). The set's first take probes its own graph and levels itself to its pose's run
     level, 74 ± 2 dB at a seat and 80 ± 2 dB at a bearing. The set's other spots, repeats and
     lateral poses carry that level, as a close set's takes do (ADR-0406). A run with no take at
     its fader holds its probe fader (`programs.probe_fader_db`) for every take.
  2. **The bass ladder.** A take that plays the bass stimulus keeps its run's fader. The
     `bass/axis` ladder is a deliberate one-fader series (ADR-0403 §4), so its trial still plays
     both graphs at one fader, with rule A's cut, until #6227 A5 retires the ladder.
  3. **The readers.** A trial's graphs now play at one fader but at different stimulus levels, and
     each take is deconvolved from the stimulus it played. So readers that compare two graphs
     compare the stimulus's shape, not its level (`program.stimulus_shape_id`), as `round compare`
     does since ADR-0408: the room grade, whose seat selection names the shape in its evidence, and
     the rear document's level facts.
- **Hearing:** every probe starts at −60 dBFS at the output, rises at most 6 dB a burst and stops at
  the 76 dB ramp bound (ADR-0405). Each graph's first take is held to its run level, 74 ± 2 dB at a
  seat, so it reads at most 76 dB there: the bound that the run's probe held there before. A take
  that carries its set's level reads what its spot adds, bounded by the 85 dB stop, as a seat spot
  that held the run's fader did. `volume_limit` 0.0, the graph doors, the `set_volume_db` clamp, the
  85 dB commissioning stop and the declared driver caps do not change.
- **Consequences:**
  - A room or rear trial, and a speaker or tournament trial that names no base, plays one more
    probe for each graph after the first: about 14.4 s for a two-graph trial in the preview. A run
    of one graph plays the same probe as before, on its set's first take.
  - A redo at a set's first spot plays its probe again and spends no retry, as at any pose that
    levels itself (ADR-0361). A redo at a spot that carries the level still spends one.
  - Each graph's level retake at its set's first spot is a `speaker` charge against the
    placement's one cap of two extra takes (ADR-0422). With several graphs at one spot, one graph's
    two retakes can spend the cap: another graph's first take there is then left
    `level_off_target` and its set carries the level its probe solved. Before this ADR a room
    trial's seat takes took no level retakes. It costs takes, never level: no take plays above its
    pose's run level.
  - Rule A now reads only a timing take, which gives it no margin, and the bass ladder's takes.
    #6227 A6 deletes it once A5 retires the ladder. Until then the bound in `plan_run` that holds a
    run's first seat spot to 76 dB serves only the ladder.
  - A bass document's trial gets this rule when #6227 A2 routes it to the seat trial. Until then
    the `bass/axis` ladder trial keeps F3's cut.
  - A resolve of every preset at every layout, for a base, a base and a trial, and a trial alone,
    changes only where a summed take on a candidate graph that played at its run's fader now
    levels itself: `room/seat` and `rear/seat` at their layouts, `rear/express` at its layouts,
    and `speaker/mark` and `tournament/express` trials that name no base. Every `bass/axis` plan is
    unchanged.
  - Rejected: a rule A cut by the measured rise instead of the declared reserve. A first trial has
    no measured rise to read, and each graph's own probe reads it in the run.
