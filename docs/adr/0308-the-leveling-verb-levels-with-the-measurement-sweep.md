# ADR-0308: The leveling verb levels with the measurement sweep

- **Date:** 2026-09-12
- **Status:** Accepted
- **Context:** On jts3 on 2026-09-12, noise leveled to 74.6 dB SPL at
  the fader cap. At −12.7 dB, summed sweeps read 80.7 dB SPL and a
  driver sweep read 87.1 dB SPL, crossing the 85 dB commissioning stop.
  The noise level did not predict the sweep's loudest window.
- **Decision:** Ratified by the owner in session: the leveling verb plays
  the room/bass summed program sweep through the accepted tuning graph.
  It reads the watch's `max_window_db_spl`, including for silent ambient
  observation. The session level and every take stamp share this statistic.
  Two successive in-band sweeps at the same fader must agree within 0.5 dB.
  Only the first sweep carries a courtesy prelude; none carries transfer pilots.
  This amends the stimulus clause of
  [ADR-0306](0306-one-auto-level-tool-levels-a-session-once.md).
- **Consequences:** A 75 dB session target refers to the measured sweep,
  leaving about 10 dB below the unchanged commissioning stop. Leveling
  takes longer. Noise normalization was rejected because it would still
  leave the session level and take stamps on different statistics.
