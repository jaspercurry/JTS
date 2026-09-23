# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.location_state import CoordinateError, SavedLocation, parse_manual_coordinates, round_coord


def test_round_coord_three_decimals():
    assert round_coord(40.646292) == 40.646
    assert round_coord(-73.994324) == -73.994


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        ("", "", None),
        (" \t", "\n", None),
        (" 40.646292 ", " -73.994324 ", SavedLocation(40.646, -73.994, "Manual: 40.646, -73.994")),
        ("-90", "180", SavedLocation(-90, 180, "Manual: -90.000, 180.000")),
        ("90.0004", "-180.0004", SavedLocation(90, -180, "Manual: 90.000, -180.000")),
        ("0", "0", SavedLocation(0, 0, "Manual: 0.000, 0.000")),
    ],
)
def test_parse_manual_coordinates(lat, lon, expected):
    assert parse_manual_coordinates(lat, lon) == expected


@pytest.mark.parametrize(
    ("lat", "lon"),
    [("40", " "), ("", "10"), ("bad", "0"), ("0", "bad"),
     ("90.001", "0"), ("-90.001", "0"), ("0", "180.001"), ("0", "-180.001"),
     ("nan", "0"), ("0", "nan"), ("inf", "0"), ("0", "-inf")],
)
def test_parse_manual_coordinates_rejects_invalid_input(lat, lon):
    with pytest.raises(CoordinateError):
        parse_manual_coordinates(lat, lon)
