"""PEQ magnitude response, new cut/notch band types, and JS↔Python parity.

The drawn /sound/ curve is real RBJ biquad magnitude (jasper.sound.profile)
mirrored in deploy/assets/sound-profile/js/eq-math.js. The shared contract is
tests/fixtures/peq_response_fixture.json: this module asserts Python matches
it; scripts/check-peq-parity.mjs asserts the JS module matches it.
"""

import json
import re
from pathlib import Path

from jasper.sound import GAINLESS_BIQUAD_TYPES, build_sound_filters
from jasper.camilla_stereo_prefix import emit_filter_spec as _emit_filter_spec
from jasper.sound.profile import (
    CUT_MAX_Q,
    FilterSpec,
    ParametricBand,
    SoundProfile,
    _filter_response_db,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "peq_response_fixture.json").read_text()
)
_EQ_MATH_JS = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "assets"
    / "sound-profile"
    / "js"
    / "eq-math.js"
).read_text()


def _db(biquad_type, freq, gain, q, at_freq):
    return _filter_response_db(FilterSpec("x", biquad_type, freq, gain, q=q), [at_freq])[
        0
    ]


def test_python_matches_shared_parity_fixture():
    """Python reference reproduces the committed fixture (the JS contract)."""
    freqs = FIXTURE["test_freqs"]
    for case in FIXTURE["cases"]:
        spec = FilterSpec("x", case["type"], case["freq"], case["gain_db"], q=case["q"])
        got = _filter_response_db(spec, freqs)
        for value, expected in zip(got, case["db"]):
            assert abs(value - expected) < 1e-6, case


def test_peaking_peaks_at_gain_on_center_frequency():
    # A bell's response equals its gain exactly at the center frequency — this
    # is why the dot has always sat on the line for Peaking.
    assert abs(_db("Peaking", 1000.0, 6.0, 1.0, 1000.0) - 6.0) < 0.05


def test_shelf_reaches_half_gain_at_corner_and_full_gain_in_band():
    # The shelf bug: at the corner frequency the curve is only gain/2, so a dot
    # pinned to full gain floated above the line. The curve math confirms the
    # half-gain knee; the UI now anchors the dot to the summed curve instead.
    assert abs(_db("Lowshelf", 1000.0, 8.0, 1.0, 1000.0) - 4.0) < 0.1
    assert abs(_db("Lowshelf", 1000.0, 8.0, 1.0, 20.0) - 8.0) < 0.2
    assert abs(_db("Highshelf", 1000.0, 8.0, 1.0, 20000.0) - 8.0) < 0.2


def test_highpass_is_minus_3db_at_corner_with_butterworth_q():
    assert abs(_db("Highpass", 1000.0, 0.0, 0.707, 1000.0) - (-3.0)) < 0.1
    # Deep in the stopband it rolls off hard; in the passband it is flat.
    assert _db("Highpass", 1000.0, 0.0, 0.707, 100.0) < -20.0
    assert abs(_db("Highpass", 1000.0, 0.0, 0.707, 15000.0)) < 0.5


def test_lowpass_passband_flat_stopband_rolls_off():
    assert abs(_db("Lowpass", 12000.0, 0.0, 0.707, 1000.0)) < 0.5
    assert _db("Lowpass", 1000.0, 0.0, 0.707, 12000.0) < -20.0


def test_notch_is_a_deep_gainless_cut_at_center():
    assert _db("Notch", 60.0, 0.0, 8.0, 60.0) < -40.0
    # Away from the notch it returns to unity.
    assert abs(_db("Notch", 60.0, 0.0, 8.0, 1000.0)) < 1.0


def test_new_types_round_trip_and_pin_gain_to_zero():
    for raw_type, canonical in [
        ("highpass", "Highpass"),
        ("hpf", "Highpass"),
        ("low_pass", "Lowpass"),
        ("notch", "Notch"),
    ]:
        band = ParametricBand.from_mapping(
            {"type": raw_type, "freq_hz": 80.0, "gain_db": 7.0, "q": 1.2}
        )
        assert band.biquad_type == canonical
        # Gain is pinned to 0 even when hostile input supplies one.
        assert band.gain_db == 0.0
        assert band.biquad_type in GAINLESS_BIQUAD_TYPES


def test_gainless_band_is_active_despite_zero_gain():
    # build_sound_filters drops |gain|<0.05 bands; a cut/notch has no gain and
    # must still reach CamillaDSP, so FilterSpec.active() exempts it.
    profile = SoundProfile(
        parametric_bands=(
            ParametricBand(biquad_type="Highpass", freq_hz=30.0, gain_db=0.0, q=0.707),
        )
    )
    names = [spec.name for spec in build_sound_filters(profile)]
    assert "sound_advanced_1" in names


def test_yaml_emit_omits_gain_for_cut_and_notch_types():
    for biquad_type in ("Highpass", "Lowpass", "Notch"):
        lines = _emit_filter_spec(FilterSpec("f", biquad_type, 80.0, 0.0, q=0.9))
        body = "\n".join(lines)
        assert f"type: {biquad_type}" in body
        assert "q:" in body
        assert "gain:" not in body  # cut/notch carry no gain term


def test_gainless_type_set_matches_between_python_and_js():
    # The two implementations carry independent literals; keep them in lockstep
    # so a type added on one side can't silently behave like Peaking on the other.
    match = re.search(r"GAINLESS_TYPES\s*=\s*\[([^\]]*)\]", _EQ_MATH_JS)
    assert match, "GAINLESS_TYPES not found in eq-math.js"
    js_types = set(re.findall(r"'([^']+)'", match.group(1)))
    assert js_types == set(GAINLESS_BIQUAD_TYPES)


def test_cut_filters_q_is_capped_but_notch_and_peaking_are_not():
    # A high-Q high/low-pass is a large resonant boost; the model caps it.
    hp = ParametricBand.from_mapping({"type": "highpass", "freq_hz": 100.0, "q": 8.0})
    lp = ParametricBand.from_mapping({"type": "lowpass", "freq_hz": 8000.0, "q": 10.0})
    assert hp.q == CUT_MAX_Q
    assert lp.q == CUT_MAX_Q
    # Notch and peaking keep the full Q range (a notch wants to be narrow).
    notch = ParametricBand.from_mapping({"type": "notch", "freq_hz": 60.0, "q": 8.0})
    peak = ParametricBand.from_mapping({"type": "peaking", "freq_hz": 1000.0, "q": 8.0})
    assert notch.q == 8.0
    assert peak.q == 8.0


def test_capped_highpass_resonant_boost_stays_small():
    # The cap exists to bound the corner-frequency boost; confirm it works.
    band = ParametricBand.from_mapping({"type": "highpass", "freq_hz": 100.0, "q": 8.0})
    profile = SoundProfile(parametric_bands=(band,))
    from jasper.sound.profile import response_preview

    peak = max(point["db"] for point in response_preview(profile))
    assert peak < 3.5  # ~+3 dB at Q=1.4, vs ~+18 dB uncapped at Q=8


def test_yaml_emit_keeps_gain_for_shelf_and_peaking():
    shelf = "\n".join(_emit_filter_spec(FilterSpec("f", "Lowshelf", 100.0, 4.0, slope=6.0)))
    assert "slope:" in shelf and "gain:" in shelf
    peak = "\n".join(_emit_filter_spec(FilterSpec("f", "Peaking", 1000.0, 3.0, q=1.0)))
    assert "q:" in peak and "gain:" in peak
