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

from jasper.audio_measurement.gating import (
    ENTANGLEMENT_SOURCE_DECLARED,
    TRUSTED_FLOOR_MULTIPLIER,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.measurement_geometry import (
    MAX_CEILING_M,
    MAX_DISTANCE_M,
    MAX_HEIGHT_M,
    MIN_DISTANCE_M,
    MIN_HEIGHT_M,
    DeclaredGeometry,
    GeometryFieldError,
    declared_first_bounce_s,
    load_declared_geometry,
)


def test_first_bounce_and_entanglement_floor_match_an_independently_derived_case():
    # h_s = h_m = 0.84 m, d = 1.0 m: direct path is exactly the distance (the
    # heights cancel), and the floor-bounce path is the mirror-image geometry
    # sqrt(d^2 + (h_s+h_m)^2). Computed here from the raw geometry, not by
    # calling the method under test on itself.
    geometry = DeclaredGeometry(speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0)
    direct_m = math.hypot(1.0, 0.84 - 0.84)
    floor_path_m = math.hypot(1.0, 0.84 + 0.84)
    expected_t_s = (floor_path_m - direct_m) / DEFAULT_SOUND_SPEED_M_S

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
        (floor_path_m - direct_m) / DEFAULT_SOUND_SPEED_M_S, rel=1e-12
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
        (ceiling_path_m - direct_m) / DEFAULT_SOUND_SPEED_M_S, rel=1e-12
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
        pytest.param(
            {
                "speaker_height_m": 0.84,
                "mic_height_m": 0.84,
                "distance_m": 1.0,
                "ceiling_height_m": MAX_CEILING_M + 0.01,
            },
            "ceiling_height_m",
            id="ceiling_above_max",
        ),
    ],
)
def test_out_of_range_fields_are_refused_and_named(kwargs, bad_field):
    with pytest.raises(GeometryFieldError) as exc:
        DeclaredGeometry(**kwargs)
    assert exc.value.field == bad_field


@pytest.mark.parametrize(
    "ceiling_height_m",
    [
        pytest.param(0.84, id="equal_to_both_heights"),
        pytest.param(0.5, id="below_both_heights"),
    ],
)
def test_a_ceiling_not_above_both_heights_is_refused(ceiling_height_m):
    with pytest.raises(GeometryFieldError) as exc:
        DeclaredGeometry(
            speaker_height_m=0.84,
            mic_height_m=0.84,
            distance_m=1.0,
            ceiling_height_m=ceiling_height_m,
        )
    assert exc.value.field == "ceiling_height_m"


def test_save_load_round_trip_including_provenance(tmp_path):
    path = tmp_path / "measurement_geometry.json"
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2, ceiling_height_m=2.4,
    )
    geometry.save(path)

    loaded = DeclaredGeometry.load(path)
    assert loaded == geometry

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["source"] == ENTANGLEMENT_SOURCE_DECLARED
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


# --------------------------------------------------------------------------- #
# distance is the CAPTURE's, not the rig's (#3502 owner ruling)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("distance_m", "expected_distance_m"),
    [
        pytest.param(0.3, 0.3, id="capture-closer-than-declared"),
        pytest.param(2.0, 2.0, id="capture-further-than-declared"),
        pytest.param(None, 1.2, id="capture-states-none"),
    ],
)
def test_the_first_bounce_is_timed_at_the_captures_own_distance(
    distance_m, expected_distance_m
):
    """The heights are the rig's and the distance is the capture's.

    Derived from the raw mirror-image geometry at ``expected_distance_m``,
    never by calling the method under test on itself. ``None`` -- and only
    ``None`` -- is a capture that states no distance, and falls back to the
    declared one.
    """
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2,
    )
    direct_m = math.hypot(expected_distance_m, 0.84 - 0.5)
    bounce_m = math.hypot(expected_distance_m, 0.84 + 0.5)
    expected_t_s = (bounce_m - direct_m) / DEFAULT_SOUND_SPEED_M_S

    assert geometry.first_bounce_s(distance_m) == pytest.approx(expected_t_s)
    assert geometry.entanglement_floor_hz(distance_m) == pytest.approx(
        TRUSTED_FLOOR_MULTIPLIER / expected_t_s
    )


@pytest.mark.parametrize(
    "distance_m",
    [
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
    ],
)
def test_a_stated_distance_that_is_not_a_length_is_refused(distance_m):
    """Only ``None`` means "use the declared distance".

    Substituting the rig's distance for a caller that stated one would report
    the rig's floor under the capture's name -- a wrong number nothing
    downstream can tell from a right one.
    """
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2,
    )

    with pytest.raises(GeometryFieldError) as excinfo:
        geometry.first_bounce_s(distance_m)
    assert excinfo.value.field == "distance_m"


def test_evaluating_at_a_distance_does_not_mutate_the_declared_record():
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2,
    )
    declared_t_s = geometry.first_bounce_s()
    geometry.first_bounce_s(0.3)

    assert geometry.distance_m == 1.2
    assert geometry.first_bounce_s() == declared_t_s


def test_a_closer_capture_has_a_lower_room_floor():
    """The physical direction, pinned as an inequality rather than a number.

    Closing in shortens the DIRECT path faster than the mirror-image bounce
    path, so the excess arrival time grows and the floor the room entangles
    below FALLS — which is why a near-field capture buys low-end validity a
    far-field one cannot. A rig-wide floor evaluated once would report the
    same number at every seat.
    """
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.84, distance_m=1.0,
    )

    assert geometry.entanglement_floor_hz(0.3) < geometry.entanglement_floor_hz(1.0)


# --------------------------------------------------------------------------- #
# absent is normal; malformed is a defect
# --------------------------------------------------------------------------- #


def test_an_undeclared_rig_reads_as_none_rather_than_raising(tmp_path):
    assert load_declared_geometry(tmp_path / "absent.json") is None
    assert declared_first_bounce_s(1.0, path=tmp_path / "absent.json") is None


def test_a_declared_rig_reads_back_and_times_its_bounce_at_a_capture(tmp_path):
    path = tmp_path / "measurement_geometry.json"
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2, ceiling_height_m=2.4,
    )
    geometry.save(path)

    assert load_declared_geometry(path) == geometry
    assert declared_first_bounce_s(0.3, path=path) == pytest.approx(
        geometry.first_bounce_s(0.3)
    )
    assert declared_first_bounce_s(path=path) == pytest.approx(
        geometry.first_bounce_s()
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("{not json", id="unparseable"),
        pytest.param('{"speaker_height_m": 0.84}', id="missing-fields"),
        pytest.param(
            '{"speaker_height_m": 99.0, "mic_height_m": 0.5, "distance_m": 1.0}',
            id="out-of-range",
        ),
    ],
)
def test_a_malformed_declaration_raises_rather_than_reading_as_absent(tmp_path, text):
    """A file that exists and does not parse is a defect in the single writer.

    Reading it as "nothing declared" would publish ``unknown`` forever with
    nothing anywhere saying why, which is the one failure this reader must not
    hide.
    """
    path = tmp_path / "measurement_geometry.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises((ValueError, KeyError, TypeError)):
        load_declared_geometry(path)
