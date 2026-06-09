"""Modular transit-provider registry.

A "transit provider" is one source of stop + arrival data — NYC Subway,
NYC Bus today; future Berlin BVG, Citi Bike, London TfL, etc. Each
provider lives in its own module under `jasper.transit.providers.`,
declares its bounding box + credentials, and implements two methods:

  - `find_stops_near(lat, lon)` — used by the setup wizard
  - `validate_credentials(credentials)` — cheap probe before save

See `jasper.transit.base.TransitProvider` for the contract. The
wizard at `/transit/` iterates this REGISTRY to discover which
providers cover a user's geocoded coordinates.

**Adding a new provider — concretely.** The discovery layer (bbox
coverage, `find_stops_near`, credential probe) is fully data-driven
over `REGISTRY` — drop a provider in and the wizard's geocode flow
picks it up. But the *full* contribution is NOT "three places": it
lands ~9-12 edits across six files. None of them are sneaky, but the
count is bigger than the data-driven `REGISTRY` makes it look, so this
list exists so a contributor isn't surprised half-way through. Each
numbered item is one logical edit point; the `voice_daemon.py` wiring
(item 7) is genuinely three separate edits in one file.

  1. Provider module: drop `jasper/transit/providers/<slug>.py`
     exposing a `PROVIDER` instance that satisfies `TransitProvider`
     (id, label, kind, help_url, bbox, env_keys, credentials, plus
     `find_stops_near` and `validate_credentials`). Mirror
     `nyc_subway.py` (keyless) or `nyc_bus.py` (credentialed).
  2. Registry: append `<module>.PROVIDER` to `REGISTRY` below.
  3. Wizard card dispatch: add an `elif p.id == "<slug>":` branch in
     `jasper.web.transit_setup._index_html` (next to the existing
     `nyc_subway` / `nyc_bus` / `citibike` cases). The unknown-id
     fallback there renders a "no UI yet" placeholder so the page
     still works while a contributor wires the rest up.
  4. Bespoke card renderer: write the `_<slug>_card_html(p, state)`
     function that item 3 dispatches to. There is no generic card —
     each provider's is hand-written (`_subway_card_html` has a
     direction radio, `_bus_card_html` has the locked-until-keyed
     flow, `_citibike_card_html` has the live dock/bike snapshot).
     This is the single biggest chunk of new code.
  5. Voice tool factory: add a `make_<slug>_tools(client)` factory
     under `jasper/tools/<slug>.py`. The tool's docstring is what the
     LLM reads — match the subway/bus docstring shape.
  6. Runtime client class: the `<Slug>Client` that item 5 wraps and
     item 7 constructs (mirror `jasper/subway.py`, `jasper/bus.py`,
     `jasper/citibike.py`). Owns the live arrival/status fetch.
  7. Voice daemon wiring — THREE separate edits in
     `jasper/voice_daemon.py`, easy to miss because they're far apart
     in the file:
       a. Client construction: import the client at the top, build it
          in `run()` as `<slug> = (<Slug>Client(...) if
          cfg.<slug>_enabled else None)` next to the `subway = (...)`
          / `bus = (...)` / `citibike = (...)` blocks, then thread it
          into the `_build_registry(...)` call AND add the matching
          keyword param to the `_build_registry` signature.
       b. Tool import + registration: import the factory at the top
          (`from .tools.<slug> import make_<slug>_tools`) and add the
          `for fn in make_<slug>_tools(<slug>): registry.register(fn)`
          loop inside `_build_registry`, near the existing
          `make_subway_tools` / `make_bus_tools` calls.
       c. The `transit_configured` boolean: add
          `or bool(<slug> and <slug>.enabled)` to the
          `transit_configured = (...)` expression in `run()` so the
          system-prompt transit nudge stays off whenever ANY transit
          mode is live.
  8. Install migration: add the new provider's env keys to the
     `keys=(...)` array in `migrate_transit_config` (in
     `deploy/install.sh`). This list duplicates
     `transit.all_env_keys()` — the wizard already learns the same
     keys from Python, but `install.sh` reads from a bash literal
     because it runs before Python is available. Drift here is benign
     (operator-edited values stay in `jasper.env` instead of migrating
     to `transit.env`), but worth keeping in sync.

Items 1+2 are the pure-data part the `REGISTRY` abstraction buys you.
Items 3-7 are bespoke (UI cards + the per-provider tool/client surface
+ the voice-daemon wiring) and there's no clean way to fold them into
deeper abstractions without baking provider kind everywhere — for v1
the explicit per-provider cost beats the abstraction tax. Just know
it's ~9-12 edits, not three.
"""
from __future__ import annotations

from .base import (
    BoundingBox,
    CredentialSpec,
    ProviderKind,
    Stop,
    TransitError,
    TransitProvider,
    haversine_miles,
)
from .providers import citibike, nyc_bus, nyc_subway


# Display order on the wizard. Subway first because most users will
# use it AND it's keyless (no friction); bus second because it
# requires the user to go register externally first; Citi Bike last
# because it's the newest and the niche-est (not everyone bikes).
# Keyless GBFS so it could in principle slot above bus, but reordering
# would shuffle the visual layout for existing users — keep new
# additions at the bottom.
REGISTRY: tuple[TransitProvider, ...] = (
    nyc_subway.PROVIDER,
    nyc_bus.PROVIDER,
    citibike.PROVIDER,
)


def by_id(provider_id: str) -> TransitProvider | None:
    """Lookup a registered provider by short id ("nyc_subway")."""
    for p in REGISTRY:
        if p.id == provider_id:
            return p
    return None


def covering(lat: float, lon: float) -> tuple[TransitProvider, ...]:
    """Providers whose bounding box includes (lat, lon).

    Cheap, IO-free first-pass filter. The wizard runs this immediately
    after geocoding to decide which provider cards to render; providers
    that DON'T cover get muted to a "no support for your area" notice.
    """
    return tuple(p for p in REGISTRY if p.bbox.includes(lat, lon))


def all_env_keys() -> tuple[str, ...]:
    """Every env variable owned by any registered provider, in stable
    order. Used by the wizard (to know which keys it writes) and by
    install.sh's migration (to know which keys to move from operator
    env to wizard env)."""
    seen: list[str] = []
    for p in REGISTRY:
        for k in p.env_keys:
            if k not in seen:
                seen.append(k)
    return tuple(seen)


__all__ = [
    "BoundingBox",
    "CredentialSpec",
    "ProviderKind",
    "REGISTRY",
    "Stop",
    "TransitError",
    "TransitProvider",
    "all_env_keys",
    "by_id",
    "covering",
    "haversine_miles",
]
