# ADR-0371: A round's evidence packet is built once, when it is banked

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes (partial) [ADR-0346](0346-analysis-views-never-write-a-rounds-evidence.md):
  §3's rebuilt fingerprint, its known room exception, and its known debt (the fingerprinted
  `feature_classification` and `harmonics` blocks).
- **Context:** Two documents were "the packet" ([#5660](https://github.com/jaspercurry/JTS/issues/5660),
  review R1-13 and R4-C5). `packet.json` is written once, when the round is banked. The crossover
  evidence packet was rebuilt by every `status`, `judge`, `compose` and round view, and candidates bind
  to its `packet_fingerprint` (ADR-0346). The rebuild read files that change after the bank:
  - `structural_history` scans the round's store, so every later round banked beside it with a
    candidate moved the fingerprint.
  - The room contract reads the `room` view's file, so a re-run moved it (ADR-0346's named exception).

  So the only fixed copy of a round's fingerprint was the one its bank already stored in
  `packet.json`. Half the rebuilt blocks carry nothing on current rounds, because their writers (the
  round receipt, the cloud group, the finding sets) went with the Gen A engine.
- **Decision:**
  1. The bank builds the round's evidence packet once, from the round's banked inputs. These include
     its frozen flow state and CamillaDSP statefile. The bank writes the packet into `packet.json` as
     `evidence`, beside `packet_fingerprint`, which covers exactly that block (`jts_round_packet/3`).
  2. `status`, `judge`, `compose` and the round views read that packet, not a rebuild; only the
     derived views (ADR-0346) are read per call. A round with no stored packet is built from its inputs
     when it is read: a live session (without its flow state and statefile, which it rewrites as it
     runs), a laptop-banked tree, or a round banked before this decision.
  3. A round's fingerprint is the one its `packet.json` stores. A round banked before this decision
     keeps the fingerprint its bank stored, whatever a rebuild now reads. `status --state`,
     `--drivers`, `--applied-profile`, `--repeat-floor` and `--declared-geometry` stay a what-if: a
     packet built from the named inputs and fingerprinted as built. `judge` and `compose` take no such
     input, so a candidate never binds a what-if.
  4. The blocks no current round fills leave the packet:
     - the round receipt's `round`, `crossover_region` and `incumbent.from_round_receipt`;
     - the cloud group's `spec`, `flatness`, `curve`, `positions`, `honesty_mask` and `reflections`,
       and the accuracy-budget components built from them;
     - `findings` and `verify`;
     - the fingerprinted `feature_classification` and `harmonics` copies of pre-ADR-0346 view files.

     A reader that needs one of them says it is missing.
- **Consequences:**
  - The bank pays for the packet once, and a reader reads a file.
  - A later round banked beside it, or a re-run `room` view, cannot move a banked round's
    fingerprint.
  - A round read without a stored packet still fingerprints what it reads, as before.
  - A candidate composed while a rebuild had drifted from the stored value named a fingerprint no
    file kept. It matches nothing now, and it matched nothing after the next bank before.
  - Rejected: re-fingerprinting old rounds under the new block set, which orphans every binding.
  - Rejected: keeping the full builder for old rounds. It keeps the retired blocks alive for readers
    that are gone, on rounds from a deleted engine; banked evidence stays readable within the active
    campaign only (#2902).
