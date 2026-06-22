# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os

import pytest

import jasper.volume_curve as volume_curve
from jasper.sound import settings as sound_settings
from jasper.volume_curve import (
    DEFAULT_VOLUME_FLOOR_DB,
    configured_volume_floor_db,
    db_to_percent,
    delta_db_to_delta_percent,
    percent_to_db,
)


def test_zero_and_one_share_floor_db():
    assert percent_to_db(0) == DEFAULT_VOLUME_FLOOR_DB
    assert percent_to_db(1) == DEFAULT_VOLUME_FLOOR_DB
    assert percent_to_db(100) == 0.0


def test_nonzero_percent_round_trips_above_floor():
    for percent in [10, 25, 50, 75, 90, 100]:
        assert db_to_percent(percent_to_db(percent)) == percent


def test_floor_db_maps_to_zero_for_legacy_db_callers():
    assert db_to_percent(DEFAULT_VOLUME_FLOOR_DB) == 0


def test_custom_floor_changes_curve_span():
    assert percent_to_db(1, floor_db=-20.0) == -20.0
    assert percent_to_db(50, floor_db=-20.0) == pytest.approx(-10.101, abs=0.001)
    assert db_to_percent(-10.101, floor_db=-20.0) == 50


def test_delta_db_uses_calibrated_span():
    assert delta_db_to_delta_percent(5.0, floor_db=-50.0) == 10
    assert delta_db_to_delta_percent(5.0, floor_db=-25.0) == 20


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
