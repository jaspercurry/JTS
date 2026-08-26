# ADR-0111: One fixed, config-declared leader per bond — no election

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

A bonded set needs one speaker that advertises to senders, receives the source
audio, runs the shared pipeline, and fans it to the others. Snapcast is designed
around a central server, which is in tension with JTS's no-single-point-of-
failure instinct, so the alternative on the table was automatic leader election
with failover.

Election across a partition-prone consumer mesh is exactly the hand-rolled
distributed-consensus glue — split-brain, no fencing token, no term or epoch —
that buying the sync engine was meant to avoid.

## Decision

**Each bond has one leader declared in config (`grouping.env`), never elected.**
`role ∈ {leader, follower}` and a single `leader_addr` model the pair. The
leader is the only member that advertises to senders and the only one that runs
snapserver. Leader loss is handled by loud, observable degradation — a cue, a
`/state` flag, and a dashboard card — not by failover.

The leader's address is minted as the leader's stable mDNS `.local` handle, so a
follower's snapclient survives the leader changing DHCP address; a literal IPv4
is still accepted.

Requested and landed roles stay separate. A requested follower bond that a
safety gate refuses is preserved in the wizard-owned env while the reconciler
falls back to solo with the block reason kept. Role transitions are deny-first:
the guard publishes the new request's deny before teardown, and the matching
same-boot grant appears only after every landed-role predicate succeeds.

## Consequences

- Dramatically simpler and more predictable than election. If the leader loses
  power the room stops until it returns, and auto-recovers on reboot.
- Blast radius scales with bond size: losing a pair's leader drops half the
  image, losing a six-speaker group silences six. **A bond of more than two
  members is the trigger to revisit election** — building it for a pair is
  astronaut engineering.
- Auto-unwind to solo is likewise not built: tearing down a household's bond is
  not a 30-second-poll decision. Disband is one tap on `/rooms`, and the
  supervisor's reconciler kick has converged every silence class observed so
  far.
- Dissolve is best-effort by liveness. `/unbond` disables the members it can
  reach; an offline member comes back still configured, which the `degraded`
  runtime health surfaces and the next bond or leave self-corrects. Self is
  always disabled, so a local leave never depends on a peer being up.
  Guaranteed teardown of an offline member is where a persisted roster plus
  retry would go — deliberately not built, because it trades drift-free
  discovery for roster bookkeeping that can itself go stale.
