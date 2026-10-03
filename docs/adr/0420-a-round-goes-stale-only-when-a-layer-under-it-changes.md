# ADR-0420: A round goes stale only when a layer under it changes

- **Date:** 2026-10-02
- **Status:** Accepted. Applies the layer rule of
  [ADR-0301](0301-an-intact-trial-capture-is-the-measured-requirement.md) ("A room-only or
  bass-only candidate ... may reuse the unchanged speaker layer's evidence") to round staleness.
  No earlier ADR states the staleness rule this replaces; it replaces the code's rule and the
  runbook's description of `stale`. Carries rule 2 of the owner's design on
  [#6227](https://github.com/jaspercurry/JTS/issues/6227) (C1).
- **Context:** Finding F8 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md). An apply that
  changed the candidate or the config record made every banked round stale, and so did a bank
  before the last apply. That included near-field rounds, which divide the DSP out, and speaker
  rounds, which play none of the upper layers. A preference EQ save changed the record too.
  Nothing asked to measure room again after a later bass apply: room repeated only when another
  program had a current round newer than room's.
- **Decision:**
  1. The applied identity names each applied layer that plays by a fingerprint of its content in
     the applied snapshot: each program's own candidate layer (`linearization`,
     `rear_calibration`, `bass_extension`, `room_correction`), and `base` for the rest of the
     snapshot (routing, crossover, trims, timing, blend, driver protection, device) less the
     whole-candidate fingerprint. A banked packet records them in `applied.layer_fingerprints`,
     and each packet take carries the `cleared_layers` its record states.
  2. The programs stack in the order of the program rows: speaker, rear, bass, room. `base` and
     `linearization` lie under every program. Each other program's own layer lies under the
     programs after it. A purpose outside the stack (reference: `nearfield/each`,
     `drivers/each`) plays the neutral driver graph, so no applied layer lies under it.
  3. A round of a program stays current while each layer that changed since its bank lies above
     its program, or every kept take played that layer cleared. `stale_by` names the programs
     whose layer changed under the round. The candidate fingerprint, the config record and the
     time of the last apply are not compared.
  4. The snapshot holds no preference EQ and no output trim, so a preference save changes no
     layer.
  5. The next-program pointer reads the latest round of each program, current or stale. When the
     room layer is applied and room's latest round went stale through a layer under room
     (speaker, rear or bass), room is next: `run_program` with reason code `upstream_changed`. A
     change to the room layer alone does not do this. A program offers its latest round to copy
     only while that round is current.
- **Consequences:**
  - A near-field round and a speaker round stay current across cardioid, bass and room applies.
    A rear round stays current across bass and room applies, and a bass round across a room
    apply. A `base` or `linearization` change stales the rounds of every program in the stack.
  - A preference EQ save stales nothing.
  - Room is next after a later bass, rear or speaker apply. When a current room round exists,
    room is no longer next, whether or not a new room layer was applied; the LLM decides.
  - A change to a round's own layer stales it, unless every kept take played that layer cleared:
    a rear pair round stays current across a rear apply.
  - The rule follows the stack, not what each take played. Today a rear take still plays the
    applied bass and room layers, and a speaker trial plays its whole candidate, until their
    programs clear those layers (#6227 A2, B3). A change above them leaves those rounds current.
  - The rule reads what the takes recorded. A drivers-scope take and the timing take record no
    cleared layer, though they play no candidate layer. So a `linearization` change still stales
    a speaker round, which is the speaker program's own layer.
  - The rule needs no clock: when an earlier layer is restored, the rounds banked under it are
    current again.
  - A round banked before this has no layer fingerprints, so a round of a program in the stack
    reads stale (no backward support).
  - An emitter change that compiles the same snapshot to a different graph stales nothing.
  - Rejected: `tuning_scope_fingerprint` of the applied graph as the compared identity. Each layer
    change moves the graph, so the graph cannot tell which layer changed. Also, the output trim
    (with its loudness match, which a preference save moves) is in the `active_baseline_headroom`
    gain, which is not a preference slot.
