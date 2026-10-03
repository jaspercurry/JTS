# ADR-0424: The applied speaker tune carries its woofer's trusted floor

- **Date:** 2026-10-02
- **Status:** Accepted. Restores the writer of
  [ADR-0256](0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md)
  rule 1. Supersedes the consequence of
  [ADR-0400](0400-the-window-follows-the-pose-not-the-purpose.md) that the room ceiling is always
  rule 1's fallback.
- **Context:** ADR-0256 rule 1 (owner ruling): the room layer's upper band edge is the applied
  candidate's disclosed trusted floor, clamped to 250–500 Hz; with no readable floor it is 350 Hz,
  disclosed. The floor's only writer went with the Gen A planner (`4d5353536e`, 09-23), and
  ADR-0400 deleted the round gate that the ceiling read instead. So every room document used the
  350 Hz fallback ([#6110](https://github.com/jaspercurry/JTS/issues/6110); finding F9 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md)). #6110 left one
  choice open: which floor to bank. Step C2 of
  [#6227](https://github.com/jaspercurry/JTS/issues/6227) chose the highest one.
- **Decision:**
  1. **The floor.** A composed candidate whose document names a speaker section (driver, blend,
     alignment or topology) banks `exclusion_evidence: {"trusted_floor_hz": F}`. F is the highest
     `trusted_floor_hz` of a gated woofer curve over the kept speaker MEASURE takes of the round
     that the document was judged on. This is the rule the round gate read (ADR-0383), narrowed to
     the woofer and to MEASURE. A judge with no round, or a round with no such take, banks no floor.
  2. **Carry forward.** A document that names no speaker section (room, bass or rear only) keeps its
     base's floor. `compose_candidate` makes this choice, as it resolves every other layer.
  3. **Persistence.** The field rides the applied snapshot (`recomposition_snapshot_for`). The apply
     persists it with the profile, and a banked round's `applied-profile.json` holds it.
  4. **The reader.** `room_ceiling` reads the applied snapshot's floor: the ceiling is the floor,
     clamped, with source `applied_candidate` and the floor disclosed. With no floor it is the
     350 Hz fallback, with source `fallback` and its reason. The room and rear views read it.
  5. **The field.** The floor keeps the field that ADR-0256 named as its carrier,
     `exclusion_evidence`. Nothing wrote or read its old content.
- **Consequences:**
  - Candidate fingerprints change. A speaker composition judged on a round with gated woofer
    MEASURE takes carries the floor in its fingerprinted core. A composition with no floor keeps
    its fingerprint, because an empty field stays out of the core. Every new applied snapshot has
    the field, so the first apply after this change writes a new applied identity.
  - Room documents read the applied floor, so the room ceiling moves when the speaker tune moves.
    A room session designed against the old ceiling is disclosed-stale (ADR-0256), never parked.
  - The floor is only as honest as the gate (finding F11). jts3's speaker takes found no
    reflection, so the window stayed at the 7 ms search ceiling and the takes trust 357 Hz and up.
    The wall bounce arrives about 3 ms after the direct sound, inside that window. Step C3's
    declared geometry ends the search at the declared first bounce, which makes the floor honest.
  - `round_gate` stays a ceiling source until no applied tune carries it (ADR-0400).
  - Rejected:
    - The floor of the one take a prescription was judged on (#6110's other option). A document
      names no one take, and the highest floor leaves to the room layer every band that a kept
      woofer take does not trust.
    - The low edge of each curve's trusted band (ADR-0366 §3). It is stated before the take, from
      the declared room or the search bound. ADR-0256 sets the transition by the gate that the take
      achieved. The two agree when the search runs to its bound.
    - The base's floor when the judged round has none. A new speaker layer would then claim the old
      layer's evidence.
    - A new field. Its refusal codes would need new copy, and ADR-0256 already names this one.
