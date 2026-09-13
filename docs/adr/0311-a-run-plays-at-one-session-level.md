# ADR-0311: A run plays at one session level

- **Date:** 2026-09-13
- **Status:** Accepted
- **Context:** The bass program was the only user of the in-run level axis.
  Its two-window runs produced no usable table. The owner retired this axis
  in the tuning-loop work tracked by issue #4942.
- **Decision:** A run plays every pose at the session level, the seat-level
  anchor in `request.level.resolved`. There is no in-run level axis. Judge a
  bass boost from the room program's low band at the session level; the
  woofer's level-dependent boost schedule stays a declaration.
  This supersedes ADR-0304's level axis; its canonical-pose-set and JTS3
  campaign-limit decisions stay live.
- **Consequences:** Each run opens one measurement door. Old request documents
  must be restaged as version 4. A historical run with several base sets
  returns `room_incumbent_set_ambiguous` for its room incumbent.
