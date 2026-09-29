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
     lateral): a take its verdict measured and its run manifest selected. For each take it reads
     the record and the impulse the take keeps: the summed impulse, or else the take's one driver
     role, at repeat 0. It opens no recording or program and deconvolves nothing.
  2. Each impulse is on its take's own clock (ADR-0355). `raw_arrival_ms` counts from the take's
     sweep's scheduled start, less the take's clock shift. Between takes, only magnitude compares.
  3. A kept take that banked no band refuses `take_curves_not_banked` with `field: curves`. A kept
     take with no single kept impulse refuses the same reason with `field: impulses`. A round with
     no kept speaker take refuses `round_shape_inadmissible` if it holds takes of other phases, and
     `no_admissible_captures` if it holds none. No older shape is read (#2902).
  4. The proof is equality with a fresh decode. A kept impulse equals, at float32, the impulse the
     take's own analysis reads from its recording, and classification reads the same from either.
- **Consequences:**
  - On a production-like VERIFY program, the kept impulses and the whole-program decodes agree:
    - gated responses within 0.05 dB from 300 Hz to 16 kHz;
    - feature depths within 0.01 dB;
    - decay times within 0.2 ms.
  - The known-answer controls fail on that program's whole-program decode and pass on the kept
    impulse. So a synthetic minimum-phase peak now reads `defect-cuttable`, where before it read
    `ambiguous`.
  - The decay noise floor is read from the kept impulse's tail, 0.35 to 0.5 s after the sweep's
    start. It reads higher than before: −36 to −50 dB, against −61 to −73 dB.
  - Live bundles and laptop trees classify. A round whose takes kept no impulse refuses by field.
  - Captures are named by take id, and the artifact's schema is `jts_feature_classification/2`.
    The `--walk-log` flag goes, because a take's angle comes from its record.
  - `distortion` is now the capture ring's only reader. Moving it to records and removing the ring
    follow in #5737 C4 PR 2b.
  - Rejected: decoding each take's recording again through its analysis at view time. It is exact,
    but it repeats work the take has already banked.
