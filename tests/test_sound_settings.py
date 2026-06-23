# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.sound.profile import SimpleEq, SoundProfile
from jasper.sound.settings import (
    HEADROOM_TRIM_MAX_DB,
    SoundSettings,
    VOLUME_FLOOR_MAX_DB,
    VOLUME_FLOOR_MIN_DB,
    load_sound_settings,
    output_trim_db,
    save_sound_settings,
)
from jasper.volume_curve import DEFAULT_VOLUME_FLOOR_DB


def test_defaults_are_the_do_nothing_state():
    s = SoundSettings()
    assert s.headroom_trim_db == 0.0
    assert s.match_loudness is False
    assert s.volume_floor_db == DEFAULT_VOLUME_FLOOR_DB


def test_missing_file_fails_soft_to_defaults(tmp_path: Path):
    assert load_sound_settings(tmp_path / "nope.json") == SoundSettings()


def test_corrupt_file_fails_soft_to_defaults(tmp_path: Path):
    p = tmp_path / "sound_settings.json"
    p.write_text("{not valid json")
    assert load_sound_settings(p) == SoundSettings()


def test_round_trip(tmp_path: Path):
    p = tmp_path / "sound_settings.json"
    save_sound_settings(
        SoundSettings(
            headroom_trim_db=6.0,
            match_loudness=True,
            volume_floor_db=-24.0,
        ),
        p,
    )
    loaded = load_sound_settings(p)
    assert loaded.headroom_trim_db == 6.0
    assert loaded.match_loudness is True
    assert loaded.volume_floor_db == -24.0


def test_saved_file_is_group_readable_0640_with_parent_group(tmp_path: Path):
    """WS1 Phase 3b-2: the non-root jasper-control reads sound settings for
    /state; 0640 group jasper (these are non-secret EQ config), not 0600."""
    import os
    import stat
    p = tmp_path / "sound_settings.json"
    save_sound_settings(SoundSettings(), p)
    saved = os.stat(p)
    assert stat.S_IMODE(saved.st_mode) == 0o640
    assert saved.st_gid == os.stat(tmp_path).st_gid


def test_headroom_trim_is_clamped_nonnegative_and_safe():
    assert (
        SoundSettings.from_mapping({"headroom_trim_db": 99}).headroom_trim_db
        == HEADROOM_TRIM_MAX_DB
    )
    assert SoundSettings.from_mapping({"headroom_trim_db": -5}).headroom_trim_db == 0.0
    assert (
        SoundSettings.from_mapping({"headroom_trim_db": "garbage"}).headroom_trim_db
        == 0.0
    )


def test_volume_floor_is_clamped_to_audible_safe_range():
    assert (
        SoundSettings.from_mapping({"volume_floor_db": -90}).volume_floor_db
        == VOLUME_FLOOR_MIN_DB
    )
    assert (
        SoundSettings.from_mapping({"volume_floor_db": 0}).volume_floor_db
        == VOLUME_FLOOR_MAX_DB
    )
    assert (
        SoundSettings.from_mapping({"volume_floor_db": "garbage"}).volume_floor_db
        == DEFAULT_VOLUME_FLOOR_DB
    )


def test_match_loudness_coercion():
    assert SoundSettings.from_mapping({"match_loudness": "on"}).match_loudness is True
    assert SoundSettings.from_mapping({"match_loudness": 0}).match_loudness is False


def test_output_trim_db_combines_headroom_and_match_loudness():
    # The shared trim policy used by /sound/ apply, control /state, and doctor.
    boosted = SoundProfile(simple_eq=SimpleEq(bass_db=6.0))
    # Default settings -> no trim at all (boosts boost).
    assert output_trim_db(boosted, SoundSettings()) == 0.0
    # Manual headroom only.
    assert output_trim_db(boosted, SoundSettings(headroom_trim_db=6.0)) == 6.0
    # Match-loudness adds the loudness-weighted compensation (> 0 for a boost).
    assert output_trim_db(boosted, SoundSettings(match_loudness=True)) > 0.0
    # The two stack.
    assert (
        output_trim_db(boosted, SoundSettings(headroom_trim_db=6.0, match_loudness=True))
        > 6.0
    )
    # A flat profile has nothing to compensate, even with match-loudness on.
    assert output_trim_db(SoundProfile(), SoundSettings(match_loudness=True)) == 0.0
