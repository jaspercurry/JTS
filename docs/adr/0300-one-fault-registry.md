# ADR-0300: One fault registry: code, household copy, next action, retriable

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

Measurement refusals grew in several families: walk constants, CLI constants,
graph exceptions, prescription slugs, and stimulus incidents. Routes exposed
different envelopes, and some callers had to inspect prose or exception type
to choose recovery. That made the same fault acquire several owners.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §2.5 and evidence comments 1, 2, 5, and 11.


## Decision

`refusal_copy.REASON_REGISTRY` is the one owner of operator-facing tuning
faults. Every entry contains a stable code, one household sentence, a next
action, and whether the action is retriable. A bounded retry also reports the
attempts that remain in the run's structured evidence.

The registry absorbs the `WALK_*`, CLI `REFUSE_*`, graph-emission,
prescription, and stimulus families. `MeasurementGraphRefused` is classified
through the registry and does not escape as a bare `ValueError`.

Every HTTP and CLI boundary carries the same structured fields. Programs may
attach evidence, but they do not rewrite the code or household copy. Exit
status remains the CLI boundary's concern and is derived from the fault class,
not from sentence matching.

A proposed code enters the registry only if ADR-0002's discriminator separates
it from an existing one: would measuring again fix the condition? Capture
integrity, a description of the measured world, and an unsupported request are
not aliases merely because they lead to the same immediate stop.

## Consequences

The page, CLI, manifest, `/state`, doctor, and generated runbook can render one
vocabulary. Recovery logic becomes a typed decision and prose can change
without breaking callers.

The registry becomes a reviewed compatibility surface. Adding a code costs a
clear distinction and a next action; this is intentional pressure against
synonyms.

## Supersedes / Amends

This ADR applies ADR-0002's discriminator and ADR-0196's surviving rule that
denials use typed causes rather than exception or prose inspection.
