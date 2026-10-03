# ADR-0429: One in-room program: the room round plays with bass and room off

- **Date:** 2026-10-03
- **Status:** Accepted: owner decision 1 on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step A2. Supersedes in part
  [ADR-0260](0260-poses-are-flexible-and-categorized-and-bass-extension-has-no-nearfield-rung.md) §3 and
  [ADR-0370](0370-each-run-purpose-declares-what-it-plays-and-a-bass-run-plays-with-room-off.md) §1,
  and restates the premise of [ADR-0413](0413-one-resolver-the-door-resolves-every-run-request.md)
  §3, each quoted below. Rewrites the bass and room bullets of
  [the measurement-loop doctrine](../measurement-loop-doctrine.md) §1a as one in-room bullet.

## Context

The owner's design on #6227 makes in-room one program: room correction, with the bass boost as an
option inside it. Three seat spots are measured once, with bass and room off. The full-band summed
sweep serves the room view, the bass view (with H2/H3) and the rear seat bands. A preview with no
sound adds the composed bass's boost and the room set to that seat median
([ADR-0421](0421-bass-has-a-preview-model-the-in-room-preview-adds-the-composed-boost.md)). One
trial follows, then apply.

Before this ADR:

- `room/seat` cleared no layer. Its base played the applied bass and room layers, so ADR-0421's
  preview refused its median (`room_median_played_layer`).
- `room/seat` served room only. Its bank filed no bass view, and its round did not count for the
  bass program.
- `jasper-round trial` sent every document with a bass section, `"bass": {}` included, to the bass
  row's trial: the 24-take `bass/axis` ladder (finding F12 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md)).

The full-band sweep already banks what the bass view reads: `bass_evidence` banks `analysis.bass`,
H2/H3 included, for every take whose program has a summed `sweep_verify` segment. One pass, not the
ladder's three averaged passes, costs about 4.8 dB of SNR.

## Decision

1. **The in-room base plays with bass and room off.** A program row states the layers its base
   plays cleared, in addition to the layers that every take of it clears: `base_clears` replaces
   the flag `base_clears_own`. The room row's base clears the room and the bass layers, and its
   candidates play as composed. The bass row keeps its rule for `bass/axis`: room off on every
   take, and bass off on the base. The door derives the played graph as before (ADR-0370 §2), and
   nothing measurement-only is banked.
2. **One seat set serves room and bass.** `room/seat` names the purposes room and bass. Its bank
   files the room views and the bass view of each set from the same takes. A banked packet counts
   for room and for bass when it carries their views (`packet_purposes`), so the in-room round
   counts for both programs.
3. **Bass documents trial at the seat.** The bass row's trials are the room row's: `room/seat` at
   `seat_express`, or at `room_quick` for the arm. Its first plan is `room/seat` at
   `seat_express`. So a bass-only document, a document with bass and room, and a `"bass": {}`
   document each trial on the in-room round. `trial_preset` reads the rows, and it stays the one
   owner of that routing.
4. **Both program rows stay.** The bass row keeps the bass section, its contract and its
   prescription. Only its trial and its first plan move.

### What this supersedes

- ADR-0260 §3, lines 61–62: "The family is fitted on the seat-cube median, through the applied
  tune". The family is fitted on the in-room seat median, measured with bass and room off, and the
  preview adds the composed boost.
- ADR-0260 §3, line 65: "the in-room distortion-versus-level ladder" as a protection basis, and its
  consequence at line 83: "The protection ladder is a code-owned program: stepped-level sweeps at
  the seat". The in-room round reads H2/H3 at one level from its full-band sweep, and no program
  default plays the ladder. The `bass/axis` preset stays, and plays only when a run names it,
  until #6227 A5 deletes it.
- ADR-0370 §1, lines 36–37: a row states "whether its base also clears the purpose's own layer".
  A row now states which layers its base clears. Line 40: "every other row clears nothing, so
  speaker, rear, room and reference runs play as before". The room row's base clears bass and
  room. ([ADR-0386](0386-a-rear-pair-take-clears-the-rear-layer-at-the-door.md) amended line 40 for
  the rear row.)
- ADR-0413 §3, lines 27–28, its premise: "Only a bass run's takes clear the room layer, and no bass
  preset takes a timing take, so a bass run's probe clears it too." An in-room base clears the room
  layer too. The conclusion stays: each candidate graph's set probes its own graph
  ([ADR-0423](0423-each-candidate-graphs-summed-set-levels-itself-at-every-spot.md)), so of the
  takes at a run's fader only a bass run's clear the room layer, and its probe clears it too.

The composition order does not change. [ADR-0303](0303-a-trial-plays-the-candidate-as-composed.md)
already replaced the order of
[ADR-0259](0259-room-correction-and-bass-extension-are-layers-of-the-one-tuning-toolbox.md) §1
("1 speaker · 2 bass · 3 room") with speaker, then room, then bass.
[ADR-0311](0311-a-run-plays-at-one-session-level.md) already says to judge a bass boost from the
room program's low band.

## Consequences

- **Hearing:** no level changes. Each in-room take plays only a graph that its own set's probe read
  (ADR-0423): the probe starts at −60 dBFS at the output, rises at most 6 dB a burst and stops at
  the 76 dB ramp bound, and the set's first take lands at 74 ± 2 dB at the seat. The base graph
  with bass and room off goes through the same graph doors as every graph. `volume_limit` 0.0, the
  graph doors, the `set_volume_db` clamp, the 85 dB commissioning stop and the declared driver caps
  do not change.
- An in-room round's base set previews (ADR-0421). A trial's candidate set still refuses
  `room_median_played_layer`, because it played its layers.
- A bass trial gets ADR-0423's own level for each graph, so rule A's cut by the declared bass
  reserve (finding F3) no longer applies to it.
- An in-room round whose kept takes all played bass and room cleared stays current across a bass
  or a room apply
  ([ADR-0420](0420-a-round-goes-stale-only-when-a-layer-under-it-changes.md)).
- A room trial's base plays bass off. A room-only document inherits the applied bass, so its
  candidate plays that bass, and `room-grade` compares the candidate with the bare seat response.
- `catalog` lists the bass views for an in-room round, and `catalog --program room` lists them too.
- Rejected:
  - Removing the bass program row. The bass contract comes from the rows that the topology runs
    (`programs_for_topology`), so the bass section would lose its contract.
  - Routing bass documents by a special case in `trial_preset` or in the CLI. The routing is data
    on the bass row.
