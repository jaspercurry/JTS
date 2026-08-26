# ADR-0167: Each transit network is its own provider module

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-transit-citibike.md was trimmed
  to its operational spine)

## Context

Citi Bike is one instance of a general shape: an open GBFS feed published per
city by an operator that runs several of them (Capital Bikeshare, BIXI, BAY
Wheels are the same vendor, the same schema, a different base URL and bounding
box). The subway path has the same pull toward generality — one "MTA-ish"
client parameterized by feed group.

The tempting consolidation is a single provider parameterized by network:
one module, one env key namespace, one tool that takes a `network` argument.
Third-party aggregators (citybik.es and friends) offer the same consolidation
from the other side — one endpoint covering every system, at the cost of an
intermediary between JTS and the operator's own feed.

## Decision

**A transit network is a provider module. It owns its own feed base URL,
bounding box, `env_keys`, `build_client`, and stop discovery, and it is
registered by adding it to a `CityPack`; the flat `REGISTRY` is derived from
the packs, never maintained alongside them.** JTS talks to the operator's own
feed, not to an aggregator.

A voice tool is likewise per network rather than one tool with a network
argument: the model picks the tool from the question's city context.

Shared machinery is extracted on evidence, not in advance — the GBFS cache
helpers stay module-private to `jasper.citibike` until a second network wants
them, and become a shared helper on the third.

## Consequences

- **A new city is additive.** A provider module plus one `CityPack` entry
  lights up the wizard, the daemon wiring, and the city toggle at once,
  because the registry is derived rather than hand-listed.
- **The user gets their system named.** A DC household sees "Capital
  Bikeshare", not "bikeshare (network: capital_bikeshare)". This is most of
  why the parameterized version was rejected.
- **One dependency fewer, one failure mode fewer.** No aggregator sits between
  JTS and the operator's feed, so an aggregator outage or schema lag cannot
  take out a working feed. The cost is that JTS carries each operator's
  quirks itself.
- **Duplication is accepted, briefly.** The second GBFS network will copy the
  first before the shared helper exists. That is the intended order: the
  duplicate is what proves which parts are actually common.
- **Two contracts must stay satisfied.** Every provider `env_key` must appear
  in the install migration's literal key array (a test enforces this), and a
  provider whose nearest stop is beyond the wizard's distance guard is dropped
  from the picker — a generous bounding box is therefore safe.
