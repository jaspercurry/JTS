# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement.transfer_composition import (
    CONDITIONING_FLOOR_AMPLITUDE,
    CONDITIONING_RATIO_CAP_AMPLITUDE,
    ILL_CONDITIONED_REASON,
    NONFINITE_REASON,
    LinearTransferSection,
    TotalTransferCompositionError,
    compose_total_transfer,
    linkwitz_riley_response_complex,
    transfer_policy_payload,
)
from jasper.audio_measurement.program_analysis import (
    DriverResponse,
    predicted_branch_sum,
    solve_branch_trims,
)


def _sections():
    return {
        "woofer": (
            LinearTransferSection(True, 150.0),
            LinearTransferSection(False, 6000.0),
        ),
        "tweeter": (
            LinearTransferSection(True, 1600.0),
            LinearTransferSection(False, 23000.0),
        ),
    }, {
        "woofer": (LinearTransferSection(False, 2000.0),),
        "tweeter": (LinearTransferSection(True, 2000.0),),
    }


def test_p_equals_c_identity_ratio_subfixture_is_exact():
    M = np.asarray([1 + 2j, -0.5 + 0.25j], dtype=np.complex128)
    P = np.asarray([0.5 + 0j, 0.25 + 0.25j], dtype=np.complex128)
    result = compose_total_transfer(M, P.copy(), P, required_mask=np.ones(2, bool))
    np.testing.assert_array_equal(result.ratio, np.ones(2, dtype=np.complex128))
    np.testing.assert_array_equal(result.fitter_input, M)


@pytest.mark.parametrize("role", ["woofer", "tweeter"])
def test_same_plant_configured_fc_equivalence_with_role_specific_p(role: str):
    protection, configured = _sections()
    freqs = np.geomspace(170.0 if role == "woofer" else 1700.0, 5000.0 if role == "woofer" else 18000.0, 257)
    phase = np.exp(-1j * 2.0 * np.pi * freqs * 0.00031)
    plant = (0.7 + 0.2 * np.cos(np.log(freqs))) * phase
    P = linkwitz_riley_response_complex(freqs, protection[role])
    C = linkwitz_riley_response_complex(freqs, configured[role])
    result = compose_total_transfer(
        plant * P, C, P, required_mask=np.ones(freqs.size, dtype=bool)
    )
    legacy = plant * C
    assert np.max(np.abs(result.fitter_input - legacy)) <= 1e-6
    new_db = 20.0 * np.log10(np.maximum(np.abs(result.fitter_input), 1e-12))
    old_db = 20.0 * np.log10(np.maximum(np.abs(legacy), 1e-12))
    assert np.max(np.abs(new_db - old_db)) <= 1e-6


def test_same_plant_configured_fc_downstream_fit_and_sum_tolerances():
    """Pin every deterministic tolerance after the configured-Fc transform."""

    from jasper.active_speaker.linearization_envelope import compose_envelope
    from jasper.active_speaker.linearization_fit import (
        complex_correction_response,
        fit_driver_linearization,
    )

    protection, configured = _sections()
    freqs = np.geomspace(1800.0, 5000.0, 257)
    plant = (
        0.72
        + 0.18 * np.exp(-0.5 * (np.log2(freqs / 2800.0) / 0.18) ** 2)
    ) * np.exp(-1j * 2.0 * np.pi * freqs * 0.00031)
    new_by_role = {}
    legacy_by_role = {}
    fits = {}
    for role in ("woofer", "tweeter"):
        P = linkwitz_riley_response_complex(freqs, protection[role])
        C = linkwitz_riley_response_complex(freqs, configured[role])
        transformed = compose_total_transfer(
            plant * P,
            C,
            P,
            required_mask=np.ones(freqs.size, dtype=bool),
        ).fitter_input
        legacy = plant * C
        new_by_role[role] = transformed
        legacy_by_role[role] = legacy

        role_fits = []
        for values in (transformed, legacy):
            magnitude = 20.0 * np.log10(np.maximum(np.abs(values), 1e-12))
            response = DriverResponse(
                role=role,
                freqs_hz=freqs,
                magnitude_db=magnitude,
                complex_tf=values,
                gating={},
                snr=None,
                validity_floor_hz=1800.0,
                repeat_responses=(),
            )
            envelope = compose_envelope(
                role,
                response,
                excited_band_hz=(1800.0, 5000.0),
                mic_tier="reference",
                driver_class="unknown",
            )
            role_fits.append(fit_driver_linearization(response, envelope))
        fits[role] = tuple(role_fits)

        new_fit, legacy_fit = role_fits
        assert new_fit.role == legacy_fit.role == role
        assert new_fit.reason_summary == legacy_fit.reason_summary
        assert (new_fit.fit_band_hz != (0.0, 0.0)) == (
            legacy_fit.fit_band_hz != (0.0, 0.0)
        )
        assert len(new_fit.filters) == len(legacy_fit.filters)
        for new_filter, legacy_filter in zip(new_fit.filters, legacy_fit.filters):
            assert new_filter.biquad_type == legacy_filter.biquad_type
            for field in ("freq", "q", "gain"):
                assert abs(getattr(new_filter, field) - getattr(legacy_filter, field)) <= 1e-6
        for field in (
            "target_level_db",
            "residual_rms_db",
            "residual_max_db",
            "headroom_cost_db",
        ):
            assert abs(getattr(new_fit, field) - getattr(legacy_fit, field)) <= 1e-6
        new_correction = complex_correction_response(new_fit.filters, freqs)
        legacy_correction = complex_correction_response(legacy_fit.filters, freqs)
        assert np.max(np.abs(new_correction - legacy_correction)) <= 1e-6

    new_trim = solve_branch_trims(
        freqs,
        new_by_role["woofer"],
        new_by_role["tweeter"],
        2000.0,
        woofer_span_hz=(1800.0, 5000.0),
        tweeter_span_hz=(1800.0, 5000.0),
    )
    legacy_trim = solve_branch_trims(
        freqs,
        legacy_by_role["woofer"],
        legacy_by_role["tweeter"],
        2000.0,
        woofer_span_hz=(1800.0, 5000.0),
        tweeter_span_hz=(1800.0, 5000.0),
    )
    np.testing.assert_allclose(new_trim, legacy_trim, rtol=0.0, atol=1e-6)
    new_sum = predicted_branch_sum(
        new_by_role["woofer"],
        new_by_role["tweeter"],
        new_trim[0],
        new_trim[1],
        1,
    )
    legacy_sum = predicted_branch_sum(
        legacy_by_role["woofer"],
        legacy_by_role["tweeter"],
        legacy_trim[0],
        legacy_trim[1],
        1,
    )
    new_sum_db = 20.0 * np.log10(np.maximum(np.abs(new_sum), 1e-12))
    legacy_sum_db = 20.0 * np.log10(np.maximum(np.abs(legacy_sum), 1e-12))
    assert np.max(np.abs(new_sum_db - legacy_sum_db)) <= 1e-6


def test_conditioning_boundaries_are_inclusive_and_never_repaired():
    mask = np.ones(1, dtype=bool)
    floor = np.asarray([CONDITIONING_FLOOR_AMPLITUDE + 0j])
    cap = np.asarray([CONDITIONING_FLOOR_AMPLITUDE * CONDITIONING_RATIO_CAP_AMPLITUDE + 0j])
    allowed = compose_total_transfer(np.ones(1), cap, floor, required_mask=mask)
    assert abs(allowed.ratio[0]) == pytest.approx(CONDITIONING_RATIO_CAP_AMPLITUDE)

    with pytest.raises(TotalTransferCompositionError) as floor_error:
        compose_total_transfer(
            np.ones(1), floor,
            np.asarray([np.nextafter(CONDITIONING_FLOOR_AMPLITUDE, 0.0) + 0j]),
            required_mask=mask,
        )
    assert floor_error.value.reason == ILL_CONDITIONED_REASON

    with pytest.raises(TotalTransferCompositionError) as cap_error:
        compose_total_transfer(
            np.ones(1),
            np.asarray([
                np.nextafter(
                    CONDITIONING_FLOOR_AMPLITUDE * CONDITIONING_RATIO_CAP_AMPLITUDE,
                    np.inf,
                ) + 0j
            ]),
            floor,
            required_mask=mask,
        )
    assert cap_error.value.reason == ILL_CONDITIONED_REASON


def test_nonfinite_required_bin_refuses_and_mask_fingerprint_is_bound():
    M = np.asarray([1 + 0j, np.nan + 0j])
    P = np.ones(2, dtype=np.complex128)
    C = np.ones(2, dtype=np.complex128)
    first_only = compose_total_transfer(
        M, C, P, required_mask=np.asarray([True, False])
    )
    assert first_only.fitter_input[1] == 0j
    with pytest.raises(TotalTransferCompositionError) as exc:
        compose_total_transfer(M, C, P, required_mask=np.ones(2, dtype=bool))
    assert exc.value.reason == NONFINITE_REASON
    assert first_only.fingerprints["mask"] != compose_total_transfer(
        np.ones(2), C, P, required_mask=np.ones(2, dtype=bool)
    ).fingerprints["mask"]


def test_policy_fingerprint_pins_formula_boundaries_and_no_hardware_inverse():
    policy = transfer_policy_payload()
    assert policy["formula"] == "S=M*C_configured/P"
    assert policy["configured_transfer_semantics"] == "complete_replacement_for_P"
    assert policy["hardware_emits_inverse"] is False
    assert policy["boundary_rule"] == "equal_to_floor_or_cap_is_allowed"
    assert policy["repair_policy"] == "reject_without_clipping_interpolation_or_bin_omission"
