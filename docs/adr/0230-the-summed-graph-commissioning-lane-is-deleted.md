# ADR-0230: The summed-graph commissioning lane is deleted

- **Date:** 2026-09-03
- **Status:** Accepted. Supersedes [ADR-0228](0228-rulings-carried-out-of-refactor-tuning-on-its-retirement.md) entry 9 (row 4g).

## Context

Row 4g of the retired refactor plan ruled the summed-graph commissioning lane
"repaired, not abandoned" and withdrew its deletion. That ruling rested on two
premises: that the relay capture lane would supply the producer's inputs, and
that `record_driver_capture` was production's admitted-capture door into
`promote_isolated_driver_capture`. [ADR-0188](0188-wired-first-measurement-relay-parked.md)
parked the relay lane; at HEAD `record_driver_capture` never called the
promoter. A history search of the web, CLI and deploy trees finds no commit
that ever wired a user surface to the lane; the wizard's summed capture sweep
is a permanent refusal stub; [ADR-0197](0197-the-commissioning-capture-stack-is-deleted.md)
already deleted the sibling producer on the same evidence.

The owner re-ruled on 2026-09-03: delete it if no functionality, analysis
ability or resilience is lost. PR #3836 carries the proof: every deleted
symbol grepped dead at HEAD; the hearing clamp (non-negotiable 1) is enforced
at twenty live sites without `_normal_graph`, whose only caller was a test;
the audible summed check the wizard uses (`start_summed_test`,
`play_driver_capture_sweep`, `commission_status_payload`) is untouched.

## Decision

The lane is deleted: 51 production symbols across `commissioning_runtime`,
`commissioning_apply`, `web_commissioning`, `staging` and the isolated
producer, with their test-only tests. Row 4g no longer stands.

One disclosure travels with it. `restore_pending_candidate_apply` was the
last implementation behind the `restore_required` /
`restore_finalization_required` states that `commissioning_service.status()`
advertises. It had no route, unit or hook before, so nothing reachable
changes: a candidate apply left pending across a restart is disclosed by the
status and restored by the operator. The doctrine §4 says so in those words.
If that restore is ever wanted automatically, it is a new build against the
wired lane, not a revival of the deleted class.

## Consequences

- −4,075 lines; the summed/isolated distinction survives only as one fact in
  the doctrine: a summed capture cannot attribute a deficit to a driver.
- A status state without an automatic actor is now stated as such rather than
  implied to be handled.
- Rejected: keeping the producer against a future relay revival. ADR-0188
  parked that lane; a revival rebuilds against whatever supplies captures
  then.
