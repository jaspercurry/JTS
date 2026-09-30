# ADR-0401: Predicted topology corners get a shortlist order

- **Date:** 2026-09-30
- **Status:** Accepted. Supersedes (partial)
  [ADR-0336](0336-the-seat-trial-judges-rear-and-room-from-the-same-seat-takes.md)'s "software
  never ranks candidates", for predicted corners only.

## Context

`judge --preview --vary` forecasts each crossover corner and order of a topology document from a
round's branch take ([#5928](https://github.com/jaspercurry/JTS/issues/5928) TB11,
[#6103](https://github.com/jaspercurry/JTS/pull/6103)). The
forecast carries no score (`forward_model`), so an agent that wants a shortlist of corners must
compute a figure from each forecast itself. The toolbox rule is that an agent never writes its
own analysis (#5928). The owner chose one flatness figure over one shared band
([#5925](https://github.com/jaspercurry/JTS/issues/5925), call D3 = A).

## Decision

1. **The figure.** When a grid's axis moves the `topology` section, each row that forecasts
   carries `flatness`: the forecast read against a flat line, with the level removed by
   [ADR-0358](0358-one-level-rule-for-b-versus-a.md)'s median rule
   (`series_stats.curve_difference`), summed up by `series_stats.deviation_summary`
   (`rms_db`, `max_abs_db`, `max_abs_hz`, `mean_abs_db`, `bins`).
2. **One band.** Every row is read over the same band: the lowest corner ÷ 2 to the highest
   corner × 2 (`comparison_bands.OVERLAP_OCTAVE_RATIO`), inside the take's trusted band. The
   answer states it as `parameters.rank_band_hz`. If the band holds no bin, each row's
   `flatness` is the `coverage_short` gap, and no row ranks.
3. **The rank.** Rows rank by `rms_db` (1 is the flattest) and are listed in rank order.
   Refused rows follow in grid order. The grid answer is `jts_prescription_preview_grid/4`.
4. **What stays.** The rank only makes a shortlist. The same-round trial measures the pick and
   decides ([doctrine](../measurement-loop-doctrine.md) §3). ADR-0336 stands for measured
   candidates, and a grid that does not move the corner ranks nothing. When
   [#5661](https://github.com/jaspercurry/JTS/issues/5661) picks its one flatness formula, the
   rank reads that formula.

## Consequences

- One answer gives an agent the corners to trial, with no analysis of its own.
- The rank is a forecast from one take at one pose. A corner far below the measured one leans on
  branch data that the old crossover attenuated, so the trial is the check.
- Rejected: `predicted_ripple_db` (the maximum minus the minimum over each corner's own
  Fc ± 1 octave). It grades each corner over a different band, and one bin sets it.
