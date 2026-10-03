# ADR-0430: Features that move with angle stay uncorrected

- **Date:** 2026-10-03
- **Status:** Accepted. It amends no ADR sentence: the "report-only" rule that finding F7 cites is a
  code docstring, and it stays true (see Consequences).
- **Context:** Finding F7 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): angles change no
  filter. Each take is fitted alone, and the position spread over a round's design poses is
  report-only. The position-variance classifier (`feature_position_variance`, from
  [research 07](../research/2026-07-29-attribution/07-reanalysis-position-variance.md)) labelled
  each proposed filter after the fit and changed nothing. The two exclusion seams,
  `CloudFitTerms.excluded_bands_hz` and `FitVocabulary.boost_excluded_bands_hz`, lost their
  producer with the Gen A cloud pipeline (`9e733917c`, `4d5353536`). Owner decision 6 of
  [#6227](https://github.com/jaspercurry/JTS/issues/6227): driver linearization is gated at the
  mark, and angles come only after a reader uses them. This is step D3, part 1.
- **Decision:**
  1. **The reader.** `speaker_fit` fits a take as before. Then, for each role, it runs the
     classifier on each bell (`Peaking` filter) of that fit, against the role's design poses: the
     curves the round's verdicts read (`fit_feature_curves`). A shelf is not a feature.
  2. **The rule.** A bell whose feature the classifier calls `position_variant` gives one band:
     the span that the feature's centre walks over the positions that show it. That verdict needs
     the feature at least 2 dB deep at `FEATURE_MIN_DEEP_POSITIONS` (6) positions or more, with a
     frequency CV over 8%. A `source_fixed`, `unsure` or `too_few_positions` feature gives no band
     and stays correctable.
  3. **The seams.** When a role has a band, the take is fitted once more. Each role's bands go to
     `CloudFitTerms.excluded_bands_hz`, so the envelope allows no correction depth there, and to
     `FitVocabulary.boost_excluded_bands_hz`, so a boost aimed into one is dropped (#1967). There
     is one refit, and the classifier does not run on it again.
  4. **Disclosure.** `boost_evidence.excluded_bands_hz` lists each role's bands; it is empty when
     there are none. The round's verdicts still classify every proposed filter.
- **Consequences:**
  - A round with six or more design poses corrects less where features move. `baseline_full`
    (13 poses) qualifies now. `speaker_mark` and `baseline_express` (5 poses) do not, and their
    fits do not change. Part 2 of D3 follows: a 6-spot linearization layout with 1 sweep per
    driver off axis, which needs a per-pose sweep count.
  - The change only takes bands away from the fit. No cap, envelope term or boost permission
    widens. Inside the same envelope and caps, the refit can spend a filter slot that it freed on
    another feature. Its target level no longer reads the excluded bins, so its other filters can
    move a little. The fit's residual covers only the bins that it may correct.
  - When no feature is position-variant, which is always so below six positions, the fit
    documents stay byte-identical. The answer gets the empty `excluded_bands_hz` key; adding a key
    keeps the schema (ADR-0344 §4).
  - The classifier reads a feature only inside its bell's half-gain band. A feature that walks out
    of that band at some positions is not deep there, so it can stay corrected
    (`too_few_positions`). The fit of an off-axis take whose bell sits at one end of the walk sees
    only part of the walk, so it can keep its correction. Linearization is gated at the mark.
  - The rule that F7 cites, "Position spread is report-only"
    (`jasper/active_speaker/linearization_envelope.py:333` at `084638bc7`), stays true: the level
    spread over the positions is still only a disclosure. This decision is a separate use of the
    positions: the classifier's verdict.
  - Rejected:
    - Detecting features on the cloud before the fit. That is a second detector, and it needs a Q
      for each feature. The fit's own bells are the corrections in question, and the verdicts
      already classify them.
    - Excluding each bell's whole half-gain band. It is wider than the walk, and it can cover a
      fixed feature nearby.
    - Fitting again until no position-variant bell remains. That is an open loop on the Pi for a
      second-order case that the verdicts disclose.
    - The `classify-features` instrument (`crossover_v2/feature_classifier`). It reads impulses
      from disk, and its room verdict is about the gate window, not about position.
    - The envelope's old position-stability term, which cut depth by level spread
      (`6b4539574` made it a disclosure). Level spread is not a moving feature.
