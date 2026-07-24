# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math

import pytest

from jasper.active_speaker.driver_pad import (
    PAD_KINDS,
    DriverPadError,
    effective_sensitivity_db,
    normalise_pad,
)


def test_pad_kinds_is_the_closed_four_value_vocabulary():
    assert PAD_KINDS == ("none", "series_resistor", "l_pad", "direct_db")


# --- l_pad: the JTS3 tweeter acceptance check --------------------------------
#
# 6.8 ohm series + 2.0 ohm shunt against an 8 ohm nominal driver. Verified
# against the real rig (2026-07-23): -14.4 dB / 8.4 ohm effective load.


def test_l_pad_matches_the_jts3_rig_verified_numbers():
    out = normalise_pad(
        {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
        nominal_impedance_ohm=8.0,
        field_name="driver.pad",
    )
    assert out == {
        "kind": "l_pad",
        "series_ohm": 6.8,
        "shunt_ohm": 2.0,
        "attenuation_db": -14.4,
        "effective_impedance_ohm": 8.4,
    }


def test_l_pad_requires_shunt_and_never_assumes_8_ohms():
    with pytest.raises(DriverPadError, match=r"shunt_ohm is required for kind=l_pad"):
        normalise_pad(
            {"kind": "l_pad", "series_ohm": 6.8},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )
    with pytest.raises(DriverPadError, match=r"requires nominal_impedance_ohm"):
        normalise_pad(
            {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
            nominal_impedance_ohm=None,
            field_name="driver.pad",
        )


def test_l_pad_requires_series_greater_than_zero():
    with pytest.raises(DriverPadError, match=r"series_ohm must be > 0"):
        normalise_pad(
            {"kind": "l_pad", "series_ohm": 0, "shunt_ohm": 2.0},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )
    with pytest.raises(DriverPadError, match=r"series_ohm must be > 0"):
        normalise_pad(
            {"kind": "l_pad", "series_ohm": -1.0, "shunt_ohm": 2.0},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


# --- series_resistor: same formula, R_par degenerates to the bare impedance --


def test_series_resistor_uses_bare_impedance_as_r_par():
    out = normalise_pad(
        {"kind": "series_resistor", "series_ohm": 10.0},
        nominal_impedance_ohm=8.0,
        field_name="driver.pad",
    )
    # attenuation_db = 20*log10(8/(10+8)); effective_impedance_ohm = 10+8 = 18
    assert out == {
        "kind": "series_resistor",
        "series_ohm": 10.0,
        "attenuation_db": -7.0,
        "effective_impedance_ohm": 18.0,
    }


def test_series_resistor_requires_impedance():
    with pytest.raises(DriverPadError, match=r"requires nominal_impedance_ohm"):
        normalise_pad(
            {"kind": "series_resistor", "series_ohm": 10.0},
            nominal_impedance_ohm=None,
            field_name="driver.pad",
        )


def test_series_resistor_rejects_a_stray_shunt():
    with pytest.raises(DriverPadError, match=r"shunt_ohm is only valid for kind=l_pad"):
        normalise_pad(
            {"kind": "series_resistor", "series_ohm": 10.0, "shunt_ohm": 2.0},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


def test_resistor_kinds_reject_a_client_supplied_derived_attenuation():
    # attenuation_db is server-computed for l_pad/series_resistor; a client
    # that echoes back the last readout instead of leaving it out is a bug,
    # not a legitimate round-trip -- see driver_pad.py's docstring.
    with pytest.raises(DriverPadError, match=r"attenuation_db is derived for kind=l_pad"):
        normalise_pad(
            {
                "kind": "l_pad",
                "series_ohm": 6.8,
                "shunt_ohm": 2.0,
                "attenuation_db": -14.4,
            },
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


# --- direct_db: operator-known attenuation, no resistor topology -------------


def test_direct_db_stores_the_declared_value_verbatim():
    out = normalise_pad(
        {"kind": "direct_db", "attenuation_db": -3.5},
        nominal_impedance_ohm=8.0,
        field_name="driver.pad",
    )
    assert out == {"kind": "direct_db", "attenuation_db": -3.5}
    assert "effective_impedance_ohm" not in out


def test_direct_db_works_without_a_declared_impedance():
    # Unlike l_pad/series_resistor, direct_db needs no resistor topology, so
    # it must not require nominal_impedance_ohm either.
    out = normalise_pad(
        {"kind": "direct_db", "attenuation_db": -6.0},
        nominal_impedance_ohm=None,
        field_name="driver.pad",
    )
    assert out == {"kind": "direct_db", "attenuation_db": -6.0}


def test_direct_db_rejects_a_positive_attenuation():
    with pytest.raises(DriverPadError, match=r"attenuation_db must be <= 0"):
        normalise_pad(
            {"kind": "direct_db", "attenuation_db": 3.0},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


def test_direct_db_requires_attenuation_db():
    with pytest.raises(DriverPadError, match=r"attenuation_db is required for kind=direct_db"):
        normalise_pad(
            {"kind": "direct_db"},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


def test_direct_db_rejects_resistor_fields():
    with pytest.raises(DriverPadError, match=r"must not declare resistor values"):
        normalise_pad(
            {"kind": "direct_db", "attenuation_db": -3.0, "series_ohm": 1.0},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )
    with pytest.raises(DriverPadError, match=r"must not declare resistor values"):
        normalise_pad(
            {"kind": "direct_db", "attenuation_db": -3.0, "shunt_ohm": 1.0},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


# --- none / absent: the collapsed no-pad shape --------------------------------


def test_absent_pad_is_none():
    assert normalise_pad(None, nominal_impedance_ohm=8.0, field_name="driver.pad") is None
    assert normalise_pad("", nominal_impedance_ohm=8.0, field_name="driver.pad") is None


def test_explicit_none_kind_is_also_none():
    assert (
        normalise_pad(
            {"kind": "none"}, nominal_impedance_ohm=8.0, field_name="driver.pad"
        )
        is None
    )


# --- structural validation -----------------------------------------------------


def test_non_mapping_pad_is_rejected():
    with pytest.raises(DriverPadError, match=r"must be an object"):
        normalise_pad("l_pad", nominal_impedance_ohm=8.0, field_name="driver.pad")


def test_unknown_pad_fields_are_rejected():
    with pytest.raises(DriverPadError, match=r"unknown fields: typo"):
        normalise_pad(
            {"kind": "none", "typo": 1},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


def test_unknown_kind_is_rejected():
    with pytest.raises(DriverPadError, match=r"kind must be one of"):
        normalise_pad(
            {"kind": "resistor_ladder"},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


@pytest.mark.parametrize("bad", [True, "loud", float("nan"), float("inf")])
def test_resistor_values_reject_non_finite_and_boolean(bad):
    with pytest.raises(DriverPadError):
        normalise_pad(
            {"kind": "l_pad", "series_ohm": bad, "shunt_ohm": 2.0},
            nominal_impedance_ohm=8.0,
            field_name="driver.pad",
        )


# --- effective_sensitivity_db: folding a pad into declared sensitivity -------


def test_effective_sensitivity_db_folds_the_pads_attenuation():
    pad = normalise_pad(
        {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
        nominal_impedance_ohm=8.0,
        field_name="driver.pad",
    )
    assert effective_sensitivity_db(108.0, pad) == pytest.approx(93.6)


def test_effective_sensitivity_db_unchanged_without_a_pad():
    assert effective_sensitivity_db(108.0, None) == 108.0
    assert effective_sensitivity_db(108.0, {}) == 108.0


def test_effective_sensitivity_db_never_invents_a_naked_value():
    pad = normalise_pad(
        {"kind": "direct_db", "attenuation_db": -3.0},
        nominal_impedance_ohm=8.0,
        field_name="driver.pad",
    )
    assert effective_sensitivity_db(None, pad) is None


def test_effective_sensitivity_db_ignores_a_malformed_attenuation():
    assert effective_sensitivity_db(108.0, {"attenuation_db": "loud"}) == 108.0
    assert effective_sensitivity_db(108.0, {"attenuation_db": True}) == 108.0


# --- #1675 ka-beaming guidance: JS closed-form lockstep pin ------------------
#
# kaBeamingOnsetHz(diameterMm) is pure JS (deploy/assets/sound-profile/js/
# main.js) with no Python production implementation to import against -- the
# guidance is display-only, client-side. This is a hand-maintained mirror of
# the SAME closed form, verified against the rig's own anchor (114 mm ->
# 958 Hz / 1916 Hz). If you change one side, change the other.


def _ka_beaming_onset_hz(diameter_mm: float) -> tuple[int, int]:
    """Mirrors main.js's kaBeamingOnsetHz exactly, including its rounding
    order: f_ka1 is rounded to an integer FIRST, then f_ka2 = 2 * that
    rounded value (not 2x the raw float) -- see the JS function's own
    docstring for why this order is deliberate."""

    radius_m = diameter_mm / 2000.0
    ka1_hz = round(343.0 / (2.0 * math.pi * radius_m))
    return ka1_hz, ka1_hz * 2


def test_ka_beaming_onset_hz_matches_the_js_closed_form():
    ka1_hz, ka2_hz = _ka_beaming_onset_hz(114.0)
    assert (ka1_hz, ka2_hz) == (958, 1916)
