"""Shared types for the transit-provider registry.

The wizard at `/transit/` and the provider modules under
`jasper.transit.providers.` all agree on these shapes. New providers
implement the `TransitProvider` Protocol (structural — no inheritance
required) and add themselves to `jasper.transit.REGISTRY`.

Design intent: keep this file as small as the contract. Anything
provider-specific (CSV parsers, MTA API quirks) belongs in the
provider module; anything wizard-specific belongs in
`jasper.web.transit_setup`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


# Provider kind drives wizard UI grouping. Open set — add new values
# when introducing a new mode (e.g. "tram", "metro", "cable_car").
ProviderKind = Literal["subway", "bus", "rail", "ferry", "bike", "metro"]


@dataclass(frozen=True)
class BoundingBox:
    """Lat/lon rectangle. Used as a cheap first-pass coverage check
    before doing the real (network) stop-lookup. Rectangles are
    coarse — a user just outside the metro area can still match the
    bbox, so the wizard double-checks by dropping providers whose
    nearest stop is unreasonably far."""

    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def includes(self, lat: float, lon: float) -> bool:
        return (
            self.lat_min <= lat <= self.lat_max
            and self.lon_min <= lon <= self.lon_max
        )


@dataclass(frozen=True)
class CredentialSpec:
    """One credential the provider needs to perform live queries.

    The wizard renders one input per spec on the provider's card.
    Providers with empty `credentials` are keyless — no input shown.
    """

    env_key: str         # e.g. "JASPER_MTA_BUSTIME_KEY"
    label: str           # human-readable input label
    help_url: str        # where to obtain one
    placeholder: str = ""


@dataclass(frozen=True)
class Stop:
    """One stop returned by `find_stops_near`. Provider-agnostic shape
    so the wizard can render any provider's results the same way.

    `lines` is what the user picks AT this stop. **This field is
    overloaded across providers and the overload is undocumented at the
    call sites — read this before adding a provider.** Two distinct
    meanings live in the same tuple:

      - Route-id list (nyc_subway, nyc_bus): one route identifier per
        element — subway lines (`("D", "N", "R")`) or bus route
        short-names (`("B70", "B35")`). The bus wizard card treats it
        as exactly that, unioning it with SIRI-probed routes into a
        set of route ids (`_bus_card_html`: `live or s.lines`); the
        subway display formatter joins it as labels.
      - Display string (citibike): a SINGLE element holding a
        human-readable snapshot, not a route id — e.g.
        `("3 classic, 2 e-bikes, 5 docks",)`. `_citibike_card_html`
        joins it straight to text for the picker.

    Empty tuple means "not applicable" or "couldn't determine". A new
    provider should follow whichever convention its card renderer
    expects; if it needs both route ids and a display blurb, add a
    dedicated field rather than overloading this one further.

    `direction_hint` carries provider-specific direction context that's
    too fragmentary for `lines` (e.g. "Northbound" on a bus stop). The
    wizard surfaces it in the display name; the daemon ignores it.
    """

    stop_id: str
    display_name: str
    lat: float
    lon: float
    distance_mi: float
    lines: tuple[str, ...] = field(default_factory=tuple)
    direction_hint: str = ""
    # Raw name field as the provider's source reports it (e.g.
    # "4 AV/39 ST"). Used by the wizard to cluster opposing-direction
    # stops at the same intersection — MTA gives both sides the same
    # name and disambiguates via `direction_hint`. Empty string =
    # provider doesn't expose a name (defaults to `display_name`).
    name: str = ""


class TransitError(Exception):
    """Provider-side failure during a transit query (credential rejected,
    upstream API down, malformed response). The wizard catches these
    and surfaces the message text to the user."""


@runtime_checkable
class TransitProvider(Protocol):
    """Structural type every provider satisfies. Not an ABC — providers
    are plain classes (or even modules with the right attributes) that
    happen to expose these names. `runtime_checkable` so the wizard's
    test suite can `isinstance(p, TransitProvider)` cheaply.

    `id` is the short slug used in URLs and form fields ("nyc_subway").
    `env_keys` enumerates every variable this provider owns — used by
    install.sh's migration to know which keys to move from the operator
    env into the wizard-owned env file.
    """

    id: str
    label: str
    kind: ProviderKind
    help_url: str
    bbox: BoundingBox
    env_keys: tuple[str, ...]
    credentials: tuple[CredentialSpec, ...]

    def find_stops_near(
        self,
        lat: float,
        lon: float,
        *,
        credentials: dict[str, str] | None = None,
        count: int = 5,
    ) -> list[Stop]:
        """Return up to `count` nearest stops, sorted by distance ascending.

        `credentials` carries the user's pasted values keyed by env_key —
        the wizard supplies these mid-flow before persisting, so a
        freshly-typed key can be tested without writing to disk yet.

        Raises `TransitError` for caller-visible failures (bad credentials,
        upstream down). Returns an empty list when there genuinely are no
        stops nearby (rare — usually means the user is outside coverage)."""
        ...

    def validate_credentials(
        self, credentials: dict[str, str],
    ) -> dict[str, str] | None:
        """Probe the provider's API to confirm credentials work.

        Returns `None` on success, or `{env_key: error_message}` for
        every credential the provider rejects. The dict-keyed-by-env_key
        shape is intentional: providers like London TfL need
        `app_id` + `app_key` together — a one-key signature would force
        the wizard to test each in isolation (and TfL doesn't expose
        single-key checks). NYC Bus is single-cred but uses the same
        shape for uniformity.

        Implementations should pick a low-cost endpoint (no parameters
        where possible) and return error messages rather than raising —
        the wizard's UX is "show error and let the user retry"; raising
        kills the redirect path. Unknown env_keys (typos) may raise
        `NotImplementedError` since those are programming errors, not
        user-facing conditions."""
        ...


_EARTH_RADIUS_MI = 3958.8  # WGS84 mean radius


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points.

    Used to sort stops by distance from the user's home coords. The
    spherical-earth approximation is fine at our scale (errors well
    under 0.5% within a metro area)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_MI * math.asin(math.sqrt(a))


# Matches query-string `key=…` (case-insensitive) — the credential-bearing
# parameter convention every BusTime endpoint uses. Anchored on `[?&]` so
# we don't accidentally scrub free-form prose like "the key is foo".
_KEY_QUERY_RE = re.compile(r"(?i)([?&]key=)[^&\s'\"<>]+")


def scrub_secrets(value: object) -> str:
    """Render any value with `key=SECRET` query-string parameters masked.

    `httpx.HTTPError.__str__` includes the full URL on error, which means
    a stray `f"BusTime failed: {e}"` in a log line or user-facing message
    leaks the API key. Pipe every interpolation that might carry a URL
    through this scrubber so the key only ever appears in the env file."""
    return _KEY_QUERY_RE.sub(r"\1***", str(value))
