# ADR-0265: The mic calibration door is a CLI verb; the daemon's root-mounted routes are gone

- **Date:** 2026-09-09
- **Status:** Accepted. Amends
  [ADR-0259](0259-room-correction-and-bass-extension-are-layers-of-the-one-tuning-toolbox.md)
  §4's closing sentence only, for the calibration and healthz routes; the rest
  of §4 (what moves into `audio_measurement`, and the shared capture slot
  staying in the daemon) stands.
- Refs: PR #4602 (the room product's retirement, which removed the `location
  /sound/room/` nginx blocks); [ADR-0255](0255-every-product-measures-through-the-wired-microphone.md) §3
  (one household mic record); [ADR-0237](0237-a-tuning-tools-stdout-is-its-answer.md).

## Context

ADR-0259 §4 said "the crossover, sync, calibration and healthz routes stay"
while the room product retired around them. They did not survive that
retirement: `/calibration/models`, `/calibration/fetch`, `/calibration/upload`,
`/test-tone` and `/healthz` are mounted at the root of
`jasper-correction-web`, and the only nginx block that ever proxied that root
was `location /sound/room/`, deleted with the room pages in #4602. The three
calibration handlers were also the ONLY writers of the household mic record,
so between #4602 and this decision no box could register a measurement
microphone at all and every take resolved uncalibrated.

## Decision

The mic calibration door is `jasper-mic-calibration` (`models` / `fetch` /
`upload` / `show`, on the shared tuning-CLI exit vocabulary, ADR-0237), and the
five routes with their handlers and tests are deleted rather than re-proxied.
`/calibration/*` is **SUPERSEDED**: the CLI is where the household mic is
established now. `/test-tone` is **SPENT** — `jasper-seat-level` supersedes it —
and `/healthz` is **SPENT**: nothing ever probed this daemon's copy — the
health probe the deploy and the supervisor use is `jasper-control`'s
`:8780/healthz`, which is untouched. This amends ADR-0259 §4 only; the
crossover and sync routes still stay, and the record's own move into
`audio_measurement` is unchanged.

## Consequences

- Registering a mic becomes an operator action on the speaker rather than a
  browser one, and it needs `sudo`: both writing verbs file under the
  root-owned, group-`jasper` calibration root
  (`docs/tuning-operator-runbook.md` step 3 spells the invocation out).
- One writer for the household record, reachable from the tuning CLIs, instead
  of three handlers on a daemon no path reached.
- Gives up the browser upload card; a household with a calibration file now
  needs a shell on the box. Rejected: re-proxying the five routes under a new
  nginx location, which would have restored a page nobody can reach from the
  retired room product, and a `/healthz` probe added to give the route a
  consumer.
