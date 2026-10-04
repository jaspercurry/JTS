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
under room changed since room's latest round was banked, unless the room layer changed since that bank
too **and** the round still names a current set to design on
(`commissioning_coordinator.next_program_action`).

### What this supersedes

- ADR-0437 §4, lines 54–56: "`upstream_changed` reads it" (`base_stale_by`). It now also reads whether
  the room layer itself changed since the bank and whether the round names a current set.
- ADR-0437, lines 89–92: "A round that is current only through a candidate's set, applied after the bank,
  leaves room next with `upstream_changed`." Not when the room layer was applied after that bank too.
- ADR-0437, Consequences, lines 106–108: "A room layer designed on that set and applied with no trial
  leaves room next with `upstream_changed`. The latest room round is still the cardioid trial, which was
  banked on the old rear. The room trial banks a round on the stack that plays now, and then tuning is
  complete."

## Consequences

- The rule reads two facts about room's latest round: the room layer changed since its bank, and the
  round still names a current set. It reads no order, and it links the room layer to no set. A speaker
  change after the room apply still flags room (no set of the round stays current), and so does a rear
  change while room was not applied again since that round (ADR-0420 l.44).
- **Stated limit:** a rear change made after the room apply that lands on another current set of a round
  banked before the room apply reads as done — for example a trial's muted copy, or a second trial's
  seed re-composed on `saved`, applied after a room designed on the first seed. A room applied from an
  older round's set also reads as done. The owner chose fewer checks. If it bites, compare the room
  layer's stored `basis` (`round_id`, `room_median_sha256`) with the round and set the pointer picks.
