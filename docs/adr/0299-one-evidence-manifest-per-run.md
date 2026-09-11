# ADR-0299: One evidence manifest per run, written by the executor

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

Run facts were split across a plan result, banked records, derived views, and a
speaker-oriented packet. Some readers needed a per-set identity while apply
and prescription binding needed a run identity. A successful loop could also
describe a run as measured without consulting each take.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §2.4 and evidence comments 1, 3, 4, and 11.


## Decision

The executor writes one evidence manifest for each run. It appends immutable
take facts as work happens and closes the manifest with a status derived from
those facts. Sets group one configuration and capture condition across poses;
they do not split the run into separate packages.

Each take records pose kind, distance, angle, elevation, and place; output side
and driver role; candidate identity, graph identity, and `program_id`; Main,
Aux1, and stimulus levels; quality verdict, supporting evidence, capabilities,
and attempts; raw WAV and sidecar pointers under ADR-0017; and timing. Refused
attempts remain evidence.

The run header records the plan fingerprint. Run status is `complete`,
`partial`, or `cancelled` from per-take facts. It lists typed faults with next
actions and states what was not measured and why. An interrupted or terminally
refused run cannot be called complete because its loop returned normally.

The manifest is raw evidence plus execution status. It is not the round
receipt governed by ADR-0015, and it does not grade the round. Derived analysis
may bind to a set and a prescription may bind to the manifest, but neither may
rewrite it.

## Consequences

Status, analysis, prescription, and apply read one run identity. Multi-pose
analysis no longer reconstructs sets from scattered documents. The manifest
is larger than a summary, so CLI answers stay compact and point to it.

Incomplete work is useful and honest: kept takes remain available while the
missing measurements and their causes are explicit.

## Supersedes / Amends

This ADR honors ADR-0015's receipt boundary, ADR-0017's raw-retention rule,
ADR-0258's side × role identity, and ADR-0260's pose vocabulary.
