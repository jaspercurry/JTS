# ADR-0296: One executor lives in the daemon; every mover is a client of one gate

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

The tuning redesign found two walk loops with different behavior. The library
loop could walk a plan but its production caller could not, while the web flow
owned retries, assessment, and completion separately. This split made the
mover determine orchestration and made cleanup rules easy to diverge.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.1 and 2.3 and evidence comments 1, 2, 6, and 11.

Three older records contain stale facts and remain immutable. ADR-0179 counts
five engine seams although four remain. ADR-0198 defers `RecordStore` protocol
slots that have since reduced to `bank`. ADR-0014 names a source-text pin that
no longer exists; the invariant needs a behavioral pin when this design lands.

## Decision

`plan_run.run_plan` is the only loop that walks a measurement plan, and the
voice daemon hosts it. `jasper-round run` sends the plan to the daemon. The web
page, turntable arm, and `jasper-round placed` are movers: each releases the
same position gate over HTTP and none owns a second walk loop.

The mover may appear in provenance, but the executor does not branch on mover
kind. Each release grants only the pending pose batch. Retakes return to the
same gate. Cleanup at every executor seam follows ADR-0179: release completes
before cancellation propagates.

Creating a run reserves no microphone, volume claim, or measurement hold. The
session opens when the mover joins: the page's first placement, the arm's first
move, or `placed`. Participation and arrival remain distinct; joining does not
release a capture before placement completes.

`jasper-measure --specs` may host the same loop in-process for one release for
the no-daemon diagnostic path. It must not become another implementation, and
its removal condition is that the daemon path can serve that diagnostic use.

## Consequences

There is one owner for order, retries, status, evidence, and cleanup. Adding a
mover requires only a gate client. The daemon keeps one interpreter for this
concern, as ADR-0226 requires, and ADR-0198's resurrect condition is met by a
caller rather than by adding a `run` verb to `TuningSession`.

The page no longer authors experiments. This deliberately amends ADR-0188 §1:
a page-only human need not complete a whole round without a plan author. Its
wired-first rule and its CLI/SSH-only arm boundary remain.

## Supersedes / Amends

This ADR amends ADR-0188 §1. It honors ADR-0179's release shape, ADR-0198's
class boundary and resurrect condition, ADR-0226, and ADR-0228 MS-17.
