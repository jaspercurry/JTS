# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import numpy as np
import pytest

from jasper.bass_extension.alignment import (
    linkwitz_transform_params,
    low_shelf_response_db,
    lt_boost_db,
    lt_response_db,
    peaking_response_db,
    second_order_highpass_db,
)


@pytest.mark.parametrize(
    ("f0", "fp", "expected"),
    ((61.0, 31.0, 11.75), (40.0, 20.0, 12.04), (50.0, 35.0, 6.20)),
)
def test_lt_boost_worked_examples(f0, fp, expected):
    assert lt_boost_db(f0, fp) == pytest.approx(expected, abs=0.02)


def test_lt_boost_identity_is_zero():
    assert lt_boost_db(50.0, 50.0) == pytest.approx(0.0)


@pytest.mark.parametrize("qp", (0.5, 0.65, 0.71))
def test_lt_dc_limit_and_exact_pole_replacement(qp):
    freqs = np.geomspace(1e-4, 500.0, 2000)
    f0, q0, fp = 61.0, 0.72, 31.0
    lt = lt_response_db(freqs, f0, q0, fp, qp)
    assert lt[0] == pytest.approx(lt_boost_db(f0, fp), abs=0.1)
    transformed = second_order_highpass_db(freqs, f0, q0) + lt
    target = second_order_highpass_db(freqs, fp, qp)
    assert np.max(np.abs(transformed - target)) < 0.05


@pytest.mark.parametrize(
    "args",
    (
        (40.0, 0.7, 50.0, 0.65),
        (float("nan"), 0.7, 30.0, 0.65),
        (40.0, 0.2, 30.0, 0.65),
        (40.0, 0.7, 30.0, 1.3),
    ),
)
def test_linkwitz_transform_params_rejects_invalid_domain(args):
    with pytest.raises(ValueError):
        linkwitz_transform_params(*args)


def test_linkwitz_transform_params_exact_camilla_shape():
    assert linkwitz_transform_params(61.0, 0.72, 31.0, 0.65) == {
        "type": "LinkwitzTransform",
        "freq_act": 61.0,
        "q_act": 0.72,
        "freq_target": 31.0,
        "q_target": 0.65,
    }


def test_peaking_analog_prototype_center_and_asymptotes():
    response = peaking_response_db(
        np.asarray((1e-4, 61.0, 1e7)), 61.0, 0.7, 6.0
    )
    assert response == pytest.approx((0.0, 6.0, 0.0), abs=1e-6)


def test_low_shelf_analog_prototype_limits_and_center():
    response = low_shelf_response_db(
        np.asarray((1e-4, 61.0, 1e7)), 61.0, 0.7, 6.0
    )
    assert response == pytest.approx((6.0, 3.0, 0.0), abs=1e-6)
