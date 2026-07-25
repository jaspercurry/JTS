# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R10 — the live cross-check, the only rule that binds a render to reality.

Pins: the unavailable-observation refusal ([]/short accessor readings); the
per-channel comparison (a 4-channel map with owner_channels [0, 2], one
channel passing and the other failing refuses the whole pass); the
render-derived tolerance bound (never the stimulus), including the
synthetic compressed-render case and the lead-in-silence-excluded case;
one-sided tolerance (over/under/within); a get_clipped_samples increase
refusal; the campaign-authoring owner-channel-index refusal; and the
min-across-channels collapse helper.
"""

from __future__ import annotations

import numpy as np
import pytest

from jasper.bass_extension.bench import cross_check


# --------------------------------------------------------------------------- #
# Availability (R10(a))
# --------------------------------------------------------------------------- #


def test_cross_check_available_true_when_long_enough() -> None:
    assert cross_check.cross_check_available([-10.0, -12.0, -8.0], owner_channels=[0, 2])


def test_cross_check_available_false_on_empty_reading() -> None:
    assert not cross_check.cross_check_available([], owner_channels=[0])


def test_cross_check_available_false_when_shorter_than_max_owner_channel() -> None:
    assert not cross_check.cross_check_available([-10.0, -12.0], owner_channels=[0, 2])


def test_cross_check_available_false_on_none_reading() -> None:
    assert not cross_check.cross_check_available(None, owner_channels=[0])


def test_cross_check_owner_channels_refuses_when_unavailable() -> None:
    with pytest.raises(cross_check.CrossCheckError, match="unavailable"):
        cross_check.cross_check_owner_channels(
            owner_channels=[0, 2],
            rendered_peaks_dbfs={0: -10.0, 2: -10.0},
            live_peak_all=[],
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=1.0,
            computed_bound_db=2.0,
            clipped_samples_before=0,
            clipped_samples_after=0,
        )


# --------------------------------------------------------------------------- #
# R10(b): comparison offset
# --------------------------------------------------------------------------- #


def test_offset_zero_when_render_carries_fader_gain() -> None:
    assert (
        cross_check.comparison_offset_db(
            render_carries_fader_gain=True, recorded_main_volume_db=-35.0
        )
        == 0.0
    )


def test_offset_subtracts_fader_when_render_carries_none() -> None:
    assert cross_check.comparison_offset_db(
        render_carries_fader_gain=False, recorded_main_volume_db=-35.0
    ) == 35.0


# --------------------------------------------------------------------------- #
# R10(c): the render-derived tolerance bound
# --------------------------------------------------------------------------- #


def test_tolerance_bound_is_zero_for_a_flat_signal() -> None:
    samples = np.full(4800, 0.1, dtype=np.float64)
    bound = cross_check.compute_permissive_tolerance_bound_db(
        samples, sample_rate_hz=48000, poll_interval_s=0.1
    )
    assert bound == pytest.approx(0.0, abs=1e-9)


def test_tolerance_bound_reflects_a_genuine_rise() -> None:
    # Two 0.1s windows at 48kHz: first quiet, second loud -> ~20 dB rise.
    quiet = np.full(4800, 0.01, dtype=np.float64)
    loud = np.full(4800, 0.1, dtype=np.float64)
    samples = np.concatenate([quiet, loud])
    bound = cross_check.compute_permissive_tolerance_bound_db(
        samples, sample_rate_hz=48000, poll_interval_s=0.1
    )
    assert bound == pytest.approx(20.0, abs=0.1)


def test_tolerance_bound_ignores_a_falling_envelope() -> None:
    loud = np.full(4800, 0.1, dtype=np.float64)
    quiet = np.full(4800, 0.01, dtype=np.float64)
    samples = np.concatenate([loud, quiet])
    bound = cross_check.compute_permissive_tolerance_bound_db(
        samples, sample_rate_hz=48000, poll_interval_s=0.1
    )
    assert bound == pytest.approx(0.0, abs=1e-9)  # only RISE matters, never fall


def test_tolerance_bound_is_computed_from_the_render_never_the_stimulus() -> None:
    """The synthetic compressed-render case: a stimulus-derived bound would
    admit a peak the render-derived bound correctly refuses, because
    sustain_stress's compression makes the RENDER's rise far smaller than
    the STIMULUS's own (uncompressed) rise."""

    sample_rate = 48000
    # Stimulus-like envelope: sharp onset (large rise).
    stimulus_quiet = np.full(4800, 0.001, dtype=np.float64)
    stimulus_loud = np.full(4800, 0.5, dtype=np.float64)
    stimulus_signal = np.concatenate([stimulus_quiet, stimulus_loud])
    stimulus_bound = cross_check.compute_permissive_tolerance_bound_db(
        stimulus_signal, sample_rate_hz=sample_rate, poll_interval_s=0.1
    )

    # The POST-LIMITER render of the same passage is compressed: a much
    # smaller rise because the limiter is holding the level down.
    render_quiet = np.full(4800, 0.09, dtype=np.float64)
    render_loud = np.full(4800, 0.1, dtype=np.float64)
    render_signal = np.concatenate([render_quiet, render_loud])
    render_bound = cross_check.compute_permissive_tolerance_bound_db(
        render_signal, sample_rate_hz=sample_rate, poll_interval_s=0.1
    )

    assert render_bound < stimulus_bound  # the render bound is the tighter (correct) one

    # A manifest tolerance between the two bounds: admitted against the loose
    # stimulus-derived bound, refused against the correct render-derived one.
    tolerance_db = (render_bound + stimulus_bound) / 2.0
    with pytest.raises(cross_check.CrossCheckError, match="exceeds the computed"):
        cross_check.cross_check_channel(
            channel=0,
            rendered_peak_dbfs=-10.0,
            live_peak_all=[-10.0],
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=tolerance_db,
            computed_bound_db=render_bound,
        )
    # The same tolerance against the (wrong) stimulus-derived bound would not refuse.
    cross_check.cross_check_channel(
        channel=0,
        rendered_peak_dbfs=-10.0,
        live_peak_all=[-10.0],
        recorded_main_volume_db=-35.0,
        render_carries_fader_gain=True,
        tolerance_db=tolerance_db,
        computed_bound_db=stimulus_bound,
    )


def test_tolerance_bound_excludes_lead_in_silence_when_caller_passes_body_only() -> None:
    """R6 mandates the padded artifact BEGIN with silence; a bound taken over
    silence-to-body onset would be effectively unbounded. The caller passes
    ONLY the R3 analysis window (stimulus body, lead-in/lead-out already
    excluded) — this test proves that exclusion is what keeps the bound
    tight, by contrasting body-only against lead-in-included."""

    sample_rate = 48000
    lead_in_silence = np.zeros(4800, dtype=np.float64)
    body = np.full(4800, 0.1, dtype=np.float64)

    body_only_bound = cross_check.compute_permissive_tolerance_bound_db(
        body, sample_rate_hz=sample_rate, poll_interval_s=0.1
    )
    assert body_only_bound == pytest.approx(0.0, abs=1e-9)

    with_lead_in = np.concatenate([lead_in_silence, body])
    lead_in_included_bound = cross_check.compute_permissive_tolerance_bound_db(
        with_lead_in, sample_rate_hz=sample_rate, poll_interval_s=0.1
    )
    assert lead_in_included_bound > 60.0  # silence -> full level: an enormous, useless bound
    assert lead_in_included_bound > body_only_bound


def test_tolerance_bound_requires_positive_inputs() -> None:
    with pytest.raises(ValueError):
        cross_check.compute_permissive_tolerance_bound_db(
            np.zeros(10), sample_rate_hz=0, poll_interval_s=0.1
        )
    with pytest.raises(ValueError):
        cross_check.compute_permissive_tolerance_bound_db(
            np.zeros(10), sample_rate_hz=48000, poll_interval_s=0.0
        )


# --------------------------------------------------------------------------- #
# One-sided tolerance
# --------------------------------------------------------------------------- #


def test_agreement_within_tolerance_passes() -> None:
    result = cross_check.cross_check_channel(
        channel=0,
        rendered_peak_dbfs=-10.2,
        live_peak_all=[-10.0],
        recorded_main_volume_db=-35.0,
        render_carries_fader_gain=True,
        tolerance_db=1.0,
        computed_bound_db=2.0,
    )
    assert result.verdict == "pass"


def test_exceeding_by_more_than_tolerance_fails() -> None:
    result = cross_check.cross_check_channel(
        channel=0,
        rendered_peak_dbfs=-5.0,  # 5 dB above the live reading
        live_peak_all=[-10.0],
        recorded_main_volume_db=-35.0,
        render_carries_fader_gain=True,
        tolerance_db=1.0,
        computed_bound_db=6.0,
    )
    assert result.verdict == "fail"


def test_falling_below_by_more_than_tolerance_fails() -> None:
    result = cross_check.cross_check_channel(
        channel=0,
        rendered_peak_dbfs=-15.0,  # 5 dB below the live reading
        live_peak_all=[-10.0],
        recorded_main_volume_db=-35.0,
        render_carries_fader_gain=True,
        tolerance_db=1.0,
        computed_bound_db=6.0,
    )
    assert result.verdict == "fail"


def test_falling_below_uses_the_plain_manifest_tolerance_not_the_bound() -> None:
    """The under (falls-below) side is gated by the plain manifest
    ``tolerance_db`` alone — ``computed_bound_db`` only caps the exceeds
    side (enforced by the precondition above this comparison runs). A
    generous bound with a tight manifest tolerance still fails an
    under-by-more-than-tolerance reading."""

    result = cross_check.cross_check_channel(
        channel=0,
        rendered_peak_dbfs=-13.0,  # 3 dB below the live reading
        live_peak_all=[-10.0],
        recorded_main_volume_db=-35.0,
        render_carries_fader_gain=True,
        tolerance_db=1.0,
        computed_bound_db=50.0,  # generous bound; irrelevant to the under side
    )
    assert result.verdict == "fail"


def test_tolerance_exceeding_the_computed_bound_refuses_the_campaign() -> None:
    with pytest.raises(cross_check.CrossCheckError, match="exceeds the computed"):
        cross_check.cross_check_channel(
            channel=0,
            rendered_peak_dbfs=-10.0,
            live_peak_all=[-10.0],
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=5.0,
            computed_bound_db=1.0,
        )


def test_nonpositive_tolerance_refuses() -> None:
    with pytest.raises(cross_check.CrossCheckError):
        cross_check.cross_check_channel(
            channel=0,
            rendered_peak_dbfs=-10.0,
            live_peak_all=[-10.0],
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=0.0,
            computed_bound_db=1.0,
        )


def test_channel_index_outside_live_peak_all_refuses() -> None:
    with pytest.raises(cross_check.CrossCheckError):
        cross_check.cross_check_channel(
            channel=3,
            rendered_peak_dbfs=-10.0,
            live_peak_all=[-10.0, -12.0],
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=1.0,
            computed_bound_db=2.0,
        )


# --------------------------------------------------------------------------- #
# Per-channel: a 4-channel map with owner_channels [0, 2], one pass one fail
# --------------------------------------------------------------------------- #


def test_four_channel_map_owner_0_2_one_channel_fails_refuses_the_whole_pass() -> None:
    live_peak_all = [-10.0, -99.0, -10.0, -99.0]  # channels 1/3 irrelevant (not owned)
    with pytest.raises(cross_check.CrossCheckError, match="disagreement"):
        cross_check.cross_check_owner_channels(
            owner_channels=[0, 2],
            rendered_peaks_dbfs={0: -10.1, 2: -3.0},  # channel 2 wildly off
            live_peak_all=live_peak_all,
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=1.0,
            computed_bound_db=2.0,
            clipped_samples_before=0,
            clipped_samples_after=0,
        )


def test_four_channel_map_owner_0_2_both_pass() -> None:
    live_peak_all = [-10.0, -99.0, -10.0, -99.0]
    results = cross_check.cross_check_owner_channels(
        owner_channels=[0, 2],
        rendered_peaks_dbfs={0: -10.1, 2: -9.9},
        live_peak_all=live_peak_all,
        recorded_main_volume_db=-35.0,
        render_carries_fader_gain=True,
        tolerance_db=1.0,
        computed_bound_db=2.0,
        clipped_samples_before=0,
        clipped_samples_after=0,
    )
    assert {r.channel for r in results} == {0, 2}
    assert all(r.verdict == "pass" for r in results)


def test_missing_rendered_peak_for_an_owner_channel_refuses() -> None:
    with pytest.raises(cross_check.CrossCheckError, match="no rendered peak"):
        cross_check.cross_check_owner_channels(
            owner_channels=[0, 2],
            rendered_peaks_dbfs={0: -10.0},  # channel 2 missing
            live_peak_all=[-10.0, -99.0, -10.0],
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=1.0,
            computed_bound_db=2.0,
            clipped_samples_before=0,
            clipped_samples_after=0,
        )


# --------------------------------------------------------------------------- #
# R10(d): clipped-samples increase
# --------------------------------------------------------------------------- #


def test_clipped_samples_unchanged_passes() -> None:
    assert cross_check.clipped_samples_verdict(before=10, after=10) == "pass"


def test_clipped_samples_increase_fails() -> None:
    assert cross_check.clipped_samples_verdict(before=10, after=11) == "fail"


def test_clipped_samples_increase_refuses_the_whole_pass() -> None:
    with pytest.raises(cross_check.CrossCheckError, match="clipped_samples"):
        cross_check.cross_check_owner_channels(
            owner_channels=[0],
            rendered_peaks_dbfs={0: -10.0},
            live_peak_all=[-10.0],
            recorded_main_volume_db=-35.0,
            render_carries_fader_gain=True,
            tolerance_db=1.0,
            computed_bound_db=2.0,
            clipped_samples_before=5,
            clipped_samples_after=6,
        )


# --------------------------------------------------------------------------- #
# R3's minimum-across-owner-channels collapse helper (forward-compat only)
# --------------------------------------------------------------------------- #


def test_min_across_channels_returns_the_conservative_minimum() -> None:
    assert cross_check.min_across_channels([-10.0, -5.0, -20.0]) == -20.0


def test_min_across_channels_requires_nonempty() -> None:
    with pytest.raises(ValueError):
        cross_check.min_across_channels([])


# --------------------------------------------------------------------------- #
# R10(a): campaign-manifest authoring-time owner-channel-index refusal
# --------------------------------------------------------------------------- #


def test_owner_channels_admissible_within_range() -> None:
    cross_check.validate_owner_channels_admissible([0, 2], devices_playback_channels=4)


def test_owner_channels_admissible_refuses_index_at_or_beyond_channel_count() -> None:
    with pytest.raises(cross_check.OwnerChannelsInadmissible, match="index 4"):
        cross_check.validate_owner_channels_admissible([0, 4], devices_playback_channels=4)


def test_owner_channels_admissible_refuses_negative_index() -> None:
    with pytest.raises(cross_check.OwnerChannelsInadmissible):
        cross_check.validate_owner_channels_admissible([-1], devices_playback_channels=2)


def test_owner_channels_admissible_multi_entry_is_now_admissible() -> None:
    """Rev 6's dissolved mono-only scope: multi-entry owner_channels are
    admissible whenever every index is in range — owner KIND is never the
    criterion."""

    cross_check.validate_owner_channels_admissible([0, 1, 2, 3], devices_playback_channels=4)
