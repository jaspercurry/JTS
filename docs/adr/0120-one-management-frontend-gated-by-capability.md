# ADR-0120: One management frontend for every install profile, gated by capability

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

JTS ships two install profiles: full speakers and the Zero-class `streambox`.
The obvious shape is a bespoke frontend per profile — a smaller page for the
smaller box. That path was taken once (the `endpoint` / `satellite` tier) and
produced a second visual system to keep in step with the first.

A third case makes per-profile frontends actively wrong: a box bonded as a
multiroom **follower** behaves like the old endpoint at runtime — grouping
lands its role and data plane, the source coordinator parks its renderer stack,
and grouping derives the voice-park flag — but that is a runtime role, not an
install tier. It can change without reinstalling.

## Decision

**One landing page, one design system, one card vocabulary, for every
profile.** Differences are capability gates, never a second frontend.
`jasper.install_profile.system_capabilities_for_profile` is the single source
of truth, derived purely from the profile rather than from frontend-local
hardware guessing, and feeds two consumers: `jasper-control`'s
`/system/snapshot.system_capabilities` at runtime, and `install.sh`, which
**bakes** the map into the page at install time.

The page applies the baked map **synchronously at first paint**, so layout is
correct with no network round trip and survives any backend daemon being down.
The `/system/data.json` poll refreshes live values only — it never drives
layout. Gates fail closed: every gated card ships `hidden` and is shown only
when its capability is `true`.

The shared frontend rule does not extend to systemd activation. Full speakers
and streamboxes install different units under the same runtime unit names, so a
streambox never binds voice/wake/integration ports or sources assistant-only
env files.

## Consequences

- A new gated card is one capability key plus one `hidden` attribute, not a
  second page to maintain.
- A backend outage degrades to stale values, never to a scrambled layout.
- The legacy `endpoint` / `satellite` tokens normalise to `streambox`; nothing
  new may introduce a third install tier to express a runtime role.
- A capability map that is wrong hides a card the box can actually use, which
  is the deliberate direction to fail in — the alternative offers controls that
  cannot work.
- Rejected: probing hardware in the browser to decide what to show. The profile
  already knows, and two answers would drift.
