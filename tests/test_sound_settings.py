from __future__ import annotations

from pathlib import Path

from jasper.sound.settings import (
    HEADROOM_TRIM_MAX_DB,
    SoundSettings,
    load_sound_settings,
    save_sound_settings,
)


def test_defaults_are_the_do_nothing_state():
    s = SoundSettings()
    assert s.headroom_trim_db == 0.0
    assert s.match_loudness is False


def test_missing_file_fails_soft_to_defaults(tmp_path: Path):
    assert load_sound_settings(tmp_path / "nope.json") == SoundSettings()


def test_corrupt_file_fails_soft_to_defaults(tmp_path: Path):
    p = tmp_path / "sound_settings.json"
    p.write_text("{not valid json")
    assert load_sound_settings(p) == SoundSettings()


def test_round_trip(tmp_path: Path):
    p = tmp_path / "sound_settings.json"
    save_sound_settings(SoundSettings(headroom_trim_db=6.0, match_loudness=True), p)
    loaded = load_sound_settings(p)
    assert loaded.headroom_trim_db == 6.0
    assert loaded.match_loudness is True


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


def test_match_loudness_coercion():
    assert SoundSettings.from_mapping({"match_loudness": "on"}).match_loudness is True
    assert SoundSettings.from_mapping({"match_loudness": 0}).match_loudness is False
