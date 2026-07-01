# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Google Routes API client for destination ETA / directions voice tools."""
from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from . import location_state
from .log_event import log_event
from .tools import fence_untrusted

logger = logging.getLogger(__name__)

GOOGLE_ROUTES_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_ROUTES_SECRET_FILE = "/var/lib/jasper-secrets/google_routes.env"
GOOGLE_ROUTES_API_KEY_ENV = "GOOGLE_ROUTES_API_KEY"
TRAVEL_DEFAULT_MODE_ENV = "JASPER_TRAVEL_DEFAULT_MODE"
DEFAULT_TRAVEL_MODE = "transit"
GOOGLE_ROUTES_TIMEOUT_SEC = 8.0

TRAVEL_MODE_TO_API = {
    "transit": "TRANSIT",
    "drive": "DRIVE",
    "walk": "WALK",
    "bicycle": "BICYCLE",
}

_MODE_ALIASES = {
    "": "",
    "transit": "transit",
    "public transit": "transit",
    "train": "transit",
    "subway": "transit",
    "bus": "transit",
    "drive": "drive",
    "driving": "drive",
    "car": "drive",
    "walk": "walk",
    "walking": "walk",
    "bike": "bicycle",
    "biking": "bicycle",
    "bicycle": "bicycle",
    "cycling": "bicycle",
}

# Keep the field mask intentionally small; the voice tool returns an overview,
# not a full turn-by-turn navigation payload.
GOOGLE_ROUTES_FIELD_MASK = ",".join((
    "routes.duration",
    "routes.distanceMeters",
    "routes.legs.duration",
    "routes.legs.distanceMeters",
    "routes.legs.steps.distanceMeters",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.navigationInstruction",
    "routes.legs.steps.transitDetails",
))


class _AsyncPoster(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
        timeout: float,
    ) -> Any:
        ...


@dataclass(frozen=True)
class GoogleRoutesConfig:
    api_key: str
    origin: location_state.SavedLocation
    default_mode: str = DEFAULT_TRAVEL_MODE
    setup_url: str = "jts.local/transit"


@dataclass(frozen=True)
class GoogleRoutesConfigStatus:
    api_key_present: bool
    origin_present: bool
    default_mode: str
    default_mode_valid: bool
    setup_url: str

    @property
    def configured(self) -> bool:
        return self.api_key_present and self.origin_present


def _setup_url(env: Mapping[str, str]) -> str:
    hostname = (env.get("JASPER_HOSTNAME") or "jts.local").strip() or "jts.local"
    return f"{hostname}/transit"


def normalize_travel_mode(value: str) -> str | None:
    """Return the canonical travel mode, "" for "use default", or None."""
    key = " ".join((value or "").strip().lower().replace("_", " ").split())
    return _MODE_ALIASES.get(key)


def default_travel_mode(env: Mapping[str, str]) -> tuple[str, bool]:
    raw = (env.get(TRAVEL_DEFAULT_MODE_ENV) or "").strip()
    if not raw:
        return DEFAULT_TRAVEL_MODE, True
    mode = normalize_travel_mode(raw)
    if mode in TRAVEL_MODE_TO_API:
        return mode, True
    return DEFAULT_TRAVEL_MODE, False


def config_status(env: Mapping[str, str] | None = None) -> GoogleRoutesConfigStatus:
    source = os.environ if env is None else env
    origin = location_state.parse_transit_location(dict(source))
    mode, valid = default_travel_mode(source)
    return GoogleRoutesConfigStatus(
        api_key_present=bool((source.get(GOOGLE_ROUTES_API_KEY_ENV) or "").strip()),
        origin_present=origin is not None,
        default_mode=mode,
        default_mode_valid=valid,
        setup_url=_setup_url(source),
    )


def build_google_routes_client(
    env: Mapping[str, str] | None = None,
    *,
    http: _AsyncPoster | None = None,
) -> "GoogleRoutesClient | None":
    source = os.environ if env is None else env
    api_key = (source.get(GOOGLE_ROUTES_API_KEY_ENV) or "").strip()
    origin = location_state.parse_transit_location(dict(source))
    if not api_key or origin is None:
        return None
    mode, _valid = default_travel_mode(source)
    return GoogleRoutesClient(
        GoogleRoutesConfig(
            api_key=api_key,
            origin=origin,
            default_mode=mode,
            setup_url=_setup_url(source),
        ),
        http=http,
    )


def _parse_duration_seconds(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("s"):
        text = text[:-1]
    try:
        return max(0, int(float(text)))
    except ValueError:
        return None


def _minutes(seconds: int | None) -> int | None:
    if seconds is None:
        return None
    if seconds <= 0:
        return 0
    return max(1, int(math.ceil(seconds / 60.0)))


def _fence(value: Any) -> str:
    return fence_untrusted("" if value is None else str(value), source="google routes")


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("text")
    return "" if value is None else str(value).strip()


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text_value(value)
        if text:
            return text
    return ""


def _distance_meters(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _step_seconds(step: Mapping[str, Any]) -> int | None:
    return _parse_duration_seconds(
        step.get("staticDuration") or step.get("duration"),
    )


def _step_type(api_mode: str) -> str:
    return {
        "WALK": "walk",
        "TRANSIT": "transit",
        "DRIVE": "drive",
        "BICYCLE": "bicycle",
        "TWO_WHEELER": "two_wheeler",
    }.get(api_mode, api_mode.lower() if api_mode else "unknown")


def _transit_step(step: Mapping[str, Any]) -> dict[str, Any]:
    details = step.get("transitDetails") or {}
    stop_details = details.get("stopDetails") or {}
    departure_stop = stop_details.get("departureStop") or {}
    arrival_stop = stop_details.get("arrivalStop") or {}
    line = details.get("transitLine") or {}
    vehicle = line.get("vehicle") or {}
    seconds = _step_seconds(step)
    line_name = _first_text(
        line.get("nameShort"),
        line.get("shortName"),
        line.get("name"),
    )
    out: dict[str, Any] = {
        "type": "transit",
        "duration_minutes": _minutes(seconds),
        "distance_meters": _distance_meters(step.get("distanceMeters")),
    }
    if line_name:
        out["line"] = _fence(line_name)
    vehicle_name = _first_text(vehicle.get("name"), vehicle.get("type"))
    if vehicle_name:
        out["vehicle"] = _fence(vehicle_name)
    headsign = _first_text(details.get("headsign"))
    if headsign:
        out["headsign"] = _fence(headsign)
    from_stop = _first_text(departure_stop.get("name"))
    if from_stop:
        out["from_stop"] = _fence(from_stop)
    to_stop = _first_text(arrival_stop.get("name"))
    if to_stop:
        out["to_stop"] = _fence(to_stop)
    if details.get("stopCount") is not None:
        out["stop_count"] = details.get("stopCount")
    trip = _first_text(details.get("tripShortText"))
    if trip:
        out["trip"] = _fence(trip)
    out["summary"] = _transit_summary(line_name, headsign, from_stop, to_stop, out)
    return out


def _transit_summary(
    line_name: str,
    headsign: str,
    from_stop: str,
    to_stop: str,
    step: Mapping[str, Any],
) -> str:
    bits = ["take"]
    if line_name:
        bits.append(_fence(line_name))
    else:
        bits.append("transit")
    if headsign:
        bits.append("toward")
        bits.append(_fence(headsign))
    if from_stop:
        bits.append("from")
        bits.append(_fence(from_stop))
    if to_stop:
        bits.append("to")
        bits.append(_fence(to_stop))
    stop_count = step.get("stop_count")
    if stop_count is not None:
        bits.append(f"({stop_count} stops)")
    return " ".join(str(b) for b in bits)


def _plain_step(step: Mapping[str, Any], api_mode: str) -> dict[str, Any]:
    nav = step.get("navigationInstruction") or {}
    instruction = _first_text(nav.get("instructions"))
    seconds = _step_seconds(step)
    out: dict[str, Any] = {
        "type": _step_type(api_mode),
        "duration_minutes": _minutes(seconds),
        "distance_meters": _distance_meters(step.get("distanceMeters")),
    }
    if instruction:
        fenced = _fence(instruction)
        out["instruction"] = fenced
        out["summary"] = fenced
    else:
        out["summary"] = out["type"]
    return out


def _summarize_steps(legs: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for leg in legs:
        for raw_step in leg.get("steps") or []:
            if not isinstance(raw_step, Mapping):
                continue
            api_mode = str(raw_step.get("travelMode") or "").strip()
            if api_mode == "TRANSIT":
                steps.append(_transit_step(raw_step))
            else:
                steps.append(_plain_step(raw_step, api_mode))
    if len(steps) <= 8:
        return steps
    return [
        *steps[:7],
        {
            "type": "more_steps",
            "summary": f"{len(steps) - 7} additional steps omitted",
        },
    ]


def _route_duration_seconds(route: Mapping[str, Any]) -> int | None:
    seconds = _parse_duration_seconds(route.get("duration"))
    if seconds is not None:
        return seconds
    total = 0
    found = False
    for leg in route.get("legs") or []:
        if not isinstance(leg, Mapping):
            continue
        leg_seconds = _parse_duration_seconds(leg.get("duration"))
        if leg_seconds is None:
            continue
        total += leg_seconds
        found = True
    return total if found else None


def _route_distance_meters(route: Mapping[str, Any]) -> int | None:
    distance = _distance_meters(route.get("distanceMeters"))
    if distance is not None:
        return distance
    total = 0
    found = False
    for leg in route.get("legs") or []:
        if not isinstance(leg, Mapping):
            continue
        leg_distance = _distance_meters(leg.get("distanceMeters"))
        if leg_distance is None:
            continue
        total += leg_distance
        found = True
    return total if found else None


def _normalize_response(
    data: Mapping[str, Any],
    *,
    mode: str,
    used_default_mode: bool,
    destination: str,
    route_limit: int,
    origin: location_state.SavedLocation,
) -> dict[str, Any]:
    raw_routes = data.get("routes") or []
    if not raw_routes:
        return {
            "ok": False,
            "error": (
                f"Couldn't find a route to {destination} from the saved "
                "speaker location."
            ),
        }
    routes: list[dict[str, Any]] = []
    for i, route in enumerate(raw_routes[:route_limit], start=1):
        if not isinstance(route, Mapping):
            continue
        legs = [leg for leg in route.get("legs") or [] if isinstance(leg, Mapping)]
        duration_seconds = _route_duration_seconds(route)
        steps = _summarize_steps(legs)
        routes.append({
            "rank": i,
            "duration_minutes": _minutes(duration_seconds),
            "duration_seconds": duration_seconds,
            "distance_meters": _route_distance_meters(route),
            "steps": steps,
        })
    if not routes:
        return {
            "ok": False,
            "error": (
                f"Google Routes returned an unreadable route to {destination}. "
                "Try a more specific destination."
            ),
        }
    warnings: list[str] = []
    if mode in {"walk", "bicycle"}:
        warnings.append(
            "Walking and bicycling directions can be incomplete. Use judgment.",
        )
    return {
        "ok": True,
        "mode": mode,
        "used_default_mode": used_default_mode,
        "origin": {
            "lat": origin.lat,
            "lon": origin.lon,
            "label": _fence(origin.display_name),
        },
        "destination_query": destination,
        "routes": routes,
        "warnings": warnings,
    }


class GoogleRoutesClient:
    def __init__(
        self,
        config: GoogleRoutesConfig,
        *,
        http: _AsyncPoster | None = None,
        timeout_sec: float = GOOGLE_ROUTES_TIMEOUT_SEC,
    ) -> None:
        self.config = config
        self._http = http
        self._timeout_sec = timeout_sec

    async def get_travel_routes(
        self,
        *,
        destination: str,
        travel_mode: str = "",
        max_routes: int = 1,
    ) -> dict[str, Any]:
        destination = (destination or "").strip()
        if not destination:
            return {"ok": False, "error": "Tell me the destination to route to."}

        requested = normalize_travel_mode(travel_mode)
        if requested is None:
            return {
                "ok": False,
                "error": (
                    "Travel mode must be transit, drive, walk, or bicycle."
                ),
            }
        used_default_mode = requested == ""
        mode = self.config.default_mode if used_default_mode else requested
        try:
            route_limit = int(max_routes or 1)
        except (TypeError, ValueError):
            route_limit = 1
        route_limit = max(1, min(2, route_limit))
        payload = self._request_payload(destination, mode, route_limit)
        try:
            data = await self._post(payload)
        except httpx.TimeoutException:
            return {
                "ok": False,
                "error": "Google Routes timed out. Try again in a moment.",
            }
        except httpx.HTTPError:
            logger.warning("google routes transport error", exc_info=True)
            return {
                "ok": False,
                "error": "Couldn't reach Google Routes just now. Try again in a moment.",
            }
        except GoogleRoutesAPIError as e:
            return {"ok": False, "error": e.user_message}

        result = _normalize_response(
            data,
            mode=mode,
            used_default_mode=used_default_mode,
            destination=destination,
            route_limit=route_limit,
            origin=self.config.origin,
        )
        log_event(
            logger,
            "google_routes.tool_result",
            ok=result.get("ok"),
            mode=mode,
            route_count=len(result.get("routes") or []),
        )
        return result

    def _request_payload(
        self,
        destination: str,
        mode: str,
        route_limit: int,
    ) -> dict[str, Any]:
        return {
            "origin": {
                "location": {
                    "latLng": {
                        "latitude": self.config.origin.lat,
                        "longitude": self.config.origin.lon,
                    },
                },
            },
            "destination": {"address": destination},
            "travelMode": TRAVEL_MODE_TO_API[mode],
            "computeAlternativeRoutes": route_limit > 1,
            "languageCode": "en-US",
            "regionCode": "US",
        }

    async def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.config.api_key,
            "X-Goog-FieldMask": GOOGLE_ROUTES_FIELD_MASK,
        }
        if self._http is not None:
            response = await self._http.post(
                GOOGLE_ROUTES_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=self._timeout_sec,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                response = await client.post(
                    GOOGLE_ROUTES_ENDPOINT,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout_sec,
                )
        return _response_json(response, setup_url=self.config.setup_url)


class GoogleRoutesAPIError(RuntimeError):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _response_json(response: Any, *, setup_url: str) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    status_code = int(getattr(response, "status_code", 200))
    if status_code >= 400:
        if status_code in {401, 403}:
            raise GoogleRoutesAPIError(
                "Google Routes rejected the API key. Visit "
                f"{setup_url} and check that the key is enabled for the "
                "Routes API.",
            )
        if status_code == 429:
            raise GoogleRoutesAPIError(
                "Google Routes is rate-limited or out of quota right now.",
            )
        raise GoogleRoutesAPIError(
            "Google Routes couldn't calculate that route right now.",
        )
    try:
        data = response.json()
    except ValueError as e:
        raise GoogleRoutesAPIError(
            "Google Routes returned an unreadable response.",
        ) from e
    if not isinstance(data, Mapping):
        raise GoogleRoutesAPIError(
            "Google Routes returned an unreadable response.",
        )
    return data
