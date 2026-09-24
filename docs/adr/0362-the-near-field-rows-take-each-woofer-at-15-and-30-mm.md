# ADR-0362: The near-field rows take each woofer at 15 and 30 mm, with no re-seat

- **Date:** 2026-09-24
- **Status:** Accepted. Supersedes (partial) [ADR-0360](0360-near-field-driver-takes-are-reference-evidence-one-driver-per-pose.md)
  Consequences: the bundled rows' second 15 mm take.
- **Context:** ADR-0360 had the bundled rows (`nearfield/woofer`, `nearfield/rear`,
  `nearfield/cardioid`) take each woofer at 15, 30 and 15 mm again, so the last take re-seated the
  microphone to prove the ruler placement. The 09-23 hand-built runs had already proved it: a
  re-seat within 0.1 dB. In the first web session ([#5684](https://github.com/jaspercurry/JTS/issues/5684),
  2026-09-24) the owner judged the extra placement pointless, and the round stopped at it.
- **Decision:** Each bundled row takes each woofer at 15 and 30 mm. A re-seat, when a placement
  looks suspect, is an inline pose list that names one distance twice.
- **Consequences:** `nearfield/cardioid` asks for 4 placements, not 6. The 15 → 30 mm step
  against the piston stays the view's self-test; `reseat_spread_db` is null unless a pose list
  places one distance twice. Rejected: a separate re-seat row, a fourth row for a check already
  proven.
