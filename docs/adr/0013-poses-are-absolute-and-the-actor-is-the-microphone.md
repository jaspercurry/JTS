# ADR-0013: Every prompted pose is ABSOLUTE, measured from the mark — and the actor is the microphone

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The pose table tells a mover where to put the microphone. An earlier revision
phrased each row as a delta on the previous one. The owner ruled in the field
on 2026-07-29, on
[#1806](https://github.com/jaspercurry/JTS/issues/1806). The ruling lives in
one section comment, `jasper/active_speaker/crossover_v2_flow.py:709-718`, and
nowhere else.

This is a construction rule for pose sets, not screen copy: the refactor's two
front ends both consume poses, and one of them drives a positioner that cannot
resolve a relative instruction at all. It is extracted before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7).

## Decision

**Every row states a complete target, measured from the mark.** Quoted from
`crossover_v2_flow.py:709-714`:

> EVERY ROW IS AN ABSOLUTE POSE, never a delta on the previous one (owner field
> ruling, 2026-07-29 on issue #1806): "raise one hand" then "now move two hands
> left" leaves a household guessing whether the raise survived. Each row states
> the complete target — distance, bearing, and height — measured from THE MARK,
> which is also the guidance half of issue #1874: ambiguous relative deltas
> plausibly produce the clustering that trips the geometry lock.

A relative pose is unresolvable without knowing whether the previous one
landed, and nothing measures that. The observable cost was position clustering
that tripped the geometry lock
([#1874](https://github.com/jaspercurry/JTS/issues/1874)).

**The actor is the microphone.** From `crossover_v2_flow.py:716-718`:

> The actor is THE MICROPHONE, not "the phone" (same owner ruling): households
> measure with a phone, a laptop, or a calibrated USB mic, and the device is
> incidental to the instruction.

Naming the device in the instruction makes the instruction wrong for every
other device — including, after the refactor, an arm.

**One ordered table serves every group, front-loaded.** The pre-apply and
post-apply groups take different-length prefixes of one table, *"so whichever
group ends soonest still gets the front-loaded spread"* — which is why the two
widest moves sit early rather than at the end. Reordering the table moves two
derived numbers, and the left/right alternation is deliberately unchanged:
*"what made the alternation read as weird in the field was the ambiguous
relative phrasing, which the absolute poses above remove."*

## Consequences

- Any pose a preset emits is absolute: distance, bearing, and height from the
  mark. An arm consumes it directly; a person does not have to remember whether
  the last move landed.
- Instructions stay device-agnostic. The engine describes where the microphone
  goes, and the front end owns whatever the mover happens to be holding.
- A shared, prefix-consumed pose table has to stay front-loaded, and reordering
  it changes derived counts. That coupling is real and belongs beside the table.
- Diagnosis note worth keeping: the clustering that tripped the geometry lock
  was caused by ambiguous instructions, not by a mover doing the wrong thing. A
  lock firing is evidence about the instruction as often as about the walk.
