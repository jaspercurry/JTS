# ADR-0417: The courtesy prelude announces each run once

- **Date:** 2026-10-02
- **Status:** Accepted.
- **Context:** [#1677](https://github.com/jaspercurry/JTS/issues/1677) asked the speaker to warn the
  room before a measurement plays: 3 beeps, then 3 s of quiet, 6 dB under the take's loudest
  stimulus. The owner-ratified [#2715](https://github.com/jaspercurry/JTS/pull/2715) made it announce
  a session, not a capture, through a phase list (CHECK, VERIFY, the timing take). That list no
  longer fits the runs. The summed composer asked VERIFY's answer, so every summed and branch take
  played it (33 times in a speaker run with a trial). The list alone gave room, rear, branch and
  trial-only runs none, and a speaker run two. Most runs now open on a quiet level probe. The owner
  picked one announcement per run, on its first take after the probe
  ([#5925](https://github.com/jaspercurry/JTS/issues/5925), comment 5960563022).
- **Decision:**
  1. A run plays the courtesy prelude once, on its first take. A level probe never plays it, so a
     run that opens on a probe plays it on the take that follows.
  2. One function decides which take: `capture_plan.announce_run` marks the first take's
     `MeasureSpec.courtesy_prelude`. The run that finds the fader applies it, so a ladder announces
     on its first rung's first take only. The capture plan budgets the prelude on that entry.
  3. Every composer plays the prelude when the spec says, and only then. No phase list remains.
  4. A take keys its run-manifest set and compares with other takes on the stimulus it measures:
     `program.take_stimulus_id`, its program's `stimulus_id` less the prelude, and the stimulus
     shape likewise. A take record's `stimulus_id` names that id. The stimulus WAV hash keys no set
     and no comparison; the record's provenance keeps it, and the program keeps its played id.
- **Consequences:**
  - CHECK announces a speaker or tournament run with no trial; the timing take announces one with a
    trial; the first summed, branch, bass or driver take announces every other run. Bass, driver
    and near-field runs gain the beeps; every later take loses them.
  - A retake of the first take plays the prelude again, so the take keeps one program.
  - The level path does not change: a take's probe, fader, level, admission, SPL watch and verdict
    are the same with or without its prelude.
  - The take that announces a run shares its sets with the run's other takes, so the room, rear
    and bass views keep the run's first pose, and take 2's level-drift check keeps take 1 as its
    reference.
  - Every set fingerprint moves once, because the WAV hash leaves the capture basis. A round banked
    before this change keeps the sets its manifest stored.
  - Summed and branch takes after the first get a new `stimulus_id` and `stimulus_shape_id`, so a
    comparison with a take banked before this change reports `basis_status: incompatible` (a
    report, not a refusal). Such a take also reads its bass-band noise over 1 s of quiet, not 3 s.
