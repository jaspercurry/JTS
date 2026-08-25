# Research — attribution stage (2026-07-29)

Primary sources for the attribution-stage work order, in two groups:
`01`–`03` are verbatim prior-art research (two architect-written briefs and
the owner-run deep-research report produced from the first of them); `04`–`08`
are WO-0's own findings documents — the corpus retrospective the plan's seed
mechanism table cites row by row. Bulk data behind `04`–`08` (CSVs, WAVs,
plots, scripts, the machine-readable corpus index) stays in the gitignored
`captures/wo0-retrospective-20260729/`; laptop-side session evidence stays in
the rest of the `captures/` tree.

## Prior art

- [`01-brief-measure-diagnose-prescribe.md`](01-brief-measure-diagnose-prescribe.md)
  — the brief the owner ran: prior-art targets (Trinnov named findings,
  Genelec GLM/GRADE non-DSP prescriptions, Klippel stimulus→model-fit,
  Farina ESS harmonic separation, CTA-2034 frame science), the
  discriminating-probe catalog, and the cautionary catalog. Written
  2026-07-29 after the 2 kHz crossover-notch discussion.
- [`02-dissertation-measure-diagnose-prescribe.md`](02-dissertation-measure-diagnose-prescribe.md)
  — the owner-run report from brief 01, preserved verbatim. Its Stage 0–4
  blueprint (validity gate → prioritized mechanisms → GRADE-style
  fix-class routing → confidence-gated prescription with
  refuse-and-recommend-the-probe) is the plan's skeleton. Written blind to
  the same day's Fc forensics, it independently predicted the
  two-mechanisms-stacked verdict; its load-bearing arithmetic correction is
  that τ≈303 µs is a ~10 cm **path-length difference**, implying a ~5 cm
  horn depth if the path is an internal round trip.
- [`03-brief-iterative-dialin-and-position.md`](03-brief-iterative-dialin-and-position.md)
  — the second brief: bounded dial-in loops and stopping rules (Q1–Q3,
  Q5–Q6) plus measurement-position awareness (Q4). Research input for
  WO-7 (the dial-in loop) and issues
  [#1876](https://github.com/jaspercurry/JTS/issues/1876) /
  [#1877](https://github.com/jaspercurry/JTS/issues/1877). Its report was
  still outstanding when this directory was created, and remains so.

## WO-0 corpus retrospective (both passes reported 2026-07-29)

Read-only sweeps of the 2026-07 corpus. These are the evidence the plan's §4
seed table cites; nothing here is a new measurement.

- [`04-mechanism-frequency.md`](04-mechanism-frequency.md) — per seed
  mechanism M1–M6: which sessions show it, at what magnitude, at what
  evidence tier, and what the corpus *cannot* answer. Proposes M7
  (inter-driver level frame) and M8 (vertical lobing at Fc), and lists eight
  corrections to the plan's original seed table.
- [`05-instrument-error-catalog.md`](05-instrument-error-catalog.md) — every
  case in the corpus where a shipped verdict misled, with its cause class
  (`frame` dominates at 10 of them), issue linkage, and evidence. Includes
  the #1855 root cause and the VERIFY tracking-band finding WO-5 depends on.
- [`06-reanalysis-farina.md`](06-reanalysis-farina.md) — the P6 harmonic-IR
  pass: extractor validation against synthetics, the LF-high-pass blocker and
  its fix, JTS3-vs-iLoud and per-driver distortion, why an onset level is not
  derivable from existing data, and a recommended detector with thresholds.
- [`07-reanalysis-position-variance.md`](07-reanalysis-position-variance.md)
  — the P2 pass: the feature-stability table (source-fixed CV 0.6–1.5 % vs
  room CV 15–17 %), per-position τ, the Fc notch's negative result, the
  data-layer finding that per-position curves are never persisted, and a
  recommended classifier.
- [`08-corpus-index.md`](08-corpus-index.md) — what exists and where: four
  stores with no shared identifier, the retention ring's state, per-bundle
  provenance, and the self-description gaps the quick-sweep harness must fix.

These are point-in-time artifacts. For `01`–`03`, cited product behavior,
patent numbers, and forum evidence reflect mid-2026 and are not maintained;
for `04`–`08`, the corpus itself moves (the retention ring drops its oldest
capture on every new one). The adopted decisions, as amended by review, live
in [`docs/attribution-stage-plan.md`](../../historical/attribution-stage-plan.md) — read
that for current doctrine. Owner rulings and adoption notes are recorded on
[issue #1866](https://github.com/jaspercurry/JTS/issues/1866).
