"""NYC MTA Bus transit provider — requires a BusTime API key.

Uses three BusTime endpoints:

1. `/api/where/stops-for-location.json` — nearest stops to a lat/lon.
   The wizard calls this when the user has both coords and a key.
2. `/api/where/agencies-with-coverage.json` — credential probe.
   Cheap call (no parameters except `key=`); used to test a
   freshly-pasted key before persisting it.
3. `/api/siri/stop-monitoring.json` — per-stop live arrivals; used
   here purely to **enumerate the routes actually serving each
   candidate stop** during the wizard's nearest-stops render. The
   OBA `routes` field on `stops-for-location` is GTFS-static-derived
   and lags real-world dispatch (per the BusTime wiki, OBA at MTA
   explicitly excludes real-time data). SIRI is the ground truth.
   See the v2 design review for the prior art behind this choice.

The SIRI runtime client at `jasper/bus.py` hits the same endpoint
for live arrivals. Same key is shared per the BusTime wiki.

The provider is stateless. The wizard supplies the credential at call
time so an unsaved key can be tested without disk IO.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from ..base import BoundingBox, CredentialSpec, Stop, TransitError, haversine_miles, scrub_secrets

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)


BUSTIME_BASE = "https://bustime-classic.mta.info/api/where"
BUSTIME_SIRI_URL = (
    "https://bustime-classic.mta.info/api/siri/stop-monitoring.json"
)
HTTP_TIMEOUT = 4.0

# Same metro bbox as the subway provider.
NYC_BBOX = BoundingBox(
    lat_min=40.49, lat_max=40.92,
    lon_min=-74.26, lon_max=-73.69,
)

# A 0.01° span is ~1 km at NYC latitudes. Dense enough to surface
# plenty of candidates in Manhattan, narrow enough to keep the
# response under ~30 KB. BusTime caps the response anyway and we
# take the closest N.
DEFAULT_LAT_SPAN = 0.01
DEFAULT_LON_SPAN = 0.01

CREDENTIAL = CredentialSpec(
    env_key="JASPER_MTA_BUSTIME_KEY",
    label="MTA BusTime API key",
    help_url="https://register.developer.obanyc.com/",
    placeholder="paste your key after approval (~30 min)",
)


class _NycBus:
    id = "nyc_bus"
    label = "NYC Bus"
    kind = "bus"
    help_url = "https://bustime.mta.info/wiki/Developers/Index"
    bbox = NYC_BBOX
    env_keys = (
        "JASPER_MTA_BUSTIME_KEY",
        # Multi-stop list. Value is "id|label,id|label" — see
        # `jasper.bus.parse_bus_stops` for the parser. Opposing-
        # direction stops at one intersection are saved as two
        # separate entries (they have distinct MTA stop IDs).
        "JASPER_BUS_STOPS",
    )
    credentials = (CREDENTIAL,)

    def __init__(self, http: httpx.Client | None = None) -> None:
        # `http` is a TEST-ONLY injection point — pass an
        # httpx.MockTransport-wired Client to drive the request flow
        # offline. Production callers leave it None; the methods then
        # build a short-lived client per call so resources don't leak
        # across the 10-min wizard idle window. Don't reuse this seam
        # for a long-lived production client without re-thinking the
        # resource lifecycle.
        self._http = http

    def _client(self) -> tuple[httpx.Client, bool]:
        """Return (client, owns) where owns=True means the caller must
        close it. Lets the method body stay simple — one cleanup pattern
        regardless of whether http was injected at construction."""
        if self._http is not None:
            return self._http, False
        return httpx.Client(timeout=HTTP_TIMEOUT), True

    def find_stops_near(
        self,
        lat: float,
        lon: float,
        *,
        credentials: dict[str, str] | None = None,
        count: int = 5,
    ) -> list[Stop]:
        key = (credentials or {}).get(CREDENTIAL.env_key, "").strip()
        if not key:
            # The wizard locks the bus card until a key is entered, so
            # this path is defensive — but if a future caller forgets,
            # the failure is loud and the message is actionable.
            raise TransitError("MTA BusTime API key required")
        params = {
            "key": key,
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            "latSpan": DEFAULT_LAT_SPAN,
            "lonSpan": DEFAULT_LON_SPAN,
        }
        c, owns = self._client()
        try:
            r = c.get(
                f"{BUSTIME_BASE}/stops-for-location.json", params=params,
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            # `e` may stringify with the full URL including ?key=…; scrub
            # before surfacing — this message lands in the wizard's error
            # banner where any LAN viewer would see it.
            raise TransitError(f"BusTime request failed: {scrub_secrets(e)}")
        except ValueError as e:
            raise TransitError(f"BusTime returned non-JSON: {scrub_secrets(e)}")
        finally:
            if owns:
                c.close()

        # OBA envelope: {code, data: {stops, references: {routes}}}.
        body = (data or {}).get("data") or {}
        if isinstance(body, list):
            # Defensive — the documented shape is dict but older
            # OBA deployments returned a bare list.
            body = {"stops": body, "references": {}}
        raw_stops = body.get("stops") or []
        route_map = {
            r.get("id"): r.get("shortName") or r.get("longName") or r.get("id")
            for r in (body.get("references", {}).get("routes") or [])
        }

        # (distance, stop_id, name, lat, lon, route_short_names, direction)
        ranked: list[tuple[float, str, str, float, float, list[str], str]] = []
        for s in raw_stops:
            sid = str(s.get("id") or "").strip()
            if not sid:
                # OBA has historically always populated `id`, but
                # being defensive costs us nothing and a missing id
                # would otherwise propagate as a Stop with empty
                # stop_id — the daemon would then look up routes by
                # empty string forever.
                continue
            try:
                slat = float(s["lat"])
                slon = float(s["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            d = haversine_miles(lat, lon, slat, slon)
            # OBA-flavoured shapes vary: `routeIds` is a list of string
            # IDs we look up in route_map; `routes` (the shape MTA's
            # BusTime actually returns) is a list of dicts with their
            # own shortName field already attached. Handle both — the
            # dict path was broken in the first cut of this provider
            # ("unhashable type: dict" when route_map.get was passed a
            # dict as the key, surfaced live in production).
            route_entries = s.get("routeIds") or s.get("routes") or []
            short_names: list[str] = []
            for r in route_entries:
                if isinstance(r, str):
                    short_names.append(str(route_map.get(r) or r).strip())
                elif isinstance(r, dict):
                    short_names.append(str(
                        r.get("shortName") or r.get("longName") or r.get("id") or ""
                    ).strip())
                # Anything else (None, int) silently skipped — defensive.
            short_names = [n for n in short_names if n]
            name = str(s.get("name") or sid)
            direction_hint = str(s.get("direction") or "").strip()
            ranked.append((d, sid, name, slat, slon, short_names, direction_hint))
        ranked.sort(key=lambda t: t[0])

        results: list[Stop] = []
        for d, sid, name, slat, slon, short_names, direction_hint in ranked[:count]:
            # Display: "Name (direction) — Routes". Each section
            # omitted if empty so single-route stops stay concise.
            display = name
            if direction_hint:
                display += f" ({direction_hint})"
            if short_names:
                display += f" — {'/'.join(short_names)}"
            results.append(Stop(
                stop_id=sid,
                display_name=display,
                lat=slat, lon=slon,
                distance_mi=d,
                lines=tuple(short_names),
                direction_hint=direction_hint,
                name=name,
            ))
        return results

    def enumerate_live_routes(
        self,
        stop_id: str,
        *,
        credentials: dict[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Probe SIRI for the routes currently dispatching at a stop.

        Returns a deduplicated tuple of distinct `PublishedLineName`
        values observed in the last few `MonitoredStopVisit` entries.
        On any failure returns an empty tuple — the caller (wizard)
        falls back to the OBA `routes` field for display when this
        comes up empty (quiet stop, off-peak hours, network blip).

        Why this exists: MTA's OBA `stops-for-location` returns only
        GTFS-static-scheduled routes per stop, which lags real-world
        dispatch. A stop the user sees serving B35 + B70 in person
        may show as B35-only in OBA. SIRI reflects what's actually
        coming. See the v2 design review for the citation chain.
        """
        key = (credentials or {}).get(CREDENTIAL.env_key, "").strip()
        if not key or not stop_id:
            return ()
        bare = stop_id.removeprefix("MTA_").strip()
        params = {
            "key": key,
            "MonitoringRef": bare,
            "OperatorRef": "MTA",
        }
        c, owns = self._client()
        try:
            r = c.get(BUSTIME_SIRI_URL, params=params)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.info(
                "event=transit.bus.siri_probe.error stop=%s err=%s",
                bare, scrub_secrets(e),
            )
            return ()
        finally:
            if owns:
                c.close()

        sd = (
            (data or {}).get("Siri", {})
            .get("ServiceDelivery", {})
            .get("StopMonitoringDelivery") or [{}]
        )[0]
        visits = sd.get("MonitoredStopVisit") or []
        seen: list[str] = []  # preserve first-seen order for determinism
        for v in visits:
            journey = v.get("MonitoredVehicleJourney") or {}
            name = str(journey.get("PublishedLineName") or "").strip()
            if name and name not in seen:
                seen.append(name)
        return tuple(seen)

    def validate_credentials(
        self, credentials: dict[str, str],
    ) -> dict[str, str] | None:
        value = (credentials.get(CREDENTIAL.env_key) or "").strip()
        unknown_keys = set(credentials) - {CREDENTIAL.env_key}
        if unknown_keys:
            raise NotImplementedError(
                f"nyc_bus only owns {CREDENTIAL.env_key!r}; "
                f"got unknown {sorted(unknown_keys)}"
            )
        if not value:
            return {CREDENTIAL.env_key: "key is empty"}

        c, owns = self._client()
        try:
            r = c.get(
                f"{BUSTIME_BASE}/agencies-with-coverage.json",
                params={"key": value},
            )
        except httpx.HTTPError as e:
            scrubbed = scrub_secrets(e)
            logger.warning(
                "event=transit.bus.validate.error err=%s", scrubbed,
            )
            return {CREDENTIAL.env_key: f"BusTime unreachable: {scrubbed}"}
        finally:
            if owns:
                c.close()
        # Non-200 = transient infra issue or explicit auth reject.
        # Either way the user can't proceed; we report the same
        # message and let them re-try.
        if r.status_code != 200:
            logger.info(
                "event=transit.bus.validate.rejected status=%d", r.status_code,
            )
            return {CREDENTIAL.env_key: f"BusTime returned HTTP {r.status_code}"}
        try:
            data = r.json()
        except ValueError:
            return {CREDENTIAL.env_key: "BusTime returned non-JSON response"}
        # OBA wraps responses with a numeric code; 200 = ok.
        if data.get("code") != 200:
            return {CREDENTIAL.env_key: "BusTime rejected the key"}
        return None

    def build_client(self, cfg: Config) -> object | None:
        if not cfg.bus_enabled:
            return None
        from ...bus import BusClient  # lazy: keep the wizard light

        # cfg.bus_stops is (stop_id, label) pairs: ids drive the SIRI
        # fan-out; labels name the stop in the voice answer.
        return BusClient(
            stop_ids=[sid for sid, _ in cfg.bus_stops],
            api_key=cfg.mta_bustime_key,
            stop_labels={sid: label for sid, label in cfg.bus_stops if label},
        )

    def make_tools(self, client: object):
        from ...tools.bus import make_bus_tools  # lazy

        return make_bus_tools(client)


PROVIDER = _NycBus()
