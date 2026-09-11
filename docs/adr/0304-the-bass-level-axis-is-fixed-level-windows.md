# ADR-0304: The bass level axis is fixed-level windows inside one run; canonical pose sets belong to each program and mover

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

Bass analysis needs response at known operating levels, but exposing one
session per rung would make the operator repeat the same pose choreography.
The prior handoff also remained the only home for the JTS3 trial authorization
and the arm's bounded research geometry.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.2–2.3 and 3 D4, D6, and D13, and evidence comments 2,
9, and 11.


## Decision

The bass level axis is a list of fixed-level windows inside one run. The run
takes one outer isolation hold. At each pose, it opens each requested level
window in sequence and plays every candidate before the mic moves. Each window
has one fader owner and its own SPL watch; Main, Aux1, and stimulus level are
distinct recorded facts.

Canonical pose sets belong to the program registry and state their intended
mover. Humans use seat layouts: `seat_cloud` is the default, `seat_express` is
the quick check, and `seat_cube` is selectable. `room_quick` is a three-bearing
arm smoke test, not seat evidence. Recorded geometry is never relabelled.

For the JTS3 research campaign, trial plans may authorize up to 80 dB SPL while
remaining below the commissioning stop. Arm research stays within ±45 degrees.
These are recorded experiment facts, not global product constants and not
permission to widen a hardware or hearing limit.

The research basis is preserved by this decision. This satisfies ADR-0229's
bar for retiring `docs/HANDOFF-bass-extension-plan.md` and its pointer stub;
their deletion is a later docs change and is not part of this ADR PR.

## Consequences

The operator places the microphone once per pose while the engine preserves
one-fader-per-window isolation. A deferred volume restore ends the run as
partial instead of forcing the next window.

Arm smoke-test evidence can reveal problems quickly but cannot claim seat
coverage. Campaign limits remain visible in the plan and evidence.

## Supersedes / Amends

This ADR supersedes ADR-0229's exemption and authorizes later deletion of the
named handoff and stub. It honors ADR-0009, ADR-0277, and ADR-0278; no existing
record is relabelled.
