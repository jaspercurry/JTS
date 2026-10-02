# ADR-0412: ADR-0411's bound is relative to the reading it keeps

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes (partial)
  [ADR-0411](0411-a-probe-solves-from-its-highest-step-never-a-step-over-its-loudest-reading.md): its
  Consequences line on steps that read wrong low, and the same claim in its §2 ("which is ADR-0365
  §3's answer"). No code changes.
- **Context:** ADR-0411's Consequences say such steps "play at most one step louder than ADR-0365 §3
  solved, for any number of such steps". That is false as written. ADR-0411's stop keeps a full top
  burst that ADR-0365 §3 dropped, so the loudest reading it keeps is not always ADR-0365 §3's. On
  jts3's run 1 the solve is 6.78 dB over ADR-0365 §3's answer (there ADR-0365 §3 was wrong), and
  dropouts in the kept top burst and the one under it put it 9.2 dB over
  ([#6209](https://github.com/jaspercurry/JTS/pull/6209) review).
- **Decision:** These statements replace that line.
  1. A probe's solve is never more than one probe step (6 dB) above what the loudest reading it keeps
     solves, for any number of steps that read wrong low.
  2. Against the true level, the risk equals ADR-0365 §3's: each group of dropouts has the same worst
     case under both rules.
  3. A late finish (the player's reap can take up to 2.0 s) or lost capture frames can make
     `capture_dispatch._stop_gain` name a later burst and keep the cut burst's reading. That reading
     crossed the 76 dB bound, so it is the loudest, and the cost stays under one step (at worst −39.0
     against −44.9 dBFS).
  4. A loud room sound in a lower step costs more on the quiet side: a 72 dB sound in the −60 dBFS
     burst solves −53.0 dBFS against a true −40.2 (ADR-0365 §3: −59.0).
- **Consequences:** ADR-0411's rule and code do not change, and neither do the probe envelope, the take
  ceiling, the 15 dB raise limit and the 85 dB stop. A pin now stops a probe late in a burst, where the
  post-roll runs past the next burst's start, so `_stop_gain`'s post-roll subtraction is tested.
