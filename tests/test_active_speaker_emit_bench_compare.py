# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The emit loop's analysis leg, on synthetic renders. No binary, no subprocess.

Each case builds two "rendered" streams by putting the same synchronized sweep
through scipy biquads written here from the RBJ cookbook — an implementation
independent of ``jasper.sound.profile``'s, which is what the claimed curve is
built from — and then runs the real deconvolve → window → magnitude → compare
chain over them.

The two cases that matter are the pair: an EXACT render must land at the
instrument's floor, and a shelf realized at the Q CamillaDSP's ``slope: 6``
produces (the 2026-07-27 defect) must be caught. A harness that only ever fires
proves nothing; a harness that only ever passes proves less.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.signal import lfilter

from jasper.active_speaker.bench import compare
from jasper.active_speaker.delta_probe import VERDICT_MATCHED, VERDICT_MODEL_ERROR
from jasper.active_speaker.linearization_fit import (
    LinearizationFilter,
    complex_correction_response,
)
from jasper.audio_measurement.sweep import synchronized_swept_sine
from jasper.camilla_config_contract import SHELF_Q
from tests._fake_camilladsp import rbj_biquad, slope6_shelf_q

FS = 48000
LEAD_IN_S = 0.3
SWEEP_S = 2.0

#: The ceiling an EXACT synthetic render must land under. Measured, not
#: guessed: the cases below come in at ~0.003-0.02 dB, and this sits an order
#: of magnitude under the 0.05 dB soft-clip budget and ~85x under the
#: classifier's 1.5 dB tolerance, so a regression that degrades the instrument
#: fails here long before it could mask a real defect.
EXACT_RENDER_CEILING_DB = 0.05


def _stimulus() -> tuple[np.ndarray, object]:
    sweep, meta = synchronized_swept_sine(
        duration_approx_s=SWEEP_S, sample_rate=FS, amplitude_dbfs=-24.0
    )
    stream = np.concatenate(
        [
            np.zeros(int(LEAD_IN_S * FS)),
            np.asarray(sweep, dtype=np.float64),
            np.zeros(FS),
        ]
    )
    return stream, (sweep, meta)


def _render(stream: np.ndarray, specs) -> np.ndarray:
    """One synthetic branch render: a cascade of independently-coded biquads."""

    out = np.asarray(stream, dtype=np.float64)
    for spec in specs:
        b, a = rbj_biquad(
            spec["biquad_type"], spec["freq"], spec["q"], spec["gain"], FS
        )
        out = lfilter(b, a, out)
    return out


def _curves(control_specs, treated_specs):
    stream, (sweep, meta) = _stimulus()
    control = _render(stream, control_specs)
    treated = _render(stream, treated_specs)
    control_ir = compare.deconvolved_ir(control, sweep, FS)
    treated_ir = compare.deconvolved_ir(treated, sweep, FS)
    window = compare.shared_arrival_window(control_ir, FS)
    n_fft = compare.magnitude_fft_length(window)
    freqs, control_db = compare.windowed_magnitude_db(
        control_ir, window, FS, n_fft=n_fft
    )
    _, treated_db = compare.windowed_magnitude_db(treated_ir, window, FS, n_fft=n_fft)
    return freqs, treated_db, control_db, compare.analysis_band_hz(meta)


def _claimed(specs, freqs: np.ndarray) -> np.ndarray:
    records = [LinearizationFilter(**spec) for spec in specs]
    return 20.0 * np.log10(
        np.maximum(np.abs(complex_correction_response(records, freqs)), 1e-12)
    )


DESIGNED = [
    {"biquad_type": "Lowshelf", "freq": 8400.0, "q": SHELF_Q, "gain": -11.0},
    {"biquad_type": "Peaking", "freq": 2500.0, "q": 2.0, "gain": -3.0},
]


# --------------------------------------------------------------------------- #
# the pair: an exact render passes, the historical defect is caught
# --------------------------------------------------------------------------- #


def test_an_exact_render_lands_at_the_instrument_floor() -> None:
    freqs, treated_db, control_db, band = _curves([], DESIGNED)
    result = compare.compare_branch(
        freqs, treated_db, control_db, _claimed(DESIGNED, freqs),
        role="tweeter", band_hz=band,
    )
    assert result.verdict.verdict == VERDICT_MATCHED
    assert result.band_max_error_db < EXACT_RENDER_CEILING_DB
    assert result.frame.fit.fitted
    assert abs(result.frame.fit.tilt_db_per_octave) < 0.01
    assert result.n_bins > 1000


def test_a_shelf_realized_at_slope6_q_is_caught() -> None:
    """The 2026-07-27 class: the graph says one Q, the DSP realizes another.

    The claim is built at the Butterworth Q every evaluator in the flow assumes
    (:data:`jasper.camilla_config_contract.SHELF_Q`); the render is at the Q
    ``slope: 6`` actually produces. Nothing inside the fit could see this — its
    gate, its residual, and its VERIFY prediction all evaluate the shelf the
    config claims. This harness sees it because it measures the render.
    """

    realized_q = slope6_shelf_q(DESIGNED[0]["gain"])
    assert realized_q == pytest.approx(0.4759, abs=1e-3)
    realized = [{**DESIGNED[0], "q": realized_q}, DESIGNED[1]]

    freqs, treated_db, control_db, band = _curves([], realized)
    result = compare.compare_branch(
        freqs, treated_db, control_db, _claimed(DESIGNED, freqs),
        role="tweeter", band_hz=band,
    )
    assert result.verdict.verdict == VERDICT_MODEL_ERROR
    assert result.verdict.rollback
    # The documented depth of the historical defect, reproduced.
    assert result.band_max_error_db == pytest.approx(1.7, abs=0.15)
    assert result.band_max_error_db > 30 * EXACT_RENDER_CEILING_DB
    # A mis-Q'd shelf is broad by construction — the width rule is satisfied by
    # structure, not by a single bin.
    assert result.verdict.exceedance_octaves > 1.0 / 3.0


# --------------------------------------------------------------------------- #
# the frame, and the level move the emitter knows about
# --------------------------------------------------------------------------- #


def test_a_known_level_move_is_removed_and_does_not_count_as_error() -> None:
    freqs, treated_db, control_db, band = _curves([], DESIGNED)
    claimed = _claimed(DESIGNED, freqs)
    plain = compare.compare_branch(
        freqs, treated_db, control_db, claimed, role="tweeter", band_hz=band
    )
    shifted = compare.compare_branch(
        freqs, treated_db - 4.0, control_db, claimed,
        role="tweeter", band_hz=band, expected_offset_db=-4.0,
    )
    assert shifted.expected_offset_db == pytest.approx(-4.0)
    assert shifted.band_max_error_db == pytest.approx(plain.band_max_error_db, abs=1e-9)
    assert shifted.verdict.verdict == VERDICT_MATCHED


def test_an_unaccounted_level_move_is_visible_in_the_frame() -> None:
    """A pure offset lands in ``offset_db``, and the frame-removed grade drops.

    The raw grade still carries it — a frame is evidence about a comparison, not
    a licence to re-grade one.
    """

    freqs, treated_db, control_db, band = _curves([], DESIGNED)
    result = compare.compare_branch(
        freqs, treated_db - 2.0, control_db, _claimed(DESIGNED, freqs),
        role="tweeter", band_hz=band,
    )
    assert result.frame.fit.offset_db == pytest.approx(-2.0, abs=0.01)
    assert result.band_max_error_db > 1.9
    assert result.frame.tilt_removed_max_db < EXACT_RENDER_CEILING_DB


def test_an_injected_tilt_is_measured_as_a_tilt() -> None:
    freqs, treated_db, control_db, band = _curves([], DESIGNED)
    octaves = np.where(freqs > 0.0, np.log2(np.maximum(freqs, 1e-6) / 1000.0), 0.0)
    result = compare.compare_branch(
        freqs, treated_db + 0.8 * octaves, control_db, _claimed(DESIGNED, freqs),
        role="tweeter", band_hz=band,
    )
    assert result.frame.fit.tilt_db_per_octave == pytest.approx(0.8, abs=0.02)
    assert result.frame.tilt_removed_max_db < result.frame.raw_max_db


# --------------------------------------------------------------------------- #
# the validity mask
# --------------------------------------------------------------------------- #


def test_validity_mask_keeps_only_in_band_bins_near_the_control_peak() -> None:
    freqs = np.array([10.0, 100.0, 1000.0, 10000.0, 30000.0])
    control = np.array([0.0, -5.0, 0.0, -80.0, 0.0])
    mask = compare.branch_validity_mask(
        freqs, control, band_hz=(50.0, 20000.0), floor_db=60.0
    )
    assert list(mask) == [False, True, True, False, False]


def test_validity_mask_is_measured_against_the_control_not_the_treated_arm() -> None:
    """A filter that cuts deeply must not exclude the bins it is graded on."""

    freqs, treated_db, control_db, band = _curves([], DESIGNED)
    mask = compare.branch_validity_mask(freqs, control_db, band_hz=band)
    shelf_bins = mask & (freqs > 9000.0) & (freqs < 16000.0)
    assert int(shelf_bins.sum()) > 100


def test_invisible_bins_never_reach_the_verdict() -> None:
    """A branch's stopband cannot manufacture a finding.

    The control arm is 90 dB down above 5 kHz here while the claim asks for a
    large cut there, so an unmasked comparison would read an enormous error. The
    validity mask hands those bins to the classifier as ``NaN``, which is its
    own way of saying "not a measurement".
    """

    freqs = np.geomspace(30.0, 18000.0, 600)
    control_db = np.where(freqs < 5000.0, 0.0, -90.0)
    claimed = np.where(freqs < 5000.0, 0.0, -8.0)
    treated_db = control_db.copy()  # the stopband did not move at all
    result = compare.compare_branch(
        freqs, treated_db, control_db, claimed, role="woofer", band_hz=(30.0, 18000.0)
    )
    assert result.valid_band_hz is not None
    assert result.valid_band_hz[1] < 5000.0
    assert result.verdict.verdict != VERDICT_MODEL_ERROR


def test_a_measured_branch_that_commands_nothing_is_ungraded_not_failed() -> None:
    """A role the fit left alone: measured cleanly, nothing to verify.

    This is the ordinary shape of a single-driver correction, and it is exactly
    the case a two-state pass/fail gets wrong — the branch is measured across
    thousands of bins at 0.000 dB of error and still reaches no verdict, because
    nothing was commanded there. ``measured`` and ``graded`` are separate facts
    so a caller can tell this from a branch the instrument could not see.
    """

    freqs, treated_db, control_db, band = _curves([], [])
    result = compare.compare_branch(
        freqs, treated_db, control_db, np.zeros_like(freqs),
        role="woofer", band_hz=band,
    )
    assert result.measured
    assert result.n_bins > 1000
    assert not result.graded
    assert not result.matched
    assert result.verdict.verdict == "unavailable"
    assert result.verdict.reason == "nothing_commanded"
    assert result.band_max_error_db == pytest.approx(0.0, abs=1e-9)


def test_no_valid_bins_reports_absence_rather_than_a_grade() -> None:
    freqs = np.geomspace(30.0, 18000.0, 100)
    result = compare.compare_branch(
        freqs,
        np.full_like(freqs, np.nan),
        np.full_like(freqs, np.nan),
        np.zeros_like(freqs),
        role="woofer",
        band_hz=(30.0, 18000.0),
    )
    assert result.n_bins == 0
    assert result.valid_band_hz is None
    assert math.isnan(result.band_max_error_db)
    assert not result.frame.fit.fitted
    assert not result.matched
    # Neither measured NOR graded — the other half of the distinction above.
    assert not result.measured
    assert not result.graded


def test_grid_mismatch_is_loud() -> None:
    with pytest.raises(compare.EmitComparisonError, match="one shape"):
        compare.compare_branch(
            np.arange(4.0), np.arange(4.0), np.arange(4.0), np.arange(3.0),
            role="woofer", band_hz=(1.0, 3.0),
        )


# --------------------------------------------------------------------------- #
# the soft clip
# --------------------------------------------------------------------------- #


def test_soft_clip_gain_matches_the_closed_form() -> None:
    clip = -1.0
    clip_linear = 10.0 ** (clip / 20.0)
    for a in (0.05, 0.2, 0.5, 1.0):
        expected = 20.0 * math.log10(1.0 - a * a / 9.0)
        assert compare.soft_clip_fundamental_gain_db(
            a * clip_linear, clip
        ) == pytest.approx(expected, abs=1e-12)


def test_soft_clip_gain_is_zero_at_silence_and_refuses_a_positive_limit() -> None:
    assert compare.soft_clip_fundamental_gain_db(0.0, -1.0) == 0.0
    assert compare.soft_clip_fundamental_gain_db(10.0, -1.0) == float("-inf")
    with pytest.raises(compare.EmitComparisonError):
        compare.soft_clip_fundamental_gain_db(0.1, 3.0)


def test_the_bounds_disclosed_optimism_is_the_measured_one() -> None:
    """The docstring says "computed"; this is the computation, simulated.

    Driven through :func:`jasper.bass_extension.bench.render.reference_soft_clip`
    — the repo's own line-for-line port of the pinned implementation — rather
    than through this module's algebra, so the disclosed figures cannot agree
    with a mistake in the derivation they describe. (They once did: the first
    version of that docstring quoted the FUNDAMENTAL-gain factor ``1 − a²/9``
    where the measured quantity is the output waveform's PEAK, ``1 − a²/6.75``,
    and understated the shortfall by a third.)
    """

    from jasper.bass_extension.bench.render import reference_soft_clip

    clip_dbfs = -1.0
    clip = 10.0 ** (clip_dbfs / 20.0)
    theta = np.linspace(0.0, 2.0 * np.pi, 200_001)

    def simulated_shortfall(a: float) -> tuple[float, float]:
        """``(true |g|, fraction the measured-peak bound falls short by)``."""
        rendered = reference_soft_clip(a * clip * np.sin(theta), clip_limit_dbfs=clip_dbfs)
        measured_peak = float(np.max(np.abs(rendered)))
        # The peak factor, confirmed against the closed form.
        assert measured_peak / (a * clip) == pytest.approx(1.0 - a * a / 6.75, abs=1e-7)
        true = -compare.soft_clip_fundamental_gain_db(a * clip, clip_dbfs)
        bound = -compare.soft_clip_fundamental_gain_db(measured_peak, clip_dbfs)
        return true, (true - bound) / true

    # At the budget: 1.53 % low, 0.000765 dB.
    a_budget = 0.227287
    true_db, fraction = simulated_shortfall(a_budget)
    assert true_db == pytest.approx(compare.SOFT_CLIP_BUDGET_DB, abs=1e-5)
    assert fraction == pytest.approx(0.0153, abs=2e-4)
    assert true_db * fraction == pytest.approx(0.000765, abs=1e-5)

    # At the bench's own default stimulus level: 0.148 %.
    a_default = (10.0 ** (-24.0 / 20.0)) / clip
    default_db, default_fraction = simulated_shortfall(a_default)
    assert default_db == pytest.approx(0.00484, abs=1e-5)
    assert default_fraction == pytest.approx(0.00148, abs=2e-5)


def test_soft_clip_bound_is_conservative_and_clears_at_the_bench_level() -> None:
    clip_linear = 10.0 ** (-1.0 / 20.0)
    quiet = 10.0 ** (-24.0 / 20.0)
    bound = compare.soft_clip_error_bound_db(
        quiet, quiet * 0.5, clip_limit_dbfs=-1.0
    )
    assert 0.0 < bound < compare.SOFT_CLIP_BUDGET_DB
    # And it exceeds the budget once a branch is driven near the clip limit.
    hot = compare.soft_clip_error_bound_db(
        0.9 * clip_linear, 0.1 * clip_linear, clip_limit_dbfs=-1.0
    )
    assert hot > compare.SOFT_CLIP_BUDGET_DB
    assert compare.soft_clip_error_bound_db(
        10.0, 0.0, clip_limit_dbfs=-1.0
    ) == float("inf")


# --------------------------------------------------------------------------- #
# reading a render
# --------------------------------------------------------------------------- #


def test_decode_render_channel_deinterleaves_float64(tmp_path) -> None:
    frame = np.array([[1.0, -1.0], [2.0, -2.0], [3.0, -3.0]], dtype="<f8")
    path = tmp_path / "out.raw"
    path.write_bytes(frame.tobytes())
    left = compare.decode_render_channel(
        path, channel_index=0, channel_count=2, precision="F64_LE"
    )
    right = compare.decode_render_channel(
        path, channel_index=1, channel_count=2, precision="F64_LE"
    )
    assert list(left) == [1.0, 2.0, 3.0]
    assert list(right) == [-1.0, -2.0, -3.0]


def test_decode_render_channel_refuses_an_undecodable_precision(tmp_path) -> None:
    path = tmp_path / "out.raw"
    path.write_bytes(b"\x00" * 16)
    with pytest.raises(compare.EmitComparisonError, match="F64_LE"):
        compare.decode_render_channel(
            path, channel_index=0, channel_count=2, precision="F32_LE"
        )


def test_decode_render_channel_refuses_a_ragged_frame(tmp_path) -> None:
    path = tmp_path / "out.raw"
    path.write_bytes(b"\x00" * 20)
    with pytest.raises(compare.EmitComparisonError, match="frames"):
        compare.decode_render_channel(
            path, channel_index=0, channel_count=2, precision="F64_LE"
        )


def test_analysis_band_backs_off_a_sixth_of_an_octave_from_the_sweep() -> None:
    _, (_, meta) = _stimulus()
    lo, hi = compare.analysis_band_hz(meta)
    assert lo == pytest.approx(meta.f1 * 2.0 ** (1.0 / 6.0))
    assert hi == pytest.approx(meta.f2 * 2.0 ** (-1.0 / 6.0))
    assert meta.f1 < lo < hi < meta.f2
