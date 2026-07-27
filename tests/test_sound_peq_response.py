# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""PEQ magnitude response, new cut/notch band types, and JS↔Python parity.

The drawn /sound/ curve is real RBJ biquad magnitude (jasper.sound.profile)
mirrored in deploy/assets/sound-profile/js/eq-math.js. The shared contract is
tests/fixtures/peq_response_fixture.json: this module asserts Python matches
it; scripts/check-peq-parity.mjs asserts the JS module matches it.

**Third leg (linearization-integrity PR-L2).** Two implementations agreeing
with each other proves nothing about the speaker — before 2026-07-27 both drew
a Butterworth shelf while the emitter wrote ``slope: 6.0``, whose realized Q is
gain-dependent (0.476 at -11 dB). So the fixture is also pinned to *CamillaDSP*:
``_camilladsp_shelf_db`` below is an independent transcription of CamillaDSP's
own shelf coefficients (v4.1.3 ``src/filters/biquad.rs``), evaluated on the
parameters ``emit_filter_spec`` actually writes. Model == emitted == realized,
or these tests fail.
"""

import cmath
import json
import math
import re
from pathlib import Path

from jasper.sound import GAINLESS_BIQUAD_TYPES, build_sound_filters
from jasper.camilla_config_contract import SHELF_Q, SHELF_Q_EMIT_DECIMALS
from jasper.camilla_stereo_prefix import emit_filter_spec as _emit_filter_spec
import numpy as np
import pytest

from jasper.sound.profile import (
    CUT_MAX_Q,
    RESPONSE_SAMPLE_RATE_HZ,
    FilterSpec,
    ParametricBand,
    SoundProfile,
    _filter_response_complex,
    _filter_response_db,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "peq_response_fixture.json").read_text()
)
SHELF_TYPES = ("Lowshelf", "Highshelf")


def _camilladsp_shelf_db(
    biquad_type: str,
    freq: float,
    gain_db: float,
    *,
    q: float | None = None,
    slope: float | None = None,
    at_freqs,
) -> list[float]:
    """Magnitude (dB) of the shelf CamillaDSP would build from these params.

    Transcribed from CamillaDSP v4.1.3 ``src/filters/biquad.rs`` — the four
    ``Highshelf``/``Lowshelf`` × ``ShelfSteepness::{Q,Slope}`` match arms —
    deliberately NOT reusing ``jasper.sound.profile._biquad_coeffs``: this is
    the independent side of the comparison. Exactly one of ``q``/``slope``, as
    CamillaDSP itself requires.

    The two steepness forms differ ONLY in ``beta``:
        Q:     beta = sn * sqrt(A) / q
        Slope: alpha = sn/2 * sqrt((A + 1/A) * (1/(slope/12) - 1) + 2)
               beta  = 2 * sqrt(A) * alpha
    so ``slope: 12`` (S = 1) collapses to ``q = 1/sqrt(2)`` — CamillaDSP's own
    ``lowshelf_slope_vs_q`` test asserts exactly that pair. Anything else makes
    the realized Q a function of the band's gain.
    """
    if (q is None) == (slope is None):
        raise ValueError("exactly one of q/slope, as CamillaDSP requires")
    omega = 2.0 * math.pi * freq / RESPONSE_SAMPLE_RATE_HZ
    sn, cs = math.sin(omega), math.cos(omega)
    ampl = 10.0 ** (gain_db / 40.0)
    if q is not None:
        beta = sn * math.sqrt(ampl) / q
    else:
        alpha = (
            sn
            / 2.0
            * math.sqrt((ampl + 1.0 / ampl) * (1.0 / (slope / 12.0) - 1.0) + 2.0)
        )
        beta = 2.0 * math.sqrt(ampl) * alpha
    if biquad_type == "Highshelf":
        b0 = ampl * ((ampl + 1.0) + (ampl - 1.0) * cs + beta)
        b1 = -2.0 * ampl * ((ampl - 1.0) + (ampl + 1.0) * cs)
        b2 = ampl * ((ampl + 1.0) + (ampl - 1.0) * cs - beta)
        a0 = (ampl + 1.0) - (ampl - 1.0) * cs + beta
        a1 = 2.0 * ((ampl - 1.0) - (ampl + 1.0) * cs)
        a2 = (ampl + 1.0) - (ampl - 1.0) * cs - beta
    elif biquad_type == "Lowshelf":
        b0 = ampl * ((ampl + 1.0) - (ampl - 1.0) * cs + beta)
        b1 = 2.0 * ampl * ((ampl - 1.0) - (ampl + 1.0) * cs)
        b2 = ampl * ((ampl + 1.0) - (ampl - 1.0) * cs - beta)
        a0 = (ampl + 1.0) + (ampl - 1.0) * cs + beta
        a1 = -2.0 * ((ampl - 1.0) + (ampl + 1.0) * cs)
        a2 = (ampl + 1.0) + (ampl - 1.0) * cs - beta
    else:
        raise ValueError(biquad_type)
    out = []
    for f in at_freqs:
        z = cmath.exp(-2j * math.pi * f / RESPONSE_SAMPLE_RATE_HZ)
        out.append(
            20.0
            * math.log10(abs((b0 + b1 * z + b2 * z * z) / (a0 + a1 * z + a2 * z * z)))
        )
    return out


def _emitted_params(spec: FilterSpec) -> dict[str, str]:
    """The ``parameters:`` block ``emit_filter_spec`` writes, as raw strings."""
    params = {}
    for line in _emit_filter_spec(spec):
        if line.startswith("      ") and ":" in line:
            key, _, value = line.strip().partition(":")
            params[key] = value.strip()
    return params
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


def test_filter_response_complex_magnitude_matches_filter_response_db():
    """#1667 magnitude-consistency parity: the complex (minimum-phase) twin
    shares the ``_biquad_coeffs`` SSOT with the parity-pinned magnitude one, so
    ``abs(_filter_response_complex)`` equals ``10**(_filter_response_db/20)``
    bin-for-bin across every biquad type. The complex twin ADDS phase; it must
    never perturb the magnitude the emitted graph realizes."""
    freqs = list(np.geomspace(20.0, 20000.0, 400))
    cases = [
        ("Peaking", 1000.0, -6.0, 2.0),
        ("Peaking", 4064.0, -2.92, 2.0),
        ("Highshelf", 6000.0, -3.0, 1.0 / 2.0 ** 0.5),
        ("Lowshelf", 200.0, 4.0, 1.0 / 2.0 ** 0.5),
        ("Highpass", 1000.0, 0.0, 0.707),
        ("Lowpass", 12000.0, 0.0, 0.707),
        ("Notch", 60.0, 0.0, 8.0),
    ]
    for biquad_type, freq, gain, q in cases:
        spec = FilterSpec("x", biquad_type, freq, gain, q=q)
        mag = np.array(_filter_response_db(spec, freqs))
        cplx = np.array(_filter_response_complex(spec, freqs))
        # Notch reaches a true zero (its magnitude flooring at 1e-12 in the dB
        # twin has no complex counterpart); compare where the magnitude is not
        # near that floor, which is the whole physical range of every case here.
        keep = mag > -100.0
        np.testing.assert_allclose(
            np.abs(cplx)[keep], (10.0 ** (mag / 20.0))[keep], atol=1e-6, rtol=0, err_msg=biquad_type,
        )


def test_filter_response_complex_peaking_is_minimum_phase():
    """The load-bearing property vs a zero-phase magnitude scale: a peaking
    biquad is real (phase 0) at its centre and rotates on the skirts."""
    spec = FilterSpec("x", "Peaking", 1000.0, -6.0, q=1.0)
    cplx = _filter_response_complex(spec, [500.0, 1000.0, 2000.0])
    assert abs(cplx[1].imag) < 1e-9  # centre: real
    assert abs(np.degrees(np.angle(cplx[0]))) > 5.0  # below: rotated
    assert abs(np.degrees(np.angle(cplx[2]))) > 5.0  # above: rotated


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
    shelf = "\n".join(_emit_filter_spec(FilterSpec("f", "Lowshelf", 100.0, 4.0)))
    assert "q:" in shelf and "gain:" in shelf
    peak = "\n".join(_emit_filter_spec(FilterSpec("f", "Peaking", 1000.0, 3.0, q=1.0)))
    assert "q:" in peak and "gain:" in peak


# --- Third parity leg: model == emitted == what CamillaDSP realizes ----------


@pytest.mark.parametrize("biquad_type", SHELF_TYPES)
def test_shelf_is_emitted_as_the_modelled_q_never_as_a_slope(biquad_type):
    """The emitter spells CamillaDSP's ``q``, at the Q the model draws.

    ``slope`` must not appear: CamillaDSP rejects q+slope together, and a
    ``slope`` value is only Butterworth at 12 — the PR-L2 defect was a
    hardcoded ``slope: 6.0`` believed to be Butterworth.
    """
    params = _emitted_params(FilterSpec("f", biquad_type, 4000.0, -11.0))
    assert "slope" not in params
    assert params["q"] == FIXTURE["shelf_emission"]["value_str"]
    # ...and that literal is the constant every evaluator draws with.
    assert float(params["q"]) == pytest.approx(SHELF_Q, abs=5e-8)


def test_emitted_shelf_realizes_the_fixture_the_model_and_js_are_pinned_to():
    """Leg 3: CamillaDSP's own math, on the emitted params, reproduces the
    fixture — so model (leg 1) and JS (leg 2) are pinned to the speaker, not
    just to each other. Covers deep cuts including the -11 dB shelf whose
    realization error is what the 2026-07-27 iLoud comparison measured."""
    freqs = FIXTURE["test_freqs"]
    emitted_q = FIXTURE["shelf_emission"]["value"]
    checked = 0
    for case in FIXTURE["cases"]:
        if case["type"] not in SHELF_TYPES:
            continue
        # The emitter must still be writing the parameterisation the fixture
        # describes, or "realized" below would be checking a stale claim.
        params = _emitted_params(
            FilterSpec("x", case["type"], case["freq"], case["gain_db"])
        )
        assert float(params[FIXTURE["shelf_emission"]["param"]]) == emitted_q
        realized = _camilladsp_shelf_db(
            case["type"], case["freq"], case["gain_db"], q=emitted_q, at_freqs=freqs
        )
        for value, expected in zip(realized, case["db"]):
            assert abs(value - expected) < 1e-6, case
        checked += 1
    assert checked >= 6, "shelf coverage regressed out of the fixture"


def test_camilladsp_butterworth_shelf_is_slope_12_not_slope_6():
    """Pin the identity the codebase got wrong, in CamillaDSP's own terms.

    ``slope: 12`` is Butterworth (CamillaDSP's ``lowshelf_slope_vs_q`` test
    asserts ``slope: 12.0`` ≡ ``q: FRAC_1_SQRT_2``). ``slope: 6`` is not, and
    the gap grows with gain — which is why the defect hid: it is ~0.3 dB on a
    -2 dB taste shelf and ~1.7 dB on the -11 dB linearization shelf JTS3
    carried. If this ever passes at slope 6, the formula assumption moved.
    """
    freqs = list(np.geomspace(200.0, 20000.0, 400))
    for biquad_type in SHELF_TYPES:
        for gain, floor in ((-2.0, 0.25), (-11.0, 1.6)):
            butterworth = _camilladsp_shelf_db(
                biquad_type, 4000.0, gain, q=SHELF_Q, at_freqs=freqs
            )
            slope_12 = _camilladsp_shelf_db(
                biquad_type, 4000.0, gain, slope=12.0, at_freqs=freqs
            )
            slope_6 = _camilladsp_shelf_db(
                biquad_type, 4000.0, gain, slope=6.0, at_freqs=freqs
            )
            assert max(abs(a - b) for a, b in zip(butterworth, slope_12)) < 1e-9
            assert max(abs(a - b) for a, b in zip(butterworth, slope_6)) > floor
    # The realized Q at the JTS3 shelf, from CamillaDSP's slope->Q relation.
    ampl = 10.0 ** (-11.0 / 40.0)
    realized_q = 1.0 / math.sqrt((ampl + 1.0 / ampl) * (12.0 / 6.0 - 1.0) + 2.0)
    assert realized_q == pytest.approx(0.476, abs=0.001)


def test_shelf_emission_precision_keeps_realized_inside_parity_tolerance():
    """Why SHELF_Q is spelled at ``SHELF_Q_EMIT_DECIMALS``, not the shared
    4-decimal ``fmt``.

    The emitted literal is what CamillaDSP builds from, so its rounding is a
    real (if tiny) realization error. Pin that it stays an order of magnitude
    inside the 1e-6 dB parity tolerance the other two legs use — and that a
    coarser spelling would not have.

    Both arms are DERIVED from ``SHELF_Q_EMIT_DECIMALS``, so lowering the
    constant fails this test rather than sliding past it on a hardcoded 4.
    """
    freqs = list(np.geomspace(200.0, 20000.0, 400))

    def realized(decimals):
        return _camilladsp_shelf_db(
            "Highshelf",
            4000.0,
            -11.0,
            q=float(f"{SHELF_Q:.{decimals}f}"),
            at_freqs=freqs,
        )

    exact = _camilladsp_shelf_db("Highshelf", 4000.0, -11.0, q=SHELF_Q, at_freqs=freqs)
    emitted = realized(SHELF_Q_EMIT_DECIMALS)
    coarser = realized(SHELF_Q_EMIT_DECIMALS - 3)
    assert max(abs(a - b) for a, b in zip(exact, emitted)) < 2e-7  # measured 1.3e-7
    assert max(abs(a - b) for a, b in zip(exact, coarser)) > 1e-6  # measured 4.6e-5
    # The fixture's literal must be that same spelling — it is what the JS
    # parity leg evaluates CamillaDSP at.
    assert FIXTURE["shelf_emission"]["value_str"] == f"{SHELF_Q:.{SHELF_Q_EMIT_DECIMALS}f}"
