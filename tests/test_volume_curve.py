# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os

import pytest

import jasper.volume_curve as volume_curve
from jasper.music_sources import VolumeMode
from jasper.sound import settings as sound_settings
from jasper.volume_curve import (
    canonical_target_db,
    configured_volume_floor_db,
    db_to_percent,
    main_mute_for_db,
    main_mute_for_level,
    percent_to_db,
)
from jasper.volume_floor import (
    DEFAULT_VOLUME_FLOOR_DB,
    VOLUME_CEILING_DB,
    VOLUME_FLOOR_MAX_DB,
    VOLUME_FLOOR_MIN_DB,
    normalize_volume_floor_db,
)


def test_zero_is_mute_one_is_audible_above_floor():
    assert percent_to_db(0) == DEFAULT_VOLUME_FLOOR_DB
    assert percent_to_db(1) > DEFAULT_VOLUME_FLOOR_DB
    assert percent_to_db(100) == 0.0


@pytest.mark.parametrize("level", range(1, 101))
def test_main_mute_predicates_agree_for_every_audible_level(level):
    # R-006: a level and its own dB must not disagree on mute, or the
    # coordinator re-mutes an audible level forever.
    assert main_mute_for_level(level) == main_mute_for_db(percent_to_db(level))


# -1.01 dB is a guard and -1.0 dB is not: the drift dead band is 1 dB.
@pytest.mark.parametrize(
    ("level", "mode", "persisted_db", "expected"),
    [
        pytest.param(70, VolumeMode.CAMILLA_MASTER, -1.0, percent_to_db(70), id="master"),
        pytest.param(70, VolumeMode.CAMILLA_MASTER, -1.01, percent_to_db(70), id="master_ignores_guard"),
        pytest.param(0, VolumeMode.CAMILLA_MASTER, -1.0, percent_to_db(0), id="master_zero"),
        pytest.param(0, VolumeMode.CAMILLA_MASTER, -1.01, percent_to_db(0), id="master_zero_ignores_guard"),
        pytest.param(70, VolumeMode.PUSH, -1.0, 0.0, id="push_pin"),
        pytest.param(70, VolumeMode.PUSH, -1.01, -1.01, id="push_keeps_guard"),
        pytest.param(0, VolumeMode.PUSH, -1.0, percent_to_db(0), id="push_content_mute"),
        pytest.param(0, VolumeMode.PUSH, -1.01, percent_to_db(0), id="push_mute_before_guard"),
    ],
)
def test_canonical_target_follows_the_carrier(level, mode, persisted_db, expected):
    assert canonical_target_db(level, mode, persisted_db) == pytest.approx(expected)


def test_nonzero_percent_round_trips_above_floor():
    for percent in [1, 10, 25, 50, 75, 90, 100]:
        assert db_to_percent(percent_to_db(percent)) == percent


def test_floor_db_maps_to_zero_for_legacy_db_callers():
    assert db_to_percent(DEFAULT_VOLUME_FLOOR_DB) == 0


def test_custom_floor_changes_curve_span():
    assert percent_to_db(1, floor_db=-20.0) > -20.0
    assert percent_to_db(50, floor_db=-20.0) == pytest.approx(-10.101, abs=0.001)
    assert db_to_percent(-10.101, floor_db=-20.0) == 50


def test_curve_never_exceeds_ceiling_and_stays_nondecreasing_over_every_floor():
    """Regression pin: `span*((p-1)/99)` and `(span/99)*(p-1)` are not
    bit-identical in floating point, and the latter can push
    `percent_to_db(100, floor)` fractionally above the ceiling for some
    floors. Every configured floor must cap at 0 dB and keep the curve
    non-decreasing across the slider."""
    tenths = round(VOLUME_FLOOR_MIN_DB * 10)
    limit = round(VOLUME_FLOOR_MAX_DB * 10)
    while tenths <= limit:
        floor = normalize_volume_floor_db(tenths / 10.0)
        assert percent_to_db(100, floor_db=floor) <= VOLUME_CEILING_DB
        previous = percent_to_db(0, floor_db=floor)
        for p in range(1, 101):
            current = percent_to_db(p, floor_db=floor)
            assert current >= previous
            previous = current
        tenths += 1


def test_configured_floor_cache_reloads_when_settings_file_changes(
    tmp_path, monkeypatch,
):
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(volume_curve, "_SETTINGS_FLOOR_CACHE", None)
    monkeypatch.setattr(volume_curve, "_SETTINGS_FLOOR_WARNING_LOGGED", False)
    settings_path.write_text(json.dumps({"volume_floor_db": -30.0}))

    assert configured_volume_floor_db() == -30.0

    settings_path.write_text(json.dumps({"volume_floor_db": -24.0}))
    stat = settings_path.stat()
    os.utime(
        settings_path,
        ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns + 1_000_000),
    )

    assert configured_volume_floor_db() == -24.0


def test_configured_floor_logs_unexpected_settings_failure_once(
    tmp_path, monkeypatch, caplog,
):
    settings_path = tmp_path / "sound_settings.json"
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(volume_curve, "_SETTINGS_FLOOR_CACHE", None)
    monkeypatch.setattr(volume_curve, "_SETTINGS_FLOOR_WARNING_LOGGED", False)

    def boom(path=None):
        raise RuntimeError("settings reader exploded")

    monkeypatch.setattr(sound_settings, "load_sound_settings", boom)
    caplog.set_level(logging.WARNING, logger="jasper.volume_curve")

    assert configured_volume_floor_db() == DEFAULT_VOLUME_FLOOR_DB
    assert configured_volume_floor_db() == DEFAULT_VOLUME_FLOOR_DB

    warnings = [
        record for record in caplog.records
        if record.name == "jasper.volume_curve"
        and "using default floor" in record.getMessage()
    ]
    assert len(warnings) == 1
