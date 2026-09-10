# Bass extension: implementation and measurement plan

The current program replaces the previous sealed-only runtime and bench. The
owner requested a small, measured bass extension that retreats smoothly as
volume or low-frequency demand rises. Absolute excursion limits are a later
measurement task; this release does not claim to estimate them from far-field SPL.

## Three programs, one tuning stack

1. Speaker tuning owns driver linearization, crossover, delay, polarity, and trims.
2. Room correction starts with the saved speaker tune and measures the listening
   area. It can correct bass and supported broad deviations higher in frequency.
3. Bass extension starts with the saved speaker tune plus Room correction. It
   adds an optional low-frequency layer, then verifies that layer at several levels.

The existing candidate bank, graph compiler, measurement door, trial receipt,
DSP writer, and applied snapshot own these steps. The bass descriptor lives in
the fingerprinted candidate and saved recomposition snapshot. There is no second
bass profile file, apply transaction, or background controller.

## Runtime

`jasper/bass_extension/dynamic.py` owns the descriptor and volume law.
`dynamic_graph.py` builds and verifies the native CamillaDSP fragment.

- The original signal keeps its saved speaker and Room filters.
- A native low shelf forms an extra bass signal: filtered output minus original.
- A detector measures low-frequency demand after source volume. Its compressor
  reduces only the extra bass, with no makeup gain.
- Canonical listening volume controls the shelf through Aux1. Native DSP ramps
  changes over 400 ms. The existing volume coordinator owns those writes.
- At zero extension the extra signal is zero. An optional high-pass acts only
  on the extra signal. It does not add a cutoff to the original speaker path.
- Existing output limiters and the non-positive volume ceiling remain in place.

The descriptor permits at most 12 dB of low shelf boost. Its admission gain
reserve is a filter gain bound, not a waveform peak or driver excursion model.
Group playback is not supported with dynamic bass until canonical volume reaches
every output endpoint; static speaker tuning retains its existing behavior.

The architecture draws on the added-bass branch and demand reduction described in
[Microsoft US12342139B2](https://patents.google.com/patent/US12342139B2/en), with
volume-dependent extension also informed by
[Google US10200003B1](https://patents.google.com/patent/US10200003B1/en). It uses
CamillaDSP's existing filters and compressor instead of a new audio processor.

## Measurement contract

`jasper-measure` is the shared capture path. Position plans remain configurable;
the normal Room plan has eleven positions. The current microphone-arm experiment
uses center, −20°, and +20°. It is a limited spatial smoke test, not an eleven-point
listening-area survey.

- `speaker_tune` measures the saved speaker layer.
- `room_tune` measures saved speaker plus Room, with bass extension off.
- `bass_candidate` measures the same upstream layers plus a named bass candidate.
- `applied` measures the complete saved stack.

`--volume-db` holds both Main and the bass reference for a batch and restores them
on exit. Take records retain the measurement level. `--level-dbfs` varies stimulus
level at that held volume. These are different experiments and must be compared
at matching settings. All measurements use the calibrated wired microphone.

For this experiment, use 20–20,000 Hz sweeps at all three positions. Start quietly,
then increase level in small steps. Request an 80 dB SPL stop and aim near 75 dB
to leave response and stop-delay margin. Stop on excessive SPL, failed capture,
or abnormal sound; do not treat the measured level as a discovered driver limit.
Declared frequency, level, duration, and repeat limits still govern playback.
The speaker measurement-quality floor is not the Room sweep floor.

## Completion checks

- Review native DSP math and the shared capture/apply boundaries.
- Pass focused tests, fast checks, merge checks, and CI before merging.
- Deploy through the standard Pi deploy script and confirm the running version.
- Take matched baseline and bass measurements at all three positions and several
  volume settings. Inspect response, spatial variation, noise, and distortion.
- Save only the selected measured candidate through the existing apply path.
- Show a measured before/after graph, report the tested level range, and return
  the arm to center.

Copyable UI prompts and program-specific runbook work are deferred to
[issue #4768](https://github.com/jaspercurry/JTS/issues/4768).
