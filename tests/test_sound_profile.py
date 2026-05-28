from __future__ import annotations

import json

from jasper.sound.profile import (
    ParametricBand,
    SimpleEq,
    SoundProfile,
    build_sound_filters,
    delete_named_profile,
    estimate_compare_headroom_db,
    estimate_headroom_db,
    load_profile,
    load_profile_library,
    profile_library_payload,
    rename_named_profile,
    response_component_payload,
    response_preview,
    save_named_profile,
    save_profile,
)


def test_profile_input_is_clamped_and_normalized():
    profile = SoundProfile.from_mapping({
        "enabled": True,
        "curve_id": "not-a-real-curve",
        "simple_eq": {"bass_db": 99, "mid_db": -99, "treble_db": "2.5"},
        "parametric_bands": [
            {"type": "low_shelf", "freq_hz": 3, "gain_db": 40, "q": 99},
            {"type": "peaking", "freq_hz": 1000, "gain_db": -3, "q": 2},
        ],
    })

    assert profile.curve_id == "flat"
    assert profile.simple_eq == SimpleEq(bass_db=6.0, mid_db=-6.0, treble_db=2.5)
    assert profile.parametric_bands[0] == ParametricBand(
        enabled=True,
        biquad_type="Lowshelf",
        freq_hz=20.0,
        gain_db=12.0,
        q=10.0,
    )


def test_build_filters_uses_curve_then_simple_then_advanced():
    profile = SoundProfile(
        enabled=True,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=1.5, mid_db=0.0, treble_db=-1.0),
        parametric_bands=(ParametricBand(freq_hz=2000.0, gain_db=-2.0, q=2.0),),
    )

    names = [spec.name for spec in build_sound_filters(profile)]

    assert names == [
        "sound_curve_harman_bass",
        "sound_curve_harman_tilt",
        "sound_simple_bass",
        "sound_simple_treble",
        "sound_advanced_1",
    ]


def test_disabled_profile_emits_no_sound_filters():
    profile = SoundProfile(
        enabled=False,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=6.0, mid_db=6.0, treble_db=6.0),
    )

    assert build_sound_filters(profile) == ()
    assert estimate_headroom_db(profile) == 0.0


def test_string_false_is_parsed_as_disabled():
    profile = SoundProfile.from_mapping({
        "enabled": "false",
        "parametric_bands": [{"enabled": "false", "gain_db": 6.0}],
    })

    assert profile.enabled is False
    assert profile.parametric_bands[0].enabled is False
    assert build_sound_filters(profile) == ()


def test_headroom_tracks_positive_broad_boosts():
    profile = SoundProfile(
        enabled=True,
        curve_id="flat",
        simple_eq=SimpleEq(bass_db=4.0, mid_db=0.0, treble_db=0.0),
    )

    assert 3.0 <= estimate_headroom_db(profile) <= 4.1


def test_headroom_samples_narrow_off_grid_advanced_boosts():
    profile = SoundProfile(
        enabled=True,
        curve_id="flat",
        parametric_bands=(
            ParametricBand(freq_hz=1234.0, gain_db=9.0, q=10.0),
        ),
    )

    assert estimate_headroom_db(profile) >= 8.9


def test_compare_headroom_uses_loudest_profile_anchor():
    saved = SoundProfile(simple_eq=SimpleEq(bass_db=2.0))
    draft = SoundProfile(simple_eq=SimpleEq(bass_db=5.0))
    bypass = SoundProfile(enabled=False, simple_eq=SimpleEq(bass_db=6.0))

    assert estimate_compare_headroom_db([saved, draft, bypass]) == (
        estimate_headroom_db(draft)
    )


def test_response_component_payload_splits_advanced_bands():
    profile = SoundProfile(
        curve_id="harman",
        simple_eq=SimpleEq(treble_db=-1.0),
        parametric_bands=(
            ParametricBand(freq_hz=2000.0, gain_db=-2.0, q=2.0),
            ParametricBand(enabled=False, freq_hz=3000.0, gain_db=3.0, q=1.0),
        ),
    )

    payload = response_component_payload(profile)

    assert payload["curve"]
    assert payload["simple"]
    assert payload["advanced"][0]["index"] == 0
    assert payload["advanced"][0]["preview"]
    assert payload["advanced"][1]["index"] == 1
    assert payload["advanced"][1]["preview"] == []


def test_save_and_load_profile_round_trip(tmp_path):
    path = tmp_path / "sound_profile.json"
    profile = SoundProfile(curve_id="bk", simple_eq=SimpleEq(bass_db=2.0))

    save_profile(profile, path)

    raw = json.loads(path.read_text())
    assert raw["curve_id"] == "bk"
    assert raw["simple_eq"]["bass_db"] == 2.0
    assert load_profile(path).curve_id == "bk"


def test_profile_library_includes_stock_profiles():
    payload = profile_library_payload()

    assert [entry["id"] for entry in payload[:3]] == [
        "stock:flat",
        "stock:harman",
        "stock:bk",
    ]
    assert payload[0]["editable"] is False
    assert payload[1]["profile"]["curve_id"] == "harman"
    assert payload[1]["profile"]["profile_id"] == "stock:harman"
    assert payload[1]["profile"]["profile_name"] == "Harman-style"


def test_preview_uses_dense_log_frequency_grid():
    preview = response_preview(SoundProfile())

    assert len(preview) == 121
    assert preview[0]["freq_hz"] == 20.0
    assert preview[-1]["freq_hz"] == 20000.0


def test_named_profile_library_lifecycle(tmp_path):
    path = tmp_path / "sound_profiles.json"
    profile = SoundProfile(curve_id="harman", simple_eq=SimpleEq(bass_db=2.0))

    created = save_named_profile(profile, name="  Evening  Tune  ", path=path)

    assert created.id.startswith("custom_")
    assert created.name == "Evening Tune"
    assert created.profile.profile_id == created.id
    assert created.profile.profile_name == "Evening Tune"
    assert load_profile_library(path)[0].profile.curve_id == "harman"

    updated = save_named_profile(
        SoundProfile(curve_id="bk"),
        name=None,
        path=path,
        profile_id=created.id,
    )
    assert updated.name == "Evening Tune"
    assert updated.profile.curve_id == "bk"
    assert updated.profile.profile_id == created.id

    renamed = rename_named_profile(created.id, name="Late Night", path=path)
    assert renamed.name == "Late Night"
    assert renamed.profile.profile_name == "Late Night"

    delete_named_profile(created.id, path=path)
    assert load_profile_library(path) == ()


def test_missing_profile_has_no_applied_timestamp(tmp_path):
    profile = load_profile(tmp_path / "missing.json")

    assert profile.updated_at == ""


def test_corrupt_profile_has_no_applied_timestamp(tmp_path):
    path = tmp_path / "sound_profile.json"
    path.write_text("{not json")

    profile = load_profile(path)

    assert profile.updated_at == ""
