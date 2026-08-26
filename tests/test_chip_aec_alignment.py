# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from dataclasses import replace

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
    window = [282, 283, 284, 283, 282, 284, 283, 283]
    assert runtime_sys_delay(245, window, commissioned_sys_delay=-38) == -38


def test_runtime_delay_rejects_instability_and_out_of_range_without_clamp() -> None:
    with pytest.raises(ValueError, match="unstable"):
        runtime_sys_delay(
            245, [270, 271, 272, 273, 290, 291, 292, 293], commissioned_sys_delay=-38
        )
    # Out of range is reachable only from a commissioned delay near the
    # chip's own ceiling, since the median bound below keeps the two within
    # MIN_EDGE_MARGIN of each other.
    with pytest.raises(ValueError, match="out of range"):
        runtime_sys_delay(
            283 + xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1,
            [283] * 8,
            commissioned_sys_delay=xvf3800.CHIP_AEC_SYS_DELAY_MAX,
        )


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
    commissioned = 400 - alignment.median_samples(jts3_window)
    assert (
        runtime_sys_delay(400, jts3_window, commissioned_sys_delay=commissioned)
        == commissioned
    )
    with pytest.raises(ValueError, match="unstable"):
        runtime_sys_delay(
            400, jts3_window[:231], commissioned_sys_delay=commissioned
        )


def test_precision_and_drift_are_independent_conditions() -> None:
    # Neither implies the other, so a window has to clear both. A tight window
    # that stepped is precise and wrong; a wide still one is imprecise and
    # honest. Pinned because collapsing the two would leave the survivor
    # looking like it guards both.
    stepped = tuple([300] * 200 + [300 + alignment.QUEUE_MAX_MEDIAN_DRIFT + 1] * 200)
    assert len(stepped) >= alignment.required_queue_samples(max(stepped) - min(stepped))
    assert not alignment.queue_window_is_stable(stepped)

    # Two identical halves: the readings cover the whole 86-frame spread and
    # the median provably did not move.
    sweep = [362 + (index * 86) // 57 for index in range(58)]
    wide_but_still = tuple(sweep + sweep)
    assert max(wide_but_still) - min(wide_but_still) == 86
    assert alignment.queue_median_drift(wide_but_still) == 0
    assert not alignment.queue_window_is_stable(wide_but_still), "too few readings"


def test_boot_bounds_the_delay_against_the_commissioned_one_both_ways() -> None:
    # K = commissioned SYS_DELAY + commissioned median, so the gap between the
    # delay boot resolves and the commissioned one IS the two windows' median
    # difference — the only error term between the alignment the commissioner
    # verified and what boot applies. choose_delay reserves MIN_EDGE_MARGIN
    # frames on both causal-window edges, so that is exactly how far it may
    # move. Pin the acceptance boundary on both sides, and in both directions.
    window = [283] * 8
    for offset in (
        -alignment.MIN_EDGE_MARGIN,
        0,
        alignment.MIN_EDGE_MARGIN,
    ):
        # A live median `offset` frames BELOW the commissioned one resolves a
        # delay `offset` frames above it, and vice versa.
        assert (
            runtime_sys_delay(245, window, commissioned_sys_delay=-38 - offset)
            == -38
        )
    # Past the margin the delay is still handed out — it cleared the chip's own
    # range — so boot applies it and discloses (ADR-0101) instead of stopping.
    for offset in (
        -alignment.MIN_EDGE_MARGIN - 1,
        alignment.MIN_EDGE_MARGIN + 1,
    ):
        with pytest.raises(alignment.QueueMovedFromCommissioned) as moved:
            runtime_sys_delay(245, window, commissioned_sys_delay=-38 - offset)
        assert moved.value.delay == -38


def test_the_driver_cap_is_checked_before_the_margin_it_makes_safe() -> None:
    # The margin is a disclosure; CHIP_AEC_SYS_DELAY_MIN..MAX is the declared
    # driver cap and stays a refusal. A delay outside the cap must therefore
    # never leave here as a QueueMovedFromCommissioned a caller would apply.
    out_of_range = xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1
    with pytest.raises(ValueError) as refused:
        runtime_sys_delay(
            283 + out_of_range,
            [283] * 8,
            commissioned_sys_delay=xvf3800.CHIP_AEC_SYS_DELAY_MIN,
        )
    assert not isinstance(refused.value, alignment.QueueMovedFromCommissioned)


def test_a_malformed_artifact_is_not_merely_a_superseded_one() -> None:
    # The exact-field-set check still rejects an artifact missing the
    # commissioned SYS_DELAY. Only a recognisable OLDER SCHEMA earns
    # ArtifactSchemaSuperseded, which hands its banked K out to be applied —
    # a shape nobody can validate must not reach that path.
    artifact = AlignmentArtifact(_identity(), 245, -38)
    older = artifact.to_dict()
    del older["sys_delay"]

    with pytest.raises(ValueError) as invalid:
        artifact_from_dict(older)
    assert not isinstance(invalid.value, alignment.ArtifactSchemaSuperseded)

    with pytest.raises(ValueError, match="out of range"):
        AlignmentArtifact(_identity(), 245, xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1)


def test_artifact_is_strict_identity_plus_k_only() -> None:
    artifact = AlignmentArtifact(_identity(), 245, -38)
    assert artifact_from_dict(artifact.to_dict()) == artifact

    # An older schema still banks a usable K, so it is handed out for boot to
    # apply and disclose rather than parking the box on a version number.
    legacy = artifact.to_dict() | {"schema": 1}
    with pytest.raises(alignment.ArtifactSchemaSuperseded) as superseded:
        artifact_from_dict(legacy)
    assert (superseded.value.k_samples, superseded.value.sys_delay) == (245, -38)

    # ...but only when what it banked survives the same checks the current
    # schema applies.
    unusable = artifact.to_dict() | {
        "schema": 1,
        "sys_delay": xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1,
    }
    with pytest.raises(ValueError) as invalid:
        artifact_from_dict(unusable)
    assert not isinstance(invalid.value, alignment.ArtifactSchemaSuperseded)

    # A schema from the FUTURE is what a rollback leaves behind
    # (JASPER_DEPLOY_ALLOW_DOWNGRADE). This build cannot know what its K means,
    # so it refuses rather than applying it under a false "predates" message —
    # and a non-integer schema is not an ordering at all.
    for schema in (alignment.ARTIFACT_SCHEMA + 1, "3", 3.0, None):
        with pytest.raises(ValueError) as newer:
            artifact_from_dict(artifact.to_dict() | {"schema": schema})
        assert not isinstance(newer.value, alignment.ArtifactSchemaSuperseded)

    # Same schema, mangled identity field-set: a shape this build DOES claim to
    # read but cannot, so it stays a hard refusal too.
    mangled = artifact.to_dict()
    mangled["identity"] = {
        name: v for name, v in mangled["identity"].items() if name != "output_format"
    }
    with pytest.raises(ValueError) as broken:
        artifact_from_dict(mangled)
    assert not isinstance(broken.value, alignment.ArtifactSchemaSuperseded)

    expanded = json.loads(json.dumps(artifact.to_dict()))
    expanded["timing_trials"] = [1, 2, 3]
    with pytest.raises(ValueError) as extra:
        artifact_from_dict(expanded)
    assert not isinstance(extra.value, alignment.ArtifactSchemaSuperseded)


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, ()),
        ({"xvf_serial": "XVF3800-002"}, ("xvf_serial",)),
        (
            {"xvf_serial": "XVF3800-002", "output_hardware_key": "usb-serial:OTHER"},
            ("xvf_serial", "output_hardware_key"),
        ),
        (
            {"output_format": "S32_LE", "xvf_serial": "XVF3800-002"},
            ("xvf_serial", "output_format"),
        ),
    ],
)
def test_identity_divergence_reports_every_moved_field(changes, expected) -> None:
    # The field names ARE the disclosure, and the caller splits them against
    # PER_UNIT_IDENTITY_FIELDS to decide how loud it is — so both the set and
    # its declaration order are the contract.
    commissioned = _identity()
    assert (
        alignment.identity_divergence(commissioned, replace(commissioned, **changes))
        == expected
    )
    assert alignment.PER_UNIT_IDENTITY_FIELDS < set(
        AlignmentIdentity.__dataclass_fields__
    )


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
