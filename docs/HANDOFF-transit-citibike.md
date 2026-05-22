# HANDOFF — Citi Bike transit integration

Canonical reference for the Citi Bike (NYC + Jersey City + Hoboken)
voice tool. If you're modifying `jasper/citibike.py`, the citibike
provider, the `get_citibike_status` tool, or the Citi Bike card in
the `/transit/` wizard, read this first.

## What it does

One voice tool, `get_citibike_status`, that answers questions like:

- "What's the Citi Bike situation?"
- "Any bikes at 9 Av?"
- "Any e-bikes at Atlantic Avenue?"
- "Are there docks open near home?"

Responses split classic (pedal-only) bikes from e-bikes — a
distinction that matters operationally (e-bikes need charge, are
priced differently, and let users skip the BQE hill on a bad day).
Open docks report as one number; the e-bike/classic split doesn't
apply on return.

Configuration lives in two env vars, both wizard-written into
`/var/lib/jasper/transit.env`:

- `JASPER_CITIBIKE_STATIONS` — pipe-list of saved stations
  (`id|label,id|label`), same shape as `JASPER_BUS_STOPS`. IDs are
  GBFS station UUIDs from `gbfs.citibikenyc.com`.
- `JASPER_CITIBIKE_EBIKE_ONLY` — household-wide flag. `"1"` →
  voice answers omit classic-bike counts entirely; anything else
  reports both. Per-station overrides were considered (a household
  might only need e-bikes at the far station but accept classic at
  the near one) and explicitly rejected for simplicity.

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
  same shape under a different base URL — each becomes a separate
  provider module under `jasper/transit/providers/`, not a parameter
  on a generic "Lyft" provider.
- **TTL contract.** GBFS publishes a `ttl` in the manifest (60 s
  for Citi Bike); the spec encourages 30-s refresh on
  `station_status.json`. Stable enough to cache in-process.

What we don't use: Citi Bike's older `/stations/json` legacy
endpoint (deprecated), Lyft's internal API (private), or any
third-party aggregator (citybik.es is excellent but adds an
intermediary we don't need).

## Architecture in one paragraph

`jasper/citibike.py` owns the GBFS data layer (sync fetcher with
TTL cache + stale-on-error, runtime `CitiBikeClient`, dataclasses,
the `parse_saved_stations` parser). `jasper/transit/providers/citibike.py`
is the thin wizard adapter satisfying `TransitProvider` — same
`fetch_feed` powers `find_stops_near`. `jasper/tools/citibike.py`
wraps `CitiBikeClient.get_status` in `asyncio.to_thread` for the
realtime LLM session; the tool factory short-circuits to `[]` when
no stations are saved. `jasper/web/transit_setup.py` renders the
multi-station picker + e-bike-only toggle and persists picks into
`transit.env`.

## File map

| File | Role |
|---|---|
| [jasper/citibike.py](../jasper/citibike.py) | GBFS fetcher (`fetch_feed`), TTL cache, `CitiBikeClient`, `StationStatus`, parsers |
| [jasper/transit/providers/citibike.py](../jasper/transit/providers/citibike.py) | `_CitiBike` provider for the wizard; satisfies `TransitProvider` Protocol |
| [jasper/tools/citibike.py](../jasper/tools/citibike.py) | `make_citibike_tools` factory; `get_citibike_status` async tool |
| [jasper/web/transit_setup.py](../jasper/web/transit_setup.py) | `_citibike_card_html` wizard card + save-handler branch |
| [jasper/config.py](../jasper/config.py) | `citibike_stations`, `citibike_ebike_only`, `citibike_enabled` fields |
| [jasper/voice_daemon.py](../jasper/voice_daemon.py) | `CitiBikeClient` construction, registry wiring, system-prompt rules |
| [jasper/cli/doctor.py](../jasper/cli/doctor.py) | `check_citibike` health probe (added in PR 4) |
| [tests/test_citibike.py](../tests/test_citibike.py) | Unit tests for fetcher, cache, client |
| [tests/test_tools_citibike.py](../tests/test_tools_citibike.py) | Tool-dispatch tests |

## Cache strategy

Two feeds, two TTLs:

| Feed | TTL | Why |
|---|---|---|
| `station_information.json` | 1 h | Static-ish: id/name/lat/lon/capacity. Lyft retires/adds maybe a handful per month. |
| `station_status.json` | 30 s | Live bike/ebike/dock counts. GBFS publishes ≤ 60 s; 30 s catches every other publish while letting two tool calls within a window share one fetch. |

Cache is **module-private** in `jasper.citibike` — not a new
codebase-wide layer. If a third provider ever needs caching, promote
to a shared helper at that point; don't pre-promote. Implementation:

```python
_FEED_CACHE: dict[str, _CacheEntry] = {}
_FEED_LOCK = threading.Lock()
```

The lock guards cache reads/writes only; HTTP runs **outside the
lock**. Two concurrent calls might both fetch (wasteful but
harmless — both succeed, the second overwrites the cache entry).
Holding the lock during HTTP would serialize all GBFS requests
across all callers, which is worse than the duplicate-fetch edge
case.

### Stale-on-error

When a fresh fetch fails (timeout, 5xx, network error, JSON parse
error) AND any cached entry exists for the URL (even past its TTL),
`fetch_feed` returns the stale copy and logs at WARN with the cache
age. Only when fetch fails *and* no cached entry exists does it
raise `TransitError`.

This makes the voice tool degrade gracefully through a transient
GBFS outage:

- 0–30 s after a successful fetch: tool answers from cache, no I/O.
- 30 s–N min after, with GBFS healthy: tool fetches fresh, answers.
- During an outage: tool serves stale data, `last_reported_age_seconds`
  reveals the age to the LLM, the LLM prefaces with "as of N minutes
  ago…" (system prompt instructs this when age > 120 s).
- During an outage with no cache (cold start): tool returns
  `{error: "Citi Bike data is unavailable: ..."}`. The LLM speaks
  the error verbatim.

This is the same fail-soft posture HA uses (`OUTCOME_NETWORK` →
`speech="I can't reach Home Assistant right now"`) and what the
voice prompt expects from any tool.

## Tool response contract

The LLM-visible shape:

```python
{
    "stations": [
        {
            "label": "9 Av & 41 St",
            "station_id": "abc-uuid",
            "ebikes": 3,
            "docks": 25,
            "classic_bikes": 5,   # OMITTED when ebike_only_mode=true
            "status": "ok" | "offline" | "missing",
            "last_reported_age_seconds": 23,
        },
        ...
    ],
    "ebike_only_mode": bool,    # household-wide flag echoed
    "filter": "9 av",           # echoed back; empty if not passed
    "no_match": bool,           # true iff filter excluded ALL saved stations
}
```

Or on hard failure:

```python
{
    "error": "Citi Bike data is unavailable: GBFS request failed: ...",
}
```

Per-station status semantics:

- `"ok"` — `is_renting=1` and `is_installed=1`. Counts are honest.
- `"offline"` — present in GBFS but `is_renting=0` or
  `is_installed=0` (kiosk in maintenance, post-Sandy-relocation
  pending re-install). Counts may still be reported but the LLM
  is instructed to call this out ("Atlantic is offline").
- `"missing"` — saved `station_id` no longer appears in GBFS at
  all. Lyft retired the station. Counts are zero. Logged at WARN
  for the doctor to surface.

`is_returning=0` is treated as a non-event — most often it means
"every dock is full" not "this station refuses returns." The dock
count goes to zero on its own when full; no need to surface a
separate flag.

## Resilience

Five outcome buckets logged at `event=transit.citibike.fetch.*`:

| Bucket | Trigger | What user sees |
|---|---|---|
| `ok` | 2xx + parseable JSON | Normal response |
| `timeout` | `httpx.TimeoutException` (3 s budget) | Cache fallback or `{error}` |
| `network` | Other `httpx.HTTPError` | Cache fallback or `{error}` |
| `parse_error` | `ValueError` on `.json()` | Cache fallback or `{error}` |
| `station_missing` | Saved id absent from GBFS | Per-station `status="missing"` |

Timeout is tighter (3 s) than the bus tool's 4 s because GBFS is
CDN-served and consistently sub-500-ms — if we're not getting a
response inside 3 s, falling through to cache (or `{error}`) gets
the user a faster answer than waiting another second.

`station_missing` is not a hard error — other saved stations still
return live data; only the missing one degrades. `jasper-doctor`'s
`check_citibike` enumerates saved IDs against GBFS and surfaces
drift at boot.

## Geographic scope

Bbox: `(40.62, -74.10, 40.83, -73.85)` — generous rectangle
covering NYC's five boroughs, Jersey City, and Hoboken. The wizard
double-checks by dropping providers whose nearest station is more
than 5 miles away (the existing `MAX_NEAREST_STOP_MILES` guard), so
over-coverage just means the picker renders a "no nearby stations"
state for users at the bbox edge.

If Lyft expands Citi Bike's footprint further, edit
`CITIBIKE_BBOX` in `jasper/transit/providers/citibike.py`. If a
**different** Lyft system gets a JTS user (Capital Bikeshare DC,
BIXI Montreal, BAY Wheels SF), add a *new* provider module — same
shape, different bbox + GBFS base URL. Don't generalize the
existing module to multi-network: each city's user wants a
provider that names their system specifically.

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

## Adding another bikeshare network

A new Lyft GBFS network (or any GBFS network anywhere) is a fresh
provider:

1. Pick a stable slug (e.g. `capital_bikeshare`).
2. New module `jasper/transit/providers/capital_bikeshare.py`. Copy
   `citibike.py` as the starting point; change `GBFS_BASE`, `bbox`,
   `id`, `label`, `help_url`. Decide whether to share the cache
   helpers from `jasper.citibike` (yes if behavior is identical;
   factor them into a `_gbfs.py` shared helper at that point).
3. One line in `REGISTRY` at
   `jasper/transit/__init__.py`.
4. `elif p.id == "capital_bikeshare":` branch in
   `jasper/web/transit_setup.py:_index_html`. Reuse the citibike
   card if the UX is identical (likely is); just dispatch to
   `_citibike_card_html(p, state)` with a different provider —
   the card is provider-keyed only by `p.label` so it generalises
   if the env keys do.
5. New env keys in `migrate_transit_config`'s `keys=(...)` array
   at `deploy/install.sh`.
6. A `make_capital_bikeshare_tools` factory if you want a separate
   tool surface, or extend `get_citibike_status` to take a network
   arg if you want one tool per household for multiple networks. (I
   lean toward separate tools — the LLM benefits from explicit tool
   selection based on the question's city context.)

The shared cache helper migration is a real refactor opportunity;
flag it on the second provider, do it on the third.

## Testing

- `tests/test_citibike.py` — 38 unit tests for the fetcher (cache
  hit/miss/expiry/stale-on-error/5xx/parse-error), parser
  (round-trip + edge cases), runtime client (missing/offline/filter),
  provider (bbox, sort, snapshot rendering).
- `tests/test_tools_citibike.py` — 17 tool-dispatch tests (gating,
  schema, station_label routing, ebike_only_mode toggling, no_match
  semantics, TransitError → {error}, programming-error propagation).
- `tests/voice_eval/regression/test_citibike.py` (PR 4) — paid
  end-to-end against the live LLM provider. Two scenarios: general
  status, station-specific. Pass^3, with bike-count reality
  assertions ducking GBFS's minute-to-minute fluctuation (≥ 0
  rather than exact match).

## Open questions / future work

- **Per-station e-bike-only override.** If the household needs
  e-bikes-only at one station but accepts classic at another, the
  current global flag is too coarse. Add a per-station checkbox
  alongside the multi-select, store as `id|label|ebike_only,...`.
  Defer until the global flag bites in practice.
- **Walking time, not distance.** "1.4 mi" is information; "8
  minutes walk" is decision-grade. Could call Open-Route Service
  or OSRM at wizard render time. Adds a dep and a failure mode for
  marginal UX gain — defer.
- **Service alerts** (`system_alerts.json`). When Lyft posts a
  service-disrupting alert affecting a saved station, surface it
  in the voice answer. Free win — alerts feed exists, just not
  hooked up.
- **Multi-network UX.** When the household adds Capital Bikeshare
  alongside Citi Bike (e.g., a DC traveller), should the voice
  tool detect "which network does the user mean" automatically? At
  one network, this question doesn't exist. At two+, we'd want
  the provider to inject context (system name in the response) so
  the LLM can disambiguate. Defer until two networks exist.
