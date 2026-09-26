# ADR-0371: A round's evidence packet is built once, when it is banked

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes (partial) [ADR-0346](0346-analysis-views-never-write-a-rounds-evidence.md):
  §3's rebuilt fingerprint, and its known exception (a `room` re-run moves the fingerprint).
- **Context:** Two documents were "the packet" ([#5660](https://github.com/jaspercurry/JTS/issues/5660),
  review R1-13 and R4-C5). `packet.json` is written once, when the round is banked. The crossover
  evidence packet was rebuilt by every `status`, `judge`, `compose` and round view, and candidates bind
  to its `packet_fingerprint` (ADR-0346). The rebuild read files that change after the bank:
  - `structural_history` scans the round's store, so every later round banked beside it with a
    candidate moved the fingerprint.
  - The room contract reads the `room` view's file, so a re-run moved it (ADR-0346's named exception).

  So the only fixed copy of a round's fingerprint was the one its bank already stored in
  `packet.json`.
- **Decision:**
  1. The bank builds the round's evidence packet once, from the round's banked inputs, by the rule
     readers used before: never its flow state or CamillaDSP statefile. It writes the packet into
     `packet.json` as `evidence`, beside `packet_fingerprint`, which covers exactly that block
     (`jts_round_packet/3`).
  2. `status`, `judge`, `compose` and the round views read that packet, not a rebuild; only the
     derived views (ADR-0346) are read per call. A round with no stored packet (a live session, a
     laptop-banked tree, a round banked before this decision) is built from its inputs when it is read,
     as before.
  3. A round's fingerprint is the one its `packet.json` stores. A round banked before this decision
     keeps the fingerprint its bank stored, whatever a rebuild now reads. `status --state`,
     `--drivers`, `--applied-profile`, `--repeat-floor` and `--declared-geometry` stay a what-if: the
     round's packet built with the named inputs swapped in, and fingerprinted as built. `judge` and
     `compose` take no such input, so a candidate never binds a what-if.
  4. A banked round's contracts read the room documents its bank stored in `packet.json` (`room`), not
     the `room` view's file. A re-run `room` view is a view: it changes neither the contract that is
     served and judged nor the fingerprint. A room the bank could not compute stays missing.
- **Consequences:**
  - The bank pays for the packet once, and a reader reads a file.
  - A later round banked beside it, or a re-run `room` view, cannot move a banked round's
    fingerprint, and `status`, `contract` and `judge` read the same room evidence.
  - A stored packet records the contracts as the bank computed them. `contract` and `judge` serve
    the contract code's current shape, so after a contract change the two can differ.
  - A round read without a stored packet fingerprints what it reads, as before.
  - A candidate composed while a rebuild had drifted from the stored value named a fingerprint no
    file kept. It matches nothing now, and it matched nothing after the next bank before.
  - Rejected: re-fingerprinting old rounds, which orphans every binding.
  - Rejected: a separate artifact name for the `room` view. The bank's copy in `packet.json` already
    holds the room evidence, so a second copy beside it would add a file and settle nothing.
