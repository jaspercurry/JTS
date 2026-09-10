# ADR-0283: CamillaDSP starts only on a graph proved against its own topology

- **Date:** 2026-09-10
- **Status:** Accepted

## Context

`jasper-camilla.service` declared `Requires=jasper-audio-hardware-reconcile.service`.
That is a hard dependency on a `Type=oneshot`, so ANY non-zero reconciler exit
cancelled camilla's start job and left the daemon that must never stay stopped
with no `Restart=` to bring it back — silently, and for failures that have
nothing to do with the audio graph. #4416 R8 asked for `Wants=`.

`Wants=` alone was reverted in e0ee7e10e, because the hard dependency was
carrying something real: the statefile CamillaDSP loads is a POINTER to a graph
the reconciler proved, and a reconcile that fails after a topology change leaves
that pointer naming a graph whose crossover and protection belong to the
PREVIOUS speakers. Full-range through the previous topology's filters into a
tweeter is the hazard. A stopped daemon is not. The owner's ruling was to build
the narrow gate first, then soften.

## Decision

**1. The gate.** `jasper-camilla.service` runs
`deploy/bin/jasper-camilla-topology-gate` as its `ExecCondition=`. It compares
two fingerprints the root convergence publishes beside the CamillaDSP statefile:

- `<statefile>.topology` — the topology the statefile's graph was PROVED
  against. Written only AFTER the statefile write it certifies succeeded.
- `<statefile>.topology.unproved` — the topology a convergence was working on.
  Written at the TOP of the pass, as soon as the saved topology is read, and
  removed only when a statefile write succeeded. What survives a pass therefore
  names the topology whose graph nobody proved, whatever ended the pass —
  a refusal, an early exit on some other stage, or a kill mid-flight.

Two present, non-empty, DIFFERENT values refuse the start. **Unknown allows**:
either stamp absent, empty or unreadable is a fact nobody observed, and a
permissions regression must not silence a speaker over one. The gate never
writes a stamp, because shell cannot prove a graph.

It is shell (ADR-0226 rules 1 and 2): no interpreter in an `ExecCondition=`, no
subshell, two file reads. It shares the guard family's logger and statefile
default through `deploy/lib/jasper-camilla-guard-common.sh`.

**2. The heal `stopped` case is the narrow exception to ADR-0271.** That ADR
rules "a guard unit that is not active means the fault is systemd's and heal
stands down". This supersedes that ONE sentence for exactly one posture: a
condition skip is a SUCCESS, so nothing reads failed anywhere and no
`Restart=always` will ever retry it. `heal_supervisor.camilla_stopped_reason`
answers `topology_gate` when camilla is loaded, not active, not mid-transition,
the reconciler is not failed, AND the gate's own `/run` record says it refused.
Observe-only, like every other heal case: the named action is the dashboard's
own restart-audio.

**3. The softening.** `Requires=` becomes `Wants=`; `After=` stays. The
tree-wide "no `Restart=always` daemon hard-depends on a oneshot" pin is
restored, and now merges `*.service.d/*.conf` drop-ins the way PID 1 does.

**Removal condition**, stated beside the gate script's header and beside
`camilla_stopped_reason`: both go when jasper-camilla no longer starts from a
persisted pointer — a CamillaDSP handed its graph, or one that re-proves the
statefile itself, leaves the gate nothing to compare.

## Consequences

**Accepted residual.** `Wants=` does not propagate failure, so a convergence
that fails for a reason OTHER than the topology — and leaves the hardware
unchanged — now lets CamillaDSP start on the PREVIOUS environment where
`Requires=` would have stopped it. That is deliberate: the previous environment
is the one the box was already playing through. The hazard the hard dependency
actually guarded is still closed, because the hardware IS in the hash
(`topology_config_fingerprint` covers `hardware`/`speaker_groups`/`routing`), so
a detected hardware change moves the unproved stamp and the gate refuses.

A refusal is a silent speaker and NO cue can play, because every cue path
traverses jasper-camilla (docs/audio-paths.md) — the gate would have to announce
through the daemon it just refused. This is not a new deafness path: it narrows
one, since `Requires=` stopped camilla for every non-zero reconciler exit with
no cue either. The refusal reaches three surfaces instead —
`event=camilla_topology_gate.refused`, the `camilla statefile topology` doctor
row (fail, `camilla_statefile_topology_mismatch`), and
`/state.resilience.heal`. A box whose stamp WRITES fail logs
`event=camilla_topology_stamp.write_failed` and the same doctor row warns
`camilla_topology_stamps_missing`, so a gate that has gone blind is visible
rather than merely permissive.

Rejected: keeping `Requires=` and widening the reconciler's success definition —
it leaves every unrelated reconciler failure a silent stop. Rejected: a Python
`ExecCondition=` that re-derives the topology — an interpreter on every
CamillaDSP start, against ADR-0226 on a 415 MB box.
