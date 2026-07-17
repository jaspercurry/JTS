# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace

import numpy as np
import pytest

from jasper.audio_measurement.analysis import resample_log
from jasper.bass_extension.adapters.base import (
    COMMISSION_FLOOR_HZ,
    CabinetInfo,
    CaptureRole,
    FitRefusal,
    MagnitudeCurve,
    adapter_for_enclosure,
)
from jasper.bass_extension.adapters.passive_radiator import (
    PASSIVE_RADIATOR_ADAPTER,
    PassiveRadiatorPlantFit,
)
from jasper.bass_extension.adapters.ported import PORTED_ADAPTER, PortedPlantFit
from jasper.bass_extension.adapters.sealed import SEALED_ADAPTER, SealedPlantFit
from jasper.bass_extension.alignment import (
    butterworth_highpass_db,
    lt_boost_db,
    second_order_highpass_db,
)
from jasper.bass_extension.targets import MARGINS


FREQS = np.geomspace(10.0, 500.0, 1200)
CABINET = CabinetInfo("sealed", 1, 165.0, 220.0)


def _curve(magnitude):
    return MagnitudeCurve(tuple(FREQS), tuple(np.asarray(magnitude, dtype=float)))


def _natural_curve():
    freqs = np.geomspace(10.0, 500.0, 96)
    magnitude = butterworth_highpass_db(freqs, 68.0, 4)
    passband = (freqs >= 200.0) & (freqs <= 400.0)
    magnitude -= float(np.mean(magnitude[passband]))
    return MagnitudeCurve(tuple(freqs), tuple(magnitude))


NATURAL_CURVE = _natural_curve()


@pytest.mark.parametrize("f0", (45.0, 61.0, 80.0))
@pytest.mark.parametrize("q0", (0.55, 0.707, 0.9))
def test_sealed_clean_fit_round_trip(f0, q0):
    fit = SEALED_ADAPTER.fit_plant(
        {CaptureRole.WOOFER_NEARFIELD: _curve(second_order_highpass_db(FREQS, f0, q0) + 7.0)},
        CABINET,
    )
    assert isinstance(fit, SealedPlantFit)
    assert fit.f0_hz == pytest.approx(f0, rel=0.01)
    assert fit.q0 == pytest.approx(q0, abs=0.02)


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


def _vented_woofer(fb=45.0, knee=68.0):
    base = butterworth_highpass_db(FREQS, knee, 4)
    notch = -14.0 * np.exp(-(np.log2(FREQS / fb) / 0.09) ** 2)
    return base + notch


def _port_curve(peak_hz):
    return -25.0 + 20.0 * np.exp(-(np.log2(FREQS / peak_hz) / 0.18) ** 2)


def test_ported_fb_location_and_port_disagreement():
    fit = PORTED_ADAPTER.fit_plant(
        {
            CaptureRole.WOOFER_NEARFIELD: _curve(_vented_woofer()),
            CaptureRole.PORT_NEARFIELD: _curve(_port_curve(45.0)),
        },
        CabinetInfo("vented", 1, 165.0, 220.0),
    )
    assert isinstance(fit, PortedPlantFit)
    assert fit.fb_hz == pytest.approx(45.0, rel=0.03)
    assert len(fit.natural_curve.freqs_hz) == 96
    passband = np.asarray(fit.natural_curve.freqs_hz) >= 200.0
    passband &= np.asarray(fit.natural_curve.freqs_hz) <= 400.0
    assert np.mean(np.asarray(fit.natural_curve.magnitude_db)[passband]) == pytest.approx(
        0.0, abs=1e-12
    )
    refused = PORTED_ADAPTER.fit_plant(
        {
            CaptureRole.WOOFER_NEARFIELD: _curve(_vented_woofer()),
            CaptureRole.PORT_NEARFIELD: _curve(_port_curve(80.0)),
        },
        CabinetInfo("vented", 1, 165.0, 220.0),
    )
    assert isinstance(refused, FitRefusal)
    assert refused.detail == "woofer minimum and port maximum disagree"


def test_ported_fit_stores_resampled_measured_curve_not_smoothed_landmarks():
    feature = 4.0 * np.exp(-(np.log2(FREQS / 115.0) / 0.025) ** 2)
    measured = _vented_woofer() + feature
    fit = PORTED_ADAPTER.fit_plant(
        {CaptureRole.WOOFER_NEARFIELD: _curve(measured)},
        CabinetInfo("vented", 1, 165.0, 220.0),
    )
    assert isinstance(fit, PortedPlantFit)
    passband = (FREQS >= 200.0) & (FREQS <= 400.0)
    normalized = measured - float(np.mean(measured[passband]))
    expected_freqs, expected_db = resample_log(
        FREQS, normalized, f_min=10.0, f_max=500.0, n_points=96
    )
    expected_passband = (expected_freqs >= 200.0) & (expected_freqs <= 400.0)
    expected_db -= float(np.mean(expected_db[expected_passband]))
    assert fit.natural_curve.freqs_hz == pytest.approx(expected_freqs)
    assert fit.natural_curve.magnitude_db == pytest.approx(expected_db, abs=1e-12)


def _passive_radiator_pair(notch_hz):
    woofer_db = _vented_woofer()
    woofer = 10.0 ** (woofer_db / 20.0)
    relative_db = 12.0 * np.log2(FREQS / notch_hz)
    radiator = woofer * 10.0 ** (relative_db / 20.0)
    complex_sum = woofer.astype(complex) + radiator * np.exp(1j * np.pi)
    located = FREQS[int(np.argmin(np.abs(complex_sum)))]
    assert located == pytest.approx(notch_hz, rel=0.01)
    return woofer_db, 20.0 * np.log10(radiator)


def test_passive_radiator_notch_location_and_absent_crossover_refusal():
    truth = 27.0
    woofer, pr = _passive_radiator_pair(truth)
    captures = {
        CaptureRole.WOOFER_NEARFIELD: _curve(woofer),
        CaptureRole.PR_NEARFIELD: _curve(pr),
    }
    fit = PASSIVE_RADIATOR_ADAPTER.fit_plant(
        captures, CabinetInfo("passive_radiator", 1, 165.0, 220.0, 165.0)
    )
    assert isinstance(fit, PassiveRadiatorPlantFit)
    assert fit.notch_hz == pytest.approx(truth, rel=0.20)
    assert fit.notes == ()

    unscaled = PASSIVE_RADIATOR_ADAPTER.fit_plant(
        captures, CabinetInfo("passive_radiator", 1, 165.0, 220.0)
    )
    assert isinstance(unscaled, PassiveRadiatorPlantFit)
    assert unscaled.notes == ("pr_nearfield_unscaled",)

    absent = PASSIVE_RADIATOR_ADAPTER.fit_plant(
        {
            CaptureRole.WOOFER_NEARFIELD: _curve(woofer),
            CaptureRole.PR_NEARFIELD: _curve(woofer + 5.0),
        },
        CabinetInfo("passive_radiator", 1, None, 220.0),
    )
    assert isinstance(absent, FitRefusal)
    assert absent.refusal == "bass_extension_pr_notch_not_located"


@pytest.mark.parametrize(
    "adapter,plant",
    (
        (SEALED_ADAPTER, SealedPlantFit(61.0, 0.72, 0.2)),
        (PORTED_ADAPTER, PortedPlantFit(45.0, 68.0, 24.0, 0.5, NATURAL_CURVE)),
        (
            PASSIVE_RADIATOR_ADAPTER,
            PassiveRadiatorPlantFit(
                45.0, 68.0, 24.0, 0.5, NATURAL_CURVE, 27.0
            ),
        ),
    ),
)
def test_family_invariants_for_every_adapter(adapter, plant):
    family = adapter.generate_family(plant, margin=MARGINS["normal"])
    assert family[-1].target_id == "natural"
    assert family[-1].filters == ()
    assert family[-1].boost_headroom_db == 0.0
    assert all(target.subsonic is not None for target in family)
    assert len({target.target_id for target in family}) == len(family)
    boosts = [target.boost_headroom_db for target in family]
    assert all(left >= right for left, right in zip(boosts, boosts[1:]))
    for left, right in zip(family, family[1:]):
        if left.boost_headroom_db == pytest.approx(right.boost_headroom_db):
            assert left.fp_hz <= right.fp_hz
    if adapter is not SEALED_ADAPTER:
        assert not any(
            filter_spec.get("type") == "LinkwitzTransform"
            for target in family for filter_spec in target.filters
        )
    if adapter is PASSIVE_RADIATOR_ADAPTER:
        assert all(float(target.subsonic["freq"]) >= 1.1 * plant.notch_hz for target in family)
        shaping = (
            filter_spec
            for target in family
            for filter_spec in target.filters
            if filter_spec["type"] != "ButterworthHighpass"
        )
        assert all(
            filter_spec["type"] == "Peaking"
            and 0.7 <= float(filter_spec["q"]) <= 1.5
            and 0.0 <= float(filter_spec["gain"]) <= 6.0
            for filter_spec in shaping
        )
        grid = np.geomspace(10.0, plant.notch_hz, 512)
        natural = np.interp(
            grid,
            plant.natural_curve.freqs_hz,
            plant.natural_curve.magnitude_db,
        )
        for target in family:
            delta = adapter.predicted_response(plant, target, grid) - natural
            assert np.max(delta) <= 0.5 + 1e-12


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


def test_ported_and_pr_below_floor_plants_are_natural_only():
    ported = PortedPlantFit(15.0, 18.0, 24.0, 0.5, NATURAL_CURVE)
    pr = PassiveRadiatorPlantFit(
        15.0, 18.0, 24.0, 0.5, NATURAL_CURVE, 12.0
    )
    for adapter, plant in ((PORTED_ADAPTER, ported), (PASSIVE_RADIATOR_ADAPTER, pr)):
        family = adapter.generate_family(plant, margin=MARGINS["normal"])
        assert tuple(target.target_id for target in family) == ("natural",)
        assert family[0].fp_hz >= COMMISSION_FLOOR_HZ


def test_ported_family_is_sorted_by_headroom_then_corner():
    plant = PortedPlantFit(60.0, 120.0, 24.0, 0.5, NATURAL_CURVE)
    family = PORTED_ADAPTER.generate_family(
        plant, margin=MARGINS["conservative"]
    )
    assert all(
        left.boost_headroom_db >= right.boost_headroom_db
        for left, right in zip(family, family[1:])
    )
    for left, right in zip(family, family[1:]):
        if left.boost_headroom_db == right.boost_headroom_db:
            assert left.fp_hz <= right.fp_hz


@pytest.mark.parametrize("margin", MARGINS.values(), ids=MARGINS)
def test_pr_composite_constraint_includes_exact_notch(margin):
    plant = PassiveRadiatorPlantFit(
        45.0, 68.0, 24.0, 0.5, NATURAL_CURVE, 22.5
    )
    family = PASSIVE_RADIATOR_ADAPTER.generate_family(plant, margin=margin)
    notch = np.array([plant.notch_hz])
    natural = np.interp(
        notch, plant.natural_curve.freqs_hz, plant.natural_curve.magnitude_db
    )
    for target in family:
        delta = PASSIVE_RADIATOR_ADAPTER.predicted_response(plant, target, notch)
        assert np.max(delta - natural) <= 0.5 + 1e-12


def test_sealed_low_q_headroom_captures_peak_above_dc_boost():
    dc_boost = lt_boost_db(60.0, 40.0)
    margin = replace(MARGINS["normal"], boost_cap_db=dc_boost)
    family = SEALED_ADAPTER.generate_family(
        SealedPlantFit(60.0, 0.5, 0.0), margin=margin, n_targets=2
    )
    assert family[0].fp_hz == pytest.approx(40.0)
    assert family[0].qp == pytest.approx(0.65)
    assert family[0].boost_headroom_db > dc_boost


@pytest.mark.parametrize(
    "adapter,plant",
    (
        (PORTED_ADAPTER, PortedPlantFit(45.0, 68.0, 24.0, 0.5, NATURAL_CURVE)),
        (
            PASSIVE_RADIATOR_ADAPTER,
            PassiveRadiatorPlantFit(
                45.0, 68.0, 24.0, 0.5, NATURAL_CURVE, 27.0
            ),
        ),
    ),
)
def test_empirical_natural_target_predicted_response_round_trip(adapter, plant):
    target = adapter.generate_family(plant, margin=MARGINS["normal"])[-1]
    freqs = np.geomspace(10.0, 500.0, 257)
    expected = np.interp(
        freqs, plant.natural_curve.freqs_hz, plant.natural_curve.magnitude_db
    )
    actual = adapter.predicted_response(plant, target, freqs)
    assert np.max(np.abs(actual - expected)) < 0.1


def test_plant_fit_round_trips_are_strict():
    for fit_type, fit in (
        (SealedPlantFit, SealedPlantFit(61.0, 0.72, 0.2)),
        (PortedPlantFit, PortedPlantFit(45.0, 68.0, 24.0, 0.5, NATURAL_CURVE)),
        (
            PassiveRadiatorPlantFit,
            PassiveRadiatorPlantFit(
                45.0, 68.0, 24.0, 0.5, NATURAL_CURVE, 27.0
            ),
        ),
    ):
        assert fit_type.from_dict(fit.to_dict()) == fit
        with pytest.raises(ValueError, match="schema"):
            fit_type.from_dict({**fit.to_dict(), "unknown": 1})


def test_plant_fit_reconstruction_rejects_unrealizable_domains():
    with pytest.raises(ValueError, match="valid domain"):
        SealedPlantFit.from_dict(SealedPlantFit(61.0, 0.72, 0.2).to_dict() | {
            "q0": 0.01,
        })

    ported = PortedPlantFit(45.0, 68.0, 24.0, 0.5, NATURAL_CURVE)
    with pytest.raises(ValueError, match="valid domain"):
        PortedPlantFit.from_dict(ported.to_dict() | {"knee_hz": 40.0})
    string_curve = ported.to_dict()
    string_curve["natural_curve"]["magnitude_db"][0] = "-80.0"
    with pytest.raises(ValueError, match="numeric"):
        PortedPlantFit.from_dict(string_curve)

    pr = PassiveRadiatorPlantFit(
        45.0, 68.0, 24.0, 0.5, NATURAL_CURVE, 27.0
    )
    with pytest.raises(ValueError, match="values are invalid"):
        PassiveRadiatorPlantFit.from_dict(pr.to_dict() | {"notch_hz": 44.0})


@pytest.mark.parametrize("fit_type", (PortedPlantFit, PassiveRadiatorPlantFit))
def test_empirical_plant_fit_rejects_wrong_natural_curve_grid(fit_type):
    fit = (
        PortedPlantFit(45.0, 68.0, 24.0, 0.5, NATURAL_CURVE)
        if fit_type is PortedPlantFit
        else PassiveRadiatorPlantFit(
            45.0, 68.0, 24.0, 0.5, NATURAL_CURVE, 27.0
        )
    )
    payload = fit.to_dict()
    payload["natural_curve"]["freqs_hz"] = list(np.geomspace(1000.0, 2000.0, 96))
    with pytest.raises(ValueError):
        fit_type.from_dict(payload)


@pytest.mark.parametrize("fit_type", (PortedPlantFit, PassiveRadiatorPlantFit))
def test_empirical_plant_fit_rejects_unnormalized_natural_curve(fit_type):
    fit = (
        PortedPlantFit(45.0, 68.0, 24.0, 0.5, NATURAL_CURVE)
        if fit_type is PortedPlantFit
        else PassiveRadiatorPlantFit(
            45.0, 68.0, 24.0, 0.5, NATURAL_CURVE, 27.0
        )
    )
    payload = fit.to_dict()
    payload["natural_curve"]["magnitude_db"] = [
        value + 1.0 for value in payload["natural_curve"]["magnitude_db"]
    ]
    with pytest.raises(ValueError):
        fit_type.from_dict(payload)


def test_adapter_for_enclosure_mapping():
    assert adapter_for_enclosure("sealed") is SEALED_ADAPTER
    assert adapter_for_enclosure("vented") is PORTED_ADAPTER
    assert adapter_for_enclosure("passive_radiator") is PASSIVE_RADIATOR_ADAPTER
    assert adapter_for_enclosure("transmission_line") is None
