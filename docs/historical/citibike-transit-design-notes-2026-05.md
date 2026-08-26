# Citi Bike / transit design notes — 2026-05

> **Archived design record.** The live operational spine is
> [HANDOFF-transit-citibike.md](../HANDOFF-transit-citibike.md); the
> per-network provider rule is
> [ADR-0167](../adr/0167-each-transit-network-is-its-own-provider-module.md).
> This file keeps the feed-choice survey, the prior art, the
> add-another-network walkthrough, and the deferred UX questions from the
> original build. Paths and step numbers here are as of 2026-05 and have not
> been re-verified since.

## Why GBFS (and not something else)

The General Bikeshare Feed Specification ([gbfs.org](https://gbfs.org/))
is an open standard for shared mobility feeds maintained by
[MobilityData](https://github.com/MobilityData/gbfs). Lyft operates
Citi Bike and publishes GBFS at
`gbfs.citibikenyc.com/gbfs/gbfs.json`. Properties that made it the
right choice:

- **No API key.** Public CDN. No registration, no rate limits beyond
  what the CDN imposes, no approval delay (cf. MTA BusTime's ~30
  min wait).
- **Open standard with a registry.** Adding any other Lyft network
  later (Capital Bikeshare DC, BIXI Montreal, BAY Wheels SF) is the
  same shape under a different base URL.
- **TTL contract.** GBFS publishes a `ttl` in the manifest (60 s
  for Citi Bike); the spec encourages 30-s refresh on
  `station_status.json`. Stable enough to cache in-process.

What we don't use: Citi Bike's older `/stations/json` legacy
endpoint (deprecated), Lyft's internal API (private), or any
third-party aggregator (citybik.es is excellent but adds an
intermediary we don't need).

## Prior art surveyed

The pattern of "ask a voice device for nearby bike availability" is
well-trodden:

- **[Alexa "City Bike" skill](https://www.amazon.com/npci-City-Bike/dp/B01MU6BR5W)**
  — NYC-specific, save-station model. Closest direct analog.
- **[US Bike Share (VOGO Voice)](https://www.vogovoice.com/apps/bikeshare/)**
  — Multi-city Alexa skill (70+ US bike-share systems) via GBFS.
- **[Home Assistant CityBikes integration](https://www.home-assistant.io/integrations/citybikes/)**
  — Sensor-per-station model, radius-based or explicit-list config.
  Built on the citybik.es GBFS aggregator. Closest design-pattern
  prior art for a non-voice consumer of the same data.
- **[Raycast "Check Citi Bike Availability"](https://www.raycast.com/kcole93/check-citi-bike-availability)**
  — Desktop extension with saved-stations UX.
- **[kardolus/citi-bike-dock-tracker](https://github.com/kardolus/citi-bike-dock-tracker)**
  — Go CLI hitting the same GBFS feeds. Reference implementation
  for the JSON parsing.
- **[citybikes/gbfs-api](https://github.com/citybikes/gbfs-api)**
  — Reference Python GBFS client (we don't depend on it — adds a
  layer for one provider).
- **[citibike.live](https://citibike.live)** — Real-time web
  tracker; great for visualising what GBFS exposes.

JTS's contribution is the *voice* shape — first-class e-bike vs.
classic split, household-wide e-bike-only preference, stale-on-error
graceful degradation, integration with the existing transit-tool
ergonomics (same wizard, same provider abstraction, same
`{stop_id, label}` pipe-list config format).

## Adding another bikeshare network (2026-05 walkthrough)

A new Lyft GBFS network (or any GBFS network anywhere) is a fresh
provider:

1. Pick a stable slug (e.g. `capital_bikeshare`).
2. New provider module alongside the Citi Bike one. Copy it as the
   starting point; change `GBFS_BASE`, `bbox`, `id`, `label`,
   `help_url`. Decide whether to share the cache helpers from
   `jasper.citibike` (yes if behavior is identical; factor them into a
   shared `_gbfs` helper at that point).
3. Add the provider to the matching `CityPack` in `CITY_PACKS`; the flat
   `REGISTRY` derives automatically.
4. An `elif p.id == "capital_bikeshare":` branch in the wizard's
   `_index_html`. Reuse the Citi Bike card if the UX is identical
   (likely is) — the card is provider-keyed only by `p.label` so it
   generalises if the env keys do.
5. New env keys in `migrate_transit_config`'s `keys=(...)` array in
   `deploy/lib/install/env-migrations.sh`. A contract test requires that
   literal array to contain every provider `env_key`.
6. A separate tool factory if you want a separate tool surface, or extend
   `get_citibike_status` to take a network arg. (I lean toward separate
   tools — the LLM benefits from explicit tool selection based on the
   question's city context.)

The shared cache helper migration is a real refactor opportunity;
flag it on the second provider, do it on the third.

## Deferred questions (as of 2026-05, none since revisited)

- **Per-station e-bike-only override.** If the household needs
  e-bikes-only at one station but accepts classic at another, the
  global flag is too coarse. Would store as `id|label|ebike_only,...`.
  Defer until the global flag bites in practice.
- **Walking time, not distance.** "1.4 mi" is information; "8
  minutes walk" is decision-grade. Could call Open-Route Service
  or OSRM at wizard render time. Adds a dep and a failure mode for
  marginal UX gain — defer.
- **Service alerts** (`system_alerts.json`). When Lyft posts a
  service-disrupting alert affecting a saved station, surface it
  in the voice answer. The alerts feed exists, just not hooked up.
- **Multi-network UX.** At two+ networks we'd want the provider to
  inject system name into the response so the LLM can disambiguate.
  Defer until two networks exist.

---

Archived 2026-08-26 from HANDOFF-transit-citibike.md.
