# ADR-0297: The plan is posted in the run body; the staged spool is deleted

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

The old flow wrote an angle-capture request to a single-use spool, then opened
a session that consumed it. The request and the action were separate facts,
so callers had to coordinate staging, consumption, and readback before they
could know which experiment would run.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.1–2.3 and evidence comments 2 and 11.

Three older records contain stale facts and remain immutable. ADR-0179 counts
five engine seams although four remain. ADR-0198 defers `RecordStore` protocol
slots that have since reduced to `bank`. ADR-0014 names a source-text pin that
no longer exists; the invariant needs a behavioral pin when this design lands.

## Decision

The complete measurement plan is part of the run request body. The executor
persists that exact resolved plan with the run. There is no staged plan spool,
consume-on-open step, staged-plan epoch, or second readback that claims which
plan was consumed.

Preflight resolves the request before playback and reports every problem by
name. At the mover's join, the live owners check facts that may have changed.
If the executor cannot honor the posted plan, it refuses before anything
plays. The refusal identifies the module that produced the incompatible fact.

An absent plan is invalid. It never means “nothing staged,” and no failure may
fall back to a default plan, a smaller walk, the applied graph, or a different
measurement program. A retry submits a new complete request.

The persisted request fingerprint is the identity used by run status and the
evidence manifest. The request body is the single writer of experimental
intent; derived capture schedules are executor output, not another plan.

## Consequences

One document now connects intent, preflight, execution, and evidence. There is
no stale spool to withdraw and no consume race. Callers must send a full plan,
which makes retries more explicit but prevents a run from silently changing.

The staged spool commands and storage can be deleted with their tests. Dry-run
and live run share resolution, while only live run persists and later plays.

## Supersedes / Amends

This ADR supersedes ADR-0006's staged-request subject. It carries forward
ADR-0006's governing rule: refuse an unhonorable request before playback and
never degrade it into a different run.
