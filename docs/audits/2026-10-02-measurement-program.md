# JTS measurement program audit — 2026-10-02

Frozen observations. Code anchors are `file:line` at `084638bc7` on `main`.
The audit read the code at `5ceb8c2db`. Each finding was checked again at
`084638bc7` before this report was written. The jts3 figures come from the take
records and the journal of runs 2 and 3 of the smoke test #6113 (main
`95cc4ea17`). This report records what was true then. It holds no plan and no
status. The plan built on it and the live work are on the tracking issue,
[#6227](https://github.com/jaspercurry/JTS/issues/6227).

## Scope and limits

The owner paused the jts3 smoke test (#6113) for this audit. The question was:
how many measurements does each tuning program play, why, and which of them
give information that some decision reads.

The audit was read-only. It read the measurement engine (programs, layouts,
schedules, excitation programs, capture, analysis, verdicts and banking), the
take records banked on jts3, the journal, and the dry run of each preset. It
played no sound, changed no code and posted nothing.

Words used below:

- **Sweep:** one full test sweep, about 3.5 to 4 s.
- **Probe burst:** one step of the auto-level probe (ADR-0405). The bursts
  start at −60 dBFS and go up by 6 dB a step.
- **Hold:** the time the speaker was busy with a run, from the journal.
- **Kept:** the sweeps of the takes whose verdict kept them.
- **(model):** computed from a model, not measured.
- **(inferred):** read from the code, not seen in a run.

The audit did not run the test suite. All its figures come from jts3.
jts3's rear woofer had a hardware fault during both runs, so no rear
measurement exists here.

## Baseline (jts3, runs 2 and 3)

| Program | Sweeps played (kept) | Probe bursts | Hold |
|---|---|---|---|
| speaker round (`c3302146c0a2`, `f85412c51407`) | 19 (13) | 7 | 357–369 s |
| speaker trial (`9cea964d3014`, `39023031c0c9`) | 5 | 7–19 | 94–118 s |
| bass round (`316a75955b30`, `084324888fff`) | 36 | 7 | 332–337 s |
| bass trial (`e1be550bff88`, `f3c58d6549b7`) | 66–69 of 72 | 7 | 600–628 s |
| room round (`847b24b5f912`) | 3 | 7 | 66 s |
| room trial (`88bdf87418ed`) | 6 | 7 | 120 s |
| rear/pair (`12d2c20e6afb`, `dc761905b4aa`) | 0 (rear woofer fault) | 22–77 | 148–643 s |

- A full run (speaker, rear, bass and room, each with a trial) plays about
  160 sweeps and holds the speaker for about 30 min. Bass is 108 sweeps and
  about 16 min of it.
- Run 3 held the speaker for 1,690 s: 68% sound, 23% analysis. Of the sound,
  47% was sweeps, 40% silence and 11% probes.
- Run 3's first bass trial (`42f0bfd16512`) stopped at the 85 dB stop on a
  room sound after 12 sweeps. The bass trial `f3c58d6549b7` was stopped by
  hand after 66 of its 72 sweeps.

## Findings

### F1. The bass level ladder answers nothing

- A bass round plays 3 seats × 4 levels (`LEVEL_OFFSETS_DB`,
  `jasper/active_speaker/run_levels.py:30`) × 3 averaged passes
  (`BASS_PASSES`, `jasper/active_speaker/bass_stimulus.py:28`) = 36 sweeps.
  A bass trial plays 72.
- Every level row in the 6 banked bass tables (24 rows) says
  `headroom_verdict: unknown`.
- No knee was found. Growth is linear (0.6–1.25 dB per dB).
- `position_spread_db` is hard-coded `None`
  (`jasper/active_speaker/bass_level_evidence.py:259`).
- No code decision reads any table field.
- The rungs play 59–76 dB at the seat. That is at least 12 dB under the point
  where the boost backs off (model). Reaching that point would break the 85 dB
  stop, so the ladder cannot measure what it was built for.

### F2. The seat fit of the Linkwitz source is unstable

- `bass-alignment --take` at the same seat spot gave 112.8 Hz/Q 1.23 (run 1),
  67.1 Hz/Q 0.38 (run 2) and 108.3 Hz/Q 0.98 (run 3).
- The near-field fits per sweep (40–300 Hz) agree: 83.8–85.6 Hz, Q about 1.0.
- The causes:
  - the room: the seat minus the near-field is −12.7 dB at 63 Hz and +6.7 dB
    at 106 Hz, stable between runs;
  - bins under 50 Hz below the 20 dB SNR floor.
- The fit weights every bin equally and never reads SNR
  (`jasper/active_speaker/bass_fit.py:127`, `:133`).
- The fits of runs 1 and 3 boost 25–63 Hz by about 4–5 dB too much (model).

### F3. A bass trial plays 9–13 dB too quiet

- Rule A (`run_margins`, `jasper/active_speaker/preflight.py:161`, its
  `lift_bound_db` at `:198`) cuts the shared fader by the candidate's declared
  bass reserve: 10.05 dB in run 2 and 14.20 dB in run 3.
- The measured rise was at most 6.2 dB and 3.9 dB.
- Below 63 Hz the base falls under the SNR floor, and the realized boost reads
  low.

### F4. Speaker analysis is slow on the Pi

- Each MEASURE take analyses for 54–87 s after it plays.
- 41–75 s of that is one delay search, `_select_summed_alignment_pair`
  (`jasper/audio_measurement/program_analysis/response.py:608`). It sits
  between the `branch_level_match` and `alignment_selection` log events
  (`jasper/audio_measurement/program_analysis/dispatch.py:575`, `:644`).

### F5. A passing take is replayed every speaker round

- The first MEASURE take passes. Its tweeter alignment SNR is 33.0 dB against
  35 dB, so the verdict asks `retake_louder`
  (`jasper/active_speaker/crossover_v2/capture_dispatch.py:386-395`).
- The take is replayed 3.3 dB louder, and the first one is discarded.
- This costs about 100 s per round, in runs 2 and 3.

### F6. The cardioid default never measures what its program states

- The rear row says "Set the rear woofer to reduce sound behind the speaker"
  (`jasper/active_speaker/measurement_programs.py:129`). But it starts
  `rear/pair` at the mark only (`:132`).
- `_rear_score` needs front and behind takes
  (`jasper/active_speaker/crossover_v2/rear_views.py:234-247`), so it is always
  unavailable.
- The page and the CLI start different rear presets. The page starts the row's
  `start` (`measurement_programs.py:671-673`). A bare program name on the CLI
  runs the program's first preset, `rear/express`.

### F7. Angles change no filter

- Each take is fitted alone. Position spread is report-only
  (`jasper/active_speaker/linearization_envelope.py:333`).
- The feature classifier needs 6 positions (`FEATURE_MIN_DEEP_POSITIONS`,
  `jasper/audio_measurement/interference_nulls.py:161`). `baseline_express`
  has 5 (`jasper/active_speaker/measurement_plans.json:29-38`).
- The seams `CloudFitTerms.excluded_bands_hz` and
  `FitVocabulary.boost_excluded_bands_hz` have no producer. The only
  constructors (`jasper/active_speaker/speaker_fit.py:86`, `:139`) set neither.

### F8. Staleness is global

- Any apply that changes the candidate or the config record makes every round
  stale (`jasper/active_speaker/crossover_v2/round_inputs.py:306-307`). That
  includes near-field rounds, which divide the DSP out, and speaker rounds,
  which play none of the upper layers.
- A preference EQ save changes the record too: the save compiles the applied
  tune again with the preference filters (`jasper/sound/graph_carrier.py:474`),
  and the record is the config's hash
  (`jasper/active_speaker/applied_identity.py:15`).
- Nothing asks to redo room after a later bass apply. The room is stale only
  when another program has a current round newer than room's
  (`jasper/active_speaker/commissioning_coordinator.py:47`).
- ADR-0301 already says that a bass-only or room-only change may reuse the
  speaker layer's evidence.

### F9. Nobody owns the broad wall and room bass gain

- The room layer is Peaking-only
  (`jasper/active_speaker/crossover_v2/room_prescription.py:478-482`), with
  boosts of at most 6 dB (`jasper/audio_measurement/room_limits.py:99-100`).
- Its ceiling is the 350 Hz fallback
  (`jasper/active_speaker/crossover_v2/room_views.py:88`,
  `jasper/audio_measurement/room_boundary.py:18`), because nothing writes the
  trusted floor (#6110).
- Bass has no preview model
  (`jasper/active_speaker/crossover_v2/prescription_document.py:260`).
- `room/seat` clears nothing. So a room re-fit adds a total on top of a median
  measured through the old room layer (inferred).

### F10. Retakes that cannot help keep playing

- The caps are 3 operator retries and 6 retakes per placement
  (`jasper/active_speaker/crossover_v2/admission.py:33`, `:35`).
- The dead rear woofer cost about 16 min across runs 2 and 3: probes to full
  scale, then a 10-minute hold timeout.
- The failed room round `2b9727465543` played 12 of 12 takes. All 12 failed the
  same way (`pilot_level_collapse`) at the same level.

### F11. The gate floor may claim too much

- jts3's speaker takes found no reflection. The window stayed at the 7 ms
  search ceiling (`SEARCH_T_MAX_MS`,
  `jasper/audio_measurement/gating.py:126`), so the takes trust everything
  above 357 Hz.
- The take's own disclosure says that no bin between that floor and the
  room's floor is proven clean, and asks to declare the rig geometry.
- The front cone is about 0.5 m from the wall. Its wall bounce arrives about
  3 ms after the direct sound, inside the window.

### F12. Small items

- The dry run prices a ladder per rung.
- The page's time estimate is too low: it showed 99 s for a 152 s program
  (`jasper/active_speaker/plan_run.py:256`).
- `mic_move_count` counts each driver as a placement
  (`jasper/active_speaker/measurement_programs.py:473`; the `place` key holds
  the driver, `:391-396`).
- The behind pose of `rear_behind` banks `distance_m: 0.1` for "halfway to the
  wall" (`jasper/active_speaker/measurement_plans.json:125-128`).
- The rear seed's CAD gap (0.2032 m,
  `jasper/active_speaker/rear_calibration.py:280-284`) is stamped as fitted,
  so a 200 mm declaration draws a wall-gap warning.
- `jasper-round trial` sends any document with a bass section, even
  `"bass": {}`, to the 24-take bass ladder (`trial_preset`,
  `jasper/active_speaker/measurement_programs.py:746`).
- `AGENTS.md:82` points at a deleted plan and at ADR-0229, which is superseded.
