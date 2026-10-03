# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator-declared rig geometry: first-bounce timing, validation, persistence.

Issue #3502: the measured reflection finder in
:mod:`jasper.audio_measurement.gating` structurally never fires on this rig
class, so ``entanglement_floor_hz`` needs a declared-geometry source. These
tests pin the geometry math against independently-derived worked cases
(never the module's own formula fed back to itself), which declared surface's
bounce comes first, field-level validation bounds, and the JSON round trip.
"""
from __future__ import annotations

import cmath
import json
import math

import pytest

from jasper.audio_measurement.gating import (
    ENTANGLEMENT_SOURCE_DECLARED,
    TRUSTED_FLOOR_MULTIPLIER,
)
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.measurement_geometry import (
    BOUNDARY_PRIOR_NULL_FLOOR_DB,
    MAX_CEILING_M,
    MAX_DISTANCE_M,
    MAX_HEIGHT_M,
    MAX_WALL_M,
    MIN_DISTANCE_M,
    MIN_HEIGHT_M,
    DeclaredGeometry,
    GeometryFieldError,
    boundary_prior,
    load_declared_geometry,
)


_FLOOR_IMAGE = (0.0, 0.0, -0.9)
_CABINET = {"cabinet_back_wall_m": 0.2, "cabinet_depth_m": 0.3}


@pytest.mark.parametrize("placement, image_m, floor_hz", [
    pytest.param({}, _FLOOR_IMAGE, 750.8, id="nothing_declared_floor"),
    pytest.param({"cabinet_back_wall_m": 0.2}, _FLOOR_IMAGE, 750.8, id="back_gap_alone_floor"),
    pytest.param({"ceiling_height_m": 3.0}, _FLOOR_IMAGE, 750.8, id="high_ceiling_floor"),
    pytest.param({"ceiling_height_m": 1.1}, (0.0, 0.0, 1.3), 21962.9, id="low_ceiling"),
    pytest.param({**_CABINET, "toe_in_degrees": 0}, (0.0, -1.0, 0.9), 859.6, id="near_back_wall"),
    pytest.param({**_CABINET, "toe_in_degrees": 30},
                 (0.0, -2 * (0.2 + 0.3 * math.cos(math.radians(30))), 0.9), 1006.4, id="toed_in_back_wall"),
    pytest.param({**_CABINET, "cabinet_back_wall_m": 1.0, "toe_in_degrees": 0}, _FLOOR_IMAGE, 750.8,
                 id="far_back_wall_floor"),
    pytest.param({"side_wall_m": 0.5}, (-1.0, 0.0, 0.9), 2077.5, id="near_side_wall"),
    pytest.param({"side_wall_m": 1.4}, _FLOOR_IMAGE, 750.8, id="far_side_wall_floor"),
])
def test_the_earliest_declared_bounce_sets_the_floor(placement, image_m, floor_hz):
    """jts3's heights (speaker 0.9 m, microphone 1.0 m) at 1 m; ADR-0427.

    Points in metres: the front-panel centre above the origin, ``y`` out along
    the wall normal, ``z`` up. ``image_m`` is the speaker mirrored in the surface
    whose bounce comes first. The microphone is on the speaker's axis, turned by
    the declared toe-in. Path lengths come from the points, not from the module's
    formula.
    """
    toe = math.radians(placement.get("toe_in_degrees", 0.0))
    speaker, mic = (0.0, 0.0, 0.9), (math.sin(toe), math.cos(toe), 1.0)
    expected_s = (math.dist(image_m, mic) - math.dist(speaker, mic)) / DEFAULT_SOUND_SPEED_M_S
    geometry = DeclaredGeometry(speaker_height_m=0.9, mic_height_m=1.0, distance_m=1.0, **placement)

    assert geometry.first_bounce_s() == pytest.approx(expected_s, rel=1e-12)
    assert geometry.entanglement_floor_hz() == pytest.approx(TRUSTED_FLOOR_MULTIPLIER / expected_s, rel=1e-12)
    assert geometry.entanglement_floor_hz() == pytest.approx(floor_hz, abs=0.1)


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
        pytest.param(
            {
                "speaker_height_m": 0.84,
                "mic_height_m": 0.84,
                "distance_m": 1.0,
                "side_wall_m": MAX_WALL_M + 0.01,
            },
            "side_wall_m",
            id="side_wall_above_max",
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


_ROOM = {"speaker_height_m": 0.9, "mic_height_m": 1.0, "distance_m": 1.05}


@pytest.mark.parametrize(
    "override, refused_field",
    [
        pytest.param({}, "", id="no_ceiling"),
        pytest.param({"ceiling_height_m": 2.4}, "", id="with_ceiling"),
        pytest.param({"speaker_height_m": None}, "speaker_height_m", id="missing"),
        pytest.param({"distance_m": "tall"}, "distance_m", id="not_a_number"),
        pytest.param({"distance_m": float("nan")}, "distance_m", id="nan"),
        pytest.param({"ceiling_height_m": float("inf")}, "ceiling_height_m", id="inf"),
        pytest.param({"distance_m": 10**400}, "distance_m", id="huge_int"),
        pytest.param({"side_wall_m": 1.4}, "", id="with_side_wall"),
        pytest.param({"side_wall_m": 0.0}, "side_wall_m", id="wall_zero_is_not_absent"),
        pytest.param({"cabinet_back_wall_m": 0.2032}, "", id="back_gap_only"),
        pytest.param({"cabinet_back_wall_m": 0.2, "cabinet_depth_m": 0.3, "toe_in_degrees": 0}, "", id="cabinet"),
        pytest.param({"cabinet_back_wall_m": True}, "cabinet_back_wall_m", id="bool_gap"),
        pytest.param({"cabinet_back_wall_m": -0.001}, "cabinet_back_wall_m", id="negative_gap"),
        pytest.param({"cabinet_back_wall_m": float("nan")}, "cabinet_back_wall_m", id="nan_gap"),
        pytest.param({"cabinet_back_wall_m": float("inf")}, "cabinet_back_wall_m", id="infinite_gap"),
        pytest.param({"cabinet_depth_m": 0}, "cabinet_depth_m", id="zero_depth"),
        pytest.param({"cabinet_depth_m": float("inf")}, "cabinet_depth_m", id="infinite_depth"),
        pytest.param({"toe_in_degrees": float("nan")}, "toe_in_degrees", id="nan_angle"),
        pytest.param({"toe_in_degrees": 91}, "toe_in_degrees", id="facing_wall"),
    ],
)
def test_the_dict_round_trip_is_exact_and_refuses_what_is_not_a_length(
    override, refused_field,
):
    """``to_dict``/``from_dict`` are the ONE banked shape (#3498).

    ``save``, the angle-capture spool and the session's declared-geometry
    artifact all carry this pair, so an undeclared ceiling is ABSENT rather
    than null, and a field that is not a usable number of metres -- missing,
    a string, NaN, infinite -- is refused BY NAME wherever it arrives.
    """
    room = {**_ROOM, **override}
    if refused_field:
        with pytest.raises(GeometryFieldError) as exc:
            DeclaredGeometry.from_dict(room)
        assert exc.value.field == refused_field
        return

    assert DeclaredGeometry.from_dict(room).to_dict() == room


@pytest.mark.parametrize("placement,walls,reason", [
    ({"cabinet_back_wall_m": 0, "cabinet_depth_m": 0.3, "toe_in_degrees": 0}, {"front": 0.3}, ""),
    ({"cabinet_back_wall_m": 0.0254, "cabinet_depth_m": 0.3, "toe_in_degrees": 0}, {"front": 0.3254}, ""),
    ({"cabinet_back_wall_m": 0.2}, {}, "front_baffle_geometry_undeclared"),
    ({"cabinet_back_wall_m": 0.2, "cabinet_depth_m": 0.3}, {}, "front_baffle_geometry_undeclared"),
    ({"cabinet_back_wall_m": 0.2, "toe_in_degrees": 0}, {}, "front_baffle_geometry_undeclared"),
    ({"cabinet_back_wall_m": 0.2, "side_wall_m": 1.4}, {"side": 1.4}, "front_baffle_geometry_undeclared"),
    ({"cabinet_back_wall_m": 0.2, "cabinet_depth_m": 0.3, "toe_in_degrees": 0}, {"front": 0.5}, ""),
    ({"cabinet_back_wall_m": 0.2, "cabinet_depth_m": 0.3, "toe_in_degrees": 60}, {"front": 0.35}, ""),
    ({"cabinet_back_wall_m": 0.2, "cabinet_depth_m": 0.3, "toe_in_degrees": -60}, {"front": 0.35}, ""),
    ({"cabinet_back_wall_m": 10, "cabinet_depth_m": 0.3, "toe_in_degrees": 0}, {"front": 10.3}, ""),
])
def test_boundary_distances_keep_their_reference_and_disclose_missing_geometry(placement, walls, reason):
    geometry = DeclaredGeometry(**_ROOM, **placement)
    before = geometry.to_dict()
    actual, actual_reason = geometry.boundary_walls()
    assert actual == pytest.approx(walls)
    assert actual_reason == reason
    prior = boundary_prior([100], walls=actual)
    for wall, distance in walls.items():
        assert prior["walls"][wall]["f_null_hz"] == pytest.approx(DEFAULT_SOUND_SPEED_M_S / (4 * distance))
    assert geometry.to_dict() == before


def test_save_load_round_trip_including_provenance(tmp_path):
    path = tmp_path / "measurement_geometry.json"
    geometry = DeclaredGeometry(
        speaker_height_m=0.84, mic_height_m=0.5, distance_m=1.2, ceiling_height_m=2.4, side_wall_m=1.4,
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
    assert loaded.side_wall_m is None


def test_the_boundary_prior_states_the_image_source_sum_for_a_declared_wall():
    """A 0.85 m front wall, against the image source itself.

    The expected level is the two-source sum ``|1 + exp(-j 2 pi f 2d/c)|``,
    evaluated here in complex arithmetic rather than through the closed form
    the module reduces it to, so agreeing is evidence rather than tautology.
    """
    distance_m, speed = 0.85, DEFAULT_SOUND_SPEED_M_S
    grid_hz = [20.0, speed / (4.0 * distance_m), speed / (2.0 * distance_m)]

    prior = boundary_prior(grid_hz, walls={"front": distance_m})

    front = prior["walls"]["front"]
    assert front["distance_m"] == pytest.approx(distance_m)
    assert front["f_null_hz"] == pytest.approx(100.9, abs=0.1)
    # Half the +6 dB rise, in dB, is where 2|cos| = sqrt(2): half the null.
    assert front["f_half_gain_hz"] == pytest.approx(front["f_null_hz"] / 2.0)
    assert prior["sound_speed_m_s"] == pytest.approx(speed)
    assert prior["sound_speed_source"] == "default"
    assert prior["freqs_hz"] == pytest.approx(grid_hz)

    at_20_hz_db = 20.0 * math.log10(
        abs(1.0 + cmath.exp(-2j * math.pi * 20.0 * 2.0 * distance_m / speed))
    )
    assert prior["prior_db"][0] == pytest.approx(at_20_hz_db, abs=1e-9)
    assert prior["prior_db"][0] == pytest.approx(5.59, abs=0.01)
    # Already within half a dB of the +6.02 dB 2-pi asymptote at 20 Hz.
    assert prior["prior_db"][0] > 6.0206 - 0.5
    # The quarter-wave null is clamped; the half-wave sum is +6 dB again.
    assert prior["prior_db"][1] == BOUNDARY_PRIOR_NULL_FLOOR_DB
    assert prior["prior_db"][2] == pytest.approx(6.0206, abs=1e-3)


def test_wall_curves_add_in_db_and_no_wall_is_no_curve():
    """Adding the per-wall dB curves multiplies their magnitudes -- the corner
    image-source sum; no wall is no claim, not a flat one."""
    grid_hz = [30.0, 60.0, 120.0]
    front = boundary_prior(grid_hz, walls={"front": 0.85})
    side = boundary_prior(grid_hz, walls={"side": 1.4})

    both = boundary_prior(grid_hz, walls={"front": 0.85, "side": 1.4})
    assert both["prior_db"] == pytest.approx(
        [f + s for f, s in zip(front["prior_db"], side["prior_db"])]
    )

    none = boundary_prior(grid_hz, walls={})
    assert none["walls"] == {}
    assert none["freqs_hz"] == []
    assert none["prior_db"] == []


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


def test_a_stored_declaration_with_front_wall_m_refuses_by_that_field(tmp_path):
    """ADR-0388: the retired field refuses, keeping the file's values its fix declares again."""
    path = tmp_path / "measurement_geometry.json"
    kept = {**_ROOM, "ceiling_height_m": 2.4, "side_wall_m": 1.4}
    path.write_text(json.dumps({**kept, "front_wall_m": 0.85}), encoding="utf-8")

    with pytest.raises(GeometryFieldError) as exc:
        load_declared_geometry(path)
    assert (exc.value.field, exc.value.declared) == ("front_wall_m", kept)


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
