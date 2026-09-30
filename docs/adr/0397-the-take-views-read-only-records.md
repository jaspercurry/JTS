# ADR-0397: The take views read only records

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial)
  [ADR-0354](0354-every-take-keeps-its-measured-impulses.md) §3's "A take banked before this keeps the
  old routes", for the route that rebuilds an impulse from the take's recording, and
  [ADR-0355](0355-take-views-read-the-way-rew-reads-traces.md) §1's "A take with no kept impulse for
  that role is rebuilt from its recording and read over about the span a kept one holds".
- **Context:** Every take keeps the impulses its analysis measured (ADR-0354), and the bass,
  distortion and classify-features views read records
  ([#5737](https://github.com/jaspercurry/JTS/issues/5737) C4; ADR-0392, ADR-0394). The shared
  reader, `crossover_v2/round_captures.py`, still rebuilt a summed impulse from a take's WAV,
  deconvolved against the whole program, when the take kept none. To bind that rebuild it hashed
  every capture WAV and every program WAV, and it still read the legacy `summed/summed_*.json`
  sidecars. So `sweep`, `impulse`, `group-delay`, `decay`, `compare` and `judge --preview` read
  recordings, and `sweep --scope round` read a MEASURE take's summed role as a whole-program
  pseudo-sum. No backward support is kept ([#2902](https://github.com/jaspercurry/JTS/issues/2902)).
  This is [#5928](https://github.com/jaspercurry/JTS/issues/5928) TB7b.
- **Decision:**
  1. `round_captures` reads a take from its record and the impulses it kept. A role's impulse is the
     one the take kept, else the one its branch diagnostic retained (#5632). Both are on the take's
     recording clock. Only canonical take records are read, and no recording or program is opened,
     hashed or deconvolved.
  2. A capture's `capture_wav_sha256` is the hash its record declares. No view checks a WAV against
     it: no answer reads a byte of the WAV, and the kept impulses are checked against their own hash
     (ADR-0354 §2).
  3. A take that kept no impulses and no branch diagnostic refuses `take_curves_not_banked` with
     `field: impulses` and the role. Measuring again banks them (ADR-0002).
  4. A take that kept impulses, none of them for the role asked, refuses `round_role_not_recorded`
     with the roles it kept. A MEASURE take keeps its drivers' impulses and no summed one. So
     `sweep --scope round`, which reads `summed` unless `--set` names another role, leaves each
     MEASURE take out under that code, and refuses with it when every take is one. The code is not
     `take_curves_not_banked`: measuring again does not give a MEASURE take a summed impulse
     (ADR-0002). Another role, or another take, answers.
  5. `frequency` loses `--analyze-wavs`, `--calibration-root` and `--reference-db`. On a round or a
     bundle it reads the curves each take banked, tagged with the set and selection its run
     manifest gives them (`base`, `set_id`, `selected`, the window and its gate). The gated
     overlay, which decodes a room take's recording, is read only by the round's bookkeeping, until
     #5737 C3 deletes it.
  6. Every catalog row reads `record`, except the laptop tools. ADR-0393 §2 keeps `recording` in the
     vocabulary.
- **Consequences:**
  - A take banked before ADR-0354 that kept no impulse no longer answers these views; it refuses by
    field. A round whose recordings were removed still answers from its records, and a preview's
    prediction fingerprint does not change.
  - A capture no longer names a program file. The `program` key leaves each view's capture row and
    `program_wav` leaves the sweep's poses, and `frequency` answers without `analyze_wavs` and
    `reference_db`. So, by ADR-0344 §4, `impulse`, `group-delay` and `decay` move to `/2`;
    `compare`, `sweep --scope round`, the frequency view (`frequency` and `sweep --scope take`,
    `jts_frequency_view`) and `judge --preview [--vary]` move to `/3`.
  - Rejected: keeping the WAV hash check. It reads every recording's bytes for no number a view gives.
  - Rejected: `take_curves_not_banked` for a MEASURE take's summed read, because its next action,
    measure again, cannot give that take a summed impulse (item 4).
