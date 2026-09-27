# ADR-0375: The volume owner arbitrates levels only; the graph-swap duck stays outside it

- **Date:** 2026-09-27
- **Status:** Accepted. Supersedes (partial)
  [ADR-0213](0213-the-reconciler-asks-the-dsp-writer-lock-before-it-corrects-the-fader.md):
  the consequence sentence that a reconcile write is a HOUSEHOLD declaration
  "which a held `TRANSIENT_DUCK` outranks at the owner". Supersedes (partial)
  [ADR-0004](0004-duck-release-algebra-and-reference.md): the consequence that
  the single volume owner inherits `min(reference, current + depth)` as its
  release algebra. Both decisions stand.
- **Context:** `VolumeOwner` ranked four claim kinds, and the fourth,
  `TRANSIENT_DUCK`, was an attenuation rather than a level. Its holders were the
  legacy Camilla `Ducker` and `CueDuck`, and both are deleted. Voice ducks run
  in fan-in, and the one live main-fader duck, the graph-swap bracket
  (`CamillaController._graph_mutation`), writes the fader directly: ADR-0213
  rejected a claim for it, because the owner is per-process. So the owner's
  duck path had no production caller
  ([#5754](https://github.com/jaspercurry/JTS/issues/5754)).
- **Decision:** The owner arbitrates three LEVEL claims: household <
  session-measurement < commissioning. `acquire_duck`, `TRANSIENT_DUCK`,
  `duck_depth_db`, `target_db` and the duck branch of `release` are deleted,
  with `release`'s `household_level_db` argument, which only a duck needed. A
  release restores the next level outright. The graph-swap bracket is the one
  main-fader duck. It releases by ADR-0004's algebra in
  `volume_latch.duck_release_target_db`, whose `entry_db` is now required. The
  DSP writer lock keeps the reconciler off the fader during a swap (ADR-0213),
  not owner rank.
- **Consequences:**
  - No runtime path changes: nothing in production took a duck claim.
  - A new main-fader duck has no owner rank to lean on. Without the writer
    lock, which the swap and the volume-floor audition hold (ADR-0368), the
    reconciler corrects it on its next tick.
  - A foreground volume change is not held off by the writer lock. During a
    swap it lands un-ducked, and the swap's release then keeps it. That was
    true before this record, because no duck claim ever covered a swap.
  - Rejected: routing the graph-swap bracket through the owner (#5754 option
    b). It changes the swap path on every graph apply, and the claim would not
    answer the cross-process question that ADR-0213 answers with the lock.
