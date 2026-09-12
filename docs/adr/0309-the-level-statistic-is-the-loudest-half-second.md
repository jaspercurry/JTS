# ADR-0309: The level statistic is the loudest half-second

- **Date:** 2026-09-12
- **Status:** Accepted
- **Context:** On jts3, silent-room maxima of 335 microphone periods
  ranged from 61.4 to 73.2 dB SPL, hiding the roughly 53 dB sweep at
  a −40 dB fader. A one-second ambient maximum under-read the longer
  capture, so leveling followed noise and exhausted its reading budget.
  The 72 dB ambient burst was room noise, not graph settling.
- **Decision:** Ratified by the owner's standing rule: "fix clear bugs
  on the spot". Session level, silent ambient, and every take use
  `loudest_half_second_db_spl`: the maximum RMS of consecutive 0.5 s
  windows on the capture sample clock. Count the last partial window
  only when it spans at least 0.25 s. This averages short transients
  while following the sweep's loudest region. Observe ambient for the
  composed program's duration. The per-period `max_window_db_spl` and
  commissioning stop remain unchanged; retain that maximum as stop
  evidence in each take. This amends the statistic clause of
  [ADR-0308](0308-the-leveling-verb-levels-with-the-measurement-sweep.md).
- **Consequences:** A 75 dB target still means the sweep's loudest part,
  retaining the intended roughly 10 dB margin to the 85 dB stop.
  Whole-sweep Leq was rejected: it would read roughly 10 dB lower and
  drive the loudest region toward the stop. Ambient takes longer;
  the buried, agreement, budget, and 2/6 dB drift rules stay unchanged.
