# ADR-0221: Source start gates are marker files published by the coordinator

- **Date:** 2026-09-02
- **Status:** Accepted
- **Supersedes:** [ADR-0148](0148-every-source-unit-re-reads-canonical-intent-at-its-own-start-boundary.md)

## Context

ADR-0148 put `ExecCondition=jasper-local-source-allowed --source <id>` on every
source-owned unit so each start re-read canonical intent instead of trusting a
derived mirror. It named the cost — "a re-read per start" — and accepted it.

The measured cost is not small. Each evaluation spawns a fresh CPython that
imports `jasper.source_intent` (and through it `dbus_next`, `asyncio`, the DAC
profile registries): 228 modules, 109 ms of import, **29.2 MB peak RSS**
against a 17.4 MB bare interpreter. Ten units carry the gate and the renderers
carry `Restart=`, so on jts4 (streambox, `MemTotal` 415 MB — issue #3697)
`jasper-local-source-allowed` was the single most OOM-killed process of the
boot, 21 kills, self-amplifying: a renderer is killed, `Restart=` fires, the
restart spawns a 29 MB gate, pressure rises, something else is killed.

## Decision

**The source coordinator publishes one marker file per label; units gate on its
existence.** `jasper.local_sources.markers.publish_allowed_markers()` computes
the same verdicts ADR-0148's guard computed and mirrors each into
`/run/jasper-source-intent/allowed/<label>` for `airplay`, `spotify`,
`bluetooth`, `usbsink`, and `shared` (the role-only verdict for infrastructure
with no single source intent). Every gated unit carries `ConditionPathExists=`
against its label; the Python entry point and its argparse CLI are deleted.

`jasper-source-intent-reconcile.service` is the single writer — it already owns
every start and stop of the source units, and every other owner already drives
it. The markers live in the `RuntimeDirectory=jasper-source-intent` that unit
already declares: no new directory, no tmpfiles entry, no new knob. They are
published **before** the apply loop, from intent, role, and physical capability
only, so a failed transition cannot withdraw a function the household asked for
(ADR-0191).

## Consequences

- **Zero processes on the start path.** systemd stats one file.
- **Absent fails closed**, so the empty tmpfs before the coordinator's first
  pass blocks every start. The gate's failure direction is unchanged.
- **A wedged coordinator now leaves the previous verdict standing** instead of
  failing the start — deliberate, because the guard's own footprint was a
  leading cause of that wedging.
- **`jasper-mux.service` gains one start.** It is the only gated unit no source
  lifecycle owns, so the coordinator starts it after publishing. Like
  `ExecCondition=`, `ConditionPathExists=` is start-boundary only: mux is never
  stopped by the gate and keeps running through a park exactly as before.
- **USB audio appears one recompose later at boot.**
  `jasper-usbgadget-compose.sh`'s `AUDIO_ALLOWED_CMD` now tests the `usbsink`
  marker, and the gadget composes ahead of the coordinator's first pass; a
  coordinator that never completes leaves the audio function absent.
  `HARDWARE_ALLOWED_CMD` still probes hardware directly.
- **Each unit keeps `UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT
  GLIBC_TUNABLES`.** ADR-0148 needed it for the `+`-privileged gate argv, but
  it also covers that unit's own `ExecStart`/`ExecStartPre`.
- **The `<label>` vocabulary stays registry-derived.** The contract test derives
  the expected `ConditionPathExists=` line for every declared source from the
  same registry, so a new source cannot ship without its gate.
