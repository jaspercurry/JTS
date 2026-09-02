# ADR-0220: Source start gates are marker files published by the coordinator

- **Date:** 2026-09-02
- **Status:** Accepted
- **Supersedes:** [ADR-0148](0148-every-source-unit-re-reads-canonical-intent-at-its-own-start-boundary.md)

## Context

ADR-0148 put `ExecCondition=jasper-local-source-allowed --source <id>` on every
source-owned unit so each start re-read canonical intent instead of trusting a
derived mirror. It named the cost and accepted it: "a re-read per start".

The measured cost is not small. Each evaluation spawns a fresh CPython that
imports `jasper.source_intent` (and through it `dbus_next`, `asyncio`, the DAC
profile registries) — 228 modules, 109 ms of import, **29.2 MB peak RSS**
against a 17.4 MB bare interpreter. Ten units carry the gate, and the renderers
carry `Restart=`.

On jts4 (streambox, `MemTotal` 415 MB — issue #3697) that made
`jasper-local-source-allowed` the single most OOM-killed process of the boot,
21 kills. The loop is self-amplifying: a renderer is OOM-killed, `Restart=`
fires, the restart spawns a 29 MB gate, pressure rises, something else is
killed.

## Decision

**The source coordinator publishes one marker file per label; units gate on its
existence.** `jasper.local_sources.markers.publish_allowed_markers()` computes
the same verdicts ADR-0148's guard computed and mirrors each into

```
/run/jasper-source-intent/allowed/<label>
```

for `airplay`, `spotify`, `bluetooth`, `usbsink`, and `shared` (the role-only
verdict for infrastructure with no single source intent). Every gated unit
carries `ConditionPathExists=` against its label instead of `ExecCondition=`.
The Python entry point, its argparse CLI, and its `/usr/bin/env -i` invocation
are deleted.

`jasper-source-intent-reconcile.service` is the single writer. It already owns
every start and stop of the source units, and every other owner already drives
it: the grouping reconciler hands the completed role to it, the wizards kick it
through the restart broker, and it runs at boot on both install profiles. The
markers live in the `RuntimeDirectory=jasper-source-intent` that unit already
declares — no new directory, no tmpfiles entry, no new knob.

Markers are published **before** the coordinator's apply loop, from intent,
role, and physical capability only. They never record whether an apply
succeeded, so a failed transition cannot withdraw a function the household
asked for (ADR-0191).

## Consequences

- **Zero processes on the start path.** systemd stats one file. The 29 MB
  spawn per renderer start is gone.
- **Absent fails closed.** An empty or missing marker directory blocks every
  start, so the tmpfs being empty before the coordinator's first pass is safe.
  The gate's failure direction is unchanged from ADR-0148.
- **Off and follower parking still dominate**, but they do so through a
  published fact rather than a per-start re-read. A wedged or OOM-killed
  coordinator now leaves the previous verdict standing instead of failing the
  start; the trade is deliberate, because the guard's own footprint was a
  leading cause of that wedging.
- **`jasper-mux.service` gains one start.** It is the only gated unit no source
  lifecycle owns, so nothing else re-starts it once its marker reappears; the
  coordinator starts it after publishing. It is still never stopped by the
  gate — `ConditionPathExists=`, like `ExecCondition=`, is start-boundary only,
  and mux keeps running through a park exactly as before.
- **The privileged boundary is gone rather than defended.** No renderer-
  adjacent Python runs at all, so the `/usr/bin/env -i` argv and fixed `PATH`
  that ADR-0148 needed disappear with the gate. Each unit's
  `UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES` stays:
  it also covers that unit's own `ExecStart`/`ExecStartPre`.
- **The gadget's audio gate moves with the rest.**
  `jasper-usbgadget-compose.sh`'s `AUDIO_ALLOWED_CMD` — which ADR-0191 kept
  precisely because it read canonical intent — now tests the `usbsink` marker.
  ADR-0191's requirement survives in substance: the marker states intent, role,
  and physical capability, never readiness, so the failed-transition case that
  ADR-0191 was written about still composes UAC2. What is new is that the
  gadget composes before the coordinator's first pass at boot (it is ordered
  ahead of it), so USB audio appears one recompose later than it used to, and a
  coordinator that never completes leaves it absent. `HARDWARE_ALLOWED_CMD`
  still probes hardware directly and is unaffected.
- **The `<id>` vocabulary stays registry-derived.** The contract test now
  derives the expected `ConditionPathExists=` line for every declared source
  from the same registry, so a new source declaration still cannot ship without
  its gate.
