# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from jasper.sound.profile import (
    MAX_PARAMETRIC_BANDS,
    SIMPLE_EQ_FIELDS,
    ParametricBand,
    ProfileLibraryEntry,
    SimpleEq,
    SoundProfile,
    build_sound_filter_slots,
    build_sound_filters,
    delete_named_profile,
    estimate_headroom_db,
    load_profile,
    load_profile_library,
    loudness_compensation_db,
    profile_library_payload,
    rename_named_profile,
    response_preview,
    save_named_profile,
    save_profile,
    save_profile_library,
    simple_bands_payload,
)


def test_profile_input_is_clamped_and_normalized():
    profile = SoundProfile.from_mapping({
        "enabled": True,
        "curve_id": "not-a-real-curve",
        "simple_eq": {
            "sub_bass_db": 99, "bass_db": 99, "mid_db": -99,
            "presence_db": -50, "treble_db": "2.5",
        },
        "parametric_bands": [
            {"type": "low_shelf", "freq_hz": 3, "gain_db": 40, "q": 99},
            {"type": "peaking", "freq_hz": 1000, "gain_db": -3, "q": 2},
        ],
    })

    assert profile.curve_id == "flat"
    # Simple bands clamp to ±SIMPLE_EQ_LIMIT_DB (now ±12).
    assert profile.simple_eq == SimpleEq(
        sub_bass_db=12.0, bass_db=12.0, mid_db=-12.0,
        presence_db=-12.0, treble_db=2.5,
    )
    assert profile.parametric_bands[0] == ParametricBand(
        enabled=True,
        biquad_type="Lowshelf",
        freq_hz=20.0,
        gain_db=12.0,
        q=10.0,
    )


def test_simple_eq_migrates_legacy_three_band_profile():
    # Old persisted profiles only carried bass/mid/treble. They must load
    # unchanged, with the two new bands defaulting to 0 dB.
    eq = SimpleEq.from_mapping({"bass_db": 3.0, "mid_db": -2.0, "treble_db": 1.0})
    assert eq == SimpleEq(
        sub_bass_db=0.0, bass_db=3.0, mid_db=-2.0, presence_db=0.0, treble_db=1.0,
    )


def test_simple_eq_to_dict_round_trips_five_bands():
    eq = SimpleEq(
        sub_bass_db=1.0, bass_db=2.0, mid_db=3.0, presence_db=4.0, treble_db=5.0,
    )
    raw = eq.to_dict()
    assert set(raw) == {
        "sub_bass_db", "bass_db", "mid_db", "presence_db", "treble_db",
    }
    assert SimpleEq.from_mapping(raw) == eq


def test_simple_bands_payload_describes_five_fixed_slots():
    payload = simple_bands_payload()
    assert [b["field"] for b in payload] == list(SIMPLE_EQ_FIELDS)
    assert [b["label"] for b in payload] == [
        "Sub-bass", "Bass", "Mid", "Presence", "Treble",
    ]
    # Ascending centre frequencies, matching the mockup.
    assert [b["freq_hz"] for b in payload] == [60.0, 150.0, 1000.0, 4000.0, 10000.0]


def test_simple_filters_emit_five_fixed_bands():
    profile = SoundProfile(simple_eq=SimpleEq(
        sub_bass_db=1.0, bass_db=1.0, mid_db=1.0, presence_db=1.0, treble_db=1.0,
    ))
    simple = [
        s for s in build_sound_filters(profile)
        if s.name.startswith("sound_simple_")
    ]
    assert [(s.name, s.biquad_type, s.freq) for s in simple] == [
        ("sound_simple_sub_bass", "Lowshelf", 60.0),
        ("sound_simple_bass", "Peaking", 150.0),
        ("sound_simple_mid", "Peaking", 1000.0),
        ("sound_simple_presence", "Peaking", 4000.0),
        ("sound_simple_treble", "Highshelf", 10000.0),
    ]


def test_build_filters_uses_curve_then_simple_then_advanced():
    profile = SoundProfile(
        enabled=True,
        curve_id="harman",
        simple_eq=SimpleEq(bass_db=1.5, mid_db=0.0, treble_db=-1.0),
        parametric_bands=(ParametricBand(freq_hz=2000.0, gain_db=-2.0, q=2.0),),
    )

    names = [spec.name for spec in build_sound_filters(profile)]

    assert names == [
        "sound_curve_bass",
        "sound_curve_tilt",
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


def test_loudness_compensation_is_loudness_weighted_not_peak():
    # A narrow, tall boost barely moves loudness, so its compensation is far
    # below its peak gain -- the whole point of switching off the peak anchor.
    narrow = SoundProfile(
        parametric_bands=(ParametricBand(freq_hz=1000.0, gain_db=9.0, q=8.0),)
    )
    assert loudness_compensation_db(narrow) < estimate_headroom_db(narrow) / 2

    # A broad boost at the same centre/gain moves real loudness, so it
    # compensates more than the narrow one.
    broad = SoundProfile(
        parametric_bands=(ParametricBand(freq_hz=1000.0, gain_db=9.0, q=0.5),)
    )
    assert loudness_compensation_db(broad) > loudness_compensation_db(narrow)


def test_loudness_compensation_anchored_to_attenuation():
    # Flat / disabled / cuts-only never produce a positive (boosting)
    # compensation, so match-loudness can never cause clipping.
    assert loudness_compensation_db(SoundProfile()) == 0.0
    assert (
        loudness_compensation_db(SoundProfile(enabled=False, curve_id="harman")) == 0.0
    )
    cuts_only = SoundProfile(simple_eq=SimpleEq(mid_db=-6.0, treble_db=-4.0))
    assert loudness_compensation_db(cuts_only) == 0.0


def test_save_and_load_profile_round_trip(tmp_path):
    path = tmp_path / "sound_profile.json"
    profile = SoundProfile(curve_id="bk", simple_eq=SimpleEq(bass_db=2.0))

    save_profile(profile, path)

    raw = json.loads(path.read_text())
    assert raw["curve_id"] == "bk"
    assert raw["simple_eq"]["bass_db"] == 2.0
    assert load_profile(path).curve_id == "bk"
    # 0640 group jasper so the non-root jasper-control can read
    # the active profile for /state (non-secret EQ config), not 0600.
    import os
    import stat
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_save_profile_cleans_temp_file_on_publish_failure(tmp_path, monkeypatch):
    path = tmp_path / "sound_profile.json"

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("jasper.atomic_io.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_profile(SoundProfile(curve_id="harman"), path)

    assert not path.exists()
    assert list(tmp_path.glob(".sound_profile.json.*.tmp")) == []


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


def test_named_profile_description_survives_save_update_and_rename(tmp_path):
    path = tmp_path / "sound_profiles.json"
    profile_id = "custom_0123456789ab"
    save_profile_library(
        [
            ProfileLibraryEntry(
                id=profile_id,
                name="Evening",
                profile=SoundProfile(curve_id="harman"),
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                description="Warm but clear.",
            ),
        ],
        path,
    )

    loaded = load_profile_library(path)[0]
    assert loaded.description == "Warm but clear."

    updated = save_named_profile(
        SoundProfile(curve_id="bk"),
        name=None,
        path=path,
        profile_id=profile_id,
    )
    assert updated.description == "Warm but clear."

    renamed = rename_named_profile(profile_id, name="Late Night", path=path)
    assert renamed.description == "Warm but clear."
    assert load_profile_library(path)[0].description == "Warm but clear."


def test_missing_profile_has_no_applied_timestamp(tmp_path):
    profile = load_profile(tmp_path / "missing.json")

    assert profile.updated_at == ""


def test_corrupt_profile_has_no_applied_timestamp(tmp_path):
    path = tmp_path / "sound_profile.json"
    path.write_text("{not json")

    profile = load_profile(path)

    assert profile.updated_at == ""


# ---------------------------------------------------------------------------
# The fixed frame. These pin the property the whole live-EQ design rests on:
# the graph's SHAPE follows the profile's declaration and never its values, so
# an edit is a parameter write. CamillaDSP rebuilds its filter group and resets
# every filter's state when the structure changes, so a slot that came and went
# with a value would cost a ducked swap per gesture.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("band_count", [0, 1, 3, MAX_PARAMETRIC_BANDS])
def test_the_advanced_pool_is_the_same_size_whatever_the_band_count(band_count):
    profile = SoundProfile(parametric_bands=tuple(
        ParametricBand(freq_hz=100.0 * (i + 1), gain_db=2.0, q=1.0)
        for i in range(band_count)
    ))

    names = [s.name for s in build_sound_filter_slots(profile)]

    assert [n for n in names if n.startswith("sound_advanced_")] == [
        f"sound_advanced_{i}" for i in range(1, MAX_PARAMETRIC_BANDS + 1)
    ]
    # ...while only the declared ones count as doing anything.
    assert len(build_sound_filters(profile)) == band_count


def test_an_idle_slot_is_an_exact_identity():
    """A 0 dB Peaking biquad's zeros cancel its poles, so a spare slot is free."""
    idle = [
        s for s in build_sound_filter_slots(SoundProfile())
        if s.name.startswith("sound_advanced_")
    ]

    assert len(idle) == MAX_PARAMETRIC_BANDS
    assert {s.biquad_type for s in idle} == {"Peaking"}
    assert {s.gain for s in idle} == {0.0}
    assert not any(s.active() for s in idle)


@pytest.mark.parametrize("gain_db", [2.0, 0.02, 0.0, -0.02, -2.0])
def test_a_bands_slot_survives_its_gain_crossing_zero(gain_db):
    """Dragging through the flat window must not add or remove a filter."""
    profile = SoundProfile(
        parametric_bands=(ParametricBand(freq_hz=1000.0, gain_db=gain_db, q=1.0),),
    )

    band = next(
        s for s in build_sound_filter_slots(profile) if s.name == "sound_advanced_1"
    )

    assert band.gain == gain_db
    assert band.biquad_type == "Peaking"
    assert [s.name for s in build_sound_filter_slots(profile)] == [
        s.name for s in build_sound_filter_slots(SoundProfile())
    ]


def test_a_neutral_band_holds_a_slot_but_does_not_count_as_doing_anything():
    """The two questions stay separate: what the graph HOLDS vs what it DOES."""
    profile = SoundProfile(
        parametric_bands=(ParametricBand(freq_hz=1000.0, gain_db=0.0, q=1.0),),
    )

    assert build_sound_filters(profile) == ()
    assert any(
        s.name == "sound_advanced_1" for s in build_sound_filter_slots(profile)
    )
    assert estimate_headroom_db(profile) == 0.0


@pytest.mark.parametrize(
    "biquad_type, patchable",
    [
        ("Peaking", True),
        ("Lowshelf", True),
        ("Highshelf", True),
        # Inherent exception: a gainless type filters regardless of gain, so
        # silencing it REQUIRES becoming the idle Peaking — a recipe change.
        ("Highpass", False),
        ("Notch", False),
    ],
)
def test_bypass_keeps_each_bands_type_unless_it_is_gainless(biquad_type, patchable):
    """A shelf at 0 dB is an identity, so bypass must not retype it.

    An earlier cut rebuilt the advanced family from empty inputs, which made
    EVERY non-Peaking band change type on bypass — turning a parameter write
    into the ducked pipeline replace this frame exists to remove. The pin that
    missed it used profiles with no bands and compared only names.
    """
    kwargs = dict(biquad_type=biquad_type, freq_hz=100.0, q=1.0)
    on = SoundProfile(enabled=True, parametric_bands=(
        ParametricBand(gain_db=4.0, **kwargs),
    ))
    off = SoundProfile(enabled=False, parametric_bands=(
        ParametricBand(gain_db=4.0, **kwargs),
    ))

    live = next(
        s for s in build_sound_filter_slots(on) if s.name == "sound_advanced_1"
    )
    bypassed = next(
        s for s in build_sound_filter_slots(off) if s.name == "sound_advanced_1"
    )

    assert (live.biquad_type == bypassed.biquad_type) is patchable
    # Silent either way, and the frame never changes shape.
    assert not any(s.active() for s in build_sound_filter_slots(off))
    assert [s.name for s in build_sound_filter_slots(on)] == [
        s.name for s in build_sound_filter_slots(off)
    ]


@pytest.mark.parametrize("curve_id", ["flat", "harman", "bk"])
def test_bypass_keeps_the_frame_and_only_moves_values(curve_id):
    """Bypass is spelled as values, including for a curve preset.

    Emitting nothing would strip the whole frame out of the pipeline, so
    toggling bypass would cost the ducked swap the frame exists to remove. The
    curve's filters are NEUTRALISED rather than dropped for the same reason —
    dropping them would make bypass structural again for exactly the households
    who chose a preset.
    """
    on = SoundProfile(enabled=True, curve_id=curve_id)
    bypassed = SoundProfile(enabled=False, curve_id=curve_id)

    assert [s.name for s in build_sound_filter_slots(on)] == [
        s.name for s in build_sound_filter_slots(bypassed)
    ]
    # Bypassed means silent, by value.
    assert build_sound_filters(bypassed) == ()
    assert not any(s.active() for s in build_sound_filter_slots(bypassed))
