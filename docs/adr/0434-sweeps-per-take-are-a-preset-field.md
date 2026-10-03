# ADR-0434: Sweeps per take are a preset field

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step E4. Amends ADR-0383's consequence on MEASURE occurrences, quoted below.

## Context

Every MEASURE take, and every take of one driver alone, played 3 sweeps of each driver. The count
was `MEASURE_REPEAT_COUNT`, a constant in the composer (`jasper/audio_measurement/program.py`), and
its one reader was the default of `build_measure_program`. No preset and no run could ask for fewer.
The speaker program's angle step ([#6227](https://github.com/jaspercurry/JTS/issues/6227) D3) needs
1 sweep per driver off axis. The owner's rules: the sweep is the unit of cost, and one engine is
configured per program from one source of truth.

[ADR-0431](0431-the-bass-level-ladder-is-retired.md) retired the bass ladder and its three averaged
summed passes (`BASS_PASSES`). Its consequence at lines 101–102 says that repeated summed passes and
their analysis have no producer left. The executor also held a graded take's host effects until its
capture had played (`plan_run._held_effects`), so that a ladder's later rung was not composed from
them. Every capture now plays one stimulus.

## Decision

1. **The owner.** A preset row of `measurement_plans.json` may state `sweeps_per_take`: how many
   sweeps each driver plays in one take (`Preset.sweeps_per_take`,
   `jasper/active_speaker/measurement_programs.py`). A row that states none plays the one default,
   `SWEEPS_PER_TAKE` = 3 (`jasper/audio_measurement/excitation.py`). `MEASURE_REPEAT_COUNT` goes.
2. **The per-pose override.** A pose of a layout, or an inline pose of one run (`--poses`), may
   state its own `sweeps_per_take`. It wins over its preset's count for that pose. The loader
   refuses a count that is not a positive whole number (`validated_sweeps_per_take`).
3. **What it counts.** A MEASURE take plays that many interleaved sweeps of each driver. A driver's
   pose plays that many sweeps of its driver. A summed take plays its one summed sweep, and a branch
   take plays its fixed solo-and-sum shape. Neither reads the count.
4. **One path.** `request_for_preset` gives each stop its count (`AngleStop.sweeps_per_take`: its
   pose's, else its preset's). The stop's `MeasureSpec` carries it to the composers
   (`compose_target_program`, `SessionExcitation.measure_program`) and to the capture plan's sizing
   (`build_inline_session_spec`). The page's preview (`preview_schedule`) and the dry run's price
   (`preflight`) compose through the same `program_for_spec`, so they count the sweeps that play.
   The preset catalog lists each preset's count.
5. **What goes.** No program plays a summed sweep twice. So `repeated_sweep.py` goes, with every
   reader of repeated summed passes: the pass alignment and averaging in `_analyze_verify`, the
   per-pass integrity checks and record keys (`summed_pass_*`, `zero_fill_runs`, `pass_alignment`,
   `repeat_content`), the sweep-spacing anchor (`_resolve_sweep_anchor`), the summed path of
   `estimate_drift`, and the bass view's pass averaging and its `passes` key. A capture plays one
   stimulus, so `plan_run` no longer holds a graded take's host effects for a later one.

### What this amends

- ADR-0383, consequence at lines 39–40: "A two-way MEASURE take banks three occurrences per role,
  about 42 KB of curves". That holds at the default count. A take that states fewer sweeps banks
  fewer occurrences.

This ADR carries out ADR-0431's consequence at lines 101–102.

## Consequences

- **Hearing:** nothing that plays changes. Proof (scratch scripts, not committed): every preset at
  every layout, with a base, a base and a trial, and a trial alone, on four boxes (a two-way and a
  cardioid, each with and without a bass trial). Of 240 cases, 196 resolve, to 1,258 takes. These
  are byte-identical before and after: each plan's request (its fingerprint and `plan.json`), its
  takes, its 5,144 composed programs (probe, take, take at full scale and at a level, branch
  probes), its bound capture plan, its preview, its preflight answer and price, and a run played on
  a fake chain (158 runs, 1,390 plays). What a take banks (curves, analysis, diagnostic and bass
  reading) is byte-identical too, for each of the 16 distinct programs those plans compose.
  `volume_limit` 0.0, the graph doors, the `set_volume_db` clamp, the 85 dB stop and the declared
  driver caps do not change.
- A run that states a count other than 3 states it on its stops, so its request fingerprint moves.
  The fingerprint of a run at the default count does not move.
- A VERIFY take's integrity record keeps its three repeat checks, not evaluated, as one summed sweep
  always left them.
- The bass view drops `passes`, which was always empty, so its schema moves to `jts_bass_view/5`
  (ADR-0344 §4).
- Left for their readers' own files: `plan_run.after_grading` now runs its effect at once
  (`correction_run_host.py` calls it), and `AnchorEvidence`'s `anchor`, `witness`, `shift_ms` and
  `witness_residual_ms` have no producer (`capture_dispatch.py` reads them).
- Rejected:
  - A `--sweeps` flag. An inline pose already states its count for one run.
  - A count on the run request. Every request would state it, and every fingerprint would move.
  - Repeated summed passes. The in-room round reads one pass (ADR-0429), and no reader needs more.
  - A cap on the count. The capture upload cap (5 MiB) is pinned at the default count, and no
    preset asks for more.
