# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-geometry level-match lock storage and the shared ramp config.

:class:`LevelLockStore` is the live per-geometry lock store
(:class:`jasper.web.correction_crossover_backend.CrossoverLevelLease` owns
one); the room-cap and phone-timeout tests below pin the config it and the
correction backend share with the live ramp engine,
:mod:`jasper.active_speaker.seat_level_ramp`.
"""

from __future__ import annotations

from jasper.audio_measurement.level_match import LevelLockStore, MeasurementLevelLock
from jasper.audio_measurement.ramp import MeasurementRamp


def test_room_cap_keeps_attenuated_stimulus_inside_digital_envelope():
    from jasper.audio_measurement.excitation import (
        AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
    )
    from jasper.audio_measurement.ramp import (
        LISTENING_POSITION_CAP_BUMP_DB,
        LISTENING_POSITION_CAP_CEIL_DB,
    )

    shared = MeasurementRamp()
    room = MeasurementRamp(
        cap_bump_db=LISTENING_POSITION_CAP_BUMP_DB,
        cap_ceil_db=LISTENING_POSITION_CAP_CEIL_DB,
    )

    assert shared.cap_ceil_db == -3.0
    assert LISTENING_POSITION_CAP_BUMP_DB == 15.0
    assert room.cap_ceil_db == 0.0
    assert AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS == -12.0
    assert (
        room.cap_ceil_db + AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    ) == -12.0


# --- geometry lock store ------------------------------------------------------


def test_lock_store_is_per_geometry():
    store = LevelLockStore()
    near = MeasurementLevelLock(
        geometry="near_field_driver",
        main_volume_db=-40.0,
        gain_map_db=30.0,
        settled_mic_dbfs=-10.0,
        noise_floor_dbfs=-80.0,
    )
    listen = MeasurementLevelLock(
        geometry="listening_position",
        main_volume_db=-18.0,
        gain_map_db=2.0,
        settled_mic_dbfs=-16.0,
        noise_floor_dbfs=-70.0,
    )
    store.put(near)
    store.put(listen)
    # Two coexisting locks — neither clobbers the other.
    assert store.get("near_field_driver").main_volume_db == -40.0
    assert store.get("listening_position").main_volume_db == -18.0
    assert set(store.snapshot()) == {
        "near_field_driver",
        "listening_position",
    }
    store.discard("near_field_driver")
    assert store.get("near_field_driver") is None
    assert store.get("listening_position") is listen


# --- MeasurementSession seam (run_level_match) --------------------------------


def test_crossover_lease_phone_timeout_never_undercuts_server_safety_timeout():
    """The phone's hard capture deadline must always exceed the server's own
    ``MeasurementRamp.safety_timeout`` for the SAME ramp config, with the
    documented grace margin — otherwise the phone can declare a false timeout
    failure while the Pi's ramp is still legitimately running (the JTS3
    2026-07-15 crossover level-ramp incident: the phone's flat, disconnected
    hard-timeout constant undercut the server's real ~58 s safety timeout)."""

    import math

    from jasper.active_speaker.crossover_level_run import PHONE_TRANSPORT_GRACE_S
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    lease = CrossoverLevelLease()
    for geometry in (
        "near_field_driver:mono:woofer",
        "reference_axis_driver:mono:tweeter",
    ):
        server_safety_timeout_s = lease._ramp_config_for_geometry(
            geometry
        ).safety_timeout
        phone_timeout_ms = lease.phone_hard_timeout_ms(geometry)

        assert phone_timeout_ms == math.ceil(
            (server_safety_timeout_s + PHONE_TRANSPORT_GRACE_S) * 1000.0
        )
        assert phone_timeout_ms > server_safety_timeout_s * 1000.0


