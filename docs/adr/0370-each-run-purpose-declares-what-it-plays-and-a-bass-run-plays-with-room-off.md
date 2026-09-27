# ADR-0370: Each run purpose declares what it plays; a bass run plays with room off

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes the "Bass extension" bullet of
  [the measurement-loop doctrine](../measurement-loop-doctrine.md) §1a, which this ADR rewrites.
  Carries owner ruling D3 ([#5730](https://github.com/jaspercurry/JTS/issues/5730)).

## Context

The doctrine's layering rule says: when tuning layer N, play through that layer and everything
below it, with nothing above it. [ADR-0259](0259-room-correction-and-bass-extension-are-layers-of-the-one-tuning-toolbox.md)
orders the layers 1 speaker · 2 bass · 3 room · 4 preference. Owner ruling D3 applies the rule to
bass: a bass measurement plays the applied speaker layer with the room layer off, the base with
bass off and the candidate with bass on.

At HEAD the doctrine's bass bullet says the opposite ("play through the applied speaker layer and
applied room correction"), and a bass run plays whatever candidate it names. Its base is the
applied profile, when one is applied, with that profile's room layer and any bass extension an
earlier session applied.

The closed draft that first built D3 was reviewed and found four faults (#5730):

1. It composed a room-cleared candidate, banked it and named it as the played candidate, so
   applying that fingerprint would strip room from the box.
2. The room-cleared graph plays louder at the same fader. `program_headroom_db` charges the room
   layer's positive boosts into the program attenuation, and clearing the room removes that
   charge along with the room's own cuts.
3. The base still carried the applied bass extension.
4. The request builder chose layers by branching on bass, and keyed `room_sweep` and the regime
   on the candidate list being non-empty.

## Decision

### 1. Each purpose declares its layer policy on its program row

`ProgramDefinition` states which applied layers every take of its purpose plays cleared, and
whether its base also clears the purpose's own layer:

- the bass row clears the room layer on every take, and its own layer on the base;
- every other row clears nothing, so speaker, rear, room and reference runs play as before.

No request builder chooses layers or composes a candidate for them, so a bass run's candidate
list stays the one the operator named, and nothing that reads it changes.

### 2. The door derives the played graph when it compiles a take

- A summed take's spec carries the layers its purpose clears for it, for the base or for a
  candidate. The measurement door compiles the named candidate with those layers emptied, as it
  already derives the timing take's front-driver graph. The admission's graph evidence is
  derived the same way.
- The take keeps naming its parent: the banked candidate the run named, the applied base or a
  trial. The graph that played is recorded with the take, as for every take.
- No room-cleared candidate is composed, published or banked. Nothing measurement-only is
  banked.

### 3. Apply needs no refusal

Apply takes a banked fingerprint, or an in-memory candidate that it banks first. A
measurement-only graph reaches neither: the door derives its candidate inside the graph compile
and the admission's evidence, and never returns it, stores it with a take or publishes it to the
candidate bank. A take's candidate id is its parent's, a complete tuning the owner could already
apply. The first fault above therefore cannot arise, and apply gains no refusal and no marker.

### 4. A failed restore cannot make the room-off graph the boot graph

At HEAD:

- The session graph loads every measurement graph with `CamillaController.set_active_config_raw`,
  which applies a complete config without changing the persisted config file path. CamillaDSP's
  statefile keeps naming the entry graph's file, and nothing writes the measurement text to disk
  as a config.
- At boot, `converge_boot_statefile` selects the persisted graph from files on disk (the current
  config, the applied baseline, a staged graph) and never reads the running graph.
- The session graph submits every candidate-scope graph, the room-off graph among them, with a
  `jts-temporary-measurement:` description naming its anchor file and hashes. When the
  correction web service starts, `_restore_protected_neutral_program_graph` recognizes that
  description and, while the anchor file is unchanged, reloads it
  (`correction.crossover_v2_program_recovered`).
- If `MeasurementSessionGraph.restore()` fails, it logs `active_speaker.session_graph` at CRITICAL
  (`action=restore`, `result=failed` or `rejected`, the entry path) and raises `SessionGraphError`
  with the restore's own error, or, for a rejected load, "reapply the speaker profile before
  playing audio".

After a failed restore, the box can keep playing the room-off graph until the correction web
service restarts, CamillaDSP restarts or the profile is reapplied. That graph has no room
correction, so it plays louder by the room layer's charge less its response (§5), under the same
volume limit, fader clamp and driver caps. A restarted CamillaDSP loads its persisted graph,
never the room-off one.

### 5. The louder room-off graph is folded into the opener bound and disclosed

At frequency `f`, clearing the applied room layer raises the level by `charge - room_db(f)`,
where `charge` is the layer's positive-boost total (`total_positive_boost_db`) and `room_db(f)`
its response. This is never negative: the charge bounds the layer's peak. For a plan whose takes
clear the room layer, preflight takes the largest rise across the played stimulus band, from the
room floor to the top of the take's stimulus. It folds this rise into the margin wherever
`predicted_rung_admission` bounds a rung from the anchor, beside the bass lift it already folds:
the first rung, and a later rung whose previous rung left no usable SPL window. It discloses the
rise on the run as `room_off_rise_db`.

The 85 dB stop, the ramp bound, the fader clamp and the declared driver caps are unchanged
(non-negotiables 1 and 2). A later rung admitted from its measured previous rung keeps that
bound: the previous rung already played through the room-off graph.

## Consequences

- Every bass take, base and candidate alike, plays with the room layer off, and its record
  shows the graph that played. A bass fit no longer absorbs the room layer's boosts and cuts.
- A bass session's base carries no earlier session's bass extension.
- The doctrine's bass bullet is rewritten to this rule. The code follows in two changes tracked
  on #5730: the row policy with the preflight fold, then the door's derivation.
- On jts3 the 54 and 59 Hz room cuts sit in the rear calibration's common EQ, not in the room
  layer (#5730's triage), so a bass run there still plays them.
- Proof on hardware (owner present): one bass ladder on jts3 whose played graph shows room off
  and whose run discloses `room_off_rise_db`.
- Rejected:
  - The closed draft's route: compose and bank a room-cleared candidate, mark it
    measurement-only and teach apply to refuse it. It banks a fingerprint nobody should apply,
    then adds a guard to protect it.
  - Choosing layers in the request builders by purpose. The policy is data on the purpose's
    row, read at one place.
