# Research: driver linearization — HF limits, envelope architecture, fact-check (2026-07-23)

Three owner-commissioned deep-research artifacts behind the Layer-1a
(driver linearization) design in
[`active-speaker-tuning-layers-design.md`](../../active-speaker-tuning-layers-design.md).
Preserved verbatim as primary sources; the design doc carries the adopted
synthesis and supersedes these where they disagree with each other.

1. [`01-hf-limits-report.md`](01-hf-limits-report.md) — metrology grounding:
   why measurement trust (mic calibration uncertainty, directivity, aiming,
   positional repeatability), not the driver, sets the HF correction limit;
   the static per-tier policy table (adopted as cold-start priors);
   audibility and boost-cap evidence; sweep-past-the-band design rule.
2. [`02-engineering-spec.md`](02-engineering-spec.md) — the envelope
   architecture: per-frequency `allowed_depth(f)` as a `min()` of
   independent limits; repeatability gate; closed-loop verification;
   multi-level linearity; excess-phase advisory; class priors; fitting
   policy; build order. NOTE its layer numbering is inverted relative to
   its own pipeline (it labels crossover "Layer 1" yet runs linearization
   first); the repo's 1a/1b naming is canonical.
3. [`03-fact-check.md`](03-fact-check.md) — claim-by-claim verification of
   the spec's assertions with primary sources; softens the boost stance to
   evidence-earned (clean excess phase + closed-loop verified) while
   leaving mic-trust binding; validates post-amp L-pad gain structure;
   confirms the closed-loop + multi-level + excess-phase combination is
   largely novel in shipping DIY tools.

The owner's dictated framing that governs all three: JTS does field
calibration of arbitrary hardware, not factory transducer design — the
product promise is measured neutrality across an honestly-disclosed band,
never "flat to 20 kHz."
