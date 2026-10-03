# ADR-0437: A round's staleness is judged per set

- **Date:** 2026-10-03
- **Status:** Accepted: the owner's measurement plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227)
  (2026-10-02), step B3 part 2. Amends [ADR-0420](0420-a-round-goes-stale-only-when-a-layer-under-it-changes.md)
  §1, §3, §5 and three of its consequences, each quoted below. Its cardioid consequences rest on
  [ADR-0436](0436-the-cardioid-default-one-pair-take-then-one-seat-trial-against-the-rear-off-base.md)
  (#6227 B3): every rear take plays bass and room cleared.

## Context

The plan says: "The cardioid seat trial's chosen set is the in-room base, so a cardioid build plays no
separate in-room round", and "Adding bass later needs only a new trial, because the seat set stays valid
while speaker and cardioid are unchanged."

A cardioid seat trial banks two sets: the run's base and the rear seed, every take with bass and room off
(ADR-0436). ADR-0420 judged a round by one identity, the applied tune when the round was banked. After
the owner applied the seed, the rear layer had changed since the bank, so the round went stale for rear,
room and bass. The toolbox then asked for a new in-room round, although the seed's own set had measured
exactly the tune that now plays.

A round answers two questions, and they need two answers. "Can the program design on this round, and on
which set?" is about what each set played. "Is the applied room layer still right?" is about the stack
that the round was banked on.

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
   layer cleared.
3. **Which set the program designs on.** `latest_banked_rounds` judges a round by one of its sets that
   kept a take and names its layers. It picks in this order:
   - a current set before a stale set;
   - then a set whose kept takes all played the program's own layer cleared and, for room and bass, all
     stood at a seat. For room and bass, that is a seat set with bass and room off: the in-room base, or
     the seed's set of a cardioid trial (an arm smoke round at 1 m bearings is not one);
   - then the newest set (the last that the packet lists, which is the last set that the run began).

   `stale` and `stale_by` are that set's. `set_id` names it only when it is current and the program can
   design on it (its own layer cleared, and for room and bass at a seat). So a room or a
   bass round names its set with bass and room off, a rear pair round names a pair set, and a speaker
   round names none. A round with no such set names none.
4. **Whether the applied room is still right.** `base_stale_by` names the programs at or under the
   round's program whose layer changed since the round was banked, whatever its takes cleared: the stack
   that its base played, the applied tune at its bank. `upstream_changed` reads it (ADR-0420 §5).
5. **Current before stale.** With `include_stale`, a program's latest current round comes before a newer
   stale one.
6. **The next action.** When room is next (its layer is not applied, or `upstream_changed`) and room's
   round names a set, the action copies the room prompt on that round and set (`round_dir`, `set_id`,
   and reason `round_available` or `upstream_changed`): design on that set, then trial. With no such set,
   the action is `run_program`, as before. Another program copies its latest round while the round is
   current and its layer is not applied, as before. The room prompt names the round and set in its
   contract, judge and compose calls, ends with a trial, and runs no new round. `status` shows `set_id` in
   `last_banked`.
7. A round whose view sets all played the run's base is judged against the same identity as under
   ADR-0420, set by set. A round with only candidate sets is judged by the layers of its candidates. A
   candidate that the bank cannot find leaves its set unjudged, so a round with no other set reads stale.

### What this amends

Each line of ADR-0420 that this changes:

- §1, line 22: "A banked packet records them in `applied.layer_fingerprints`". The packet still records
  them, as the stack that the round was banked on (`base_stale_by`). The sets bring their own.
- §3, lines 28–30: "A round of a program stays current while each layer that changed since its bank lies
  above its program, or every kept take played that layer cleared. `stale_by` names the programs whose
  layer changed under the round." Each set is now judged alone, by the layers that it played and the
  layers that its own kept takes cleared. `stale_by` is that of the set the round is judged by.
- §5, line 34: "The next-program pointer reads the latest round of each program, current or stale." A
  current round now comes before a newer stale one, and among current rounds one that names a set to
  design on comes before a newer one that names none.
- §5, lines 34–36: "When the room layer is applied and room's latest round went stale through a layer
  under room (speaker, rear or bass), room is next: `run_program` with reason code `upstream_changed`."
  "Went stale" reads the stack the round was banked on (`base_stale_by`). When the round names a set, the
  action copies the room prompt on it, with the same reason code. ("or bass" was superseded by ADR-0429.)
- §5, lines 37–38: "A program offers its latest round to copy only while that round is current." Room
  offers its round only while the round names a set to design on.
- Consequences, lines 44–45: "When a current room round exists, room is no longer next, whether or not a
  new room layer was applied". This holds when "current" means banked on the stack that plays now. A
  round that is current only through a candidate's set, applied after the bank, leaves room next with
  `upstream_changed`.
- Consequences, lines 46–47: "A change to a round's own layer stales it, unless every kept take played
  that layer cleared". This is now per set: unless each kept take of the set played that layer cleared.
- Consequences, line 48: "The rule follows the stack, not what each take played." The rule still follows
  the stack (only a layer at or under the program counts), but each set brings the layers that it played.

## Consequences

- A first cardioid build: after the seed's apply, the seed's set is current, and room is next with
  `round_available`. The action copies the room prompt on the seed's set. No in-room round is measured.
- A cardioid re-tune over an applied room: speaker, rear S1 and room R1 are applied, and a seat trial
  plays S1 (with R1) against the seed S2. After the S2 apply, the trial's stack (S1) has changed under
  room, so room is next with `upstream_changed` (ADR-0420, lines 44–45). The seed's set is current with
  bass and room off, so the action copies the room prompt on it: design on that set, then trial.
- A room layer designed on that set and applied with no trial leaves room next with `upstream_changed`.
  The latest room round is still the cardioid trial, which was banked on the old rear. The room trial
  banks a round on the stack that plays now, and then tuning is complete.
- After a room trial's candidate is applied, two sets are current: the base set (bass and room off) and
  the candidate's set (room and bass on). `set_id` names the base set, because a bass or room document
  judged on the candidate's set refuses `room_median_played_layer`
  ([ADR-0421](0421-bass-has-a-preview-model-the-in-room-preview-adds-the-composed-boost.md)).
- A newer trial whose candidate was not applied does not hide an older trial whose candidate was.
- `set_id` is passed as `--set` only where it is named. A speaker round's views take the set that each
  view needs.
- Rejected: to compare only the applied candidate's fingerprint with the set's candidate. A later room
  apply changes the applied candidate, so the seed's set would read stale, and room would lose the set
  that it designs on.
- Rejected: to fingerprint a candidate's layers from its own fields alone. The base layer holds the
  routing, device and driver protection that a candidate does not carry, so that base would never match
  an apply.
- The prediction holds while the apply reads the same routing, device, driver protection and declared
  preset that the applied snapshot holds, and resolves the candidate's timing as the candidate states
  it. When one of these changes, the set reads stale, as ADR-0420's base layer does.
- At bank time, the bank finds each candidate that a trial played in the candidate bank.
- A round banked before this has no set fingerprints, so it reads stale by every program at or under
  its own (no backward support).
- No schema changes: keys are only added (ADR-0344 §4). The packet's set rows gain `layer_fingerprints`.
  `latest_banked_rounds` gains `set_id` and `base_stale_by`, and `last_banked` gains `set_id`. The
  `copy_prompt` action gains `set_id`, and the prompt's binding gains `room_round`.
