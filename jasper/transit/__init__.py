"""Modular transit-provider registry, grouped into toggleable city packs.

A "transit provider" is one source of stop + arrival data — NYC Subway,
NYC Bus, Citi Bike today; future Berlin BVG, London TfL, etc. Each
provider lives in its own module under `jasper.transit.providers.` and is
*self-contained*: it owns both its setup-wizard surface and its live voice
runtime, declaring everything `jasper.transit.base.TransitProvider`
requires:

  - discovery (wizard): `bbox`, `find_stops_near(lat, lon)`,
    `validate_credentials(credentials)`
  - runtime (voice daemon): `build_client(cfg)` → a client or None when
    its config is unset, and `make_tools(client)` → the LLM tools

Providers are grouped into `CityPack`s — one household-facing on/off per
city (`JASPER_TRANSIT_CITIES`, wizard-owned). The flat `REGISTRY` is
DERIVED from `CITY_PACKS`, so the two never drift. The voice daemon calls
`active_transit_tools(env, cfg)` once: it walks the household's ENABLED
packs, builds each provider's client, and collects tools — the daemon has
ZERO per-provider knowledge. The wizard at `/transit/` iterates `REGISTRY`
(or a pack) to discover which providers cover a user's geocoded coords.

**Adding transit — concretely.** Two shapes:
  - a new *mode in an existing city* (e.g. NYC ferry): add a provider to
    that `CityPack`'s `providers` tuple.
  - a new *city* (e.g. Berlin): add one `CityPack` to `CITY_PACKS` plus
    its provider module(s).
Either way `REGISTRY`, the daemon wiring, and the city toggle update for
free — the abstraction's whole point. The remaining edits are genuinely
per-provider (bespoke UI + the live tool/client surface), not daemon
plumbing. Each numbered item is one logical edit point:

  1. Provider module: drop `jasper/transit/providers/<slug>.py`
     exposing a `PROVIDER` instance that satisfies `TransitProvider` —
     discovery surface (id, label, kind, help_url, bbox, env_keys,
     credentials, `find_stops_near`, `validate_credentials`) AND runtime
     surface (`build_client`, `make_tools`, both lazy-importing their
     heavy deps so the socket-activated wizard process stays light).
     Mirror `nyc_subway.py` (keyless) or `nyc_bus.py` (credentialed).
  2. City pack: add the provider to a `CityPack` in `CITY_PACKS` below
     (new pack for a new city; append to an existing pack's `providers`
     for a new mode). `REGISTRY` derives automatically.
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
     under `jasper/tools/<slug>.py` (what `make_tools` lazy-imports).
     The tool's docstring is what the LLM reads — match the
     subway/bus docstring shape.
  6. Runtime client class: the `<Slug>Client` that item 5 wraps and
     `build_client` constructs (mirror `jasper/subway.py`,
     `jasper/bus.py`, `jasper/citibike.py`). Owns the live
     arrival/status fetch. If it holds a connection pool, give it an
     `aclose()` — the daemon closes every built transit client on
     shutdown, duck-typed, so a pool is reclaimed with no daemon edit.
  7. Install migration: add the new provider's env keys to the
     `keys=(...)` array in `migrate_transit_config` (in
     `deploy/install.sh`). This list duplicates
     `transit.all_env_keys()` — the wizard already learns the same
     keys from Python, but `install.sh` reads from a bash literal
     because it runs before Python is available. Drift here is benign
     (operator-edited values stay in `jasper.env` instead of migrating
     to `transit.env`), but worth keeping in sync. (`JASPER_TRANSIT_CITIES`
     itself is a pack-level toggle, not a provider env key, so it is NOT
     in that array; the migration seeds it separately.)

Items 1+2 are the pure-data part the `REGISTRY`/`CityPack` abstraction
buys you (and items 1+2 are ALL the daemon needs — no `voice_daemon.py`
edit). Items 3-6 are bespoke (UI cards + the per-provider tool/client
surface): there's no clean way to fold them into deeper abstractions
without baking provider kind everywhere, so the explicit per-provider
cost beats the abstraction tax.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from ..config import Config


# A "city pack" bundles one city's transit providers behind a single
# household-facing toggle. Adding a city is one CityPack entry here plus
# its provider modules; the flat REGISTRY is DERIVED from the packs so
# the two can never drift. The provider order within a pack is the
# wizard display order (subway first — keyless, lowest friction; bus —
# needs an API key; Citi Bike last — newest/nichest).
@dataclass(frozen=True)
class CityPack:
    """A toggleable bundle of a city's transit providers."""

    id: str  # short slug, e.g. "nyc"
    label: str  # human label, e.g. "New York City"
    providers: tuple[TransitProvider, ...]

    def covers(self, lat: float, lon: float) -> bool:
        """True if any provider in this pack covers (lat, lon). Used by the
        wizard to *suggest* a pack after geocoding; the actual on/off is
        always the household's explicit choice, never auto-applied."""
        return any(p.bbox.includes(lat, lon) for p in self.providers)


NYC_PACK = CityPack(
    id="nyc",
    label="New York City",
    providers=(nyc_subway.PROVIDER, nyc_bus.PROVIDER, citibike.PROVIDER),
)

# Add a new city as one more CityPack. Keep existing packs in place so the
# wizard layout is stable for current households.
CITY_PACKS: tuple[CityPack, ...] = (NYC_PACK,)

# Flat provider list, DERIVED from the packs (single source of truth). The
# discovery layer — covering(), by_id(), all_env_keys(), the wizard cards —
# keeps working over REGISTRY unchanged.
REGISTRY: tuple[TransitProvider, ...] = tuple(
    p for pack in CITY_PACKS for p in pack.providers
)


def pack_by_id(pack_id: str) -> CityPack | None:
    """Look up a city pack by its short id ("nyc")."""
    for pack in CITY_PACKS:
        if pack.id == pack_id:
            return pack
    return None


def enabled_pack_ids(env: Mapping[str, str]) -> tuple[str, ...]:
    """City packs the household has turned on, from JASPER_TRANSIT_CITIES
    (comma-separated pack ids, written by the /transit/ wizard).

    Unset/empty falls back to ALL packs — an install predating the toggle
    keeps its transit, since each provider is still independently gated by
    its own configuration downstream (a pack being "on" only makes its
    providers *eligible*; an unconfigured provider produces no tool). The
    wizard and install.sh migration write the explicit list going forward.
    Unknown ids are ignored so a removed pack can't strand the setting."""
    raw = (env.get("JASPER_TRANSIT_CITIES") or "").strip()
    if not raw:
        return tuple(pack.id for pack in CITY_PACKS)
    wanted = {tok.strip() for tok in raw.split(",") if tok.strip()}
    return tuple(pack.id for pack in CITY_PACKS if pack.id in wanted)


def enabled_packs(env: Mapping[str, str]) -> tuple[CityPack, ...]:
    """The enabled CityPacks, in registry order (see enabled_pack_ids)."""
    ids = set(enabled_pack_ids(env))
    return tuple(pack for pack in CITY_PACKS if pack.id in ids)


def active_transit_tools(
    env: Mapping[str, str], cfg: Config
) -> tuple[list, bool, list]:
    """Build clients + collect voice tools for every provider in the
    household's enabled city packs — the voice daemon's single entry point.

    A pack being enabled only makes its providers *eligible*; each provider
    still produces a client (and tools) only when its own config is set
    (its `build_client` returns None otherwise). Returns
    `(tool_functions, transit_configured, clients)`:

      - `tool_functions`: flat list to register on the tool registry.
      - `transit_configured`: True iff at least one transit TOOL actually
        registered. A built client that yields no tools (e.g. a bus mode
        whose stops were cleared — the tool factory short-circuits to `[]`)
        is NOT "configured", matching the old per-tool gating that drives
        the system-prompt transit nudge.
      - `clients`: the built client objects, so the daemon can close any
        that own a resource (e.g. `BusClient`'s long-lived
        `httpx.AsyncClient` pool) on shutdown. Per-call clients (subway,
        Citi Bike) have nothing to close; the daemon duck-types `aclose`,
        so they're skipped.

    Adding a city needs no edit here — it flows entirely from the new
    provider's own `build_client`/`make_tools`, so the daemon never grows a
    per-provider branch, cleanup included (a provider that owns a pool just
    grows an `aclose` method and is closed for free)."""
    tools: list = []
    clients: list = []
    for pack in enabled_packs(env):
        for provider in pack.providers:
            client = provider.build_client(cfg)
            if client is None:
                continue
            clients.append(client)
            tools.extend(provider.make_tools(client))
    return tools, bool(tools), clients


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
    "CITY_PACKS",
    "CityPack",
    "CredentialSpec",
    "NYC_PACK",
    "ProviderKind",
    "REGISTRY",
    "Stop",
    "TransitError",
    "TransitProvider",
    "active_transit_tools",
    "all_env_keys",
    "by_id",
    "covering",
    "enabled_pack_ids",
    "enabled_packs",
    "haversine_miles",
    "pack_by_id",
]
