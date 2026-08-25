# ADR-0008: Every begin is gated, including the on-axis ones — and a gated shape may not introduce release-order coupling

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

A remote-mover session holds each capture begin until something releases it,
so captures do not fire back to back while the microphone is still being moved.
Two design rules govern that hold, and both live only in one class docstring —
`jasper/web/correction_crossover_v2.py:6598-6612`.

The capture page is a separately deployed artifact behind its own trust
boundary (`capture-page/` + `relay/`), which is why the second rule exists at
all.

This ADR extracts both before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7).

## Decision

**Rule 1 — a gated shape reuses the shipped soft-hold and requires no change on
the far side.** Quoted from `correction_crossover_v2.py:6598-6605`:

> **The mechanism is the shipped soft-hold, not a new one.**
> :class:`~jasper.capture_relay.session.CaptureBeginDeferred` is the
> purpose-built non-terminal deferral: the Pi answers ``capture_deferred``, the
> phone parks on a wait screen with no affordance and re-posts the IDENTICAL
> begin every 1.5 s, the attempt budget is not spent, and the session does not
> end. Nothing on the capture page changes to support this — which matters,
> because that page is a separately deployed artifact and a release-order
> coupling is exactly what a gated shape must not introduce.

A new capture-side behaviour would mean the Pi and the page must ship in a
fixed order, and neither side can guarantee the other's version.

**Rule 2 — the gate is uniform per `(index, attempt)`; there is no on-axis
exemption.** From `correction_crossover_v2.py:6607-6612`:

> **Every begin is gated, including the 0° ones.** CHECK, MEASURE, the entry
> baseline, and stage 2's anchor are design-axis captures; the microphone has to
> be put back there as deliberately as it was moved away, and a gate that
> assumed "probably still on axis" would measure whatever the last pose left
> behind. Gating is per ``(index, attempt)``, so a retake re-gates — the same
> uniform rule rather than a special case that has to be reasoned about.

"Probably still on axis" is an assumption about a physical position that
nothing measured. A retake re-gates for the same reason.

**The hold has exactly three exits**, and a bound that refuses names which
failure it saw: a per-hold budget (a mover that STOPPED answering) and the
session's own wall-clock ceiling (a mover answering every position, too slowly
to finish). The ceiling is not measured at the hold, because it already has an
owner and *"a second clock for one bound is a second definition of it"*
([#2506](https://github.com/jaspercurry/JTS/issues/2506)).

## Consequences

- The engine's session gate stays uniform: one rule per `(index, attempt)`, no
  phase carve-outs. A carve-out is a special case someone has to reason about
  at exactly the moment they are debugging a wrong curve.
- No engine change may require a coordinated release of the capture page. That
  fence is what keeps the phone-mic trust boundary a boundary.
- The robot-arm front end reuses the same soft-hold rather than inventing a
  second deferral. Two hold mechanisms would be two definitions of "the
  microphone is not there yet".
- Deliberately given up: latency on design-axis captures that probably did not
  need the gate. The cost is a release round-trip; the alternative is a curve
  measured wherever the last pose left the microphone.
