# ADR-0189: An armed endpoint under no active modes discloses, on non-composite sinks

- **Date:** 2026-08-28
- **Status:** Accepted
- **Extends:** [ADR-0184](0184-a-resolvable-width-with-no-armed-endpoint-signals-rather-than-parks.md)

## Context

`jasper.control.transport_park._assess` decides what a box is told about the
one audio transport from three facts: whether the wide ring resolves a width,
whether the saved layout declares an active-crossover mode (`active_modes`),
and whether outputd's active-ring endpoint marker is armed. Once a width
resolves, the remaining two facts give four combinations. Three were answered:

| `active_modes` | marker armed | answer |
| --- | --- | --- |
| truthy | no | park `roleful_active_endpoint_unconverged` (ADR-0178) |
| truthy | yes | `converge_refused` |
| empty | no | `unproven_endpoint` (ADR-0184) |
| **empty** | **yes** | **nothing — status `ok`** |

The fourth reported the doctor's greenest line, *this box is in none of the
four named transport parks*. That sentence is true about the four classes and
misleading about the box: nothing had asked the question.

ADR-0184 defined its seam on an **unarmed** marker, deliberately, and declined
to widen it — at the time, widening would have meant the classifier inventing a
model it does not own. This ADR supplies the missing model rather than the
widening.

**What the fourth state means.** The marker's writers
(`deploy/bin/jasper-audio-hardware-reconcile`, through
`outputd_active_lane_decision`) arm it only for an accepted active-speaker
graph. A layout that declares no active mode therefore has no business carrying
an armed marker, and the pair can only mean one of two things: **reconfiguration
lag**, because the hardware reconciler disarms on its next pass rather than
instantly, or a **genuine mismatch** between the saved layout and what outputd
was told. Both are worth a sentence; neither is a park, because neither carries
a rebuild issue or a runnable command — ADR-0178's bar for a class.

**The exception that decides the shape.** The hardware reconciler also arms the
marker for **passive composite** sinks. On a composite, (armed, no active modes)
is the normal serving state, not an anomaly. A class-blind disclosure would
therefore warn on every healthy composite box the moment composites are served.

## Decision

**No fifth park class. A third operator-only signal, scoped by sink class.**

- `transport_park.snapshot()` gains one boolean,
  `endpoint_armed_without_active_modes`, `True` when the wide ring resolves a
  width, `active_modes` is empty, the marker IS armed, **and the sink is not
  composite** (`topology_sink_is_composite`, the predicate the composite park
  already reads — this ADR adds no second spelling of "composite").
- `jasper-doctor`'s `check_ring_transport_park` warns instead of `ok` on that
  boolean, naming this ADR.
- No status, class, or household surface changes. A box in this state is
  playing whatever graph it already had.

The composite exclusion is the load-bearing half. It is written as a positive
sink-class test rather than as a list of layouts, so a composite serving path
that lands later inherits the silence without a second edit here.

## Consequences

The four combinations of (width resolved, `active_modes`, marker) are now all
answered, and the greenest verdict is no longer reachable by a box in an
unmodelled state. ADR-0184's seam is untouched: its condition still requires an
UNarmed marker, so the two booleans remain mutually exclusive and nothing is
double-reported. The four-class list stays closed.

An operator can meet a warn on a shape no fleet box holds today — the marker
only arms from an accepted active-speaker graph, so today the state is reachable
only as brief reconfiguration lag. That is the cost of disclosure on a playing
box, and it is paid ahead of #2982's composite rebuild deliberately: that
rebuild is what makes the composite exclusion above load-bearing, and building
the signal afterwards would mean building it during the window it exists to see.

**Removal condition:** if a rebuild or a class ever gives this shape a tracked
issue or a runnable command, it becomes a park under ADR-0178's own rule and
this boolean goes with it.

Rejected: a fifth park class (carries neither issue nor remedy); a class-blind
disclosure (false-alarms on every served composite); and widening ADR-0184's
`unproven_endpoint` to cover both marker states (it would merge two different
facts — nothing proved the endpoint, versus something armed it that should not
have — into one boolean an operator could not act on).
