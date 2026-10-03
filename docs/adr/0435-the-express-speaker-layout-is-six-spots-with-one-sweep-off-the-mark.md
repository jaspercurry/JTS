# ADR-0435: The express speaker layout is six spots, with one sweep per driver off the mark

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step D3 part 2. Amends [ADR-0430](0430-features-that-move-with-angle-stay-uncorrected.md),
  [ADR-0408](0408-over-a-timing-take-each-candidate-graph-probes-its-own-graph.md) and
  [ADR-0433](0433-a-passing-measure-take-is-kept-and-its-raise-rides-the-next-take.md), quoted below.

## Context

Step D3 of #6227: "a 6-spot linearization layout: the mark, ±20° horizontal, ±10° vertical, and one
more. 1 sweep per driver off axis; this needs a per-pose sweep count." Part 1 (ADR-0430) made the
speaker fit leave a feature that moves with angle uncorrected. Its classifier needs the feature at
least 2 dB deep at six positions (`FEATURE_MIN_DEEP_POSITIONS`). `baseline_express` had five
(finding F7 of the [2026-10-02 audit](../audits/2026-10-02-measurement-program.md)).
[ADR-0434](0434-sweeps-per-take-are-a-preset-field.md) gave a pose its own sweep count.

Two things stood in the way:

- **A take's sweep count was part of its stimulus shape.** `program.stimulus_shape_id` hashed every
  segment and the program's length, and a run-manifest set keys on that shape (ADR-0433 §4).
  Composed by the production composer and banked through a real `RunManifest`, the six-spot walk
  made two sets per driver: the mark (2 takes) and the spots (5). The directivity view reads one
  set, and it refused both: `too_few_positions` and `no_reference_take`.
- **The fit at a take needs three sweeps per driver.** The floor is
  `LINEARIZATION_MIN_PAIRED_OCCURRENCES`. With one sweep, σ is unavailable, the envelope allows
  0 dB, and the fit proposes no filter. The packet fitted every kept driver take, so each spot would
  have shown a fit with nothing in it.

## Decision

1. **The layout.** `baseline_express` is six spots 1 m out (`measurement_plans.json`):
   - the mark twice, at the preset's count (3 sweeps per driver);
   - then once each, at `"sweeps_per_take": 1`: 20° left, 20° right, 30° right, 10° below and
     10° above the mark.

   It is still one layout. No new layout was added.
2. **The sixth spot is 30° right.**
   - It is inside the listening window, which averages 0°, ±10° vertical and ±10°, ±20° and ±30°
     horizontal. So a feature the fit leaves alone is one that moves where people listen.
   - It adds a horizontal angle. On a symmetric baffle, ±20° mirror each other.
   - `baseline_full` already measures 30°, and the arm reaches it (±45°).
   - [Research 07](../research/2026-07-29-attribution/07-reanalysis-position-variance.md) §3 and §6
     ask for about six deep positions and name no angle.
3. **A spot counts once.** The fit's design cloud keeps the newest kept MEASURE take at each
   azimuth and elevation (`round_inputs.latest_measure_takes`). So the mark's two takes are one
   position, and six spots give the classifier six positions. With exactly six, a feature is
   classified only when it is deep at every spot.
4. **A MEASURE take's shape is one cycle.** `program.stimulus_shape_id` reads a MEASURE program as it
   plays at one sweep per driver (`_one_cycle`):
   - The cycles it repeats go, from the gap before the first repeat to the last repeat. The
     segments after them keep their place.
   - So a spot and a mark take of one driver, graph and fader share a set.
   - Every reader of the shape agrees: the set key (`run_manifest.set_basis`), `speaker_fit`'s set
     match, and the ADR-0408 basis comparisons.
   - A program with no repeated cycle keeps its shape id. A branch program's fixed repeats are not
     MEASURE cycles, so they stay part of its shape.
   - The occurrence suffix now has one reader, `program.occurrence_index`. `drift.py` imports it.
5. **The packet fits only what the fit's floor admits.** In `round_packet._fits`, a MEASURE take
   that plays a driver fewer than `LINEARIZATION_MIN_PAIRED_OCCURRENCES` times gets no fit for that
   driver. Its row reads `reason_summary: {"unavailable": "too_few_repeats"}`. The fit at the mark
   does not change. The floor does not change.
6. **A spot is never replayed for its timing.** One sweep per driver reads less alignment SNR
   (ADR-0433's context). The spots rely on ADR-0433 §1: a take off the mark that is short only of
   alignment SNR is kept.

### What this amends

- ADR-0430, consequences (line 33): "`speaker_mark` and `baseline_express` (5 poses) do not, and
  their fits do not change." `baseline_express` now has six positions, so its fit at the mark
  leaves a feature that moves with angle uncorrected.
- ADR-0408, consequences (line 52): "it compares the stimulus's shape (`program.stimulus_shape_id`),
  not its level." The shape now also leaves out how many sweeps each driver plays in a MEASURE take,
  and so does ADR-0433 §4's set key that reads it.
- ADR-0433 §3 (lines 40–41): "the fourth mark take of `baseline_express`". This is now the second
  mark take.

## Consequences

- **Hearing:**
  - A spot plays the same sweeps at the same gains, only fewer of them. The 30° spot plays at the
    run's fader, as every spot does.
  - These do not change: `volume_limit` 0.0, the graph doors, the `set_volume_db` clamp, the 85 dB
    commissioning stop and the declared driver caps.
- **Price** (the dry run's price on a two-way speaker): `--program speaker --layout baseline_express`
  is now 9 captures, 6 mic moves and about 381 s. Before, it was 10 captures, 5 moves and 486 s.
- **Sweeps:** 23 instead of 49.
  - The timing take plays 1.
  - The mark plays 2 takes × 2 drivers × 3 = 12.
  - The spots play 5 × 2 drivers × 1 = 10.
  - The level check plays pilots only.
- **What a spot carries:**
  - Its magnitude is within 0.01 dB of a 3-sweep take's (synthetic capture).
  - It has no in-capture drift reading, so its delay estimate is uncorrected for clock drift. Only
    the mark's timing feeds a decision (ADR-0433).
- **Readers:**
  - Directivity reads the five spots against the mark's two takes, in one set.
  - The mark's repeat spread reads one pair, not six.
  - The packet's `fits` keeps one unavailable row per spot and driver.
- **What moves:**
  - A MEASURE take that repeats its cycle gets a new shape id, so its set id moves once.
  - A round banked before keeps its stored set bases. Its `speaker-fit` now refuses
    `selected take does not match its manifest`, as ADR-0433 already made older rounds do.
  - Take records and `stimulus_id`s do not move. The drift reference still keys on the exact
    `stimulus_id` (ADR-0433 §5), so the first spot has no reference.
- **Rejected:**
  - A new six-spot layout beside the old one. The owner prefers one layout.
  - 20° below or above the mark as the sixth spot. It would show more crossover lobing (research 07
    §5), but it is outside the listening window.
  - A set key of its own for the count. The shape's other readers would then call two takes of one
    set incompatible.
  - Directivity reading across sets. That is a second grouping beside the design cloud's.
  - Lowering the fit's floor. One sweep gives no σ.
