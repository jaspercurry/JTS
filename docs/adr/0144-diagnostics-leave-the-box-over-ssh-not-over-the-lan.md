# ADR-0144: Diagnostics leave the box over SSH, not over the LAN

- **Date:** 2026-08-26
- **Status:** Accepted (decided in review 2026-05-30, when the built feature was
  removed; recorded here when HANDOFF-observability.md was trimmed to its
  operational spine)

## Context

A one-tap "Download diagnostics" button — `GET /diagnostics-bundle` returning a
`pi-bundle.sh` tarball — was built as the fourth tier of the observability work
and removed before it shipped. It is cohort-standard (OctoPrint, Home
Assistant, Volumio all ship one), which is why it was built.

Against that: JTS is a maintainer-operated household speaker where the
maintainer already has SSH. The bundle's redaction is **name-based**, so it
misses inline secret *values* and non-secret-but-private fields (home
coordinates, SSID, HA URL). Any LAN device behind the management guard could
pull all logs plus config in one request, and the flight recorder had just
increased how much DEBUG context the journal carries for such a bundle to ship.

## Decision

**No LAN-reachable diagnostics bundle.** `scripts/pi-bundle.sh` stays the
SSH-only path it always was (`scp pi@jts.local:/tmp/jasper-bundle-*.tar.gz`).
The `/system` "Run diagnostics" button stays, because it runs a read-only
`jasper-doctor` and ships no config or logs.

## Consequences

- The LAN management surface never becomes an exfiltration path for the
  journal, and the redaction problem is not one this project has to solve.
- Diagnostics cost the maintainer an SSH session. Accepted: there is exactly
  one operator, and they have the key.
- Revisit condition, stated in advance: **only** if JTS ships to households the
  maintainer cannot SSH into, and only together with value-level redaction.
