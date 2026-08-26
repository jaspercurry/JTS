# ADR-0171: Rarely-viewed dashboard probes run in short-lived child processes

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`jasper-control` is always resident. Any module it imports to answer a
dashboard card is resident for the life of the daemon, whether or not anyone
ever opens that card. The Home Assistant status card was the clearest case: a
probe run at most a few times a day pulled the whole `jasper.home_assistant`
import graph into the always-on process. The alternative shapes were a second
long-lived daemon (another resident baseline) or lazy imports (the graph is
still retained after the first view).

## Decision

**A dashboard probe whose import graph is larger than its answer runs as a
short-lived child process; the parent keeps only the resulting JSON.** Home
Assistant status is the reference implementation: `HomeAssistantStatusCache`
spawns `python -m jasper.control.ha_probe_child`, which imports the client,
reads the wizard env file, runs the probe, prints JSON and exits. A stale read
returns immediately with `checking=true` or the previous status while one
refresh runs; failure is bounded by the child timeout and logged as
`event=ha.status_probe_failed`.

This is a targeted remedy, not a house style. A probe that reuses an import
graph the daemon already holds stays inline — the Audio status view reuses the
existing collector and the System sampler's cached service-state snapshot
rather than adding either a child process or a second `systemctl` cadence.
Reach for a child process when a measurement shows meaningful retained RSS.

## Consequences

- The card's cost is paid per view, in a process that exits, instead of
  permanently in the daemon that must survive memory pressure.
- Each probe pays a process spawn and a JSON hop, so this shape is wrong for
  anything on a hot path or needing sub-second freshness.
- The bounded-failure behavior is a real feature: a hung integration can no
  longer wedge a dashboard request, because the timeout kills a child rather
  than a thread inside the always-on daemon.
- Rejected: a second resident daemon for integration polling (adds exactly the
  baseline this avoids), and lazy imports (defers the retention, does not
  remove it).
