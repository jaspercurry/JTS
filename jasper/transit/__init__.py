"""Modular transit-provider registry, grouped into toggleable city packs.

A "transit provider" is one source of stop + arrival data — NYC Subway,
NYC Bus, Citi Bike today; future Berlin BVG, London TfL, etc. Each
provider lives in its own module under `jasper.transit.providers.` and is
*self-contained*: it owns both its setup-wizard surface and its live voice
runtime, declaring everything `jasper.transit.base.TransitProvider`
requires:

  - discovery (wizard): `bbox`, `find_stops_near(lat, lon)`,
    `validate_credentials(credentials)`
  - runtime (voice daemon): `build_client(env)` → a client or None when
    its config is unset (the provider parses its OWN env keys, so a new
    provider needs no `jasper/config.py` edit), and `make_tools(client)` →
    the LLM tools

Providers are grouped into `CityPack`s — one household-facing on/off per
city (`JASPER_TRANSIT_CITIES`, wizard-owned). The flat `REGISTRY` is
DERIVED from `CITY_PACKS`, so the two never drift. The voice daemon calls
`active_transit(env)` once: it walks the household's ENABLED packs, builds
each provider's client, and collects tools — the daemon has ZERO
per-provider knowledge. The wizard at `/transit/` iterates `REGISTRY` (or a
pack) to discover which providers cover a user's geocoded coords.

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
     in that array; `migrate_transit_config` moves AND seeds it in its own
     dedicated step.)

Items 1+2 are the pure-data part the `REGISTRY`/`CityPack` abstraction
buys you (and items 1+2 are ALL the daemon needs — no `voice_daemon.py`
edit). Items 3-6 are bespoke (UI cards + the per-provider tool/client
surface): there's no clean way to fold them into deeper abstractions
without baking provider kind everywhere, so the explicit per-provider
cost beats the abstraction tax.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

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

logger = logging.getLogger(__name__)


# The household's enabled city packs, comma-separated pack ids. Wizard-owned
# (written by /transit/); the daemon reads it via enabled_pack_ids. Lives in
# exactly one place so the wizard, the daemon, and install.sh agree on the key.
TRANSIT_CITIES_ENV = "JASPER_TRANSIT_CITIES"


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
def _derive_registry(packs: tuple[CityPack, ...]) -> tuple[TransitProvider, ...]:
    """Flatten packs → providers, rejecting duplicate provider ids.

    Provider id is the lookup key for by_id(), the wizard's card dispatch, the
    install-migration, and the gating reverse-lookup — a duplicate would
    silently shadow (first-wins) and misroute every one of those. Fail LOUD at
    import time on a developer mistake, mirroring the DAC registry's
    dedupe guard, rather than shipping a silent shadow."""
    seen: set[str] = set()
    out: list[TransitProvider] = []
    for pack in packs:
        for provider in pack.providers:
            if provider.id in seen:
                raise ValueError(
                    f"duplicate transit provider id {provider.id!r}: provider "
                    "ids must be unique across all city packs (they key by_id, "
                    "the wizard card dispatch, install migration, and gating)."
                )
            seen.add(provider.id)
            out.append(provider)
    return tuple(out)


REGISTRY: tuple[TransitProvider, ...] = _derive_registry(CITY_PACKS)


def pack_for_provider(provider_id: str) -> CityPack | None:
    """The city pack a provider belongs to ("nyc_subway" -> NYC_PACK).

    Reverse of the pack→providers containment. The /transit/ wizard uses it
    to gate a provider's card on its pack being enabled — a configured
    provider in a disabled city shouldn't render an active card, since its
    tools won't register at runtime."""
    for pack in CITY_PACKS:
        if any(p.id == provider_id for p in pack.providers):
            return pack
    return None


def enabled_pack_ids(env: Mapping[str, str]) -> tuple[str, ...]:
    """City packs the household has turned on, from JASPER_TRANSIT_CITIES
    (comma-separated pack ids, written by the /transit/ wizard).

    Absent vs present is the load-bearing distinction:

      - **Key absent** (None) → ALL packs. An install predating the toggle
        keeps its transit untouched. This is the non-breaking default; each
        provider is still independently gated by its own config downstream
        (a pack being "on" only makes its providers *eligible*; an
        unconfigured provider produces no tool).
      - **Key present** → exactly the listed packs, even if the value is
        empty. An empty/whitespace value therefore means "no cities" — the
        household explicitly turned everything off in the wizard (uncheck
        all). Without this, unchecking all cities would round-trip through
        an empty string and silently re-enable everything.

    Unknown ids are ignored so a removed pack can't strand the setting."""
    raw = env.get(TRANSIT_CITIES_ENV)
    if raw is None:  # key absent entirely → legacy "all packs" default
        return tuple(pack.id for pack in CITY_PACKS)
    wanted = {tok.strip() for tok in raw.split(",") if tok.strip()}
    return tuple(pack.id for pack in CITY_PACKS if pack.id in wanted)


def enabled_packs(env: Mapping[str, str]) -> tuple[CityPack, ...]:
    """The enabled CityPacks, in registry order (see enabled_pack_ids)."""
    ids = set(enabled_pack_ids(env))
    return tuple(pack for pack in CITY_PACKS if pack.id in ids)


@dataclass(frozen=True)
class ActiveTransit:
    """The live transit surface for the household's enabled city packs —
    what `active_transit` hands the voice daemon.

    A *managed result*: it owns the built clients and closes them via
    `aclose()`, so the daemon treats transit as one subsystem with a
    lifecycle instead of reaching into individual clients. That keeps the
    cleanup contract here, in the transit layer — the daemon never learns
    which clients hold a pool.
    """

    tools: list  # flat list to register on the tool registry
    configured: bool  # True iff ≥1 transit tool actually registered
    clients: list  # built clients, owned for lifecycle — close via aclose()

    async def aclose(self) -> None:
        """Close every built client that owns a resource (today only
        `BusClient`'s long-lived `httpx.AsyncClient` pool). Duck-typed on
        `aclose`, so per-call clients (subway, Citi Bike) are skipped and a
        future pooled provider is reclaimed for free — no daemon edit."""
        for client in self.clients:
            aclose = getattr(client, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "transit client %s aclose failed during shutdown",
                    type(client).__name__,
                )


def active_transit(env: Mapping[str, str]) -> ActiveTransit:
    """Build clients + collect voice tools for every provider in the
    household's enabled city packs — the voice daemon's single entry point.

    Takes only the env MAPPING (the daemon passes `os.environ`): each provider
    parses its OWN keys in `build_client(env)`, so this never needs the typed
    `Config` and adding a provider/city needs no `jasper/config.py` edit.

    A pack being enabled only makes its providers *eligible*; each provider
    still produces a client (and tools) only when its own config is set
    (its `build_client` returns None otherwise). Returns an `ActiveTransit`:

      - `.tools`: flat list to register on the tool registry.
      - `.configured`: True iff at least one transit TOOL actually
        registered. A built client that yields no tools (e.g. a bus mode
        whose stops were cleared — the tool factory short-circuits to `[]`)
        is NOT "configured", matching the old per-tool gating that drives
        the system-prompt transit nudge.
      - `.aclose()`: closes any built client that owns a resource (e.g.
        `BusClient`'s `httpx.AsyncClient` pool) on shutdown.

    Adding a city needs no edit here — it flows entirely from the new
    provider's own `build_client`/`make_tools`, so the daemon never grows a
    per-provider branch, cleanup included (a provider that owns a pool just
    grows an `aclose` method and is closed for free)."""
    tools: list = []
    clients: list = []
    for pack in enabled_packs(env):
        for provider in pack.providers:
            # Build each provider independently and behind a guard. The voice
            # daemon calls this at startup BEFORE its main try/except, and
            # make_tools() lazily imports each provider's tool factory — so a
            # broken provider (ImportError in a tool module, a client
            # constructor that raises) would otherwise propagate out of run()
            # and crash the WHOLE daemon, taking weather/timers/smart-home down
            # with it. Instead it degrades to "no tools for this provider",
            # mirroring the HA/Google tool factories returning [] on failure.
            pid = getattr(provider, "id", type(provider).__name__)
            try:
                client = provider.build_client(env)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "transit provider %s build_client failed; skipping it", pid,
                )
                continue
            if client is None:
                continue
            # Track the client for cleanup before make_tools, so a pooled
            # client whose make_tools then raises is still closed on shutdown.
            clients.append(client)
            try:
                tools.extend(provider.make_tools(client))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "transit provider %s make_tools failed; skipping its tools",
                    pid,
                )
    return ActiveTransit(tools=tools, configured=bool(tools), clients=clients)


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
    "ActiveTransit",
    "BoundingBox",
    "CITY_PACKS",
    "CityPack",
    "CredentialSpec",
    "NYC_PACK",
    "ProviderKind",
    "REGISTRY",
    "Stop",
    "TRANSIT_CITIES_ENV",
    "TransitError",
    "TransitProvider",
    "active_transit",
    "all_env_keys",
    "by_id",
    "covering",
    "enabled_pack_ids",
    "enabled_packs",
    "haversine_miles",
    "pack_for_provider",
]
