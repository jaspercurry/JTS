# ADR-0197: The commissioning capture stack is deleted

- **Date:** 2026-08-30
- **Status:** Accepted

## Context

`commissioning_capture_producer.py` held a complete second on-box capture
system — `SummedCaptureProducer`, `CurrentCaptureAuthority`, `RawCaptureResult`,
and the `RawCaptureTransport` seam. It has never been constructed in
production. #2362 deleted its wiring on 2026-08-22 (the service's `capture_next`
/ `capture_post_apply`, the host's `capture_next_with_runtime`), leaving the
class stranded; the class itself outlived the deletion.

PR #3322 tried to join a real caller to it — the delay sweep — and closed
unmerged. Its closing comment is the evidence and is not restated here: the
admission gates and the fail-closed quality codes make the join impossible by
construction, not by defect. Two conclusions travel from it. The producer wants
a *guarded two-output commissioning* graph while real measurement patches the
applied graph with every driver unmuted. And its one-shot fail-closed contract
aborts exactly the coordinates a graded search exists to visit — correct for
commissioning evidence, wrong for a search.

The wired crossover_v2 walk is the measurement path (ADR-0188). Keeping a
second, unreachable capture system beside it costs every future reader the work
of discovering it is unreachable — twice now, at audit and again at #3322.

## Decision

**The stack is deleted, not parked.** `commissioning_capture_producer.py` and
its test file are removed. Nothing in the tree consumed either.

`CommissioningEvidenceStore`, `commissioning_evidence.py`,
`commissioning_run.py`, `commissioning_host.py`, and `commissioning_runtime.py`
have live consumers and are untouched — the deleted stack was a *caller* of
them, not their owner.

**Resurrect condition.** A future on-box capture kind is built on the wired
walk's live patterns — the walk, its grading verbs, its honest verdicts — and
never by restoring this module from history. The `EvidenceKind`
`normal`/`reverse`/`delay_null` vocabulary survives in `commissioning_evidence.py`
and stays available to whatever produces those kinds next.

## Consequences

The capability this module was written for — a typed summed-region capture with
its own admission and evidence identities — is gone from the tree until
something rebuilds it. That is the intended trade: the capability was never
reachable, so no behavior is lost, only the option of resuming from this shape.

A rebuild starting from the wired walk pays a real cost this module had already
spent: admission, evidence identity, and artifact persistence are re-derived
rather than inherited. That cost is accepted, because #3322 showed the
inherited version's gates encode a *different measurement* than the one the
program needs.

Rejected: keeping the module dormant behind a flag. It was added 2026-07-14 and
last substantively edited 2026-07-19, and acquired no caller in the six weeks
since — including under an explicit repair-not-delete ruling. A flag would only
rename the dormancy.
