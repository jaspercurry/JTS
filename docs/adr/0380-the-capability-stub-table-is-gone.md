# ADR-0380: The capability stub table is gone; an analysis gap is tracked where its work is planned

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes (partial)
  [ADR-0228](0228-rulings-carried-out-of-refactor-tuning-on-its-retirement.md) §7's citation of
  `measure_spec.py`; its rule that a capture hole is a loud stub stands for a future capture hole.
  Supersedes (partial)
  [ADR-0360](0360-near-field-driver-takes-are-reference-evidence-one-driver-per-pose.md)'s status
  clause "its splice stays the named hole `near_field_splice_not_implemented`".
- **Context:** `measure_spec.py` kept one stub row per mic-only hole: the near-field splice (R-3),
  distortion vs level (R-4) and the vertical-axis analysis (R-5a). Every row was `captured=True`.
  `TuningSession.measure` stopped a take only for a row with `captured=False`, so the check never
  fired, and nothing read the rows' codes.
- **Decision:**
  1. Every mic-only capture regime plays and banks, so no measure-time stub remains. The table,
     its check and its three `*_not_implemented` codes are deleted.
  2. An analysis gap is tracked on the ticket that plans its work: the near-field splice on
     [#5695](https://github.com/jaspercurry/JTS/issues/5695), carried on
     [#5928](https://github.com/jaspercurry/JTS/issues/5928); distortion vs level on #5928's #5724 S7
     row. The vertical-axis analysis (R-5a) has no tracker yet and is raised with the owner.
- **Consequences:**
  - A take no longer says which analysis its evidence still waits for; the tracking ticket does.
  - A future capture hole, a regime that cannot play, is a named stub again under ADR-0228 §7.
