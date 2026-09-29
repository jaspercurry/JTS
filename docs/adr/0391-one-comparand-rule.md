# ADR-0391: One comparand rule

- **Date:** 2026-09-29
- **Status:** Accepted. States the rule [ADR-0390](0390-there-is-no-discrete-before.md) §2 names.
- **Context:** ADR-0390 removed a round's discrete "before". It said a comparison with an earlier
  take picks its comparand by [#5737](https://github.com/jaspercurry/JTS/issues/5737) P6's one
  rule, but no decision stated that rule. `jasper-round-views compare` named both sides, and side
  A defaulted to its set's unique or on-axis take. No reader paired a take with an earlier round's
  take. Every take keeps its curves (ADR-0383) and impulses (ADR-0354), so any earlier banked take
  stays comparable.
- **Decision:**
  1. A take's comparand is its round's base take at the take's place, preferring the take's own
     run: a bass-ladder rung's base plays in the rung's run. If there is none, the comparand is
     the newest selected take banked before the round with the same place, drivers (the response
     read: a driver target, or summed) and graph scope.
  2. `crossover_v2/round_inputs.comparand` owns the rule. It walks the latest 32 banked rounds
     through `banked_rounds` and reads their run manifests. It adds no index, scan or record field.
  3. The same-round A/B stays the decision evidence (measurement-loop doctrine §3). An earlier
     round's take is context.
  4. Every comparison over the pair discloses `compare_capture_basis`, and a basis difference is
     shown, never refused (ADR-0101).
- **Consequences:**
  - `compare` with one round and no side A reads the take's comparand, and says how it found it
    in `comparand` (`same_round_base` or `earlier_round`). With no comparand, it refuses
    `compare_no_comparand`, since nothing is left to compare.
  - A base take has no same-round comparand, so its comparand is an earlier round's take.
  - A take older than the 32-round window is not found.
  - P6's other users adopt the rule as they land:
    - A rear round discloses its reference against the newest earlier banked round's reference at
      the same place (#5404 09-20 item 7). The reference keeps the rear view's meaning, the
      rear-muted candidate or else the incumbent. The disclosure goes in the packet and never
      refuses.
    - The measurements page's B side, once the owner picks its reading on #5925.
    - `bass-compare`, after #5737 C4.
  - Rejected: a durable per-round "before" (ADR-0390); an index of comparands, since the banked
    manifests already are one.
