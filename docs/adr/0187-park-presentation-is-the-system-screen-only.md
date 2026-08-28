# ADR-0187: Park presentation is the system screen, not a banner

- **Date:** 2026-08-27
- **Status:** Accepted

## Context

[ADR-0178](0178-every-shape-the-ring-cannot-serve-parks-under-its-own-name.md)
named the four transport-park classes and, describing where a `parked` box
announces itself, promised "the household banner and incident rows" alongside
the doctor FAIL and `/state` (ADR-0178:88-89, inherited from ADR-0100:35). No
such banner was ever built: the only browser-facing park presentation is the
generic audio-health card, which speaks solely for a LIVE (ring-only) park,
and a browser had no way at all to see a `pending`, `unclassified`, or
`unavailable` verdict — nor either of the two signals that ride alongside
`status` without being parks (ADR-0184's `unproven_endpoint`, and the
converge refusal added with this decision). The owner ruled on the gap
directly rather than building the promised banner.

## Decision

Owner ruling, 2026-08-27: **parks and the not-a-park signals beside them
render on the `/system` screen only.** No household banner, no modal, no new
page. `jasper-doctor` and `/state.resilience.transport_park` keep their
existing roles unchanged, and the household audio card keeps speaking for a
LIVE park — that box is silent and the household must be told.

This supersedes **only ADR-0178's presentation clause**. Everything else in
ADR-0178 stands unchanged: the four classes, the bar a shape must clear to
become a class, the tracked issue each waits on, the `pending`/`parked`
severity split, and the rule that a signal carrying neither an issue nor a
remedy is not a class.

## Consequences

Every non-`ok` verdict now reaches a browser, which is more than the promised
banner would have carried — the banner was scoped to `parked` alone. The
disclosure costs a healthy box nothing: the card renders only when something
is actually parked or refused.

What it gives up: a park is no longer pushed at whoever opens the speaker's
home page. Someone must open `/system` to see one. That is the deliberate
trade — a `pending` park is not a household incident, and the one shape that
genuinely is (a live, ring-only park) still reaches the household through the
audio card that already owns that sentence.

Rejected: building the banner ADR-0178 described. It would have put a
structural, boot-persistent condition in the household's face on every page
load, with no action a household member can take, which is how a warning
surface gets trained into invisibility.
