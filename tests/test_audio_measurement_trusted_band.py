# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.trusted_band import trusted_band

#: Speaker and microphone 1 m up: the floor bounce is 5.0 ms late at 0.3 m.
ROOM = DeclaredGeometry(speaker_height_m=1.0, mic_height_m=1.0, distance_m=1.0)
#: 1.4 m up, the bounce is 7.3 ms late at 0.3 m: past the 7 ms default search.
HIGH_ROOM = DeclaredGeometry(speaker_height_m=1.4, mic_height_m=1.4, distance_m=1.0)
JTS3 = (114.0, 25.0)


@pytest.mark.parametrize("distance_m,driver,gated,diameters_mm,room,edges,sources,undeclared", [
    (0.03, "woofer", False, (114.0,), None, (None, 957.7), (None, "near_field_limit"), ()),
    (1.0, "", True, JTS3, None, (357.1, 52785.5), ("gate_floor", "far_field_ceiling"), ("room_undeclared",)),
    (0.3, "", True, JTS3, ROOM, (497.9, 15835.6), ("gate_floor", "far_field_ceiling"), ()),
    (0.3, "", True, JTS3, HIGH_ROOM, (340.8, 15835.6), ("gate_floor", "far_field_ceiling"), ()),
    (0.5, "woofer", True, (114.0,), ROOM, (549.1, 26392.7), ("gate_floor", "far_field_ceiling"), ()),
    (0.015, "tweeter", False, (None,), None, (None, None), (None, None), ("driver_size_undeclared",)),
    (None, "", False, JTS3, None, (None, None), (None, None), ()),
], ids=["driver-30mm", "mark-no-room", "close-in-room", "room-past-the-default", "driver-past-near-field",
        "cone-undeclared", "seat"])
def test_a_reading_states_its_band_from_its_pose_drivers_and_room(
    distance_m, driver, gated, diameters_mm, room, edges, sources, undeclared,
):
    """Gated: from the gate floor of the declared room's first bounce at the
    pose's distance, as far as the gate searches (#3665 item 10); an undeclared
    room gives the 7 ms search bound and says so. At a driver within 100 mm: up
    to where the cone's ka reaches 1. Elsewhere: up to the largest played
    cone's far-field ceiling at the pose's distance. An undeclared cone gives
    no ceiling and says so; a seat states no distance and no ceiling (ADR-0366)."""
    band = trusted_band(distance_m=distance_m, driver=driver, gated=gated, diameters_mm=diameters_mm, room=room)
    assert [band.low_hz, band.high_hz] == pytest.approx(list(edges), abs=0.05)
    assert (band.low_source, band.high_source, band.undeclared) == (*sources, undeclared)
