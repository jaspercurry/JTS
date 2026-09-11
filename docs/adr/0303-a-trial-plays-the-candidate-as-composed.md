# ADR-0303: A trial plays the candidate as composed; the layer order is speaker → room → bass; the composer owns inherit/clear semantics

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

Candidate trials could combine candidate speaker or bass facts with the room
layer currently applied on the box. Apply then demanded that the candidate's
room snapshot equal that incumbent, which forced room-before-bass order and
made a trial answer a question about a graph that was never composed.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.6–2.7 and 3 D2 and evidence comments 5, 8, 9, and 11.


## Decision

A trial plays each candidate exactly as the composer resolved it. The complete
layer order is speaker, then room, then bass. `base` is the applied profile
represented as a banked candidate, so baseline and candidate use the same
graph construction path.

The prescription composer owns layer resolution. An omitted section inherits
that layer from the explicitly named base. An explicit empty section clears
the layer when that layer permits clearing. Any invalid section refuses the
whole composition; valid sections are not silently emitted around it.

A fully resolved candidate is self-contained. Its lower layers need not equal
the tune applied when it was authored or trialed. Apply checks the candidate's
resolved dependencies and the exact trial graph, not equality to the current
room snapshot.

The graph vocabulary collapses to baseline and candidate. The mismatch named
`measurement_candidate_room_mismatch` and its apply-side twin retire because
they enforce the deleted hybrid graph.

## Consequences

Trial evidence describes the bytes proposed for apply. Room and bass work can
be composed in either order, and a lower layer can be deliberately inherited
or cleared without an implicit applied-state dependency.

Candidates must carry complete provenance. This is more explicit, but removes
the most dangerous ambiguity: measuring one graph and applying another.

## Supersedes / Amends

This ADR supersedes ADR-0259 §1 only for layer order. ADR-0259's one-toolbox
boundary and extension points remain. This ADR also retires the
`measurement_candidate_room_mismatch` rule.
