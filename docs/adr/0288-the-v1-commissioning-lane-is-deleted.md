# ADR-0288: The v1 commissioning lane is deleted

- **Date:** 2026-09-10
- **Status:** Accepted

## Context

Owner ruling #4788/#4792. The v1 commissioning lane — its apply/verify/receipt
service, the run store and transition journal, and every typed evidence
record — could not start a run since the v2 cutover: nothing constructed it.
PRs #4819/#4830/#4832 removed it in three parts.

## Decision

`handle_v2_apply` is the only apply door. Room authority is read from the
applied automatic profile's `measured_candidate_fingerprint`; there is no
receipt chain. The run store, its transition journal, the typed evidence
records, and their on-disk state files are gone. Files those components left
on existing boxes are inert — nothing reads them, and install no longer
provisions them.

## Consequences

ADR-0196 is superseded: its subject, the commissioning run record's
lock-and-read path, no longer exists. The advisory-lock fix it documented
still governs the live stores that share the pattern
(`active_speaker_crossover_level_run.json`,
`active_speaker_repeat_admission.json`); their citations now point here.
