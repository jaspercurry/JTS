# ADR-0358: One level rule for "B vs A"

- **Date:** 2026-09-24
- **Status:** Accepted (supersedes (partial) ADR-0355: its sentence that
  `bass-compare` and `room-grade` keep their own difference rules)
- **Context:** Views compared two curves under different level rules (issue
  #5661). The shared kernel removed `median(b) − median(a)`; bass-compare
  read each band as `median(b − a)`; `compare` averaged each octave. Aligning
  by the difference of medians let a filter in one band move the level of
  every other band: a bass cut below 120 Hz made room-grade call 120–350 Hz
  regressed (#5632 F12). On the campaign curves the two rules differ by up to
  1.2 dB in 30–40 Hz.
- **Decision:** The level that comes off a comparison is the median of the
  per-bin difference over its band, the level most bins agree on. A band's
  change is the same median over that band.
  `series_stats.curve_difference` owns the rule and `band_change_db` reads a
  band through it. `compare`, `candidates`, the forecast, room-grade and
  bass-compare all use it.
- **Consequences:** A filter that reshapes a minority of bins no longer moves
  the rest. With a −8 dB cut on 89–119 Hz only, both untouched room-grade
  bands read 0.00 dB where they read +0.76 and −0.82. A change still shows in
  the band it touches, including a filter's skirt past a band edge. A change
  across more than half the bins, such as a tilt or a shelf, still moves the
  level. The campaign room-grade pin moves from 5.124 to 5.087 dB at
  60–120 Hz; bass-compare's numbers are unchanged. Not decided here: one sign
  order across views (`candidates` reports a − b, `compare` b − a).
