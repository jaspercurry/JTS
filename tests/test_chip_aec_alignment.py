# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

import numpy as np
import pytest

from jasper.chip_aec import alignment as alignment
from jasper.chip_aec import shipped as shipped
from jasper.audio_measurement import alignment as kernel_alignment
from jasper.chip_aec.alignment import (
    AlignmentArtifact,
    AlignmentIdentity,
    MicTiming,
    TimingRejected,
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
    assert runtime_sys_delay(245, window) == -38


def test_runtime_delay_rejects_instability_and_out_of_range_without_clamp() -> None:
    with pytest.raises(ValueError, match="unstable"):
        runtime_sys_delay(245, [270, 271, 272, 273, 290, 291, 292, 293])
    with pytest.raises(ValueError, match="out of range"):
        runtime_sys_delay(283 + xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1, [283] * 8)


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


def test_a_moved_live_queue_is_absorbed_by_k_not_refused() -> None:
    # K = commissioned SYS_DELAY + commissioned median, so K - live median is
    # what compensates a reference queue that re-opened at a different fill.
    # jts.local's four commissioning runs: K held within 3 frames (248, 245,
    # 247, 248) while the queue moved, and the armed run converged on the
    # delay that move resolved (ADR-0223).
    for live in (186, 200, 252, 266):
        assert runtime_sys_delay(245, [live] * 8) == 245 - live


def test_the_driver_cap_refuses_a_delay_no_register_can_hold() -> None:
    # CHIP_AEC_SYS_DELAY_MIN..MAX is the declared driver cap: a live median that
    # resolves outside it is refused outright rather than clamped, so boot
    # leaves the chip bypassed instead of writing a delay it cannot honour.
    for k_samples, live in (
        (283 + xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1, 283),
        (283 + xvf3800.CHIP_AEC_SYS_DELAY_MIN - 1, 283),
    ):
        with pytest.raises(ValueError):
            runtime_sys_delay(k_samples, [live] * 8)


def _class_fields() -> dict[str, object]:
    return {
        name: getattr(_identity(), name)
        for name in alignment.HARDWARE_CLASS_IDENTITY_FIELDS
    }


def test_the_class_key_is_the_identity_minus_the_unit_it_was_measured_on() -> None:
    # What makes a proof transferable: the key is everything K was measured
    # against, and nothing that names one physical box.
    identity = _identity()
    key = alignment.hardware_class_key(identity)
    assert set(alignment.HARDWARE_CLASS_IDENTITY_FIELDS) == (
        set(AlignmentIdentity.__dataclass_fields__)
        - alignment.PER_UNIT_IDENTITY_FIELDS
        - alignment.RECORDED_ONLY_IDENTITY_FIELDS
    )
    for name in alignment.PER_UNIT_IDENTITY_FIELDS | alignment.RECORDED_ONLY_IDENTITY_FIELDS:
        moved = replace(identity, **{name: "a-sibling-box"})
        assert alignment.hardware_class_key(moved) == key
    for name in alignment.HARDWARE_CLASS_IDENTITY_FIELDS:
        current = getattr(identity, name)
        moved = replace(
            identity,
            **{name: "moved" if isinstance(current, str) else current + 1},
        )
        assert alignment.hardware_class_key(moved) != key

    # A shipped row carries the same fields as a mapping, and is held to the
    # identity's own rules: nothing per-unit, nothing missing, no zero geometry.
    assert alignment.hardware_class_key(_class_fields()) == key
    for broken in (
        _class_fields() | {"xvf_serial": identity.xvf_serial},
        {name: value for name, value in _class_fields().items() if name != "fixed_profile"},
        _class_fields() | {"output_rate": 0},
        _class_fields() | {"output_id": " "},
    ):
        with pytest.raises(ValueError):
            alignment.hardware_class_key(broken)


def test_hardware_class_identity_placeholder_is_pinned_not_just_nonempty() -> None:
    # AlignmentIdentity's per-unit and recorded-only fields currently clear
    # the same rule as every other text field ("non-empty"), so "unkeyed"
    # passes today by accident of that rule being generic. Nothing stops a
    # later rule from tightening one of them to a real serial's or hardware
    # key's shape — the placeholder would then fail it, and the first place
    # that would show up is a pasted REGISTRY row, not a test. Pin the
    # literal so that change breaks here, by name, instead.
    resolved = alignment.hardware_class_identity(_class_fields())
    for name in alignment.PER_UNIT_IDENTITY_FIELDS | alignment.RECORDED_ONLY_IDENTITY_FIELDS:
        assert getattr(resolved, name) == "unkeyed"


def test_a_shipped_row_meets_the_same_driver_cap_a_commissioned_one_does() -> None:
    # Non-negotiable #2: CHIP_AEC_SYS_DELAY_MIN..MAX is the chip's declared
    # cap. A shipped row is hand-pasted rather than measured here, so it fails
    # where it is declared instead of reaching the chip.
    row = shipped.ShippedAlignment(
        label="lab", identity=_class_fields(), k_samples=245, sys_delay=-38
    )
    assert row.class_key == alignment.hardware_class_key(_identity())
    for delay in (
        xvf3800.CHIP_AEC_SYS_DELAY_MIN - 1,
        xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1,
    ):
        with pytest.raises(ValueError, match="out of range"):
            replace(row, sys_delay=delay)
    with pytest.raises(ValueError):
        replace(row, label="  ")
    with pytest.raises(ValueError):
        replace(row, identity=_class_fields() | {"xvf_serial": "XVF3800-001"})
    with pytest.raises(ValueError):
        shipped._refuse_duplicate_classes((row, replace(row, label="other")))


def test_a_harvested_entry_round_trips_into_the_registry_it_is_pasted_into(
    monkeypatch,
) -> None:
    # The harvest helper's output is source: it has to parse, carry the same
    # class key the artifact it came from does, and be found by the lookup.
    artifact = AlignmentArtifact(_identity(), 245, -38)
    rendered = shipped.render_entry(
        artifact.identity, artifact.k_samples, artifact.sys_delay
    )
    row = eval(rendered, {"ShippedAlignment": shipped.ShippedAlignment})

    assert (row.k_samples, row.sys_delay) == (245, -38)
    assert row.label.strip()
    monkeypatch.setattr(shipped, "REGISTRY", (row,))
    # A sibling box — same class, different serial — is what the row is for.
    assert shipped.for_identity(replace(_identity(), xvf_serial="sibling")) is row
    assert shipped.for_identity(replace(_identity(), output_id="different_dac")) is None


def test_the_apple_dongle_shipped_row_is_found_by_class_and_only_by_class() -> None:
    # Pins the row pasted into shipped.REGISTRY for
    # xvf3800_legacy_square_6ch on apple_usb_c_dongle (jts.local,
    # 2026-09-02) — looked up against the real registry, not a
    # monkeypatched stand-in.
    identity = replace(
        _identity(),
        xvf_firmware="a1f70651e992d6f0bcff655b26925d33999b9c2d",
        fixed_profile=(
            "9e62ab0f4589a48f9918ce08974879ea41f381903da18c48e8e9a05ea595bb9e"
        ),
        output_id="apple_usb_c_dongle",
        output_pcm="single_alsa:outputd_dac",
        output_rate=48_000,
        output_channels=2,
        output_period=128,
        output_buffer=256,
    )

    row = shipped.for_identity(identity)

    assert row is not None
    assert (row.k_samples, row.sys_delay) == (248, 48)
    assert shipped.for_identity(replace(identity, output_id="different_dac")) is None
    shipped._refuse_duplicate_classes(shipped.REGISTRY)


def test_a_malformed_artifact_is_rejected() -> None:
    # The exact-field-set check rejects an artifact missing the commissioned
    # SYS_DELAY.
    artifact = AlignmentArtifact(_identity(), 245, -38)
    older = artifact.to_dict()
    del older["sys_delay"]

    with pytest.raises(ValueError):
        artifact_from_dict(older)

    with pytest.raises(ValueError, match="out of range"):
        AlignmentArtifact(_identity(), 245, xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1)


def test_artifact_is_strict_identity_plus_k_only() -> None:
    artifact = AlignmentArtifact(_identity(), 245, -38)
    assert artifact_from_dict(artifact.to_dict()) == artifact

    # An older schema predates a key this build requires to compare the
    # commissioned identity, so it is refused like any other unreadable
    # artifact — `resolve_banked_alignment` falls back to the shipped table.
    legacy = artifact.to_dict() | {"schema": 1}
    with pytest.raises(ValueError):
        artifact_from_dict(legacy)

    # A schema from the FUTURE is what a rollback leaves behind
    # (JASPER_DEPLOY_ALLOW_DOWNGRADE), and a non-integer schema is not an
    # ordering at all — both are refused the same way.
    for schema in (alignment.ARTIFACT_SCHEMA + 1, "3", 3.0, None):
        with pytest.raises(ValueError):
            artifact_from_dict(artifact.to_dict() | {"schema": schema})

    # Same schema, mangled identity field-set: a shape this build DOES claim to
    # read but cannot, so it stays a hard refusal too.
    mangled = artifact.to_dict()
    mangled["identity"] = {
        name: v for name, v in mangled["identity"].items() if name != "output_format"
    }
    with pytest.raises(ValueError):
        artifact_from_dict(mangled)

    expanded = json.loads(json.dumps(artifact.to_dict()))
    expanded["timing_trials"] = [1, 2, 3]
    with pytest.raises(ValueError):
        artifact_from_dict(expanded)


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
            {"output_id": "other_dac", "xvf_serial": "XVF3800-002"},
            ("xvf_serial", "output_id"),
        ),
        # xvf_variant/beam_plan/output_format carry no timing story (ADR-0190):
        # moving all three alone produces no divergence at all.
        (
            {
                "xvf_variant": "other_variant",
                "beam_plan": "other_plan",
                "output_format": "S32_LE",
            },
            (),
        ),
        # ...even layered under a real physics-field move, which still
        # diverges and reports only itself.
        (
            {
                "xvf_variant": "other_variant",
                "beam_plan": "other_plan",
                "output_format": "S32_LE",
                "output_id": "other_dac",
            },
            ("output_id",),
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
        MicTiming(mic, -38, (lag,) * alignment.TIMING_TRIALS)
        for mic, lag in enumerate((21, 18, 20, 22))
    )

    selected = choose_delay(evidence)

    assert selected.sys_delay == -38
    assert selected.projected_lags == (21, 18, 20, 22)
    assert selected.worst_edge_margin == 17


def _timing_capture(
    arrivals: Sequence[tuple[int, float]],
) -> tuple[np.ndarray, np.ndarray]:
    """A six-channel timing capture carrying ``(lag, gain)`` copies of the sweep."""

    _stereo, reference, active = commissioning_stimulus()
    capture = np.zeros((32_000, 6), dtype="<i2")
    marker_start = 4_000
    capture[marker_start : marker_start + len(reference), 1] = reference
    active_start = marker_start + alignment.GUARD
    mixed = np.zeros(32_000, dtype=np.float64)
    for lag, gain in arrivals:
        mixed[active_start + lag : active_start + lag + len(active)] += gain * active
    capture[:, 0] = np.clip(mixed, -32_768, 32_767).astype("<i2")
    return capture, reference


@pytest.mark.parametrize(
    ("arrivals", "expected_lag"),
    [
        (((20, 1.0),), 20),
        # A reflection 101 samples behind the arrival, however loud.
        (((20, 1.0), (121, 0.9)), 20),
        (((20, 1.0), (121, 1.0)), 20),
        (((20, 1.0), (121, 1.05)), 20),
        (((20, 1.0), (121, 1.2)), 20),
        # A side-lobe-shaped bump 9 samples early: inside
        # DISTINCT_ARRIVAL_MIN_SAMPLES no gain can turn it into the answer.
        (((11, 0.5), (20, 1.0)), 20),
        (((11, 0.7), (20, 1.0)), 20),
        (((11, 0.8), (20, 1.0)), 20),
        # A distinct earlier arrival, weaker than the peak behind it, wins.
        (((30, 0.7), (95, 1.0)), 30),
    ],
)
def test_the_arrival_is_the_first_distinct_candidate(arrivals, expected_lag) -> None:
    capture, reference = _timing_capture(arrivals)

    result = analyze_timing(capture, reference)

    assert result.lag == expected_lag
    assert result.peak > (0.99 if len(arrivals) == 1 else alignment.MIN_TIMING_PEAK)
    assert result.clipped_samples == 0
    # What was skipped travels with the verdict, and is always behind it.
    assert result.competitor_offset_ms > 0
    for later in (lag for lag, _gain in arrivals if lag > expected_lag):
        assert (result.competitor_lag, result.competitor_height > 0) == (later, True)


def test_an_earlier_peak_too_close_to_separate_is_refused() -> None:
    # A near-equal earlier peak inside the window is not evidence of an earlier
    # arrival, but it is evidence the capture cannot say which came first.
    capture, reference = _timing_capture(((5, 0.95), (20, 1.0)))

    with pytest.raises(TimingRejected) as rejected:
        analyze_timing(capture, reference)

    result = rejected.value.result
    assert (result.lag, result.earlier_lag) == (20, 5)
    assert result.peak_ratio < alignment.MIN_PEAK_RATIO
    assert result.peak > alignment.MIN_TIMING_PEAK
    assert rejected.value.fields["at_edge"] is False


def test_timing_correlation_removes_filtered_window_mean(monkeypatch) -> None:
    capture, reference = _timing_capture(((20, 1.0),))
    real_bandpass = alignment._bandpass
    monkeypatch.setattr(
        alignment, "_bandpass", lambda values: real_bandpass(values) + 50_000
    )
    observed_means: list[tuple[float, float]] = []
    real_correlation = kernel_alignment.correlation

    def observe_means(captured, stimulus, **kwargs):
        observed_means.append((float(captured.mean()), float(stimulus.mean())))
        return real_correlation(captured, stimulus, **kwargs)

    monkeypatch.setattr(kernel_alignment, "correlation", observe_means)

    assert analyze_timing(capture, reference).lag == 20
    # The marker locate correlates first; the mic-vs-reference pair is last.
    assert all(abs(value) < 1e-8 for value in observed_means[-1])


def _product_captures(*, clipped: bool = False) -> tuple[np.ndarray, ...]:
    """An AEC-on/off pair that clears every product threshold it is held to."""

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
    if clipped:
        # Same samples in both captures, so the ratio the other four
        # thresholds are measured from does not move with the clip count.
        on[start + 50 : start + 55, 2] = alignment.CLIP
        off[start + 50 : start + 55, 2] = alignment.CLIP
    return on, off, active


def test_product_analyzer_requires_both_beams_and_all_raw_mics() -> None:
    on, off, active = _product_captures()

    result = analyze_product(on, off, active)

    assert result.minimum_raw_excess_snr_db > 10
    assert min(result.beam_acquisition_db) > 8
    assert min(result.beam_suppression_db) > 20
    assert result.clipped_samples == 0


@pytest.mark.parametrize(
    "constant, impossible, measured",
    [
        ("MAX_RAW_LEVEL_DELTA_DB", -1.0, "raw_level_delta_db_abs"),
        ("MIN_RAW_EXCESS_SNR_DB", 500.0, "raw_excess_snr_db"),
        ("MIN_BEAM_ACQUISITION_DB", 500.0, "beam_acquisition_db"),
        ("MIN_BEAM_SUPPRESSION_DB", 500.0, "beam_suppression_db"),
    ],
)
def test_a_rejected_product_capture_carries_each_metric_beside_its_threshold(
    constant, impossible, measured, monkeypatch
) -> None:
    # The refusal IS the evidence: jts.local refused on the five-threshold
    # block with a bare message, so the measurement that failed and the number
    # it was held to both had to be re-derived by hand (#3271).
    on, off, active = _product_captures()
    expected = analyze_product(on, off, active).evidence()[measured]
    monkeypatch.setattr(alignment, constant, impossible)

    with pytest.raises(alignment.Rejected) as rejected:
        analyze_product(on, off, active)

    fields = rejected.value.fields
    assert (fields[measured], fields[constant.lower()]) == (expected, impossible)
    assert fields["clipped_samples"] == 0


def test_a_clipping_product_capture_is_refused_with_the_count() -> None:
    on, off, active = _product_captures(clipped=True)

    with pytest.raises(alignment.Rejected) as rejected:
        analyze_product(on, off, active)

    assert rejected.value.fields["clipped_samples"] == 10
