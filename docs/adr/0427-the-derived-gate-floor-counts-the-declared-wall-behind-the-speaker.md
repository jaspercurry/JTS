# ADR-0427: The derived gate floor counts the declared wall behind the speaker

- **Date:** 2026-10-02
- **Status:** Accepted. Amends [ADR-0317](0317-wall-placement-starts-at-the-cabinet-back.md)'s
  Decision, lines 18–19: "The shared geometry owner derives the advisory front-panel-centre distance
  as back gap plus cabinet depth projected on the wall normal." That distance is no longer only
  advisory: it also places the wall's image source for the first bounce. Its Consequences, lines 27–28,
  stand: "Neither a cabinet gap nor a baffle estimate becomes a cancellation delay, a shelf, or a
  measured calibration."

## Context

Finding F11 of the [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md):
jts3's speaker takes found no reflection, so each window stayed at the 7 ms search ceiling and the
takes trusted everything above 357 Hz. The front cone is about 0.5 m from the wall behind it, so
that wall's bounce arrives about 3 ms after the direct sound, inside the window. A declared room
ends the gate at its first bounce
([ADR-0366](0366-one-pose-model-a-level-found-at-the-pose-and-a-band-stated-from-it.md) §3), but
`DeclaredGeometry.first_bounce_s` counted only the floor and the ceiling. The placement form
([#6245](https://github.com/jaspercurry/JTS/pull/6245)) now asks for the cabinet-back gap, the
cabinet depth, the toe-in and the nearest side wall.

## Decision

1. The first bounce is the earliest declared image-source path: the floor, the ceiling, the wall
   behind the speaker and the nearest side wall. A surface that is not declared has no path. The
   microphone is on the speaker's axis at the take's distance `d`, turned by the toe-in `θ` from
   the wall normal. `h_s` and `h_m` are the speaker and microphone heights.
2. **The wall behind the speaker** counts when `boundary_walls()` derives the front-panel-centre
   distance `f` (ADR-0317). Its path is `√((2f + d·cos θ)² + (d·sin θ)² + (h_s − h_m)²)`.
3. **The side wall** counts when `side_wall_m` (`s`) is declared. Its path is
   `√(d² + (2s)² + (h_s − h_m)²)`. It comes before the floor bounce when `s < √(h_s·h_m)`, at
   any distance: about 0.95 m at jts3's heights, which a speaker near a corner or on a shelf
   meets. The declaration does not say which way a toe-in turns the speaker, so this path keeps
   the microphone as far from the side wall as the speaker. That is exact at toe-in 0.

## Consequences

- A jts3-like declaration (speaker 0.9 m, microphone 1.0 m, distance 1 m, gap 0.2 m, depth 0.3 m,
  toe-in 0, so `f` = 0.5 m) moves the first bounce from the floor's 3.33 ms to the wall's 2.91 ms.
  The gate floor `2.5 / T` moves from 751 Hz to 860 Hz. The takes in F11 trusted from 357 Hz
  because no geometry was declared.
- The gate's search bound and each gated band follow from the next capture on (ADR-0366 §3). A
  band that a take already banked keeps its number. Nothing here becomes a DSP delay, a shelf or a
  calibration.
- On the axis the wall's extra path stays near `2f` at any distance, so a closer microphone does
  not lower this floor. A larger gap does.
- The paths put the microphone in front of the speaker. At a behind pose the wall's bounce comes
  earlier than this path; the rear readers read that take's ungated window
  ([ADR-0400](0400-the-window-follows-the-pose-not-the-purpose.md)).
- Rejected: leaving the side wall out. Nearer than about 0.95 m it is the first bounce, and the
  gate would claim too much again.
