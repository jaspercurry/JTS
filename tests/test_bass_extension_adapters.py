# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import numpy as np
import pytest

from jasper.camilla_emit import fmt
from jasper.bass_extension.adapters import adapter_for_enclosure
from jasper.bass_extension.adapters.base import (
    COMMISSION_FLOOR_HZ,
    MIN_CURVE_POINTS,
    TARGET_RESPONSE_RESERVE_DB,
    CabinetInfo,
    CaptureRole,
    FitRefusal,
    MagnitudeCurve,
)
from jasper.bass_extension.adapters.sealed import SEALED_ADAPTER, SealedPlantFit
from jasper.bass_extension.alignment import (
    butterworth_highpass_db,
    second_order_highpass_db,
)
from jasper.bass_extension.adapters.base import BassExtensionRefusal
from jasper.bass_extension.targets import MARGINS


FREQS = np.geomspace(10.0, 500.0, 1200)
CAP_AUDIT_FREQS = np.concatenate(
    ([0.0], np.geomspace(0.01, 23_999.0, 65_536))
)
_AUDIT_Z1 = np.exp(-2j * np.pi * CAP_AUDIT_FREQS / 48_000.0)
_AUDIT_Z2 = _AUDIT_Z1 * _AUDIT_Z1
CABINET = CabinetInfo("sealed", 1, 165.0, 220.0)


def _curve(magnitude):
    return MagnitudeCurve(tuple(FREQS), tuple(np.asarray(magnitude, dtype=float)))


def _emitted(value):
    return float(fmt(float(value)))


def _digital_response_db(coefficients):
    b0, b1, b2, a0, a1, a2 = coefficients
    numerator = b0 + b1 * _AUDIT_Z1 + b2 * _AUDIT_Z2
    denominator = a0 + a1 * _AUDIT_Z1 + a2 * _AUDIT_Z2
    magnitude = np.abs(numerator / denominator)
    return 20.0 * np.log10(np.maximum(magnitude, 1e-300))


def _digital_biquad_db(filter_spec):
    kind = filter_spec["type"]
    if kind == "LinkwitzTransform":
        f0 = _emitted(filter_spec["freq_act"])
        q0 = _emitted(filter_spec["q_act"])
        fp = _emitted(filter_spec["freq_target"])
        qp = _emitted(filter_spec["q_target"])
        k0 = np.tan(np.pi * f0 / 48_000.0)
        kp = np.tan(np.pi * fp / 48_000.0)
        return _digital_response_db((
            1.0 + k0 / q0 + k0 * k0,
            2.0 * (k0 * k0 - 1.0),
            1.0 - k0 / q0 + k0 * k0,
            1.0 + kp / qp + kp * kp,
            2.0 * (kp * kp - 1.0),
            1.0 - kp / qp + kp * kp,
        ))

    raise AssertionError(kind)


def _emitted_target_boost_db(target):
    response = np.zeros_like(CAP_AUDIT_FREQS)
    for filter_spec in target.filters:
        response += _digital_biquad_db(filter_spec)
    return max(0.0, float(np.max(response)))


@pytest.mark.parametrize("role", (CaptureRole.WOOFER_NEARFIELD, CaptureRole.SEAT_MEDIAN))
@pytest.mark.parametrize("f0", (45.0, 61.0, 80.0))
@pytest.mark.parametrize("q0", (0.55, 0.707, 0.9))
def test_sealed_clean_fit_round_trip(f0, q0, role):
    fit = SEALED_ADAPTER.fit_plant(
        {role: _curve(second_order_highpass_db(FREQS, f0, q0) + 7.0)},
        CABINET,
    )
    assert isinstance(fit, SealedPlantFit)
    assert fit.f0_hz == pytest.approx(f0, rel=0.01)
    assert fit.q0 == pytest.approx(q0, abs=0.02)


def test_sealed_fit_window_without_support_refuses_rather_than_raising():
    """A grid so coarse, with its roll-off so near the top of it, that the
    fit's own window keeps too few points to fit."""
    freqs = np.geomspace(20.0, 320.0, MIN_CURVE_POINTS + 1)
    curve = MagnitudeCurve(
        tuple(freqs), tuple(second_order_highpass_db(freqs, 300.0, 0.707))
    )

    fit = SEALED_ADAPTER.fit_plant({CaptureRole.SEAT_MEDIAN: curve}, CABINET)

    assert isinstance(fit, FitRefusal)
    assert fit.refusal == BassExtensionRefusal.FIT_QUALITY_INSUFFICIENT


@pytest.mark.parametrize("f0,q0", ((45.0, 0.55), (61.0, 0.707), (80.0, 0.9)))
def test_sealed_seeded_noise_fit_is_bounded(f0, q0):
    rng = np.random.default_rng(20260716)
    magnitude = second_order_highpass_db(FREQS, f0, q0) + rng.normal(0.0, 0.5, len(FREQS))
    fit = SEALED_ADAPTER.fit_plant(
        {CaptureRole.WOOFER_NEARFIELD: _curve(magnitude)}, CABINET
    )
    assert isinstance(fit, SealedPlantFit)
    assert fit.f0_hz == pytest.approx(f0, rel=0.05)
    assert fit.q0 == pytest.approx(q0, abs=0.1)


def test_sealed_order_sanity_refuses_third_order_with_leakage_detail():
    fit = SEALED_ADAPTER.fit_plant(
        {CaptureRole.WOOFER_NEARFIELD: _curve(butterworth_highpass_db(FREQS, 61.0, 3))},
        CABINET,
    )
    assert isinstance(fit, FitRefusal)
    assert "leakage" in fit.detail
    clean = SEALED_ADAPTER.fit_plant(
        {CaptureRole.WOOFER_NEARFIELD: _curve(second_order_highpass_db(FREQS, 61.0, 0.707))},
        CABINET,
    )
    assert isinstance(clean, SealedPlantFit)


@pytest.mark.parametrize("margin", MARGINS.values(), ids=MARGINS)
def test_sealed_family_invariants(margin):
    adapter, plant = SEALED_ADAPTER, SealedPlantFit(61.0, 0.72, 0.2)
    family = adapter.generate_family(plant, margin=margin)
    assert family[-1].target_id == "natural"
    assert family[-1].filters == ()
    assert family[-1].boost_headroom_db == 0.0
    assert all(target.subsonic is not None for target in family)
    assert all(
        int(target.subsonic["order"]) == margin.subsonic_order
        for target in family
    )
    assert len({target.target_id for target in family}) == len(family)
    boosts = [target.boost_headroom_db for target in family]
    assert all(left >= right for left, right in zip(boosts, boosts[1:]))
    for target in family:
        actual_boost = _emitted_target_boost_db(target)
        assert actual_boost <= margin.boost_cap_db
        assert target.boost_headroom_db == pytest.approx(actual_boost, abs=0.002)
    for left, right in zip(family, family[1:]):
        if left.boost_headroom_db == pytest.approx(right.boost_headroom_db):
            assert left.fp_hz <= right.fp_hz
    expected_subsonic = max(15.0, margin.subsonic_corner_ratio * family[0].fp_hz)
    assert all(
        float(target.subsonic["freq"]) == pytest.approx(expected_subsonic)
        for target in family
    )


def test_sealed_floor_rule_and_commission_floor():
    fit = SEALED_ADAPTER.fit_plant(
        {
            CaptureRole.WOOFER_NEARFIELD: _curve(
                second_order_highpass_db(FREQS, 18.0, 0.707)
            )
        },
        CABINET,
    )
    assert isinstance(fit, SealedPlantFit)
    assert fit.notes == ("already_at_floor",)
    family = SEALED_ADAPTER.generate_family(fit, margin=MARGINS["normal"])
    assert len(family) == 1
    assert family[0].target_id == "natural"

    family = SEALED_ADAPTER.generate_family(
        SealedPlantFit(24.0, 0.707, 0.0),
        margin=MARGINS["conservative"],
    )
    assert all(target.fp_hz >= COMMISSION_FLOOR_HZ for target in family)


def test_sealed_fit_above_lt_q_domain_is_natural_only():
    fit = SEALED_ADAPTER.fit_plant(
        {
            CaptureRole.WOOFER_NEARFIELD: _curve(
                second_order_highpass_db(FREQS, 61.0, 1.3)
            )
        },
        CABINET,
    )
    assert isinstance(fit, SealedPlantFit)
    assert fit.q0 == pytest.approx(1.3, abs=0.02)
    family = SEALED_ADAPTER.generate_family(fit, margin=MARGINS["normal"])
    assert tuple(target.target_id for target in family) == ("natural",)


def test_sealed_low_q_family_moves_corner_to_honor_actual_boost_cap():
    margin = MARGINS["conservative"]
    family = SEALED_ADAPTER.generate_family(
        SealedPlantFit(60.0, 0.5, 0.0), margin=margin, n_targets=2
    )
    dc_corner = 60.0 / 10.0 ** (margin.boost_cap_db / 40.0)
    assert family[0].fp_hz > dc_corner
    assert family[0].qp == pytest.approx(0.65)
    assert family[0].boost_headroom_db == pytest.approx(
        margin.boost_cap_db - TARGET_RESPONSE_RESERVE_DB, abs=2e-4
    )


@pytest.mark.parametrize("margin", MARGINS.values(), ids=MARGINS)
@pytest.mark.parametrize("f0_hz", np.geomspace(15.0, 200.0, 15))
@pytest.mark.parametrize("q0", np.linspace(0.3, 1.2, 13))
def test_sealed_family_actual_boost_cap_across_fit_domain(margin, f0_hz, q0):
    plant = SealedPlantFit(f0_hz, q0, 0.0)
    family = SEALED_ADAPTER.generate_family(plant, margin=margin)
    for target in family:
        actual = _emitted_target_boost_db(target)
        assert actual <= margin.boost_cap_db
        assert target.boost_headroom_db == pytest.approx(actual, abs=0.002)


def test_sealed_plant_fit_round_trip_is_strict():
    fit = SealedPlantFit(61.0, 0.72, 0.2)
    assert SealedPlantFit.from_dict(fit.to_dict()) == fit
    with pytest.raises(ValueError):
        SealedPlantFit.from_dict({**fit.to_dict(), "unknown": 1})
    with pytest.raises(ValueError):
        SealedPlantFit.from_dict({**fit.to_dict(), "q0": 0.01})


def test_adapter_for_enclosure_mapping():
    assert adapter_for_enclosure("sealed") is SEALED_ADAPTER
    assert adapter_for_enclosure("vented") is None
    assert adapter_for_enclosure("passive_radiator") is None
    assert adapter_for_enclosure("transmission_line") is None
