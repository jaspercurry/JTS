# ADR-0416: The measurement overlays go

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes [ADR-0192](0192-the-campaign-is-the-validation.md) §2 (R-1 reverse
  polarity: build).
- **Context:** A `MeasureSpec` could state a polarity flip, a delay and a level match. They were the
  inputs of R-1's reverse-null walk (ADR-0192 §2). Its executor and its door (`jasper-null`) were
  deleted on 2026-09-22, and [ADR-0342](0342-one-measurement-path.md) gave up raw overlays for
  variants banked and compared as candidates. Since
  [ADR-0413](0413-one-resolver-the-door-resolves-every-run-request.md) no request can state a
  template, so no take carries an overlay. The overlays still passed through the session, its graph,
  the measurement emitter, the door and the v2 state. This is the deletion pass on
  [#5925](https://github.com/jaspercurry/JTS/issues/5925) (scout finding F2, item 2a).
- **Decision:**
  1. A `MeasureSpec` states no polarity flip, delay or level match. The session installs each
     take's graph with no overlay, and the program emitter takes none.
  2. A take's record drops `polarity`, `inverted_role`, `level_matched` and
     `level_match_trims_db`. They were always `normal`, empty, `false` and empty.
  3. A delay or a polarity flip is measured as a candidate (ADR-0342).
- **Hearing:** a scratch proof staged 5,352 requests (every preset and layout on a 1-way, 2-way,
  3-way and cardioid speaker, with trials, levels, movers, repeats, inline poses and drivers) and
  installed every graph the door stages through the real session and session graph: 36,295
  installs, byte-identical to main. 485 direct emits (program, commissioning and tuning graphs) are
  byte-identical too. `volume_limit`, the graph doors, the clamp, the 85 dB stop and the driver caps
  do not change.
- **Consequences:** The plan document's template drops five keys, so each walk's
  `request_fingerprint` moves once. The refusal codes `walk_polarity_not_accepted`,
  `walk_delay_not_accepted` and `walk_level_match_no_evidence` go. Rejected: keeping the overlays
  for a later reverse-null walk, because a candidate already carries its delay and polarity.
