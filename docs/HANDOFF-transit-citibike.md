# HANDOFF — NYC transit integrations

Operational spine for the subway, Citi Bike, and Google Routes paths: who
owns configuration, what each runtime client guarantees when its upstream
misbehaves, and where the answer shapes live. The `/transit/` wizard owns
saved configuration; provider modules own discovery and client construction;
runtime clients own live-data parsing and caching.

Design record (why GBFS, prior art, the add-another-network recipe, the
deferred UX questions):
[historical/citibike-transit-design-notes-2026-05.md](historical/citibike-transit-design-notes-2026-05.md).
Per-network provider modules are [ADR-0167](adr/0167-each-transit-network-is-its-own-provider-module.md).

## NYC subway: primary API plus direct fallback

`jasper.subway.SubwayClient` tries Subway Now first because its station
endpoint aggregates all seven MTA feeds and therefore sees rerouted trains.
When that request fails, JTS polls the relevant public MTA GTFS-Realtime feed
directly. The fallback deliberately sees only the configured station's
CSV-documented lines; that reroute limitation is the documented degradation,
not an accidental parser difference. The tool response reports
`source="subwaynow"` or `source="mta-gtfs"`.

The fallback parses standard GTFS-Realtime wire fields with
`gtfs-realtime-bindings`. JTS defines only MTA extension field 1001's
`train_id` locally, because the standard trip id can repeat during the
daylight-saving repeated hour and TripUpdate/VehiclePosition pairing must
remain unambiguous. Pairing uses the final seven train-id characters, matching
MTA's established prefix-tolerant contract. JTS does not depend on
`nyct-gtfs`: that package's stale generated bindings hard-pin the unsupported
protobuf 4.25.3 runtime.

The detailed response contract and feed-group map stay closest to their
implementation, in `jasper.subway` and `jasper.tools.subway`.

## Citi Bike

One voice tool, `get_citibike_status`, answers "what's the Citi Bike
situation", "any e-bikes at Atlantic Avenue", "are there docks open near
home". Responses split classic (pedal-only) from e-bikes — the distinction is
operational: e-bikes need charge, are priced differently, and skip the hill.
Open docks are one number; the split doesn't apply on return.

**The LLM-visible response shape is the docstring on
`make_citibike_tools` in `jasper.tools.citibike`** — per-station fields,
the omit-on-`ebike_only_mode` rule, and the voice answer style live there,
next to the schema the model actually sees. Don't restate it here.

Configuration, both wizard-written into `/var/lib/jasper/transit.env`:

- `JASPER_CITIBIKE_STATIONS` — pipe-list of saved stations
  (`id|label,id|label`), same shape as `JASPER_BUS_STOPS`. IDs are GBFS
  station UUIDs.
- `JASPER_CITIBIKE_EBIKE_ONLY` — household-wide flag. `"1"` → voice answers
  omit classic-bike counts entirely. There are deliberately no per-station
  overrides.

Per-station `status` semantics, which the voice answer leans on:

- `"ok"` — `is_renting=1` and `is_installed=1`. Counts are honest.
- `"offline"` — present in GBFS but not renting or not installed (kiosk in
  maintenance, re-install pending). Counts may still be reported; the prompt
  instructs the model to call the state out.
- `"missing"` — the saved `station_id` no longer appears in GBFS at all.
  Lyft retired it. Counts are zero, logged at WARN, and
  `jasper-doctor`'s `check_citibike` surfaces the drift at boot.

`is_returning=0` is treated as a non-event — it usually means "every dock is
full", which the dock count already says.

### Cache and stale-on-error

Two feeds, two TTLs, both module-private to `jasper.citibike`
(`INFO_TTL_SECONDS`, `STATUS_TTL_SECONDS`): station information is
static-ish and cached for an hour; station status is live and cached for
30 s, which catches every other GBFS publish while letting two tool calls
inside one window share a fetch. Cache reads and writes are lock-guarded;
**HTTP runs outside the lock**, so two concurrent callers may both fetch
(harmless) rather than serializing every GBFS request behind one.

When a fresh fetch fails (timeout, 5xx, network, JSON parse) and *any* cached
entry exists for that URL — even past its TTL — `fetch_feed` returns the
stale copy and logs `event=transit.citibike.fetch.stale` with the cache age.
Only a failure with no cached entry raises `TransitError`, which the tool
turns into `{error: ...}` for the model to speak verbatim. The age reaches
the model as `last_reported_age_seconds`, and the prompt makes it preface
old data with "as of N minutes ago". This is the same fail-soft posture the
Home Assistant path takes.

The GBFS timeout is tighter than the bus tool's because GBFS is CDN-served
and consistently sub-500-ms: past that budget, falling through to cache gets
the user a faster answer than waiting.

## Google Routes travel-time companion

`get_travel_routes` answers destination ETA and directions ("how long to 30
Rock", "how do I get to JFK"). It is deliberately separate from the local
arrival-board tools: subway/bus/Citi Bike answer "what is next at my
configured stop"; Routes answers "how do I get from the saved speaker
location to this destination".

The `/transit/` wizard owns the setup surface:

- origin: `JASPER_TRANSIT_LAT`, `JASPER_TRANSIT_LON`,
  `JASPER_TRANSIT_DISPLAY_NAME` in `/var/lib/jasper/transit.env`;
- default mode: `JASPER_TRAVEL_DEFAULT_MODE` in the same file (`transit`,
  `drive`, `walk`, `bicycle`; spoken wording overrides per call);
- API key: `GOOGLE_ROUTES_API_KEY` in
  `/var/lib/jasper-secrets/google_routes.env` at mode `0640`.

The Routes key is billable and never belongs in `transit.env`.
`migrate_google_routes_key` in `deploy/lib/install/env-migrations.sh` moves
stale copies out of `/etc/jasper/jasper.env` or old transit files into the
secrets compartment before stripping the broad copies.

## Provider registration

Providers are grouped into `CityPack`s (`CITY_PACKS` in `jasper.transit`);
the flat `REGISTRY` is *derived* from the packs, so the two never drift. A
provider owns its own `env_keys` and `build_client`; `active_transit(env)`
builds the clients the voice daemon registers tools against. Two contracts
bind a new provider:

- every provider `env_key` must appear in `migrate_transit_config`'s literal
  `keys=(...)` array in `deploy/lib/install/env-migrations.sh` — a contract
  test enforces it (its only non-provider member is
  `JASPER_TRAVEL_DEFAULT_MODE`);
- the wizard drops providers whose nearest stop is farther than
  `MAX_NEAREST_STOP_MILES` (`jasper/web/transit_setup.py`), so a generous
  bounding box just renders a "no nearby stations" state at its edge. Citi
  Bike's box (`CITIBIKE_BBOX`) covers the five boroughs, Jersey City, and
  Hoboken.

## File map

| Module | Role |
|---|---|
| `jasper.subway` | Subway Now client, direct MTA GTFS-Realtime parser, cache, fallback policy |
| `jasper.tools.subway` | `get_subway_arrivals` tool and its response contract |
| `jasper.citibike` | GBFS `fetch_feed`, TTL cache, `CitiBikeClient`, `StationStatus`, `parse_saved_stations` |
| `jasper.tools.citibike` | `make_citibike_tools`; `get_citibike_status` and the LLM-visible schema |
| `jasper.google_routes` | Routes config parser, API client, response normalizer |
| `jasper.tools.travel_routes` | `make_travel_routes_tools`; `get_travel_routes` |
| `jasper.transit` | `CityPack`/`REGISTRY`; `active_transit(env)` builds and owns the clients |
| `jasper.transit.providers.*` | Per-network config/discovery adapters satisfying `TransitProvider` |
| `jasper/web/transit_setup.py` | `/transit/` wizard: station pickers, toggles, key handling |
| `jasper/cli/doctor/integrations.py` | `check_citibike` saved-station drift probe |

Tests: `tests/test_subway.py` pins the primary→fallback behavior against
recorded MTA wire bytes; `tests/test_citibike.py` and
`tests/test_tools_citibike.py` pin the fetcher/cache/client and the tool
dispatch.

---

Last verified: 2026-08-26 (env keys, `CITY_PACKS`/`REGISTRY` derivation,
`MAX_NEAREST_STOP_MILES`, `CITIBIKE_BBOX`, the stale-on-error event names,
`migrate_google_routes_key`, and `check_citibike`'s home in
`jasper/cli/doctor/integrations.py` rechecked against the tree. The
per-station response contract was removed from this doc as a duplicate of
the tool docstring, which had drifted ahead of it.)
