# ADR-0226: Constrained-hardware doctrine — push don't pull, no spawns, one interpreter

- **Date:** 2026-09-03
- **Status:** Accepted

## Context

Issue #3697: jts4, a Pi Zero 2 W (415 MB RAM, 208 MB zram) is a supported
target, and one boot ran it to ~323 MB with 201 OOM kills across 28 units
including udevd, dbus, logind, and the renderers. `jasper-voice` alone held
158 MB, 58 MB of it `scipy.signal` imported for a single 2x upsample (fixed
in #3708). `jasper-local-source-allowed`, an `ExecCondition=` spawning a
~20 MB CPython on 12 renderer units, was the single most OOM-killed process
of that boot, self-amplifying under `Restart=`: a renderer dies, restarts,
spawns the gate again, pressure rises, something else dies. The owner's
ruling: JTS is whittled down to fit the Zero 2 W, never moved to bigger
hardware, and every cut must hold on every box — no per-profile switch, no
new `JASPER_*` knob.

## Decision

1. **Push, don't pull.** A decision that changes rarely is computed once by
   its owner when it changes and written somewhere cheap to read (a marker
   file in tmpfs, an env file, a systemd property). Consumers read it with
   zero code: `ConditionPathExists=`, `EnvironmentFile=`, a `stat`. Never
   compute it on every read by spawning a process.
2. **No short-lived Python in hot or restart paths.** `ExecCondition=`,
   `ExecStartPre=`, `ExecStopPost=`, path units, timers, udev rules, nginx
   hooks: none may start an interpreter. Each Python start costs ~17 MB
   before the first import and 20-30 MB with the package loaded; under
   memory pressure `Restart=` loops turn these into a cascade. Replace with
   a marker file, a few lines of POSIX shell, or fold the check into the
   long-lived daemon that already owns the state.
3. **One interpreter per concern, not per feature.** Every resident Python
   daemon pays ~15-20 MB base (interpreter + numpy + the package import
   graph). Small daemons whose lifecycles already match are folded into one
   process. Heavy imports (scipy, `jasper.active_speaker`, rapidfuzz,
   measurement code) must not be reachable from a daemon's import path
   unless that daemon uses them on its steady-state path; make them lazy at
   the call site or move the caller.

## Consequences

Applying these rules landed #3708 (drop the scipy upsample import),
#3726/#3750/#3751/#3755/#3759/#3760/#3766 (shell-only `Exec*=` guards,
lazy heavy imports, shared reconcile stamps, daemon consolidation), with
#3774 and #3775 continuing the same audit. Boot-once oneshots and path-unit
reconcilers already following the push pattern are unaffected; an on-demand
spawn like `jasper-doctor --json`, run by a human and not on a hot or
restart path, is fine. A future agent adding any `Exec*=`/udev/timer line
must audit it for a hidden interpreter start, and must run
`-X importtime` before adding an import to a resident daemon. What this
gives up: a feature that only a per-Pi-model knob could deliver on the Zero
2 W stays undone rather than shipping behind a switch.
