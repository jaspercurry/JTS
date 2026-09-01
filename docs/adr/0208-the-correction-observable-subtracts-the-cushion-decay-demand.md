# ADR-0208: The correction observable subtracts the cushion-decay demand

- **Date:** 2026-08-31
- **Status:** Accepted (amends ADR-0109's observable definition)
- **Context:** ADR-0109 made the combo servo's observable the lane resampler's
  live correction ppm — "the same atomic the STATUS block reads". With the
  post-lock cushion decay active, that ratio embeds the decay's own commanded
  drain (~125 ppm at the default) on top of genuine host-vs-DAC offset, and the
  L0 servo chased the descent (live on jts3: cmd wandering −120..−350 ppm,
  issue #3466).
- **Decision:** The decay publishes its live drain demand (single source of
  truth, authority-clamped, nonzero only while actively stepping), and fan-in's
  `build_obs` subtracts it from the ratio before the probe/servo see it; the
  fill fed to the ladder's slope/variance estimator is likewise
  descent-compensated into ceiling terms. `Obs.correction_ppm` therefore no
  longer equals the raw `resampler.ratio_ppm` STATUS gauge during a descent —
  they legitimately differ by exactly the published `decay.demand_ppm`. An
  armed decay must leave `demand × 2 ≤ max_adjust_ppm` (fail-loud at config).
- **Consequences:** The servo steers only genuine clock offset; the ratio
  settles at the demand while descending. Not decontaminated (disclosed
  residuals on #3466): the refill window after a still-locked snap-back, and
  a ~one-interval EW transient at descent phase edges.
