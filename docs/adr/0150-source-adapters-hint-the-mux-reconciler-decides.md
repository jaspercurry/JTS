# ADR-0150: Source adapters hint; the mux reconciler decides

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-source-capabilities.md was
  trimmed to its operational spine)

## Context

Four sources — Spotify Connect, AirPlay, Bluetooth, USB sink — each have a
natural "something changed" edge available to them: an inotify write on
librespot's state file, a D-Bus signal, a frame-flow transition. The tempting
design is to let whichever adapter noticed the edge act on it: open its fan-in
lane, or declare itself the winner.

That design has two failure modes that are impossible to test out. Hint arrival
order is not event order, so two adapters racing produce a different winner
depending on scheduling. And an adapter whose probe merely became *unreadable*
looks identical to one whose source genuinely stopped, so a transient failure
becomes a stop/start flap on the speaker.

## Decision

**A source adapter may only translate an edge into a dirty-source hint.
`jasper-mux` re-reads authoritative state and applies one policy path — the same
path for both hint-driven alerts and its fixed lost-alert patrol.**

Ordering comes from a process-local sequence, not from hints: a confirmed
inactive→active observation is stamped with `started_seq`, and the newest active
sequence wins regardless of source type. The same sequence controls
return-to-Auto and winner-stopped fallback. Duplicate hints coalesce.

An `unknown` observation (probe failed, not "source stopped") retains an active
last-known state for a bounded grace — `UNKNOWN_ACTIVE_HOLD_SEC = 5.0` in
`jasper/mux.py` — and then expires to inactive.

## Consequences

- **The winner is a function of state, not of luck.** Because mux re-reads
  authoritative state after a hint, a hint that arrives late, twice, or not at
  all converges to the same answer as the patrol would have reached.
- **The decision is inspectable.** Per-source `started_seq` appears in mux
  STATUS, so "why is AirPlay playing instead of Spotify" is answerable from a
  status dump rather than from log archaeology.
- **Both failure directions are bounded.** Five seconds of grace absorbs a
  transient probe failure without a flap; expiry after it means a permanently
  dead adapter cannot pin a vanished winner forever.
- **Adding a source is cheaper.** A new source supplies an activity probe and,
  optionally, a hint edge. It does not supply arbitration, which means it cannot
  get arbitration wrong.
- **The cost is one extra read per edge.** Mux re-probes rather than trusting
  the hint's payload. On four sources that is negligible, and it is what makes
  the alert path and the patrol path the same code.
