# ADR-0301: The listening flag retires; an intact trial capture is the measured requirement; verification is advice

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

A household listening flag could block a measured candidate after a prior
apply made that flag stale. The flag asked for a human ceremony even when the
candidate carried an intact capture of the exact graph. Review also found that
an apply-time writer proposed for the flag ran after the gate it had to open.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.7 and 3 D3 and evidence comments 3–5 and 12–15.


## Decision

The household listening flag retires as an apply precondition. The measured
requirement is an intact trial capture of the exact candidate graph in a
completed run. Candidate and captured graph identities must match.

The four `VerificationResult` dimensions—capture validity, realization,
benefit, and specification—are computed from that trial evidence and reported
as independent advice. A tracking miss or target miss does not corrupt an
intact capture and does not become a post-apply gate.

The loop is compose, trial, bank, apply. A later measure of the applied graph
answers a new question; it is not a condition for the preceding apply.

A room-only or bass-only candidate is identified by its layer diff, not by the
program label. It may reuse the unchanged speaker layer's evidence. A changed
speaker layer or newly composed complete graph needs trial evidence for its
own identity.

## Consequences

Measured evidence replaces a stale household bit. Advice stays visible on
`/state` and in doctor so an operator can judge quality without turning a
quality miss into a park.

An intact capture can support an apply that later sounds poor. This is the
intended scientist loop: predictions propose, measurements dispose, and only
the named output-path protections remain hard stops.

## Supersedes / Amends

This ADR applies ADR-0101 lines 38–42 to tuning proof validity. It retires the
`baseline_summed_validation_missing` listening precondition; ADR-0288's single
apply door and graph-identity requirements remain.
