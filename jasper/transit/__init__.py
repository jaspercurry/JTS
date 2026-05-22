"""Modular transit-provider registry.

A "transit provider" is one source of stop + arrival data — NYC Subway,
NYC Bus today; future Berlin BVG, Citi Bike, London TfL, etc. Each
provider lives in its own module under `jasper.transit.providers.`,
declares its bounding box + credentials, and implements two methods:

  - `find_stops_near(lat, lon)` — used by the setup wizard
  - `validate_credential(env_key, value)` — cheap probe before save

See `jasper.transit.base.TransitProvider` for the contract. The
wizard at `/transit/` iterates this REGISTRY to discover which
providers cover a user's geocoded coordinates.

**Adding a new provider — concretely.** The discovery layer (bbox
coverage, `find_stops_near`, credential probe) is fully data-driven
over `REGISTRY` — drop a provider in and the wizard's geocode flow
picks it up. But the *full* contribution touches a handful of other
files. None of them are sneaky; this list exists so a contributor
isn't surprised half-way through.

  1. Provider module: drop `jasper/transit/providers/<slug>.py`
     exposing a `PROVIDER` instance that satisfies `TransitProvider`
     (id, label, kind, help_url, bbox, env_keys, credentials, plus
     `find_stops_near` and `validate_credentials`). Mirror
     `nyc_subway.py` (keyless) or `nyc_bus.py` (credentialed).
  2. Registry: append `<module>.PROVIDER` to `REGISTRY` below.
  3. Wizard card: add an `elif p.id == "<slug>":` branch in
     `jasper.web.transit_setup._index_html`. Each provider's card is
     bespoke enough (subway has a direction radio, bus has the
     locked-on-key flow, Citi Bike would have a dock-capacity readout)
     that this dispatch is honest. The fallback renders a placeholder
     for unknown providers so the page still works while a contributor
     wires this up.
  4. Voice tool: add a `make_<slug>_tools(client)` factory under
     `jasper/tools/` and register it from `jasper/voice_daemon.py`
     near the existing `make_subway_tools` / `make_bus_tools` calls.
     The tool's docstring is what the LLM reads — match the
     subway/bus docstring shape.
  5. Install migration: add the new provider's env keys to the
     `keys=(...)` array in `migrate_transit_config` (in
     `deploy/install.sh`). This is the one currently-duplicated list
     — the wizard already learns the same keys from
     `transit.all_env_keys()`, but `install.sh` reads from a bash
     literal because it runs before Python is available. Drift here
     is benign (operator-edited values stay in `jasper.env` instead
     of migrating to `transit.env`), but worth keeping in sync.

Steps 1+2 are the pure-data part. Steps 3+4 are bespoke (UI dispatch
and the voice tool surface) and there's no clean way to avoid them
without baking provider kind into deeper abstractions — for v1 the
"3 places to touch" cost beats the abstraction tax.
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
from .providers import nyc_bus, nyc_subway


# Display order on the wizard. Subway first because most users will
# use it AND it's keyless (no friction); bus second because it
# requires the user to go register externally first.
REGISTRY: tuple[TransitProvider, ...] = (
    nyc_subway.PROVIDER,
    nyc_bus.PROVIDER,
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
