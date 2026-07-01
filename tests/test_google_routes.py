# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper import google_routes


ENV = {
    "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic-Test_Key",
    "JASPER_TRANSIT_LAT": "40.758",
    "JASPER_TRANSIT_LON": "-73.985",
    "JASPER_TRANSIT_DISPLAY_NAME": "Times Square",
    "JASPER_HOSTNAME": "jts3.local",
}


class FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, *, headers, json, timeout):
        self.calls.append({
            "url": url,
            "headers": headers,
            "json": json,
            "timeout": timeout,
        })
        return self.response


class FakeResponse:
    def __init__(self, status_code: int, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


TRANSIT_RESPONSE = {
    "routes": [
        {
            "duration": "1220s",
            "distanceMeters": 9400,
            "description": "Fast transit route",
            "legs": [
                {
                    "duration": "1220s",
                    "distanceMeters": 9400,
                    "steps": [
                        {
                            "travelMode": "WALK",
                            "staticDuration": "180s",
                            "distanceMeters": 240,
                            "navigationInstruction": {
                                "instructions": "Walk to 42 St-Bryant Park",
                            },
                        },
                        {
                            "travelMode": "TRANSIT",
                            "staticDuration": "840s",
                            "distanceMeters": 8500,
                            "transitDetails": {
                                "headsign": "Coney Island-Stillwell Av",
                                "stopCount": 3,
                                "transitLine": {
                                    "nameShort": "D",
                                    "vehicle": {
                                        "name": {
                                            "text": "Subway",
                                            "languageCode": "en",
                                        },
                                    },
                                },
                                "stopDetails": {
                                    "departureStop": {"name": "42 St-Bryant Park"},
                                    "arrivalStop": {"name": "47-50 Sts-Rockefeller Ctr"},
                                },
                            },
                        },
                    ],
                },
            ],
        },
        {
            "duration": "1500s",
            "distanceMeters": 9800,
            "legs": [],
        },
    ],
}


def test_build_google_routes_client_requires_key_and_origin():
    assert google_routes.build_google_routes_client({}) is None
    assert google_routes.build_google_routes_client({
        "GOOGLE_ROUTES_API_KEY": "AIzaSySynthetic",
    }) is None
    assert google_routes.build_google_routes_client(ENV) is not None


def test_default_mode_falls_back_to_transit_when_invalid():
    mode, valid = google_routes.default_travel_mode({
        **ENV,
        "JASPER_TRAVEL_DEFAULT_MODE": "hovercraft",
    })
    assert mode == "transit"
    assert valid is False


@pytest.mark.parametrize(
    ("spoken", "mode"),
    [
        ("public transit", "transit"),
        ("train", "transit"),
        ("subway", "transit"),
        ("bus", "transit"),
        ("car", "drive"),
        ("cycling", "bicycle"),
    ],
)
def test_travel_mode_aliases_match_voice_wording(spoken: str, mode: str):
    assert google_routes.normalize_travel_mode(spoken) == mode


@pytest.mark.asyncio
async def test_compute_routes_request_and_transit_normalization():
    http = FakeHTTP(TRANSIT_RESPONSE)
    client = google_routes.build_google_routes_client(
        {**ENV, "JASPER_TRAVEL_DEFAULT_MODE": "transit"},
        http=http,
    )
    assert client is not None

    out = await client.get_travel_routes(
        destination="30 Rock",
        travel_mode="",
        max_routes=2,
    )

    call = http.calls[0]
    assert call["url"] == google_routes.GOOGLE_ROUTES_ENDPOINT
    assert call["headers"]["X-Goog-Api-Key"] == "AIzaSySynthetic-Test_Key"
    assert "routes.duration" in call["headers"]["X-Goog-FieldMask"]
    assert "routes.description" not in call["headers"]["X-Goog-FieldMask"]
    assert call["json"]["origin"]["location"]["latLng"] == {
        "latitude": 40.758,
        "longitude": -73.985,
    }
    assert call["json"]["destination"] == {"address": "30 Rock"}
    assert call["json"]["travelMode"] == "TRANSIT"
    assert call["json"]["computeAlternativeRoutes"] is True

    assert out["ok"] is True
    assert out["mode"] == "transit"
    assert out["used_default_mode"] is True
    assert out["destination_query"] == "30 Rock"
    assert out["origin"]["label"].startswith("[untrusted_external_text")
    assert "Times Square" in out["origin"]["label"]
    assert len(out["routes"]) == 2
    first = out["routes"][0]
    assert first["duration_minutes"] == 21
    assert first["distance_meters"] == 9400
    assert "description" not in first
    assert first["steps"][0]["type"] == "walk"
    assert first["steps"][1]["type"] == "transit"
    assert first["steps"][1]["line"].startswith("[untrusted_external_text")
    assert "D" in first["steps"][1]["line"]
    assert "Subway" in first["steps"][1]["vehicle"]
    assert "42 St-Bryant Park" in first["steps"][1]["from_stop"]


@pytest.mark.asyncio
async def test_explicit_driving_override_wins_over_default():
    http = FakeHTTP({"routes": [{"duration": "600s", "distanceMeters": 1200}]})
    client = google_routes.build_google_routes_client(
        {**ENV, "JASPER_TRAVEL_DEFAULT_MODE": "transit"},
        http=http,
    )
    assert client is not None

    out = await client.get_travel_routes(
        destination="JFK",
        travel_mode="driving",
        max_routes="not-an-int",
    )

    assert http.calls[0]["json"]["travelMode"] == "DRIVE"
    assert http.calls[0]["json"]["computeAlternativeRoutes"] is False
    assert out["ok"] is True
    assert out["mode"] == "drive"
    assert out["used_default_mode"] is False
    assert out["routes"][0]["duration_minutes"] == 10


@pytest.mark.asyncio
async def test_invalid_travel_mode_returns_user_error_without_http_call():
    http = FakeHTTP(TRANSIT_RESPONSE)
    client = google_routes.build_google_routes_client(ENV, http=http)
    assert client is not None

    out = await client.get_travel_routes(destination="30 Rock", travel_mode="fly")

    assert out == {
        "ok": False,
        "error": "Travel mode must be transit, drive, walk, or bicycle.",
    }
    assert http.calls == []


@pytest.mark.asyncio
async def test_google_api_key_rejection_is_user_facing():
    http = FakeHTTP(FakeResponse(403))
    client = google_routes.build_google_routes_client(ENV, http=http)
    assert client is not None

    out = await client.get_travel_routes(destination="30 Rock")

    assert out["ok"] is False
    assert "rejected the API key" in out["error"]
    assert "jts3.local/transit" in out["error"]
