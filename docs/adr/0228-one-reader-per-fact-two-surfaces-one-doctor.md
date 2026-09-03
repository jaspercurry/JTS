# ADR-0228: One reader per fact, two surfaces, one doctor

- **Date:** 2026-09-03
- **Status:** Accepted

## Context

An audit of the observability layer at HEAD `8dfedc81` found three health
tools (`jasper-doctor`, `/state`, `deploy/bin/jasper-deploy-health`) that
each re-derive the same facts from the same evidence with their own parsers
and their own thresholds. Where the copies disagree the household and the
operator get different answers on the same box: fan-in progress age is
"stale" at 1 s in the doctor and 5 s on the dashboard; `/state` read
outputd's `aec_clock` one nesting level too high and served `null` on every
real speaker while the doctor read it correctly; `/state` parsed `nmcli`
with the colon bug the doctor's private copy had already fixed; the doctor
built its AEC intent without the two chip-beam legs `/aec` reads from the
same file.

The doctor is 175 checks in 19.5k lines with 837 test assertions on prose
and none on the `reason` code field that exists for that purpose. A skipped
check reports `ok`. `speaker_silent` never reaches the JSON the dashboard
shows. One run opens the fan-in socket five times and the CamillaDSP
statefile ten times. `/state` forks five or six processes per uncached call
for a payload with six machine readers, four of them doctor checks, while
the dashboard polls `/system/snapshot`. `jasper-deploy-health` duplicates
about ninety percent of the doctor with weaker hand-rolled parsers, and its
stated reason to exist (a broken venv) is not a path either caller takes.

## Decision

1. **One reader per fact, homed in `jasper/`.** A fact is read by exactly
   one function, which lives with the subsystem that owns the evidence,
   never in `jasper/cli/doctor/` and never in `jasper/control/`. Any
   threshold that turns the fact into a verdict lives beside that reader.
   The doctor, jasper-control, and scripts consume it. Two implementations
   of one reader in reach converge before either grows. The clean examples
   already in the tree are the pattern: `park_record` behind
   `camilla_recover_state.snapshot()`, `transport_park.snapshot()`,
   `read_mic_presence()`, `derive_grouping_runtime()`.

2. **Two surfaces, each owning what only it can know.**
   - `/system/snapshot` stays the dashboard's live feed: sampler-fed,
     forks nothing.
   - `/state` is jasper-control's in-process posture: supervisor counters,
     the measurement hold, the debug session, sampler verdicts, and the raw
     daemon `STATUS` bodies passed through verbatim. It is fail-soft, owns
     no thresholds, publishes one `active_source`, pins its top-level key
     set with a test, and carries a `schema_version`. No probe behind it
     spawns a process per request: a probe that needs `nmcli`, `busctl`,
     `journalctl`, or a child interpreter runs behind a sampler on its own
     cadence or moves to the doctor.
   - `jasper-doctor` is the one health tool: a root one-shot verdict layer
     over the readers in rule 1, plus the probes that need root or hardware
     access (renderer ALSA opens, mixers, secret compartments, configfs,
     outbound network). It reads `/state` once per run for the facts that
     exist only in jasper-control's memory. It never re-derives a fact a
     reader owns.

3. **The doctor's output is a contract, not a report.** Status is one of
   `ok`, `warn`, `fail`, `skipped`. A check that did not run says
   `skipped`, never `ok`. Every `warn` and `fail` carries a `reason` from a
   closed vocabulary that a registry test enforces; harness-generated rows
   (crash, timeout, skip, placeholder) carry one too. `speaker_silent` is
   in the JSON and leads the dashboard summary. An informational fact is
   `ok` with a reason, not `warn`. Tests pin `status` and `reason`, never
   `detail`.

4. **Evidence is collected once per run.** Sockets, statefiles, the output
   topology, and `systemctl show` are read into a per-run cache; checks are
   functions over it. A check is registered in the module of the subsystem
   it observes, and display order follows module grouping rather than
   hand-numbered keys.

5. **No third tool.** `jasper-deploy-health` retires once a registry-
   selected `--core` subset of the doctor, with per-module imports made
   lazy so the subset does not pay for the modules it skips, carries its
   only unique rows: required units active, the accessory-reconcile path
   unit, and a pairing-aware streambox voice check. Install and deploy run
   that subset on every box; the oneshot unit carries `MemoryMax=` and
   `TimeoutStartSec=`.

## Consequences

Easier: a contradiction between the doctor and the dashboard becomes
impossible by construction for any fact behind a shared reader; a
self-healing consumer can branch on `reason` without parsing English; the
doctor's per-run cost on a Zero 2 W drops to one read per evidence source;
the test suite shrinks as prose assertions become code assertions and
example clusters become parametrized tables.

Harder: adding a check now means naming its reader's home and its reason
codes, not just writing a function; a `/state` field must justify a machine
reader or be served by the wizard that owns its file.

Given up: the doctor's hand-tuned interleaved display order; `/state` as a
place to try out a new probe before it has a home; the comfort of a
stdlib-only fallback for the case where the venv is broken, which neither
caller actually reached.

Rejected: folding the doctor into jasper-control (ADR-0226 forbids the
import graph and the root probes in a resident daemon, and it would split
the operator's one surface in two); keeping `jasper-deploy-health` as a
pared-down parallel implementation; one reason vocabulary shared across
domains (each domain owns a closed set; the registry test enforces closure,
not sharing).
