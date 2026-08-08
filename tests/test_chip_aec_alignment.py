# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import numpy as np
import pytest

from jasper import chip_aec_alignment as alignment
from jasper.capture_relay import alignment as relay_alignment
from jasper.chip_aec_alignment import (
    AlignmentArtifact,
    AlignmentIdentity,
    MicTiming,
    analyze_product,
    analyze_timing,
    artifact_from_dict,
    choose_delay,
    commissioning_stimulus,
    runtime_sys_delay,
)
from jasper.mics import xvf3800


def _identity() -> AlignmentIdentity:
    plan = xvf3800.SQUARE_FIXED_150_210_PLAN
    return AlignmentIdentity(
        "xvf3800_legacy_square_6ch",
        "XVF3800-001",
        "a1f70651",
        plan.plan_id,
        xvf3800.chip_aec_fixed_profile_fingerprint(plan),
        "apple_usb_c_dongle",
        "usb-serial:DWH53530FLL2FN3A3",
        "single:outputd_dac",
        "S16_LE",
        48_000,
        2,
        128,
        384,
    )


def test_runtime_delay_uses_k_minus_exact_eight_sample_median() -> None:
    assert runtime_sys_delay(245, [282, 283, 284, 283, 282, 284, 283, 283]) == -38


def test_runtime_delay_rejects_instability_and_out_of_range_without_clamp() -> None:
    with pytest.raises(ValueError, match="unstable"):
        runtime_sys_delay(245, [270, 271, 272, 273, 290, 291, 292, 293])
    with pytest.raises(ValueError, match="out of range"):
        runtime_sys_delay(1_000, [283] * 8)


def test_the_reference_window_is_the_precision_every_cadence_is_held_to() -> None:
    # The pre-#2253 rule was a literal pair — eight readings, spread 16 — and it
    # is now the reference point the rule is expressed in, so the fine-cadence
    # answer has to come out unchanged.
    assert alignment.required_queue_samples(alignment.QUEUE_MAX_SPREAD) == (
        alignment.QUEUE_SAMPLE_COUNT
    )
    assert alignment.required_queue_samples(0) == alignment.QUEUE_SAMPLE_COUNT
    # One frame past the reference spread already costs readings.
    assert alignment.required_queue_samples(alignment.QUEUE_MAX_SPREAD + 1) > (
        alignment.QUEUE_SAMPLE_COUNT
    )


def test_a_wider_spread_buys_its_precision_back_with_readings() -> None:
    # The median's sampling error scales as spread / sqrt(readings), so the
    # required count is the smallest one holding spread / sqrt(count) at or under
    # the reference window's ratio. Anything shallower would let a coarse cadence
    # publish a K measured less precisely than the one it has to agree with at
    # boot, so pin both directions: the count is sufficient, and one fewer is not.
    for spread in (17, 20, 43, 86, 172):
        needed = alignment.required_queue_samples(spread)
        assert spread**2 * alignment.QUEUE_SAMPLE_COUNT <= (
            alignment.QUEUE_MAX_SPREAD**2 * needed
        )
        assert spread**2 * alignment.QUEUE_SAMPLE_COUNT > (
            alignment.QUEUE_MAX_SPREAD**2 * (needed - 1)
        )
        # Quadratic, not linear: a linear rule would only double.
        assert alignment.required_queue_samples(2 * spread) > 3 * needed
    # jts3's measured spread, and what it costs: about five seconds of readings
    # at one per 21.3 ms mix period, inside the 30 s collection budget.
    assert alignment.required_queue_samples(86) == 232


def test_a_window_is_stable_only_once_it_has_bought_that_precision() -> None:
    jts3_window = [362 + (index * 86) % 87 for index in range(232)]
    assert max(jts3_window) - min(jts3_window) == 86

    assert alignment.queue_window_is_stable(jts3_window)
    assert not alignment.queue_window_is_stable(jts3_window[:231])
    assert not alignment.queue_window_is_stable([])
    # The same readings are what boot feeds back in, so the re-validation has to
    # accept exactly what the collector accepted — one rule, two callers.
    assert runtime_sys_delay(400, jts3_window) == 400 - alignment.median_samples(
        jts3_window
    )
    with pytest.raises(ValueError, match="unstable"):
        runtime_sys_delay(400, jts3_window[:231])


def test_artifact_is_strict_identity_plus_k_only() -> None:
    artifact = AlignmentArtifact(_identity(), 245)
    assert artifact_from_dict(artifact.to_dict()) == artifact

    legacy = artifact.to_dict() | {"schema": 1}
    with pytest.raises(ValueError, match="unsupported"):
        artifact_from_dict(legacy)

    expanded = json.loads(json.dumps(artifact.to_dict()))
    expanded["timing_trials"] = [1, 2, 3]
    with pytest.raises(ValueError, match="schema"):
        artifact_from_dict(expanded)


def test_global_delay_centers_all_four_mics_with_strong_edge_margin() -> None:
    evidence = tuple(
        MicTiming(mic, -38, (lag, lag, lag))
        for mic, lag in enumerate((21, 18, 20, 22))
    )

    selected = choose_delay(evidence)

    assert selected.sys_delay == -38
    assert selected.projected_lags == (21, 18, 20, 22)
    assert selected.worst_edge_margin == 17


def test_timing_analyzer_reports_unambiguous_causal_lag() -> None:
    _stereo, reference, active = commissioning_stimulus()
    capture = np.zeros((32_000, 6), dtype="<i2")
    marker_start = 4_000
    capture[marker_start : marker_start + len(reference), 1] = reference
    active_start = marker_start + 5_600
    capture[active_start + 20 : active_start + 20 + len(active), 0] = active

    result = analyze_timing(capture, reference)

    assert result.lag == 20
    assert result.peak > 0.99
    assert result.peak_ratio >= 1.10
    assert result.clipped_samples == 0


def test_timing_correlation_removes_filtered_window_mean(monkeypatch) -> None:
    _stereo, reference, active = commissioning_stimulus()
    capture = np.zeros((32_000, 6), dtype="<i2")
    marker_start = 4_000
    capture[marker_start : marker_start + len(reference), 1] = reference
    active_start = marker_start + 5_600
    capture[active_start + 20 : active_start + 20 + len(active), 0] = active
    real_bandpass = alignment._bandpass
    monkeypatch.setattr(
        alignment, "_bandpass", lambda values: real_bandpass(values) + 50_000
    )
    observed_means: list[tuple[float, float]] = []
    real_correlate = relay_alignment.cross_correlation_alignment

    def observe_means(captured, stimulus, **kwargs):
        if kwargs.get("exclude_radius") == 8:
            observed_means.append((float(captured.mean()), float(stimulus.mean())))
        return real_correlate(captured, stimulus, **kwargs)

    monkeypatch.setattr(relay_alignment, "cross_correlation_alignment", observe_means)

    assert analyze_timing(capture, reference).lag == 20
    assert len(observed_means) == 1
    assert all(abs(value) < 1e-8 for value in observed_means[0])


def test_product_analyzer_requires_both_beams_and_all_raw_mics() -> None:
    _stereo, _reference, active = commissioning_stimulus()
    rng = np.random.default_rng(7)
    on = rng.integers(-10, 11, size=(32_000, 6), dtype=np.int16)
    off = on.copy()
    start = 7_000
    for raw in range(2, 6):
        on[start : start + len(active), raw] = active
        off[start : start + len(active), raw] = active
    for beam in (0, 1):
        off[start : start + len(active), beam] = active
        on[start : start + len(active), beam] = (active.astype(np.int32) // 20).astype(
            np.int16
        )

    result = analyze_product(on, off, active)

    assert result.minimum_raw_excess_snr_db > 10
    assert min(result.beam_acquisition_db) > 8
    assert min(result.beam_suppression_db) > 20
    assert result.clipped_samples == 0
