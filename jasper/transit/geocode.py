"""Address → lat/lon via OpenStreetMap services.

Nominatim is the primary; Photon (also OSM-backed, Komoot-hosted) is
the fallback for transient Nominatim outages. Both are free, keyless,
and don't require sign-up — important for an open-source project that
doesn't want users registering for a Google Maps key just to configure
transit.

Usage-policy compliance:

- **Nominatim** — max 1 req/sec, single thread, descriptive User-Agent
  required, NO auto-complete. Policy:
  https://operations.osmfoundation.org/policies/nominatim/. Our use
  fits: explicit Submit-button form, one geocode per setup session,
  rate-limited at the module level.
- **Photon** — "please be fair" with no published cap; we hit it only
  when Nominatim returns no match or errors.

Privacy: addresses are sent to OSM/Komoot servers during a geocode
call but never persisted. Only the resulting coordinates land on disk
(rounded to 3 decimals ~ 110 m by callers). The wizard discloses this
inline next to the address field.

Threading: the rate limiter is process-wide via a lock. The result
cache is in-memory only, lifecycle-bound to the wizard process which
idle-exits after 10 min (see jasper/web/_systemd.py) — so memory
pressure is naturally capped.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


# Nominatim policy: "Make sure you are using a User-Agent or HTTP-Referer
# that identifies your application." The repo URL lets OSMF reach the
# project if usage ever becomes a problem.
USER_AGENT = (
    "JTS-Speaker/1.0 (https://github.com/jaspercurry/JTS) "
    "open-source smart-speaker setup"
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api/"
HTTP_TIMEOUT = 6.0

# 1 req/sec is the documented Nominatim ceiling. We enforce this
# process-wide so concurrent requests (multiple browser tabs open
# on the wizard) serialise rather than burst.
_RATE_LIMIT_SEC = 1.0

# Three-decimal precision is ~110 m. Plenty for "what's the nearest
# subway station?" — and intentionally coarser than the user's house
# so the persisted coords don't pin them to an address.
COORD_PRECISION_DECIMALS = 3


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    display_name: str
    source: str  # "nominatim" | "photon" | "cache"


class GeocodeError(Exception):
    """Both geocoders failed or returned no match. Caller surfaces the
    message to the user; transient causes (rate-limit, timeout) are
    rolled into a single user-facing failure mode by design — the
    fix is the same either way ("try again, or enter coords manually")."""


_rate_lock = threading.Lock()
_last_call_mono = 0.0
_cache: dict[str, GeocodeResult] = {}


def _throttle() -> None:
    """Sleep just enough that successive Nominatim calls are ≥ 1 s apart.

    Held under `_rate_lock` across the sleep on purpose: Nominatim's
    policy is "single thread", not "1 req/sec per thread". With two
    concurrent geocode requests, the second must wait for the first
    to complete + the rate window — releasing the lock around sleep
    would let both fire simultaneously."""
    global _last_call_mono
    with _rate_lock:
        elapsed = time.monotonic() - _last_call_mono
        wait = _RATE_LIMIT_SEC - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_call_mono = time.monotonic()


def _normalise(query: str) -> str:
    """Cache key: lowercase, collapse whitespace. Two slightly different
    free-form spellings should hit the same cache entry."""
    return " ".join(query.lower().split())


def round_coord(value: float) -> float:
    """Round to `COORD_PRECISION_DECIMALS`. Use this before persisting
    coords to env files so we don't store sub-house-level precision."""
    return round(value, COORD_PRECISION_DECIMALS)


def geocode(query: str, *, http: httpx.Client | None = None) -> GeocodeResult:
    """Geocode `query` to coordinates. Raises GeocodeError on full failure.

    Try order: cache → Nominatim → Photon. The cache is shared across
    requests in the same process, so the wizard's "redirect-after-save"
    round-trip doesn't burn rate budget."""
    query = query.strip()
    if not query:
        raise GeocodeError("address is empty")
    key = _normalise(query)
    cached = _cache.get(key)
    if cached is not None:
        return GeocodeResult(
            lat=cached.lat,
            lon=cached.lon,
            display_name=cached.display_name,
            source="cache",
        )

    owns_client = http is None
    client = http or httpx.Client(
        timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT},
    )
    try:
        try:
            result = _nominatim(query, client)
        except GeocodeError as nominatim_err:
            logger.info(
                "nominatim miss for %r (%s); falling back to photon",
                query, nominatim_err,
            )
            try:
                result = _photon(query, client)
            except GeocodeError as photon_err:
                # Compose both failures so the user sees what happened.
                # Chain to photon_err explicitly so the daemon log
                # surfaces the photon traceback; nominatim_err is
                # captured in the message text.
                raise GeocodeError(
                    f"couldn't find that address. "
                    f"(nominatim: {nominatim_err}; photon: {photon_err})"
                ) from photon_err
    finally:
        if owns_client:
            client.close()

    _cache[key] = result
    return result


def _nominatim(query: str, client: httpx.Client) -> GeocodeResult:
    _throttle()
    try:
        r = client.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "json",
                "limit": 1,
                "addressdetails": 0,
            },
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise GeocodeError(f"nominatim request failed: {e}") from e
    if not data:
        raise GeocodeError("no match")
    hit = data[0]
    try:
        return GeocodeResult(
            lat=float(hit["lat"]),
            lon=float(hit["lon"]),
            display_name=str(hit.get("display_name") or query),
            source="nominatim",
        )
    except (KeyError, TypeError, ValueError) as e:
        raise GeocodeError(f"nominatim returned malformed result: {e}") from e


def _photon(query: str, client: httpx.Client) -> GeocodeResult:
    try:
        r = client.get(PHOTON_URL, params={"q": query, "limit": 1})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise GeocodeError(f"photon request failed: {e}") from e
    features = data.get("features") or []
    if not features:
        raise GeocodeError("no match")
    feat = features[0]
    coords = feat.get("geometry", {}).get("coordinates") or []
    # Photon uses GeoJSON convention: [lon, lat].
    if len(coords) != 2:
        raise GeocodeError("photon returned malformed coordinates")
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError) as e:
        raise GeocodeError("photon returned non-numeric coordinates") from e
    props = feat.get("properties") or {}
    name_parts = [
        str(props.get(k))
        for k in ("name", "street", "city", "state", "country")
        if props.get(k)
    ]
    name = ", ".join(name_parts) or query
    return GeocodeResult(lat=lat, lon=lon, display_name=name, source="photon")


def _reset_cache_for_tests() -> None:
    """Test-only helper. Clears the module-level cache so a test can
    drive cache-miss paths deterministically."""
    _cache.clear()
    global _last_call_mono
    _last_call_mono = 0.0
