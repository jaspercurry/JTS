# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The in-situ bass fit: the seat-cube median in, one target family out.

The median here is a MEASUREMENT, not a model: a driver's second-order roll-off
under a gentle low shelf standing in for room gain, plus seeded noise. Room
gain is included on purpose (ADR-0260 section 3), so what the fit recovers is
the in-room plant, not the datasheet one — the bars below are what that costs.
"""

from __future__ import annotations

import numpy as np
import pytest

from jasper.bass_extension.adapters.base import (
    CabinetInfo,
    CaptureRole,
    MagnitudeCurve,
    woofer_curve,
)
from jasper.bass_extension.adapters.sealed import SEALED_ADAPTER, SealedPlantFit
from jasper.bass_extension.alignment import (
    butterworth_highpass_db,
    low_shelf_response_db,
    second_order_highpass_db,
)
from jasper.bass_extension.profile import BassExtensionRefusal
from jasper.bass_extension.seat_fit import (
    DeclaredPlant,
    SeatFitRefused,
    SeatMedian,
    bass_owner_target,
    cabinet_of,
    fit_seat_median,
    read_seat_median,
)
from jasper.bass_extension.targets import MARGINS

#: 20-320 Hz: the commissioning floor up past the ceiling a seat cube carries.
FREQS = np.geomspace(20.0, 320.0, 120)
CEILING_HZ = 300.0
CABINET = CabinetInfo("sealed", 1, 165.0, 220.0)
MARGIN = MARGINS["conservative"]

#: Room gain as a low shelf (Hz, Q, dB) and the seeded measurement noise.
ROOM_GAIN_HZ, ROOM_GAIN_Q, ROOM_GAIN_DB = 35.0, 0.5, 2.0
NOISE_SIGMA_DB = 0.4
NOISE_SEED = 20260908

SEALED_TARGET = {
    "target_id": "main:woofer",
    "role": "woofer",
    "hard_excitation_band_hz": [30.0, 300.0],
    "cabinet": {
        "enclosure_kind": CABINET.enclosure_kind,
        "radiator_count": CABINET.radiator_count,
        "effective_radiating_diameter_mm": CABINET.effective_radiating_diameter_mm,
        "baffle_width_mm": CABINET.baffle_width_mm,
    },
}


def _in_room(shape: np.ndarray) -> np.ndarray:
    """One driver shape as a seat-cube median: room gain, then noise."""
    noise = np.random.default_rng(NOISE_SEED).normal(0.0, NOISE_SIGMA_DB, FREQS.size)
    shelf = low_shelf_response_db(FREQS, ROOM_GAIN_HZ, ROOM_GAIN_Q, ROOM_GAIN_DB)
    return shape + shelf + noise


def seat_median_db(f0_hz: float, q0: float) -> np.ndarray:
    """The median a sealed plant at ``f0_hz``/``q0`` leaves at the seats."""
    return _in_room(second_order_highpass_db(FREQS, f0_hz, q0))


def _third_order_db() -> np.ndarray:
    """A roll-off no sealed plant explains: the leakage the fit refuses over."""
    return _in_room(butterworth_highpass_db(FREQS, 61.0, 3))


def median_document(magnitude_db: np.ndarray) -> dict:
    """One ``room_median.json`` payload, in the shape the view writes."""
    return {
        "freqs_hz": FREQS.tolist(),
        "median_db": magnitude_db.tolist(),
        "spread_db": [1.5] * FREQS.size,
        "n_positions": 9,
        "positions": [{"id": "seat-a", "deviation_db": [0.0] * FREQS.size}],
        "ceiling_hz": CEILING_HZ,
        "ceiling_source": "applied_candidate",
        "window": "ungated",
    }


def _median(magnitude_db: np.ndarray) -> SeatMedian:
    return SeatMedian(
        freqs_hz=FREQS, median_db=magnitude_db, ceiling_hz=CEILING_HZ,
        ceiling_source="applied_candidate", n_positions=9,
    )


def _fit(median: SeatMedian, **overrides):
    return fit_seat_median(median, **{
        "adapter": SEALED_ADAPTER, "cabinet": CABINET, "margin": MARGIN,
        "declared": None, "owner_role": "woofer",
        "owner_target_id": "main:woofer",
        **overrides,
    })


@pytest.mark.parametrize(
    "f0_hz,q0", ((38.0, 0.9), (45.0, 0.707), (55.0, 0.8), (61.0, 0.6)),
)
def test_the_seat_median_recovers_the_plant_that_made_it(f0_hz, q0):
    plant = _fit(_median(seat_median_db(f0_hz, q0))).effective_plant

    assert plant["source"] == "seat_median_fit"
    assert plant["f0_hz"] == pytest.approx(f0_hz, rel=0.10)
    assert plant["q0"] == pytest.approx(q0, abs=0.15)
    assert plant["fit_rms_db"] >= 0.0


def test_the_family_runs_deepest_first_and_ends_at_the_natural_alignment():
    fit = _fit(_median(seat_median_db(45.0, 0.707)))
    natural = fit.rungs[-1]

    assert len(fit.rungs) > 1
    assert natural.target.filters == ()
    assert natural.lt_boost_db == 0.0
    assert all(rung.lt_boost_db > 0.0 for rung in fit.rungs[:-1])
    assert [rung.target.fp_hz for rung in fit.rungs] == sorted(
        rung.target.fp_hz for rung in fit.rungs
    )
    levels = [rung.max_listening_level for rung in reversed(fit.rungs)]
    assert levels == sorted(levels, reverse=True)


def test_the_model_curve_is_published_beside_the_median_it_was_fitted_to():
    fit = _fit(_median(seat_median_db(45.0, 0.707)))
    curve = fit.curve

    assert set(curve) == {"freqs_hz", "median_smoothed_db", "model_db"}
    assert len({len(values) for values in curve.values()}) == 1
    assert max(curve["freqs_hz"]) <= CEILING_HZ
    assert fit.ceiling_source == "applied_candidate"
    assert fit.n_positions == 9
    # Both published curves are levelled on the one passband rule the adapters
    # level on, so they can be read against each other.
    passband = np.asarray(curve["freqs_hz"]) >= 200.0
    for published in ("median_smoothed_db", "model_db"):
        assert float(np.mean(np.asarray(curve[published])[passband])) == pytest.approx(
            0.0, abs=1e-9
        )


def test_a_roll_off_no_sealed_plant_explains_refuses_without_a_declared_one():
    with pytest.raises(SeatFitRefused) as refused:
        _fit(_median(_third_order_db()))

    assert refused.value.reason == BassExtensionRefusal.PLANT_UNRESOLVED


def test_the_declared_plant_stands_in_and_the_refusal_it_stood_in_for_is_disclosed():
    fit = _fit(_median(_third_order_db()), declared=DeclaredPlant(52.0, 0.68))

    assert fit.effective_plant["source"] == "declared"
    assert fit.effective_plant["f0_hz"] == 52.0
    assert fit.effective_plant["q0"] == 0.68
    assert fit.effective_plant["fit_rms_db"] is None
    assert set(fit.fit_refusal or {}) == {"refusal", "detail"}


@pytest.mark.parametrize("declared", (DeclaredPlant(8.0, 0.7), DeclaredPlant(52.0, 2.0)))
def test_a_declared_plant_outside_the_adapters_own_domain_refuses(declared):
    with pytest.raises(SeatFitRefused) as refused:
        _fit(_median(_third_order_db()), declared=declared)

    assert refused.value.reason == BassExtensionRefusal.PLANT_UNRESOLVED
    assert refused.value.detail["problem"] == "declared_plant_outside_domain"


@pytest.mark.parametrize("mutation,problem", (
    ({"freqs_hz": None}, "not_a_list"),
    ({"freqs_hz": [1.0, 2.0, "3"]}, "not_finite_numeric"),
    ({"median_db": [0.0, 0.0]}, "length_mismatch"),
    ({"freqs_hz": [30.0, 20.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0],
      "median_db": [0.0] * 8}, "not_positive_ascending"),
    ({"ceiling_hz": 0.0}, "not_positive"),
    ({"ceiling_hz": None}, "not_positive"),
    ({"ceiling_hz": 22.0}, "too_few_points_below_ceiling"),
    ({"n_positions": 0}, "not_a_positive_int"),
    ({"n_positions": True}, "not_a_positive_int"),
    ({"ceiling_source": None}, "ceiling_source_must_be_text"),
))
def test_a_median_document_that_is_not_a_curve_is_unreadable(mutation, problem):
    payload = {**median_document(seat_median_db(45.0, 0.707)), **mutation}

    with pytest.raises(SeatFitRefused) as refused:
        read_seat_median(payload)

    assert refused.value.reason == BassExtensionRefusal.MEDIAN_UNREADABLE
    assert refused.value.detail["problem"] == problem


def test_a_well_formed_median_document_reads_back_as_its_own_grid():
    median = read_seat_median(median_document(seat_median_db(45.0, 0.707)))

    assert median.ceiling_hz == CEILING_HZ
    assert median.ceiling_source == "applied_candidate"
    assert median.n_positions == 9
    assert median.freqs_hz.shape == FREQS.shape


@pytest.mark.parametrize("target,reason", (
    ({"target_id": "main:woofer", "role": "woofer"},
     BassExtensionRefusal.ENCLOSURE_UNKNOWN),
    ({"target_id": "main:woofer", "role": "woofer", "cabinet": {}},
     BassExtensionRefusal.ENCLOSURE_UNSUPPORTED),
    ({"target_id": "main:woofer", "role": "woofer",
      "cabinet": {"enclosure_kind": "open_baffle"}},
     BassExtensionRefusal.ENCLOSURE_UNSUPPORTED),
    ({"target_id": "main:woofer", "role": "woofer",
      "cabinet": {"enclosure_kind": "passive_radiator"}},
     BassExtensionRefusal.ENCLOSURE_UNSUPPORTED),
))
def test_a_target_declaring_no_usable_cabinet_refuses(target, reason):
    with pytest.raises(SeatFitRefused) as refused:
        cabinet_of(target)

    assert refused.value.reason == reason


def test_the_declared_cabinet_picks_its_adapter_and_carries_its_geometry():
    adapter, cabinet = cabinet_of(SEALED_TARGET)

    assert adapter.adapter_id == SealedPlantFit.adapter_id
    assert cabinet == CABINET


@pytest.mark.parametrize("targets,expected", (
    ((
        {"target_id": "main:woofer", "role": "woofer",
         "hard_excitation_band_hz": [40.0, 300.0]},
        {"target_id": "sub:subwoofer", "role": "subwoofer",
         "hard_excitation_band_hz": [80.0, 200.0]},
    ), "sub:subwoofer"),
    ((
        {"target_id": "main:tweeter", "role": "tweeter",
         "hard_excitation_band_hz": [2000.0, 16000.0]},
        {"target_id": "main:woofer", "role": "woofer",
         "hard_excitation_band_hz": [40.0, 300.0]},
    ), "main:woofer"),
    ((
        {"target_id": "main:tweeter", "role": "tweeter"},
        {"target_id": "main:woofer", "role": "woofer",
         "hard_excitation_band_hz": [40.0, 300.0]},
    ), "main:woofer"),
))
def test_the_bass_owner_is_the_subwoofer_else_the_lowest_band_floor(targets, expected):
    assert bass_owner_target({"targets": targets})["target_id"] == expected


@pytest.mark.parametrize("profile,reason", (
    ({}, BassExtensionRefusal.ENCLOSURE_UNKNOWN),
    ({"targets": []}, BassExtensionRefusal.ENCLOSURE_UNKNOWN),
    ({"targets": "no"}, BassExtensionRefusal.ENCLOSURE_UNKNOWN),
    ({"targets": [
        {"target_id": "left:subwoofer", "role": "subwoofer"},
        {"target_id": "right:subwoofer", "role": "subwoofer"},
    ]}, BassExtensionRefusal.BASS_OWNER_AMBIGUOUS),
))
def test_a_profile_that_names_no_single_bass_owner_refuses(profile, reason):
    with pytest.raises(SeatFitRefused) as refused:
        bass_owner_target(profile)

    assert refused.value.reason == reason


def test_the_seat_median_is_the_woofer_curve_the_adapters_read():
    nearfield = MagnitudeCurve((20.0,), (0.0,))
    seat = MagnitudeCurve((30.0,), (1.0,))

    assert woofer_curve({CaptureRole.WOOFER_NEARFIELD: nearfield}) is nearfield
    assert woofer_curve({CaptureRole.SEAT_MEDIAN: seat}) is seat
    assert woofer_curve({
        CaptureRole.WOOFER_NEARFIELD: nearfield, CaptureRole.SEAT_MEDIAN: seat,
    }) is seat
    assert woofer_curve({}) is None
