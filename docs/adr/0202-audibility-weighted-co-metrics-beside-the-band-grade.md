# ADR-0202: Audibility-weighted co-metrics beside the band grade

- **Date:** 2026-08-31
- **Status:** Accepted

## Context

Deep-research reports 01 and 02 (banked under
[`research/2026-08-31-tuning-methodology-deep-research/`](../research/2026-08-31-tuning-methodology-deep-research/00-adjudications.md))
establish that a flat ±dB tolerance is not audibility-shaped — detection
thresholds depend strongly on Q (low-Q deviations audible near 0.25–1 dB,
high-Q needing ~10 dB; Toole & Olive 1988) — and that the published
alternative is Olive's preference-model metrics (NBD, SM; AES 2004,
US 8,311,232, computed on 1/20-octave-smoothed curves). They also
establish that single-axis fitting risks correcting axis-local artifacts,
which is why every standard's headline curves are spatial averages. Against
that: `flat_spec.SPEC_BANDS` is the acceptance lineage every campaign has
graded on, and comparability across campaigns is itself evidence.

## Decision

1. **NBD and SM are computed and co-reported** (ticket 6.13), per Olive's
   published definitions at 1/20-octave smoothing via the shared smoother,
   on two curves per graded round: the on-axis curve and the pooled
   horizontal window (the 0/±7/±22° average, named
   `pooled_window_horizontal` — deliberately not "listening window", which
   in CTA-2034 includes vertical poses this rig does not capture).
2. **`SPEC_BANDS` stays the acceptance metric.** Co-metrics inform; they
   never gate and never veto.
3. **Whether the fitter's target moves** from the on-axis curve to the
   pooled window is decided on measured evidence — the Wave-6 re-analysis
   of banked rounds and the recommissioning campaign's own data — by owner
   ruling, recorded as its own ADR. The reference axis stays the declared
   0° design axis (IEC 60268-5 makes the reference axis a declared
   quantity); the pooled window subsumes the "measure horns off-axis"
   convention without moving the microphone.

## Consequences

Every round gets two lenses — lineage-comparable pass/fail and
audibility-shaped bumpiness/smoothness — without a lineage break. The
target question gains an evidence path instead of a doctrine fight. Cost:
two more numbers per round that must never be conflated with the grade;
the artifact labels them as co-metrics.
