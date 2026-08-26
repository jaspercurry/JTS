# ADR-0147: The local-source lifecycle is one coordinator with three appliers — no resident daemon, no plugin API

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-source-lifecycle.md was trimmed
  to its operational spine)

## Context

Four household sources — AirPlay, Spotify Connect, Bluetooth, USB Audio Input
— can each be turned on or off, and each converges over different machinery:
two are plain systemd units, one is a radio plus a three-unit resource group
plus an accessory owner, and one is an ordered USB gadget recompose. The
obvious shapes were a resident lifecycle daemon holding the state machine, or a
plugin API letting each source register its own applier.

Both cost more than they return on a 1 GB Pi with one owner and four sources
that have not changed in a year. A resident daemon is a process to supervise, a
second place for state to drift from the file, and a thing that must be running
for a boot-time convergence that already has a perfectly good trigger. A plugin
API is a registration protocol, a lifecycle contract, and a set of extension
points, all paid to serve four hard-coded sources.

## Decision

**One root oneshot coordinator, three concrete appliers, dispatched from a
declaration — not a plugin framework and not a daemon.**

`jasper-source-intent-reconcile` reads all four intents and converges each
source independently. USB and Bluetooth select their concrete ordered appliers
first; any remaining lifecycle declaration with `intent_unit is not None` uses
the ordinary systemd applier. There is deliberately no second `{AIRPLAY,
SPOTIFY}` dispatch list to maintain, and no resident process between the intent
file and systemd.

Convergence is triggered explicitly and boundedly: a user toggle, boot, a
deploy, or a grouping role change. There is no `Restart=` loop.

## Consequences

- **Adding an ordinary source is a registry declaration, not a plugin.** A new
  source with a plain intent unit needs one row in
  `jasper/local_sources/registry.py`; the contract test then requires its
  matching unit gate before it can ship. A source that genuinely needs ordered
  hardware work needs a fourth applier and a deliberate review — which is the
  correct amount of friction for something that touches a radio or a USB
  descriptor.
- **The file is the only state.** With no daemon there is no in-memory
  lifecycle state to diverge from `/var/lib/jasper/source_intent.env`; every
  reader re-derives (ADR-0148).
- **Every convergence is bounded and attributable.** A oneshot with a finite
  `TimeoutStartSec` either completes or fails loudly with a reason, and its
  `--reason` names who asked. A resident reconcile loop would have made "why is
  Bluetooth off" a question about timing.
- **Failures isolate per source but the pass still fails.** One broken adapter
  cannot stop the other three from converging, and the oneshot still exits
  non-zero — so a partial failure is visible to deploy health without being
  contagious.
- **The cost is latency on rare paths.** A USB transition can legitimately take
  minutes, and a synchronous request waits for it (ADR-0149). A daemon would
  have made the toggle return sooner and the truth arrive later; the trade was
  taken deliberately in favour of the toggle telling the truth.
