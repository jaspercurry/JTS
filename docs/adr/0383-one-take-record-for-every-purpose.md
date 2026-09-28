# ADR-0383: One take record for every purpose

- **Date:** 2026-09-28
- **Status:** Accepted. Supersedes (partial)
  [ADR-0373](0373-a-room-bass-or-rear-take-banks-its-analysed-curves-on-its-record.md) §1-§3; its §4
  `take_curves_not_banked` stays for a CHECK-only or failed run. Supersedes (partial)
  [ADR-0371](0371-a-rounds-evidence-packet-is-built-once-when-it-is-banked.md) §2's rebuild of a
  banked round with no stored packet (a laptop-banked tree, or a round banked before ADR-0371) and
  §3's fingerprint kept for a round banked before it. Amends
  [ADR-0336](0336-the-seat-trial-judges-rear-and-room-from-the-same-seat-takes.md)'s `co_purposes`.
- **Context:** Only room, bass and rear takes banked their curves (ADR-0373), so the four record
  scanners read nothing from a speaker round and other readers decoded its recordings again. The owner
  wants one capture shape for every purpose ([#5737](https://github.com/jaspercurry/JTS/issues/5737));
  C0 made the scanners filter on phase, purpose and verdict.
- **Decision:**
  1. Every take banks `curves` (empty for CHECK) and `analysis` on its record. A take whose analysis
     failed banks `analysis_error` instead.
  2. Each curve names its `window`, `gated` or `ungated`. C3 adds a role's second window.
  3. The run manifest's rows copy the record's curves and analysis; nothing recomputes them.
  4. New facts go into blocks: `pose`, `level`, `verdict`, `analysis`. The flat keys stay until C1b
     and F1 move their readers; that is their removal condition.
  5. `analyzed_measurements` decodes no take. It passes over one with `analysis_error` and refuses
     `take_curves_not_banked` for one with neither. The bass view and the gated overlay decode until
     C4 and C3.
  6. The classifier's pose bank reads MEASURE and lateral speaker takes.
  7. The record's verdict is the take's own. The spec-level "incomplete" override and the abort rows
     stay manifest facts.
  8. A banked round whose `packet.json` holds no `evidence` refuses by that key; a live session still
     builds its packet. Under [#2902](https://github.com/jaspercurry/JTS/issues/2902), a round whose
     packet build failed at bank time stops loading.

  PRs B-D finish the rollout: `purposes` replaces `co_purposes` (PR B), then the `pose`, `level` and
  `verdict` blocks follow, with the assessment moved into the bank path.
- **Consequences:**
  - The room ceiling reads a speaker round's own gate. delay-landscape, classify-features, candidates
    and the measurements page read its speaker takes; the page draws refused retakes too.
  - A room take whose analysis failed is no longer rescued by a decode of its recording.
  - A two-way MEASURE take banks three occurrences per role, about 42 KB of curves, and the capture
    ring copies the record again.
  - The laptop bank (`scripts/bank-crossover-round.sh`) stores its tree's packet when it banks, as
    the Pi's bank does, so nothing rebuilds a laptop-banked tree's packet on read.
