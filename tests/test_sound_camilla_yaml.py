from __future__ import annotations

from jasper.camilla_config_contract import PeqFilter
from jasper.correction.camilla_yaml import emit_correction_config
from jasper.correction.peq import PEQ
from jasper.sound.camilla_yaml import (
    emit_sound_config,
    extract_room_peqs_from_config_text,
)
from jasper.sound.profile import SimpleEq, SoundProfile


def test_sound_config_preserves_room_peqs_before_preference_eq():
    profile = SoundProfile(
        enabled=True,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=2.0, mid_db=-1.0, treble_db=1.5),
    )
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        profile_id="abc123",
    )

    assert "Source: jasper.sound.camilla_yaml.emit_sound_config" in yaml
    assert "room_peq_1:" in yaml
    assert "sound_preamp:" in yaml
    assert "sound_curve_harman_bass:" in yaml
    assert "type: Lowshelf" in yaml
    assert "type: Highshelf" in yaml
    assert "sound_simple_mid:" in yaml
    assert "names: [room_peq_1, sound_preamp, sound_curve_harman_bass" in yaml
    assert yaml.count("channels: [0]") == 1
    assert yaml.count("channels: [1]") == 1


def test_disabled_sound_config_bypasses_preference_eq_but_keeps_room_peqs():
    profile = SoundProfile(enabled=False, curve_id="bk", simple_eq=SimpleEq(bass_db=6.0))
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=120.0, q=3.0, gain=-2.0)],
    )

    assert "room_peq_1:" in yaml
    assert "sound_curve_bk_bass" not in yaml
    assert "sound_simple_bass" not in yaml
    assert "sound_preamp" not in yaml
    assert "names: [room_peq_1, flat]" in yaml


def test_extract_room_peqs_from_legacy_correction_config():
    old_yaml = emit_correction_config([
        PEQ(freq=80.0, q=4.0, gain=-3.0),
        PEQ(freq=140.0, q=2.0, gain=-1.5),
    ])

    assert extract_room_peqs_from_config_text(old_yaml) == [
        PeqFilter(freq=80.0, q=4.0, gain=-3.0),
        PeqFilter(freq=140.0, q=2.0, gain=-1.5),
    ]


def test_extract_room_peqs_ignores_sound_peaking_filters():
    profile = SoundProfile.from_mapping({
        "parametric_bands": [
            {"type": "peaking", "freq_hz": 2000, "gain_db": -2, "q": 2},
        ],
    })
    yaml = emit_sound_config(
        profile,
        room_peqs=[PeqFilter(freq=90.0, q=4.0, gain=-3.5)],
    )

    assert "sound_advanced_1:" in yaml
    assert extract_room_peqs_from_config_text(yaml) == [
        PeqFilter(freq=90.0, q=4.0, gain=-3.5),
    ]
