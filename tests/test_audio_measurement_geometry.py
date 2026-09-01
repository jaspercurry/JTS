# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator-declared rig geometry: first-bounce timing, validation, persistence.

Issue #3502: the measured reflection finder in
:mod:`jasper.audio_measurement.gating` structurally never fires on this rig
class, so ``entanglement_floor_hz`` needs a declared-geometry source. These
tests pin the geometry math against an independently-derived worked case
(never the module's own formula fed back to itself), the ceiling-family
participation rule, field-level validation bounds, and the JSON round trip.
"""
from __future__ import annotations

import json
import math

import pytest

from jasper.audio_measurement.gating import TRUSTED_FLOOR_MULTIPLIER
from jasper.audio_measurement.measurement_geometry import (
    MAX_DISTANCE_M,
    MAX_HEIGHT_M,
    MIN_DISTANCE_M,
    MIN_HEIGHT_M,
    PROVENANCE,
    SPEED_OF_SOUND_M_S,
    DeclaredGeometry,
)


def test_first_bounce_and_entanglement_floor_match_an_independently_derived_case():
    # h_s = h_m = 0.84 m, d = 1.0 m: direct path is exactly the distance (the
    # heights cancel), and the floor-bounce path is the mirror-image geometry
    # sqrt(d^2 + (h_s+h_m)^2). Computed here from the raw geometry, not by
    # calling the method under test on itself.
    geometry = DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0)
    direct_m = math.hypot(1.0, 0.84 - 0.84)
    floor_path_m = math.hypot(1.0, 0.84 + 0.84)
    expected_t_s = (floor_path_m - direct_m) / SPEED_OF_SOUND_M_S

    assert direct_m == pytest.approx(1.0)
    assert floor_path_m == pytest.approx(1.9550967, abs=1e-6)
    assert geometry.first_bounce_s() == pytest.approx(expected_t_s, rel=1e-12)
    assert geometry.first_bounce_s() == pytest.approx(2.7846e-3, abs=1e-6)
    assert geometry.entanglement_floor_hz() == pytest.approx(
        TRUSTED_FLOOR_MULTIPLIER / expected_t_s, rel=1e-12
    )
    assert geometry.entanglement_floor_hz() == pytest.approx(897.7, abs=0.5)


def test_ceiling_family_is_absent_from_the_minimum_when_not_declared():
    geometry = DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0)
    direct_m = math.hypot(1.0, 0.0)
    floor_path_m = math.hypot(1.0, 1.68)
    assert geometry.first_bounce_s() == pytest.approx(
        (floor_path_m - direct_m) / SPEED_OF_SOUND_M_S, rel=1e-12
    )


def test_a_high_ceiling_does_not_change_the_floor_bounce_result():
    without_ceiling = DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0)
    with_high_ceiling = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0, ceiling_height_m=3.0,
    )
    assert with_high_ceiling.first_bounce_s() == pytest.approx(
        without_ceiling.first_bounce_s(), rel=1e-12
    )


def test_a_low_ceiling_wins_the_minimum_over_the_floor():
    without_ceiling = DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0)
    with_low_ceiling = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0, ceiling_height_m=0.85,
    )
    ceiling_path_m = math.hypot(1.0, (0.85 - 0.84) + (0.85 - 0.84))
    direct_m = 1.0
    assert with_low_ceiling.first_bounce_s() == pytest.approx(
        (ceiling_path_m - direct_m) / SPEED_OF_SOUND_M_S, rel=1e-12
    )
    assert with_low_ceiling.first_bounce_s() < without_ceiling.first_bounce_s()


@pytest.mark.parametrize(
    "kwargs, bad_field",
    [
        pytest.param(
            {"speaker_height_m": MIN_HEIGHT_M - 0.01, "mic_height_m": 0.84, "distance_m": 1.0},
            "speaker_height_m",
            id="speaker_height_below_min",
        ),
        pytest.param(
            {"speaker_height_m": MAX_HEIGHT_M + 0.01, "mic_height_m": 0.84, "distance_m": 1.0},
            "speaker_height_m",
            id="speaker_height_above_max",
        ),
        pytest.param(
            {"speaker_height_m": 0.84, "mic_height_m": MIN_HEIGHT_M - 0.01, "distance_m": 1.0},
            "mic_height_m",
            id="mic_height_below_min",
        ),
        pytest.param(
            {"speaker_height_m": 0.84, "mic_height_m": MAX_HEIGHT_M + 0.01, "distance_m": 1.0},
            "mic_height_m",
            id="mic_height_above_max",
        ),
        pytest.param(
            {"speaker_height_m": 0.84, "mic_height_m": 0.84, "distance_m": MIN_DISTANCE_M - 0.01},
            "distance_m",
            id="distance_below_min",
        ),
        pytest.param(
            {"speaker_height_m": 0.84, "mic_height_m": 0.84, "distance_m": MAX_DISTANCE_M + 0.01},
            "distance_m",
            id="distance_above_max",
        ),
        pytest.param(
            {"speaker_height_m": 0.84, "mic_height_m": 0.84, "distance_m": -1.0},
            "distance_m",
            id="distance_negative",
        ),
    ],
)
def test_out_of_range_fields_are_refused_and_named(kwargs, bad_field):
    with pytest.raises(ValueError, match=bad_field):
        DeclaredGeometry(**kwargs)


@pytest.mark.parametrize(
    "ceiling_height_m",
    [
        pytest.param(0.84, id="equal_to_both_heights"),
        pytest.param(0.5, id="below_both_heights"),
    ],
)
def test_a_ceiling_not_above_both_heights_is_refused(ceiling_height_m):
    with pytest.raises(ValueError, match="ceiling_height_m"):
        DeclaredGeometry(
            speaker_height_m=0.84,
            mic_height_m=0.84,
            distance_m=1.0,
            ceiling_height_m=ceiling_height_m,
        )


def test_save_load_round_trip_including_provenance(tmp_path):
    path = tmp_path / "measurement_geometry.json"
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2, ceiling_height_m=2.4,
    )
    geometry.save(path)

    loaded = DeclaredGeometry.load(path)
    assert loaded == geometry

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["source"] == PROVENANCE
    assert raw["source"] == "declared_geometry"


def test_save_load_round_trip_without_ceiling(tmp_path):
    path = tmp_path / "measurement_geometry.json"
    geometry = DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0)
    geometry.save(path)

    loaded = DeclaredGeometry.load(path)
    assert loaded == geometry
    assert loaded.ceiling_height_m is None


def test_load_of_a_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        DeclaredGeometry.load(tmp_path / "absent.json")
