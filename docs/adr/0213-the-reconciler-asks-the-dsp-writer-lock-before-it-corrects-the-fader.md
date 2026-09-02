# ADR-0213: The reconciler asks the DSP writer lock before it corrects the fader

- **Date:** 2026-09-02
- **Status:** Accepted

## Context

`CamillaController._graph_mutation` ducks the main fader across a graph swap,
and `GRAPH_SWAP_DUCK_DB` was 40 dB for a signalling reason its own comment
stated: deep enough that `volume_coordinator.RECONCILE_DUCK_SKIP_DB` (10 dB)
would read the drop as somebody's duck and leave it alone. That is inferring
ownership from a dB gap, which [ADR-0177](0177-duck-ownership-is-asked-of-the-owner-never-inferred-from-a-db-gap.md)
closed as a class; the swap bracket was its last consumer (#3308). The cost is
recorded beside the bracket: an interrupted release leaves the household 25-40
dB down and the reconciler is structurally forbidden from repairing it.

The bracket already publishes the fact the reconciler needs, and publishes it
across processes: it runs inside `dsp_apply.camilla_graph_mutation`, a flock on
`CANONICAL_DSP_WRITER_LOCK_PATH`. Nothing consulted it.

## Decision

**The 1 Hz reconciler stays the only arbiter of the household fader, and it
stands down by asking the lock.** `maybe_reconcile_camilla` probes the DSP
writer lock non-blocking — once, only after drift has cleared the dead band,
and again at the write boundary — and makes no write while a writer holds it.
The probe reads the lock (`CamillaController.graph_mutation_in_progress`) and
creates nothing: jasper-voice runs under `ProtectSystem=strict` with
`/var/lib/camilladsp` outside its `ReadWritePaths`. It fails open, per
ADR-0177: an unreadable lock must not make the loud-direction safety
correction inert.

`GRAPH_SWAP_DUCK_DB` is therefore sized for audio alone, at 25 dB: an
un-ducked swap measured 7-8 dB below the ambient floor's own sample-to-sample
delta (`docs/cutover-briefs-acceptance.md` §4), so what the duck covers is the
broadband gain step between two different graphs — the outgoing graph's
headroom charge, at most 22.5 dB on a shipped profile — and 25 dB is the
shipped `JASPER_DUCK_DB`, so out of the box a swap and a cue duck by the same
amount.

**Durable cross-process volume claims (#3038) are not built.** A second source
of truth about who owns the fader can go stale; the flock cannot, because the
kernel releases it when the holder dies.

## Consequences

- The swap's protection no longer depends on how deep it ducks, so the depth
  can move for audio reasons without a coordinator change, and the constants'
  coupling test is deleted with the coupling.
- `RECONCILE_DUCK_SKIP_DB` survives with one client, named beside it: the
  volume-floor audition in jasper-web takes a `COMMISSIONING` fader claim with
  no lock and no `MEASURE_PAUSE`, and jasper-voice cannot reach another
  process's `VolumeOwner`. The in-process ducks never needed it — a reconcile
  write is a HOUSEHOLD declaration, which a held `TRANSIENT_DUCK` outranks at
  the owner — so no probe was added for them. Until that audition announces
  itself the deep-quiet carve-out stays, and so does the defect it causes: a
  duck stranded by a killed swap is still not repaired. That is the follow-up
  this ADR leaves open, not a second decision.
- A DSP writer holding the lock for a long apply now also holds off the
  reconciler, mute corrections included — an unmute mid-swap is the loud write
  the bracket exists to prevent. Bounded by the apply, resumed on the next
  tick, and visible as `event=volume.reconcile_deferred reason=dsp_writer_lock`.
- The probe takes the same flock it reads, briefly and shared, so a writer
  whose first non-blocking acquire lands in that window logs one spurious
  `dsp.writer_lock result=waiting` and acquires a poll interval later. flock
  has no test-without-taking mode; the cost is bounded by the writers' 10 s
  admission budget.
- **Rejected: a shared duck lease** (`{owner, depth_db, expires_at}` in
  `/run`), as ADR-0177 rejected it and `docs/DEEP-AUDIT-2026-08-25.md`
  proposed it at ~200 lines. The lock is already there and already correct.
- **Rejected: the swap taking a `VolumeOwner` claim** (#3308's first option).
  `VolumeOwner` is per-process; a claim would not answer the cross-process
  question this ADR is about, and would still need the lock underneath it.
