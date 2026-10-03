# ADR-0437: A round's staleness is judged per set

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step B3 part 2. Amends [ADR-0420](0420-a-round-goes-stale-only-when-a-layer-under-it-changes.md)
  §3, quoted below.

## Context

The plan says: "The cardioid seat trial's chosen set is the in-room base, so a cardioid build plays no
separate in-room round", and "Adding bass later needs only a new trial, because the seat set stays valid
while speaker and cardioid are unchanged."

A cardioid seat trial banks two sets: the run's base and the rear seed, every take with bass and room off
(#6227 step B3). ADR-0420 judged a round by one identity, the applied tune when the round was banked.
After the owner applied the seed, the rear layer had changed since the bank, so the round went stale for
rear, room and bass. The toolbox then asked for a new in-room round, although the seed's own set had
measured exactly the tune that now plays.

## Decision

1. **Each set banks the layers it played.** Each set a view reads carries `layer_fingerprints` in its
   packet row.
   - The run's base set: the applied tune's fingerprints at bank time (ADR-0420 §1).
   - A candidate's set: the fingerprints an apply of that candidate would record. These are the
     candidate's own sections (crossover and timing, trims, and each program's layer) on the applied
     snapshot's routing, device and driver protection (`baseline_record.candidate_layer_fingerprints`).
     They come from the same `candidate_sections` that the apply's snapshot writer uses.
   - A composed candidate stores every section. It copies from its base each section that its document
     leaves out, so a layer that the candidate leaves to the applied tune keeps the applied fingerprint.
   - A timing set and a level-probe set name no layers: no view reads them. A candidate's set also names
     none when the bank cannot find the candidate, or when no tune is applied.
2. **Each set is judged alone.** A set is current for a program while each layer at or under the
   program that it played still has the same fingerprint, or each of the set's kept takes played that
   layer cleared. A round is current for a program when at least one of its sets that kept a take and
   names its layers is current.
3. **The pointer names the set that the program can design on.** `latest_banked_rounds` returns the set
   that it judged the round by, as `set_id`, with that set's `stale_by`. It picks in this order:
   - a current set before a stale set;
   - then a set whose kept takes all played the program's own layer cleared. For room and bass, that is
     a set with bass and room off: the in-room base, or the seed's set of a cardioid trial;
   - then the newest set (the last that the packet lists, which is the last set that the run began).

   `jasper-crossover-prescriber status` shows `set_id` in `last_banked`. The copied prompt sends the LLM
   there first, and the next step passes it as `--set`.
4. A round with one set is judged as before.

### What this amends

- ADR-0420 §3, lines 28–29: "A round of a program stays current while each layer that changed since its
  bank lies above its program, or every kept take played that layer cleared." Each set is now judged
  alone against the layers that it played, and the cleared layers are those of the set's own kept takes.
- ADR-0420 §1, line 22: "A banked packet records them in `applied.layer_fingerprints`". The packet still
  records them, but staleness reads the sets' own fingerprints.
- ADR-0420, Consequences, line 48: "The rule follows the stack, not what each take played." The rule
  still follows the stack (only a layer at or under the program counts), but each set brings the layers
  that it played.

## Consequences

- After a cardioid seat trial, the base set is current until an apply. Once the seed is applied, the
  seed's set is current. The round then stays the current round for rear, room and bass, and room is
  not measured again: the next action copies the room prompt.
- A later room or bass apply with no trial leaves the seed's set current, because each of its takes
  played bass and room cleared. So room gets no `upstream_changed`. A speaker or rear change makes the
  set stale.
- Rejected: to compare only the applied candidate's fingerprint with the set's candidate. A later room
  apply changes the applied candidate, so room would get `upstream_changed`.
- Rejected: to fingerprint a candidate's layers from its own fields alone. The base layer holds the
  routing, device and driver protection that a candidate does not carry, so that base would never match
  an apply.
- After a room trial's candidate is applied, two sets are current: the base set (bass and room off) and
  the candidate's set (room and bass on). `set_id` names the base set, because a bass or room document
  judged on the candidate's set refuses `room_median_played_layer`
  ([ADR-0421](0421-bass-has-a-preview-model-the-in-room-preview-adds-the-composed-boost.md)).
- When no set is current, the same order picks the set, so room's `upstream_changed` reads the set that
  room designs on.
- The prediction holds while the apply reads the same routing, device, driver protection and declared
  preset that the applied snapshot holds, and resolves the candidate's timing as the candidate states
  it. When one of these changes, the set reads stale, as ADR-0420's base layer does.
- At bank time, the bank finds each candidate that a trial played in the candidate bank.
- A round banked before this has no set fingerprints, so it reads stale by every program at or under
  its own (no backward support).
- No schema changes: the packet's set row and `last_banked` each gain a key (ADR-0344 §4).
