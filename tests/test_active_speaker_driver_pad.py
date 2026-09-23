# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations


import pytest

from jasper.active_speaker.driver_pad import (
    PAD_KINDS,
    DriverPadError,
    effective_sensitivity_db,
    normalise_pad,
)


def test_pad_kinds_is_the_closed_four_value_vocabulary():
    assert PAD_KINDS == ("none", "series_resistor", "l_pad", "direct_db")


# --- idempotence: normalise_pad(normalise_pad(x)) == normalise_pad(x) --------
#
# #1665 follow-up bug: normalise_pad WRITES attenuation_db /
# effective_impedance_ohm into the record it returns, then REJECTED those
# same fields as unknown/forbidden input when that returned record was fed
# back in. Live failure: crossover-v2 session-start rebuilds the design
# draft from the saved manual_settings on every prepare, so a saved
# l_pad/series_resistor pad 400ed on the very next session start. One
# normalisation must be a fixed point of itself for every kind.


@pytest.mark.parametrize(
    "raw,nominal_impedance_ohm",
    [
        pytest.param(None, 8.0, id="absent"),
        pytest.param({"kind": "none"}, 8.0, id="explicit_none"),
        pytest.param(
            {"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2.0},
            8.0,
            id="l_pad",
        ),
        pytest.param(
            {"kind": "series_resistor", "series_ohm": 10.0},
            8.0,
            id="series_resistor",
        ),
        pytest.param(
            {"kind": "direct_db", "attenuation_db": -3.5,
             # ignored-and-dropped on input (docstring contract pin):
             "effective_impedance_ohm": 8.4},
            8.0,
            id="direct_db_with_impedance",
        ),
        pytest.param(
            {"kind": "direct_db", "attenuation_db": -6.0},
            None,
            id="direct_db_without_impedance",
        ),
    ],
)
def test_normalise_pad_is_idempotent(raw, nominal_impedance_ohm):
    once = normalise_pad(
        raw, nominal_impedance_ohm=nominal_impedance_ohm, field_name="driver.pad"
    )
    twice = normalise_pad(
        once, nominal_impedance_ohm=nominal_impedance_ohm, field_name="driver.pad"
    )
    assert twice == once


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


def test_resistor_kinds_ignore_and_recompute_a_client_supplied_derived_attenuation():
    # attenuation_db is server-computed for l_pad/series_resistor. This is
    # exactly the shape normalise_pad's own output takes -- and the shape a
    # saved pad record has -- so it must round-trip cleanly rather than
    # erroring: see the idempotence contract in driver_pad.py's docstring
    # (#1665 follow-up: a saved pad record 400ed when re-normalised, e.g.
    # crossover-v2 session-start rebuilding the design draft from disk).
    out = normalise_pad(
        {
            "kind": "l_pad",
            "series_ohm": 6.8,
            "shunt_ohm": 2.0,
            "attenuation_db": -14.4,
        },
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


def test_resistor_kinds_discard_a_wrong_supplied_attenuation_rather_than_trusting_it():
    # The anti-confusion property that survives the fix above: an operator
    # still cannot MAKE UP an attenuation for a resistor pad. A deliberately
    # wrong echoed value is silently discarded (no consistency check) and the
    # correct figure is recomputed from the resistor values, never taken on
    # faith and never raised as an inconsistency error.
    out = normalise_pad(
        {
            "kind": "l_pad",
            "series_ohm": 6.8,
            "shunt_ohm": 2.0,
            "attenuation_db": -99.9,
        },
        nominal_impedance_ohm=8.0,
        field_name="driver.pad",
    )
    assert out is not None
    assert out["attenuation_db"] == -14.4


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


@pytest.mark.parametrize("raw,impedance,code", [
    ({"kind": "l_pad", "series_ohm": 6.8}, 8, "field_required"),
    ({"kind": "l_pad", "series_ohm": 6.8, "shunt_ohm": 2}, None, "field_required"),
    ({"kind": "series_resistor", "series_ohm": 10}, None, "field_required"),
    ({"kind": "series_resistor"}, 8, "field_required"),
    ({"kind": "series_resistor", "series_ohm": 10, "shunt_ohm": 2}, 8, "pad_field_not_applicable"),
    ({"kind": "direct_db", "attenuation_db": 3}, 8, "pad_attenuation_positive"),
    ({"kind": "direct_db"}, 8, "field_required"),
    *[({"kind": "direct_db", "attenuation_db": -3, key: 1}, 8, "pad_field_not_applicable")
      for key in ("series_ohm", "shunt_ohm")],
    ("l_pad", 8, "field_not_object"),
    ({}, 8, "field_required"),
    ({"kind": "none", "typo": 1}, 8, "unknown_pad_fields"),
    ({"kind": "resistor_ladder"}, 8, "field_unsupported"),
    *[({"kind": "l_pad", "series_ohm": bad, "shunt_ohm": 2}, 8, code)
      for bad, code in [(0, "field_not_positive"), (-1, "field_not_positive"),
                        (True, "field_not_numeric"), ("loud", "field_not_numeric"),
                        (float("nan"), "field_not_finite"), (float("inf"), "field_not_finite")]],
])
def test_pad_refusals_carry_condition_codes(raw, impedance, code):
    with pytest.raises(DriverPadError) as caught:
        normalise_pad(raw, nominal_impedance_ohm=impedance, field_name="driver.pad")
    assert caught.value.code == code


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


@pytest.mark.parametrize("source,expected", [(None, -10.0), ("operator_pinned", -10.0),
    ("research_estimate", -22.2), ("sensitivity_estimate", -22.2)])
def test_declared_trims_refresh_estimates_and_keep_explicit_values(source, expected):
    from jasper.active_speaker.level_trim import declared_driver_gains

    drivers = {"woofer": {"sensitivity_db_2v83_1m": 83.3}, "tweeter": {
        "sensitivity_db_2v83_1m": 108.5, "pad": {"attenuation_db": -3.0},
        "gain_offset_db": -10.0, "gain_offset_db_provenance": source,
    }}
    gains, _, _, issues = declared_driver_gains(tuple(drivers), drivers)
    assert gains == {"woofer": 0.0, "tweeter": expected}
    assert not issues
