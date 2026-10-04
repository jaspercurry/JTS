# ADR-0445: Room is done when it is applied, trial or not

- **Date:** 2026-10-04
- **Status:** Accepted: the owner's answer Q6 (#5925, 2026-10-04) to a call
  [#6227](https://github.com/jaspercurry/JTS/issues/6227) raised. Supersedes one consequence of
  [ADR-0437](0437-a-rounds-staleness-is-judged-per-set.md), quoted below.

## Context

The next-program pointer flags room with `upstream_changed` when a layer under room changed since the
stack room's latest round was banked on (ADR-0437's `base_stale_by`;
[ADR-0420](0420-a-round-goes-stale-only-when-a-layer-under-it-changes.md) l.44). After a cardioid
trial, the owner applies the seed, designs room on the seed's set and may apply it with no trial (an
apply needs none). The latest room round is still the cardioid trial, banked on the old rear, so room
stayed next until a room trial banked a round on the stack that plays now.

## Decision

Room is done when it is applied, trial or not. Room is flagged with `upstream_changed` only when a layer
under room changed since room's latest round was banked **and** room was not applied again since on a
set of that round that is still current (`commissioning_coordinator.next_program_action`: the room layer
is not among the round's `base_stale_by`, or the round names no current set to design on).

### What this supersedes

- ADR-0437, Consequences, lines 106–108: "A room layer designed on that set and applied with no trial
  leaves room next with `upstream_changed`. The latest room round is still the cardioid trial, which was
  banked on the old rear. The room trial banks a round on the stack that plays now, and then tuning is
  complete."

## Consequences

- A rear or speaker change made after the room apply still flags room (ADR-0420 l.44): then the room
  layer is not among the round's changed layers, or no set of the round is current.
- A room applied after the change, from an older round's set, also counts as done: the pointer reads
  layer fingerprints and order through the round, not which set a document was designed on. The owner
  chose fewer checks here.
