# ADR-0203: The incumbent tune retires; recommissioning is structure-first

- **Date:** 2026-08-31
- **Status:** Accepted

## Context

The flat campaign banked −100 µs as the measured inter-driver offset but
left the applied delay at 0, because applying the delay under the
inherited EQ regressed the seat-verified response — the record
([`historical/flat-campaign-2026-08-31.md`](../historical/flat-campaign-2026-08-31.md)
§5) correctly names the mechanism: two campaigns of response-space tuning
had EQ'd around the misalignment. Deep-research report 04 identifies this
as the documented industry trap and states the documented remedy: EQ
fitted on a misaligned sum is invalid once structure is corrected — it is
discarded and re-derived, never adjusted. The conductor session proposed
deferring the decision until vertical-plane evidence existed; the owner
ruled instead.

## Decision

Owner ruling, 2026-08-31, in chat: *"I totally agree with dumping our
entire config and starting from scratch, doing the right methodology and
the right sequencing with time being first… that to me is not debated. If
we know our timing is off, we should fix that first. We can take more
measurements again."*

1. **The incumbent 24-filter tune is retired as a lineage, not torn
   down.** It stays applied until the recommissioning campaign's first
   accepted candidate replaces it — a partially-working speaker beats one
   aired out by an error.
2. **The next campaign is structure-first:** polarity and the measured
   inter-driver delay are committed before any response fit; the declared
   crossover is confirmed against the committed structure; per-driver
   linearization and summed-region corrections are re-derived after.
   Nothing response-shaped is inherited from the retired tune (consistent
   with ADR-0011's no-inheritance rule).
3. **Wave 6's 6.12 (sideways-cabinet vertical family) and 6.14
   (forward-model expected delta) are verification instruments for that
   campaign**, not decision gates — the decision is this ADR.
4. The retired tune's grades remain historical truth; the recommissioning
   campaign opens by measuring its own entry baseline, per ADR-0192's
   pattern.

## Consequences

Correct-by-construction ordering, and the Wave-6 re-analysis of the banked
campaign turns from pruning work into campaign priors — which incumbent
features were artifacts is known before anything is fitted. Given up: the
alternative disposition (keep 0 µs, empirically tied on every measured
horizontal frame) — rejected by the owner in favor of structural
correctness; the vertical read will document what the correction bought.
