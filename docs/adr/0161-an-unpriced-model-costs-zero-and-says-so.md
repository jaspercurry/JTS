# ADR-0161: An unpriced model costs zero and says so — a rate is never inferred

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

The daily spend cap is a circuit breaker: `UsageStore` prices each closed
session and `SpendCap` refuses new voice sessions once the rolling 24-hour
estimate crosses the configured limit. Pricing is keyed by **exact model ID**
— there is no provider-level rate, because two models from the same provider
routinely differ by an order of magnitude.

Because the catalog does not gate which model runs (ADR-0160), a speaker can
legitimately be configured with a model that has no bundled rate and no
override. Something has to happen when the store is asked what that session
cost. The tempting answers are all inferences: fall back to the provider's
other models, to the most expensive known rate, or to the previous model's
rate.

Every one of those reports a number nobody measured, on a surface whose whole
job is to be trusted enough to cut off the household's assistant.

## Decision

**A model with no rate resolves to an all-zero `Pricing` labelled
`unpriced:<id>`, and `jasper-voice` logs `event=pricing.unpriced`.** Cost
reads $0 until someone sets a rate. We never invent a number.

Rates are data, not code: bundled defaults ship dated in
`jasper/data/model_pricing.json`, the `/voice` editor writes per-model
overrides, and an optional `JASPER_PRICING_FILE` overlays them — so
correcting an unpriced model is a data edit, not a deploy.

## Consequences

- The failure is loud but not blocking: the speaker keeps working, the
  journal names the unpriced model, and `/voice` shows $0 for it.
- The cap under-counts while a model is unpriced. That is the accepted
  direction: a breaker that fires on a fabricated number is worse than one
  that briefly under-reads, and the log line is the fix instruction.
- The same honesty rule governs the estimate's other end: stored cost is a
  true estimate and the cap pads it at read time with a safety multiplier, so
  the breaker stays conservative without inflating the number a human sees.
- For a time-billed provider the "rate" is not tokens at all — token rows
  price to $0 by design and billable active-turn intervals carry the cost.
  That is a different meter, not an inferred rate, and the provider's own
  dashboard remains the billing source of truth.
