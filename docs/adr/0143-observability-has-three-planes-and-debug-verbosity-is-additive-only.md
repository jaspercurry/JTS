# ADR-0143: Observability has three planes, and debug verbosity is additive only

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the 2026-05-30 debug-mode design; recorded
  here when HANDOFF-observability.md was trimmed to its operational spine)

## Context

A household speaker has to stay diagnosable without turning its steady state
into a profiler. Every observability request arrives as "just add a bit more
logging / one more counter / a bundle button", and each is individually cheap;
together they cost SD-card wear, RAM on a 1 GB Pi, and — worst — they blur the
line between the always-on truth an operator trusts and lab equipment that
happens to be running.

The failure that matters is the reverse one too: a verbosity control that can
*lower* a level would let a forgotten toggle silence the WARNING/ERROR lines
the resilience ladder depends on. Those get one shot when a rare failure fires.

## Decision

**Three planes, with a boundary nothing crosses:**

1. **Production health** — always-on, cheap, fixed-shape truth: `/healthz`,
   `/state`, `/system/snapshot`, `jasper-doctor --json`, daemon `STATUS`
   sockets, and structured `event=` journal lines. It may add low-cost fields
   (service `ActiveState`/`SubState`/`NRestarts`, bridge counters, observed-vs-active
   hardware). It must **not** contain raw log bundles, PSS scans, profilers, or
   soak history.
2. **Temporary debug verbosity** — the `/system` Debug card and
   `/var/lib/jasper/debug.env` raise scoped daemon logging for a TTL-bound
   session. Additive only, auto-expiring; not a memory profiler.
3. **Bounded diagnostics** — explicit operator commands that produce an
   artifact and exit (`jasper-system-soak` via `scripts/pi-run-diagnostic.sh`,
   so systemd bounds memory/runtime and the kernel has an obvious process to
   kill before product daemons). A device-specific intermittent fault may use
   an explicitly enabled, hard-capped RAM ring with deliberate artifact freezes
   — USB gadget forensics is the one instance, and it stays separate from the
   TTL toggle.

**Debug mode is additive only.** It may raise verbosity; it must never lower a
daemon below WARNING and never suppress a forensic `event=` line. There is no
quiet mode. Same spine as "no silent failure paths".

## Consequences

- Production gets truth, not lab equipment: a new observability feature must
  name which plane it lands in, and features that want two planes get split.
- The forensic/heartbeat split is what makes a debug toggle safe at all: every
  recovery, probe-fail, wedge, and restart decision is WARNING or ERROR, and
  always-on INFO is a small, known set of heartbeat emitters.
- Raising a level costs a daemon restart (the level is read once at startup),
  which is mildly self-defeating for a live issue — the flight recorder exists
  precisely for the already-happened case.
- The TTL is enforced twice on purpose: each daemon self-quiets in process at
  expiry (no restart, so a forgotten session cannot blip wake mid-use), and
  jasper-control separately clears the `debug.env` SSOT so `/state` reads off
  and the next start is clean.
- The Debug card is deliberately narrow. Only daemons with a clean
  `basicConfig` seam are members; mux's `--log-level` CLI arg and shairport's
  config-file `log_verbosity` are a different mechanism and stay out rather
  than being wrapped.
