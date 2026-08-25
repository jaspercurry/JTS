# ADR-0004: The duck release algebra — `min(reference, current + own depth)`, and the reference is a reader

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

Two duck holders can hold the main fader down at the same time and release in
either order: `CueDuck`, and the graph-mutation bracket every websocket config
swap runs inside. `_duck_release_target_db`
(`jasper/camilla.py:96-141`) decides where a releasing holder lands the fader.
Its docstring carries three separate design constraints, each bought with a
defect, and it is the only place all three are stated.
`docs/HANDOFF-volume.md:657-674` carries the first two; nothing anywhere states
the third. Both files are inside the tuning refactor's blast radius — the
release algebra is named as something the new single volume owner must own
(`docs/REFACTOR-TUNING-2026-08.md` §3 wave 5a), and wave 7e archives
`HANDOFF-volume.md`.

This ADR extracts the constraints before the code that carries them moves
(§0 rule 1, §6 R7).

## Decision

### Constraint 1 — the algebra, and both halves are load-bearing

Quoted from `jasper/camilla.py:105-116`:

> ``min(reference, current + duck_depth_db)`` — give back this holder's own
> attenuation and nothing else, and never end above the level that should be in
> effect. Both halves are load-bearing, and the two duck holders here
> (`CueDuck` and the graph-swap bracket) can interleave in either order:
>
> * replaying the entry snapshot strands the fader, because whichever holder
>   exits last replays a value the other one had already ducked — and a deep
>   drop is exactly what `maybe_reconcile_camilla` leaves alone;
> * a bare relative release fails the other way, clamping to 0 dB — loud —
>   when a volume change lands inside the window.

A holder gives back only what it took. Neither an absolute replay nor a bare
relative give-back is a correct release on its own; the failure modes are
opposite, and one of them is loud.

### Constraint 2 — a declared reference REPLACES canonical, it does not join the `min`

Quoted from `jasper/camilla.py:118-127`:

> The reference is the canonical household target, EXCEPT when a caller
> supplies ``held_target_db`` — a reader for the level that caller still owns
> (issue #2925 / #2929). Its answer replaces the canonical reference outright
> rather than joining the ``min``: the household level sits BELOW a measurement
> volume, so including it would win every time and pull the fader off the
> declared level, which is the whole defect this parameter exists to remove.

The general shape: when a claim outranks the household level, the household
level is not a second bound on that claim's release — it is not a bound at all.
Treating "quietest wins" as universally safe is what put every routed capture's
fader on the household value.

### Constraint 3 — it is a READER, not a number, and it must be synchronous

Quoted from `jasper/camilla.py:129-136`. This is the constraint that lives
nowhere else:

> **It is a reader rather than a number because the answer can change while the
> swap is in flight.** The bracket spans seconds, and a measurement session's
> volume can be drained inside it by a peer task; a pre-resolved number would
> then put back a level whose owner had just given it up. Asked HERE, a caller
> that no longer owns a level answers ``None`` and this falls through to the
> canonical release — the same path every non-measurement swap takes. The
> reader must be synchronous and non-blocking: it runs inside a shielded
> ``finally``, where awaiting could strand a ducked speaker.

Three parts, all binding: **ask at release time**, because ownership can end
inside the bracket; **`None` means "I no longer own a level"**, which is a
fall-through and not an error; and **no awaiting in the release path**, because
the release runs in a shielded `finally` under cancellation and a speaker left
ducked is a silent speaker.

The failure is named rather than swallowed — a raise from the reader logs at
WARNING and falls through to canonical, because "the reader is a plain state
read, so a raise is a defect rather than a condition".

### The bound never inverts

Supplying a reference can only lower the release, never raise it: *"Supplying
it never raises the fader above the level its reader names (the ``min`` still
bounds it by what this holder actually took away), and no caller that omits it
changes behaviour by a single write."* Any future ranked-claim release owes the
same property.

## Consequences

- The tuning engine's single volume owner inherits `min(reference, current +
  depth)` as its release algebra, and inherits with it the obligation that a
  claim outranking the household level replaces the reference rather than
  joining a `min` against it.
- The reader shape is the general answer for any release whose reference can be
  surrendered mid-bracket. A ranked-claim design that resolves references
  eagerly reintroduces constraint 3's defect under a new name.
- Scope note, so the next reader does not mistake this for still-live plumbing:
  the specific `held_target_db` parameter goes dead on the measurement path
  once the engine installs one session-scoped graph and stops swapping per
  candidate (`docs/REFACTOR-TUNING-2026-08.md` §3 wave 6e). The *reasoning*
  survives the parameter; a session that still holds a declared level across
  any bracket still needs all three constraints.
- Deliberately given up: a single unconditional release rule. The branch on a
  declared reference is real complexity, and it is the price of a measurement
  session whose stimuli play at the level its excitation-safety ledger admitted
  them against.
