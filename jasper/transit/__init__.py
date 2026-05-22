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

**Adding a new provider:**

  1. Drop a module under `jasper/transit/providers/` exposing a
     `PROVIDER` instance that satisfies `TransitProvider`.
  2. Append `<module>.PROVIDER` to `REGISTRY` below.
  3. Add a card-renderer branch in `jasper.web.transit_setup._index_html`
     so the wizard knows how to surface the new provider's stops
     picker (subway-style direction radio? bus-style locked-on-key?
     bike-share-style dock picker?). The wizard's fallback renders a
     placeholder card for unknown providers so the page still works
     while a contributor wires this up.
  4. Add a tool factory under `jasper/tools/` to expose the new
     provider's live data to the voice model (mirror
     `jasper/tools/subway.py` or `jasper/tools/bus.py`).
  5. Add the provider's env keys to `install.sh`'s migration
     function so hand-edits to `/etc/jasper/jasper.env` flow into
     `/var/lib/jasper/transit.env`.

Steps 1, 2, and 5 are pure-data. Step 3 is UI dispatch (one branch
in one function). Step 4 is the runtime tool. The discovery layer
(bbox coverage, find-stops-near, credential probe) is fully
data-driven over this REGISTRY.
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
