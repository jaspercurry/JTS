# ADR-0160: The model catalog is curated metadata, not a runtime allow-list

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`jasper/voice/catalog.py` describes every provider the speaker knows about:
which models exist, which voices they offer, which provider-specific knobs
the `/voice/` wizard should render, and — for each model — whether it is
`tested`, a `fallback`, or `experimental`.

A curated list like that invites being enforced. If the catalog is the truth
about which models exist, the obvious next step is to reject any configured
model that is not in it. That would make the file a gate, and gates on a
list of third-party model IDs age badly: providers ship new models
continuously, and a speaker that refuses a model until someone edits a Python
file and redeploys is a speaker whose owner cannot try the thing that shipped
this morning.

The opposite failure is equally real: silently tracking a provider's "latest"
alias means an upstream release changes the household's assistant without
anyone deciding to.

## Decision

**The catalog is metadata for humans and the wizard. It is not a runtime
allow-list.** Adapters pass whatever `JASPER_<PROVIDER>_MODEL` is configured
straight through to the SDK, and the wizard preserves an unknown configured
value as a custom experimental row rather than erasing it on the next save.

Two properties follow, and both are the point:

- **No silent latest.** A speaker never moves to a newly released upstream
  model on its own. Promotion from experimental to tested is a human edit.
- **No permanent lock-in.** An operator can type or script a new model ID
  into the env file and it works, without a code change.

The one place a provider list *is* enforced is the fail-closed
`voice_provider_ids` manifest read by the pre-daemon reconciler
(ADR-0163) — that gates *provider IDs* at boot, not model IDs at runtime.

## Consequences

- Model discovery can be a convenience (an operator-triggered refresh that
  appends `experimental; discovered` rows) without ever becoming a policy
  input. Refresh never changes the configured model; only an explicit Save
  does.
- Someone can configure a model that does not exist, or one that exists but
  performs badly on this hardware. That is the accepted cost of not locking
  the box; the status labels are how the catalog tells the difference between
  "the default we run" and "an escape hatch".
- Pricing follows the same shape from the other direction: a model nobody
  priced is unpriced rather than rejected (ADR-0161).
