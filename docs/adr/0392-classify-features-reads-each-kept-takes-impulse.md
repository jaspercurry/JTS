# ADR-0392: classify-features reads each kept take's impulse

- **Date:** 2026-09-29
- **Status:** Accepted
- **Context:** `jasper-round-views classify-features` read a round's capture ring, bound each
  recording to a banked program WAV by its stimulus hash, and deconvolved the whole recording
  against the whole program. Every take already keeps the impulse its analysis read (ADR-0354), so
  this was a second decode of one recording by another path. It also pooled every capture in the
  ring, refused and replaced takes included. The whole-program decode depends on the program: it
  floors the program's power spectrum at 1e-3 of its peak, so pilots or courtesy tones that
  dominate the spectrum tilt the impulse. On one synthetic speaker, the kept impulses from two
  programs agree within 0.08 dB, and the whole-program decodes differ by up to 8.4 dB. A live
  bundle or a laptop tree has no ring, so neither could be classified
  ([#5737](https://github.com/jaspercurry/JTS/issues/5737) C4).
- **Decision:**
  1. classify-features reads each kept speaker take of an admissible phase (verify, cloud_verify,
     lateral): a take its verdict measured and its run manifest selected. Each take's capture is
     built by round_captures' own record builder (`record_captures`), from its record and the
     impulse it keeps: the summed impulse, or else the take's one driver role, at repeat 0. It
     carries the take's full declared pose and its role's own band. No recording or program is
     opened, and nothing is deconvolved.
  2. Each impulse is on its take's own clock (ADR-0355). `raw_arrival_ms` is the take's
     `TakeRead.arrival_ms`: from its sweep's scheduled start, less its clock shift. Between takes
     only magnitude compares, and the timing scatter pairs takes at one declared pose.
  3. Each decay band's noise floor is read before the arrival: from the 40 ms that end 10 ms
     before the onset, the window `impulse_shape` reads a peak's noise from
     (`NOISE_BEFORE_ONSET_MS`). A kept impulse holds 250 ms before its sweep's scheduled start, so
     that window is always there. Its tail is not a floor: it ends 0.5 s after the sweep's start,
     where a slow room mode can still be decaying.
  4. A kept take that banked no band refuses `take_curves_not_banked` with `field: curves`. A kept
     take with no single kept impulse refuses the same reason with `field: impulses`. A bundle
     with no kept speaker take refuses by why:
     - `classification_no_kept_takes` if it holds verify or lateral takes but kept none of them,
       because each was refused or replaced, or no run manifest selected it;
     - `classification_round_shape_inadmissible` if its takes are all of other phases;
     - `classification_no_admissible_captures` if it holds no take.

     No older shape is read (#2902).
  5. The proof: the analysis keeps each impulse as float32. Classifying the kept impulse gives what
     classifying a fresh float64 decode of the take's recording, through its own analysis, gives:
     every verdict equal, and every number within 1e-4 relative or 1e-6 absolute.
- **Consequences:**
  - On a production-like VERIFY program, the kept impulses and the whole-program decodes agree:
    - gated responses within 0.05 dB from 300 Hz to 16 kHz;
    - feature depths within 0.01 dB;
    - decay noise floors within 6.1 dB (median 1.7 dB), and times to −20 dB within 0.25 ms.
  - The known-answer controls fail on that program's whole-program decode and pass on the kept
    impulse. So a synthetic minimum-phase peak now reads `defect-cuttable`, where before it read
    `ambiguous`.
  - Live bundles and laptop trees classify. A round whose takes kept no impulse refuses by field.
  - Captures are named by take id, and the artifact's schema is `jts_feature_classification/2`.
    The `--walk-log` flag goes, because a take's pose comes from its record.
  - `distortion` is now the capture ring's only reader. Moving it to records and removing the ring
    follow in #5737 C4 PR 2b.
  - Rejected: reading the floor from the take's banked noise (C4 PR 1's `analysis.bass`). It is
    read on the bass bands only, and in dBFS of the recording, not in a feature's band against
    its impulse's peak.
  - Rejected: decoding each take's recording again through its analysis at view time. It is exact,
    but it repeats work the take has already banked.
