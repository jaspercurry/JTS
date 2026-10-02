# ADR-0421: Bass has a preview model: the in-room preview adds the composed boost

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes (partial)
  [ADR-0256](0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md)
  §2's trend for a document that states bass and room: the room admission reads the median across
  positions with that bass's boost added.

## Context

Finding F9 of the [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): no
layer owns the broad wall and room bass gain. The room layer is Peaking-only, with boosts of at most
6 dB. Bass had no preview model: `preview_kind` refused a document with a bass section. `room/seat`
clears nothing, so a room preview added its total on top of a median that played the old room layer.

The plan on [#6227](https://github.com/jaspercurry/JTS/issues/6227) (owner, 2026-10-02) designs room
and bass together in one in-room program. One seat set is measured with bass and room off. A preview,
with no sound, adds the bass boost and the room set to that median. The system is linear at measuring
levels: on jts3 the realized boost matched the prescribed boost within 0.5 dB above 63 Hz in two runs.
So the LLM chooses the boost knowing the room's own gain, and the room layer cuts what is left. One
trial follows, then apply.

## Decision

1. **The in-room preview.** `jasper-crossover-prescriber judge --preview` previews a document whose
   sections are bass, room or both, from one round's seat median. The preview is the median, plus
   `expected_boost_db` of the composed bass (ADR-0359 §4), plus the composed room set's response
   through the one room preview engine. Each layer resolves as composition resolves it: the document's
   section, cleared (`{}` or null), or the base's. The answer's preview adds `resolution` (bass and
   room, each `document`, `cleared` or `base`) and `bass_boost_db`, the boost on the residual's grid,
   or null with no bass layer. The boost adds on the median's grid; the median's level reference stays
   the measured one. The room program row declares the model: its preview row names bass and room.
2. **A joint document's room admission reads the boosted curve.** The judge judges bass before room.
   When a document states both, the room section's boost admission reads the seat median with that
   bass's boost added. The caps, the taper and the cut floor read no median, so they do not change. A
   room-only document is judged on the median as banked.
3. **A median that played a layer added to it refuses.** The preview adds bass and room, so it reads
   only a median whose takes played both cleared. The judge's joint admission adds bass, so it reads
   only a median whose takes played bass cleared. A take played a layer unless its record lists the
   layer in `cleared_layers`, or it played its run's base and the run manifest's `incumbent` names no
   such applied layer. A set the manifest does not hold played every layer. Otherwise the read refuses
   `room_median_played_layer`, which names the sections and the set; its next action is a new room
   round.

## Consequences

- The LLM sees the room gain beside the boost before any sound plays. A document with `"bass": {}`
  shows the room without a boost, so a preview can show that no boost is needed.
- Until the in-room base clears both layers (#6227 step A2), a `room/seat` round that played an
  applied bass or room layer, as the doctrine's room bullet (§1a) still directs, previews nothing. A
  joint document's judge on a median that played bass refuses too. A room-only judge is unchanged.
- A room-only preview now adds the base's bass (`resolution.bass` is `base`), which is what plays on a
  median measured with bass off. A room-only judge reads the median as banked, so on such a median its
  admission does not see the base's bass. An in-room document states both sections; restating the
  applied bass keeps it.
- `contract --section room` evaluates `admit_boost` on the plain median: the contract cannot know the
  document's bass.
- Bass now refuses before room when both are invalid. Candidate fingerprints do not move: they hash
  canonical JSON.
- Rejected:
  - Subtracting a played layer's model from the median. The take record names the cleared layers, not
    the played descriptor.
  - Normalizing the boosted median to its own level. ADR-0359's jts3 boost would move the reference by
    about 10 dB and make the band above the boost read as dips.
  - Finding the played layers by filter name in a take's played CamillaDSP config. That couples the
    preview to the emitter's names, and a take records its config only under capture retention.
  - A second room preview engine.
