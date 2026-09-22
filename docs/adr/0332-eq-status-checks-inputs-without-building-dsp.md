# ADR-0332: EQ status checks inputs without building DSP

- **Date:** 2026-09-22
- **Status:** Accepted
- **Supersedes:** The EQ status dry-run requirement in ADR-0331.

## Decision

Each graph carrier owns one preparation step for EQ eligibility. Status,
preview, and save use it to check the loaded graph type, grouping route,
saved candidate, speaker identity, and declared driver floor as applicable.
An EQ status request does not compile a graph or prepare an apply record.

Preview and save compile and validate their actual graph, including output
trim and headroom, through the existing DSP owners. Eligibility is not an
approval of a proposed edit. The existing load, rollback, and persistence
transaction remains the only writer.

Saved candidate readers require an explicit applied record. Status owners
load that record once and pass the same snapshot to identity, coverage, and
commissioning derivations. No reader silently reloads the applied record.

## Evidence

A bounded cold-process profile on jts3 attributed most EQ status time to
`compile_applied_tune`. Preparing the apply record imported the measurement
planning stack; compilation and proof also parsed the generated YAML. These
operations are needed for an edit, but not for displaying the EQ editor.
