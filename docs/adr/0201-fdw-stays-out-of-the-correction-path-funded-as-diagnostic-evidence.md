# ADR-0201: FDW stays out of the correction path; funded as diagnostic evidence

- **Date:** 2026-08-31
- **Status:** Accepted

## Context

The master plan's "considered and deliberately not built" ledger rejected
frequency-dependent windowing for v1 as "genuinely disputed in the field."
Deep-research report 03
([`research/2026-08-31-tuning-methodology-deep-research/03-gating-windowing-and-low-frequency-truth.md`](../research/2026-08-31-tuning-methodology-deep-research/03-gating-windowing-and-low-frequency-truth.md))
confirms the dispute is real and unresolved: REW's author holds that FDW
and variable smoothing are "not equivalent" and that smoothing "provides
better EQ targets"; Acourate/Audiolense build correction on FDW with a
psychoacoustic rationale; no peer-reviewed head-to-head exists. The report
also identifies the one use with clean value for this program: an FDW'd
view *disagreeing* with the fixed-gate view is a reflection detector — a
dip that fills in under FDW but stays deep under the fixed gate is
reflection-caused and must never be boosted.

## Decision

The gating SSOT stays the fixed, versioned window for every correction
target and every grade. FDW enters as **re-analysis evidence only**
(ticket 6.10): 5- and 15-cycle variants computed offline from banked IRs,
published as per-feature facts beside the fixed-gate ladder's rungs. No
FDW-derived curve may be a fit target or a grade input. The diagnostic
reading rule lives in the operator guide, not in code.

## Consequences

One windowing truth is preserved; the reflection diagnostic is bought for
compute only; the not-built ledger entry stands amended by this ADR rather
than deleted. Revisit only on a peer-reviewed comparison with listening
end-points — that revisit is a new ADR.
