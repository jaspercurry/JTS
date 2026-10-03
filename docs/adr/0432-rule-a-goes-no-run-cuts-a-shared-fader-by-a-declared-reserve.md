# ADR-0432: Rule A goes: no run cuts a shared fader by a declared reserve

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step A6. Supersedes in part ADR-0370, ADR-0403 and ADR-0408, each quoted below.

## Context

Finding F3 of the [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md):
rule A (`preflight.run_margins` and the fader cut in `plan_run`) cut a trial's shared fader by the
candidate's declared bass reserve, 10.05 dB in [#6113](https://github.com/jaspercurry/JTS/issues/6113)
run 2 and 14.20 dB in run 3, while the measured rise was at most 6.2 and 3.9 dB.

[ADR-0423](0423-each-candidate-graphs-summed-set-levels-itself-at-every-spot.md) made each candidate
graph's summed set level itself, and [ADR-0431](0431-the-bass-level-ladder-is-retired.md) retired
the bass ladder, the last run that played candidate takes at one fader. So the only summed take on a
candidate graph that still plays at a run's fader is the run's own timing take, and its probe reads
the graph it plays. Rule A has no input left: its margin is 0.0 on every plan, its backoff has no
other graph to charge, and no run's probe plays at a seat.

## Decision

1. **What goes.**
   - `preflight.run_margins` and `bass_lift_db`: the bass lift (how far one graph's declared reserve
     passes another's) and the rear-woofer sum (20·log10 of the woofers that share a band).
   - The facts and reads that fed only them: `PreflightFacts.applied_bass_extension` and
     `applied_rear_plays`, the read of the applied profile in `preflight_live`, and
     `measured_crossover_candidate.plays_rear`.
   - The cut in `plan_run` of the fader that a run's probe finds: the probe's landed reading, the
     2 dB tolerance, the margin and the scope backoff of a take on another graph
     (`programs.probe_backoff_db`), against the 85 dB stop, or against 76 dB at a first seat spot.
     `RunDoor.margin_db` and the `cut_db` of `run_fader_db` go with it.
   - The refusal of a plan whose take at the run's fader could play before the run's probe
     (`capture_schedule.unprobed_take_at_fader`, `run_takes` and `UNPROBED_TAKE_DETAIL`). The
     ladder was its last input. CHECK and MEASURE still play at a run's fader, but only a speaker
     preset's base plays them, and that base always takes its timing take at the run's first
     placement, where the executor plays the probe first. A registry test keeps that true: every
     shipped preset at every layout places its run's probe no later than any take at the run's fader
     (`test_every_shipped_preset_plans_its_probe_before_the_takes_at_its_fader`; ADR-0405's
     probe-first design).
   - `PreflightReport.rung_admission`: with rule A and that refusal gone, it holds nothing.
2. **The rule.** A run whose probe finds its fader holds the fader that probe solves
   (`programs.run_fader_db`), never above a level the run states (ADR-0403 §4). Each candidate
   graph's summed takes play at their own set's level (ADR-0423).
3. **What stays.** A take at its run's fader (CHECK, the timing take, MEASURE) keeps the level-drift
   grade, and the executor still plays a placement's probe before its other takes.

### What this supersedes

- ADR-0370 §5, line 97: preflight folds the room-off rise "beside the bass lift it already folds".
  ADR-0413 removed the rise; this removes the lift.
- ADR-0403 §4, lines 70–71: "The first seat spot reads at most 76 dB, 9 dB under the stop, which
  leaves room for a later seat spot that reads louder than the first." It goes as a bound on a run's
  fader: no run's probe plays at a seat. Each graph's set at a seat levels itself to 74 ± 2 dB
  (ADR-0423), so its first seat take still reads at most 76 dB.
- ADR-0408 §3, lines 32–33: "Rule A's bass lift, rear-woofer sum and room-off rise still bound a run
  whose probe is not a timing take." No such run plays a take at its fader, and rule A goes.

ADR-0413 §3, line 26 ("The room-off rise goes, with its facts and the reads that fed only it.")
removed rule A's third term. This ADR removes the other two in the same way, so nothing in ADR-0413
changes. It carries out ADR-0423's consequence at lines 55–57 and ADR-0431's at lines 99–100.

## Consequences

- **Hearing:** no level changes. Every probe still starts at −60 dBFS at the output, rises at most
  6 dB a burst and stops at the 76 dB ramp bound (ADR-0405). Every take's level target is held 3 dB
  plus its tolerance under the stop (`capture_dispatch._level_target`). With no margin, the cut was
  max(0, landed + 2 − bound), and a probed take lands at least 1 dB under that target, so it was 0.
  `volume_limit` 0.0, the graph doors, the `set_volume_db` clamp, the 85 dB commissioning stop and
  its watch, the declared driver caps and ADR-0405's probe staircase do not change.
- Proof (scratch scripts, not committed): every preset at every layout, with a base, a base and a
  trial, and a trial alone, on six boxes (a two-way and a cardioid; a 14.78 dB declared bass reserve
  on the base or on the trial; a rear that the base plays, mutes or cannot read). Of 360 cases, 294
  resolve, to 1,887 takes. At c82926b38 rule A answered nothing for 234 plans and 0.0 dB for the 60
  with a timing take; each of those probes was a timing take at the mark, with no take on another
  graph at its fader. Each plan's request, takes, level rules and sets, probes, composed programs,
  preview, preflight answer (`rung_admission` apart) and a run played on a fake chain (237 runs,
  2,085 plays, each with its fader and stimulus level) are byte-identical before and after. No
  shipped plan, and none of 518 plans that resolve from 3,920 hand-posed requests (every preset;
  one or two of seven poses; four candidate shapes; with and without `--driver`), puts a take at its
  run's fader before its probe.
- The run and dry-run answers drop `rung_admission`, so their schemas move to `jts_round_run/5` and
  `jts_round_preflight/5` (ADR-0344 §4).
- A dry run no longer refuses an applied bass descriptor that rule A could not read. Only facts
  built by hand reached that refusal: a candidate validates its descriptor when it loads.
- Rejected: keeping rule A, or the refusal, as a guard with no input. Nothing reaches either, so
  neither guards anything.
