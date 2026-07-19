# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of an excitation-program capture (crossover conductor W1).

Synthetic-fixture round-trips for
docs/crossover-measurement-productization-design.md §5.6. A "capture" is
composed by convolving each program channel with a distinct synthetic driver
IR (a band-passed impulse at a known delay/amplitude/polarity), summing to
mono, applying a known global offset + clock drift ε + additive noise, and —
for the glitch case — deleting a run of samples. The analysis must recover:

  - clock drift ε within ±2 ppm (two identical woofer sweeps at a known
    scheduled separation, design §3.1);
  - tweeter-vs-woofer relative delay within ±5 µs, with the documented sign
    convention (positive delay_us ⇒ tweeter earlier ⇒ delay the tweeter);
  - polarity (correlation sign, cross-checked against the flatter sum);
  - branch trims within ±0.3 dB;
  - a dropped-buffer glitch ⇒ ``glitch_detected`` (capture must be rejected);
  - per-segment clip runs;
  - the CHECK behavioral linearity verdict (an AGC-compressed pilot delta fails).

Runtime is kept low with short (≥0.5 s) sweeps and 48 kHz mono buffers.
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest
from scipy.signal import fftconvolve, resample_poly

from jasper.audio_measurement import deconv
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
    render_program_pcm,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW,
    ALIGNMENT_OK,
    CAPTURE_BOUND_MARGIN_S,
    IR_POST_MS,
    IR_PRE_MS,
    AlignmentEstimate,
    MeasurementGeometry,
    MeasurementPriors,
    _aligned_branch_tf,
    _band_exclusive_pieces,
    _build_candidate,
    _complex_tf,
    _gate_floor_hz,
    _gcc_phat,
    _global_offset,
    _locate_segments,
    _n_fft_for,
    _predicted_sum,
    _ripple_db,
    _solve_trims,
    analyze_program_capture,
)

SR = 48_000
FC_HZ = 1600.0
GLOBAL_OFFSET = 1234


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _band_impulse(delay: int, f_lo: float, f_hi: float, amp: float, n: int = 4096) -> np.ndarray:
    """A band-passed impulse at ``delay`` samples — a synthetic driver IR."""
    imp = np.zeros(n)
    imp[delay] = amp
    spectrum = np.fft.rfft(imp)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    return np.fft.irfft(spectrum, n)


def _synthesize(
    program,
    *,
    woofer_ir: np.ndarray,
    tweeter_ir: np.ndarray,
    global_offset: int = GLOBAL_OFFSET,
    epsilon: float = 0.0,
    noise: float = 1e-4,
    seed: int = 0,
) -> np.ndarray:
    """Compose a mono capture from a program + per-channel synthetic driver IRs."""
    pcm = render_program_pcm(program)
    mono = np.zeros(pcm.shape[0], dtype=np.float64)
    if pcm.shape[1] >= 1:
        mono += fftconvolve(pcm[:, 0], woofer_ir)[: pcm.shape[0]]
    if pcm.shape[1] >= 2:
        mono += fftconvolve(pcm[:, 1], tweeter_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(global_offset), mono, np.zeros(20_000)])
    if epsilon != 0.0:
        frac = Fraction(1.0 + epsilon).limit_denominator(500_000)
        cap = resample_poly(cap, frac.numerator, frac.denominator)
    if noise:
        cap = cap + np.random.default_rng(seed).normal(0.0, noise, cap.size)
    return cap


# --------------------------------------------------------------------------- #
# MEASURE — the combined drift/delay/polarity/trim round-trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("polarity_amp,expected_polarity", [(0.7, "normal"), (-0.7, "inverted")])
def test_measure_round_trip_recovers_drift_delay_polarity_trims(polarity_amp, expected_polarity):
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    d_w = 200
    tau_true = 25  # tweeter arrives 25 samples LATER than the woofer
    eps = 80e-6
    woofer_ir = _band_impulse(d_w, 150.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(d_w + tau_true, 300.0, 20000.0, polarity_amp)
    cap = _synthesize(prog, woofer_ir=woofer_ir, tweeter_ir=tweeter_ir, epsilon=eps)

    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        geometry=MeasurementGeometry(),  # d=0 ⇒ no parallax
    )

    # Drift within ±2 ppm.
    assert res.drift is not None
    assert res.drift.epsilon_ppm == pytest.approx(eps * 1e6, abs=2.0)
    assert not res.glitch_detected

    # Delay within ±5 µs. tweeter LATER ⇒ delay_us = D_w − D_t < 0.
    expected_delay_us = -tau_true / SR * 1e6
    assert res.alignment is not None
    assert res.alignment.delay_us == pytest.approx(expected_delay_us, abs=5.0)

    # Polarity correct, and it agrees with the flatter-sum cross-check.
    assert res.alignment.polarity == expected_polarity
    assert res.alignment.polarity_agrees_with_sum

    # Trims within ±0.3 dB: the woofer (amp 1.0) is 3.1 dB louder than the
    # tweeter (|amp| 0.7), so the woofer branch is attenuated by ~3.1 dB.
    trims = res.candidate.trim_db
    assert trims["woofer"] == pytest.approx(20.0 * np.log10(0.7), abs=0.3)
    assert trims["tweeter"] == pytest.approx(0.0, abs=0.3)


def test_measure_negative_drift_round_trip():
    """A slow capture clock (ε < 0) round-trips the same way a fast one does."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 25
    eps = -80e-6
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.7),
        epsilon=eps,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.drift.epsilon_ppm == pytest.approx(eps * 1e6, abs=2.0)
    assert not res.glitch_detected
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)
    assert res.alignment.status == ALIGNMENT_OK


def test_delay_beyond_search_window_is_refused():
    """A true delay outside ±search_window must fail loud, not return a
    moderate-confidence clamped value (gate finding S2a: tau=110 samples vs a
    96-sample window once returned −2673 µs at confidence 0.575)."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 110  # beyond the default 2 ms ⇒ 96-sample window
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.alignment.status == ALIGNMENT_DELAY_EXCEEDS_SEARCH_WINDOW
    assert res.alignment.confidence == 0.0
    assert res.candidate.confidence == 0.0


def test_delay_near_search_window_edge_is_accurate():
    """A true delay just INSIDE the window is measured, not refused."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 90  # inside the 96-sample window, 6 samples from the edge
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.alignment.status == ALIGNMENT_OK
    assert res.alignment.confidence > 0.0
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)


def test_oversized_capture_is_bounded_and_still_analyzes():
    """A stuck long recording is truncated to program + margin before any
    full-rate FFT (defense at the FFT, 1 GB Pi); the program at the head of
    the capped window still analyzes correctly."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    tau_true = 30
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.8),
        epsilon=0.0,
    )
    bound_samples = prog.total_samples + int(CAPTURE_BOUND_MARGIN_S * SR)
    # A "stuck" tail far beyond the bound.
    stuck = np.concatenate([
        cap, np.random.default_rng(9).normal(0.0, 1e-4, 30 * SR)
    ])
    assert stuck.size > bound_samples
    res = analyze_program_capture(prog, stuck, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert not res.glitch_detected
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)


def test_channel_map_fails_on_swapped_channels():
    """Swapped driver wiring ⇒ each pilot window is noise-dominated (a driver
    fed fully out-of-band content produces essentially no linear output), so
    the pilot's energy is NOT concentrated in its declared band and channel-map
    sanity fails.

    The plants are double-convolved band-passes (~−88 dB stopband): a
    single 4096-tap brick-wall mask leaks at ~−44 dB, which would leave a
    coherent in-band ghost of the pilot ABOVE the noise floor — an artifact of
    the fixture, not of a physical driver, which rolls off far deeper when fed
    a fully disjoint band."""
    roles = [
        RoleBand("woofer", 0, FrequencyBand(150.0, 1200.0)),
        RoleBand("tweeter", 1, FrequencyBand(2500.0, 20000.0)),
    ]
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)

    def _deep_plant(delay, f_lo, f_hi, amp):
        single = _band_impulse(delay, f_lo, f_hi, 1.0)
        return amp * fftconvolve(single, single)  # stopband dB doubles

    woofer_plant = _deep_plant(200, 150.0, 1200.0, 1.0)
    tweeter_plant = _deep_plant(225, 2500.0, 20000.0, 0.8)

    def _capture(ch0_plant, ch1_plant, seed):
        mono = (
            fftconvolve(pcm[:, 0], ch0_plant)[: pcm.shape[0]]
            + fftconvolve(pcm[:, 1], ch1_plant)[: pcm.shape[0]]
        )
        cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
        return cap + np.random.default_rng(seed).normal(0.0, 3e-5, cap.size)

    correct = analyze_program_capture(
        chk, _capture(woofer_plant, tweeter_plant, 11), SR, priors=MeasurementPriors(),
    )
    assert correct.channel_map_ok is True

    swapped = analyze_program_capture(
        chk, _capture(tweeter_plant, woofer_plant, 12), SR, priors=MeasurementPriors(),
    )
    assert swapped.channel_map_ok is False


def test_measure_uses_check_ambient_for_snr_verdicts():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    # A quiet ambient report (from CHECK) ⇒ per-driver SNR verdicts populate.
    ambient = {
        "schema_version": 1,
        "bands": [
            {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -90.0},
        ],
    }
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, ambient_report=ambient),
    )
    woofer = next(r for r in res.driver_responses if r.role == "woofer")
    assert woofer.snr is not None
    assert woofer.snr["decision_class"] == "magnitude"
    # Without an ambient report the SNR block is absent.
    res_none = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert all(r.snr is None for r in res_none.driver_responses)


def test_measure_no_drift_delay_is_tight():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tau_true = 40
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.9),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)
    assert res.drift.epsilon_ppm == pytest.approx(0.0, abs=2.0)


# --------------------------------------------------------------------------- #
# glitch injection
# --------------------------------------------------------------------------- #


def test_dropped_buffer_glitch_is_detected():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    # Delete 128 samples mid-capture (between the two woofer sweeps): the
    # woofer-repeat separation shrinks ⇒ out-of-band ε ⇒ glitch.
    mid = cap.size // 2
    glitched = np.concatenate([cap[:mid], cap[mid + 128:]])
    res = analyze_program_capture(prog, glitched, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.glitch_detected
    assert res.drift.glitch_detected


def test_clean_capture_is_not_flagged_as_glitch():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(260, 300.0, 20000.0, 0.7),
        epsilon=40e-6,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert not res.glitch_detected


# --------------------------------------------------------------------------- #
# integrity: clip runs + locator robustness
# --------------------------------------------------------------------------- #


def test_clip_run_detection():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
        epsilon=0.0,
    )
    # Hard-clip a run inside the woofer sweep window.
    sweep_w = prog.segment("sweep_w")
    start = GLOBAL_OFFSET + sweep_w.start_sample + 500
    cap[start:start + 8] = 1.0
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    clipped = {loc.segment_id: loc.clipped for loc in res.locations}
    assert clipped["sweep_w"] is True
    assert clipped["sweep_t"] is False


def test_locator_robust_to_large_global_offset():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    tau_true = 30
    for offset in (0, int(0.5 * SR)):
        cap = _synthesize(
            prog,
            woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
            tweeter_ir=_band_impulse(200 + tau_true, 300.0, 20000.0, 0.8),
            global_offset=offset,
            epsilon=0.0,
        )
        res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
        assert res.alignment.delay_us == pytest.approx(-tau_true / SR * 1e6, abs=5.0)


# --------------------------------------------------------------------------- #
# sign convention (worked example)
# --------------------------------------------------------------------------- #


def test_gcc_phat_sign_convention():
    # A ≈ B shifted right by +lag: build st = sw delayed by 30 samples.
    base = _band_impulse(400, 800.0, 3200.0, 1.0, n=2048)
    sw = base
    st = np.roll(base, 30)  # tweeter LATER by 30 samples
    lag, sign, conf, at_edge = _gcc_phat(
        st, sw, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert lag == pytest.approx(30.0, abs=0.5)  # positive ⇒ st (tweeter) later
    assert sign == 1
    assert not at_edge
    # Inverting the tweeter flips the correlation sign.
    lag2, sign2, _conf2, _edge2 = _gcc_phat(
        -st, sw, sample_rate=SR, band_hz=(800.0, 3200.0),
        upsample=16, max_lag_samples=200,
    )
    assert sign2 == -1
    assert lag2 == pytest.approx(30.0, abs=0.5)


def test_delay_sign_convention_tweeter_earlier_is_positive():
    """positive delay_us ⇒ tweeter arrives EARLIER ⇒ delay the tweeter branch."""
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    # Tweeter EARLIER than the woofer (d_t < d_w).
    d_w, d_t = 260, 200
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(d_w, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(d_t, 300.0, 20000.0, 0.9),
        epsilon=0.0,
    )
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    # D_w − D_t = 60 samples ⇒ positive delay_us.
    assert res.alignment.delay_us > 0
    assert res.alignment.delay_us == pytest.approx((d_w - d_t) / SR * 1e6, abs=5.0)


def test_parallax_is_subtracted():
    geo = MeasurementGeometry(driver_spacing_m=0.15, mic_distance_m=1.0)
    parallax = geo.parallax_us()
    assert parallax > 0
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(230, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(200, 300.0, 20000.0, 0.9),
        epsilon=0.0,
    )
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ), geometry=geo,
    )
    # delay_us = raw_delay_us − parallax_us.
    assert res.alignment.delay_us == pytest.approx(
        res.alignment.raw_delay_us - parallax, abs=1e-6
    )


# --------------------------------------------------------------------------- #
# CHECK — ambient, linearity, channel map, gain plan
# --------------------------------------------------------------------------- #


def _check_roles() -> list[RoleBand]:
    """Disjoint bands for the CHECK fixtures below (Fix 1, W6.4).

    Unlike ``_roles()``'s heavily-overlapping MEASURE-style bands (needed
    elsewhere for Fc-overlap trim solving), the band-relative channel-map test
    (`_channel_map_ok`) measures absolute dB rise rather than a total-energy
    fraction, so a CROSS band immediately adjacent to a shared boundary now
    also picks up the stimulus generator's own ~5 ms fade-in/out spectral
    splatter (`synchronized_swept_sine`) — real, but concentrated within
    about an octave of a chirp's own start/end frequency, same as a real
    driver's transition band. Keeping these two bands clearly apart avoids
    exercising that (real, but here irrelevant) edge effect.
    """
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 1200.0)),
        RoleBand("tweeter", 1, FrequencyBand(2500.0, 20000.0)),
    ]


def _check_capture(program, *, compress_hi: bool = False, seed: int = 3):
    pcm = render_program_pcm(program)
    woofer_ir = _band_impulse(200, 150.0, 1200.0, 1.0)
    tweeter_ir = _band_impulse(225, 2500.0, 20000.0, 0.7)
    mono = (
        fftconvolve(pcm[:, 0], woofer_ir)[: pcm.shape[0]]
        + fftconvolve(pcm[:, 1], tweeter_ir)[: pcm.shape[0]]
    )
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])
    if compress_hi:
        # Simulate AGC: attenuate the two HI pilots so the captured 10 dB delta
        # is compressed to ~4 dB.
        for role in ("woofer", "tweeter"):
            seg = program.segment(f"pilot_{role}_hi")
            lo = 500 + seg.start_sample
            cap[lo:lo + seg.n_samples] *= 10 ** (-6.0 / 20.0)
    cap = cap + np.random.default_rng(seed).normal(0.0, 3e-5, cap.size)
    return cap


def test_check_linearity_and_channel_map_pass_for_clean_capture():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.linearity_ok is True
    assert res.channel_map_ok is True
    for pilot in res.pilots:
        assert pilot.captured_delta_db == pytest.approx(10.0, abs=0.5)


def test_check_linearity_fails_under_simulated_agc():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog, compress_hi=True)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    # The programmed 10 dB delta is captured as ~4 dB ⇒ linearity fails loud.
    assert res.linearity_ok is False


def test_check_gain_plan_targets_measure_window_with_guard():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(target_capture_dbfs=-10.5),
    )
    plan = res.gain_plan
    assert plan is not None
    # ≥6 dB digital guard on every solved gain.
    for gain in plan.gain_db.values():
        assert gain <= -6.0 + 1e-9
    assert plan.predicted_peak_dbfs <= -6.0 + 1e-9
    assert plan.snr_floor_ok is True


def test_check_ambient_report_present():
    prog = build_check_program(_check_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.ambient_report is not None
    assert res.ambient_report["bands"]


# --------------------------------------------------------------------------- #
# CHECK channel map — band-relative discriminator (Fix 1, W6.4)
# --------------------------------------------------------------------------- #


def test_channel_map_exclusive_pieces_subtract_overlap():
    """Direct unit coverage for `_band_exclusive_pieces` (production helper):
    two drivers' declared bands legitimately overlap around the crossover
    point, so the CROSS test only looks at the part of the OTHER role's band
    that this role's own band does NOT already cover."""
    # Overlapping bands (MEASURE-style): only the non-overlapping remainder
    # of `other` survives.
    assert _band_exclusive_pieces((300.0, 20000.0), (150.0, 6000.0)) == [(6000.0, 20000.0)]
    assert _band_exclusive_pieces((150.0, 6000.0), (300.0, 20000.0)) == [(150.0, 300.0)]
    # Disjoint bands: nothing to subtract, `other` passes through whole.
    assert _band_exclusive_pieces((2500.0, 20000.0), (150.0, 1200.0)) == [(2500.0, 20000.0)]
    # `other` fully inside `own`: nothing left of `other` at all.
    assert _band_exclusive_pieces((1000.0, 2000.0), (150.0, 6000.0)) == []


def test_channel_map_survives_concurrent_room_rumble():
    """Reproduces the W6 run-5 hardware shape (2026-07-18/19, jts3): a
    cap-clamped, genuinely-quiet tweeter pilot (~25 dB of real TARGET rise
    over its own ambient — not the ~50-70 dB the other CHECK fixtures use)
    plays alongside a strong, CONTINUOUS low-frequency room rumble present
    for the WHOLE capture — the leading ambient window AND every pilot
    window alike, not a burst timed to coincide with the tweeter pilot (a
    real room's noise floor doesn't know when the tweeter is playing).

    Under the OLD total-in-band-energy-FRACTION test (reimplemented inline
    below, exactly as `_channel_map_ok` computed it pre-Fix-1), the rumble's
    energy in the (unrelated) woofer band dominates the tweeter pilot
    window's TOTAL spectral energy, driving the in-band fraction to ~0.12 —
    comfortably under the old >0.5 threshold, so the old code would veto the
    tweeter's channel-map verdict even though the tweeter played correctly.
    This was the run-5 bug. The NEW band-relative test isn't fooled: the
    rumble is equally present in the long ambient window, so it contributes
    ~0 dB of CROSS rise regardless of which band it lives in."""
    roles = _check_roles()
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)

    # Cap-clamped tweeter: the pilot plays, just quietly (unlike the ~0.7-1.0
    # unit-gain drivers used elsewhere in this file).
    tweeter_gain = 0.05
    mono = pcm[:, 0] + tweeter_gain * pcm[:, 1]
    cap = np.concatenate([np.zeros(500), mono, np.zeros(5000)])

    # Strong, continuous LF room rumble (three low tones, present across the
    # entire capture) plus a modest broadband noise floor.
    t = np.arange(cap.size) / SR
    rumble = 0.02 * (
        np.sin(2 * np.pi * 40.0 * t) + np.sin(2 * np.pi * 70.0 * t) + np.sin(2 * np.pi * 110.0 * t)
    )
    full_cap = cap + rumble + np.random.default_rng(30).normal(0.0, 3e-4, cap.size)

    res = analyze_program_capture(chk, full_cap, SR, priors=MeasurementPriors())
    assert res.channel_map_ok is True
    for pilot in res.pilots:
        assert pilot.channel_map_ok is True

    # OLD math, reimplemented inline: >50% of the tweeter pilot's TOTAL
    # spectral energy must land in its own declared band. It does not.
    global_offset, _first, stimuli = _global_offset(chk, full_cap, SR)
    locations = _locate_segments(chk, full_cap, SR, global_offset, stimuli)
    by_id = {loc.segment_id: loc for loc in locations}
    hi_seg = chk.segment("pilot_tweeter_hi")
    hi_loc = by_id["pilot_tweeter_hi"]
    hi_samples = full_cap[hi_loc.located_start:hi_loc.located_start + hi_seg.n_samples]
    window = np.hanning(hi_samples.size)
    spectrum = np.abs(np.fft.rfft(hi_samples * window)) ** 2
    freqs = np.fft.rfftfreq(hi_samples.size, 1.0 / SR)
    in_band = (freqs >= hi_seg.f1_hz) & (freqs <= hi_seg.f2_hz)
    old_fraction = float(np.sum(spectrum[in_band])) / float(np.sum(spectrum))
    assert old_fraction < 0.5


def test_channel_map_fails_when_no_driver_played():
    """No pilot signal at all (pure noise capture) ⇒ channel-map still fails.
    Neither driver's own band ever rises above its ambient, so TARGET fails
    for both roles regardless of how quiet/loud the room noise floor is."""
    roles = _check_roles()
    chk = build_check_program(roles, ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)
    n = pcm.shape[0] + 500 + 5000
    cap = np.random.default_rng(31).normal(0.0, 3e-4, n)
    res = analyze_program_capture(chk, cap, SR, priors=MeasurementPriors())
    assert res.channel_map_ok is False
    for pilot in res.pilots:
        assert pilot.channel_map_ok is False


# --------------------------------------------------------------------------- #
# VERIFY
# --------------------------------------------------------------------------- #


def test_verify_summed_response_and_ripple():
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = _band_impulse(200, 150.0, 20000.0, 1.0, n=8192)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(5).normal(0.0, 1e-4, cap.size)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.summed_response is not None
    # A flat synthetic IR sums flat ⇒ small ripple through the crossover.
    assert res.summed_ripple_db is not None
    assert res.summed_ripple_db < 3.0


def test_verify_tracking_against_predicted_sum():
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    ir = _band_impulse(200, 150.0, 20000.0, 1.0, n=8192)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(6).normal(0.0, 1e-4, cap.size)
    # A flat predicted sum over the crossover region ⇒ small tracking error.
    pred_freqs = np.geomspace(100.0, 20000.0, 400)
    pred_db = np.zeros_like(pred_freqs)
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, predicted_sum=(pred_freqs, pred_db)),
    )
    assert res.verify_tracking is not None
    assert res.verify_tracking["rms_db"] < 3.0
    # W6.7 ruling 1: a flat prediction has no bin more than
    # ``VERIFY_NOTCH_EXCLUSION_DB`` below its own median, so nothing is
    # excluded — the notch-excluded fields are byte-identical to the raw
    # full-band ones.
    assert res.verify_tracking["max_db_notch_excluded"] == pytest.approx(
        res.verify_tracking["max_db"]
    )
    assert res.verify_tracking["rms_db_notch_excluded"] == pytest.approx(
        res.verify_tracking["rms_db"]
    )


def test_verify_tracking_notch_exclusion_reduces_max_through_the_pipeline():
    """W6.7 ruling 1, wired end-to-end through ``analyze_program_capture``: a
    real captured null (a two-tap comb filter — an actual acoustic-style
    interference notch, not a hand-drawn curve) compared against a predicted
    null of the same general shape but a slightly different exact
    frequency/depth (the "sub-dB/sub-degree branch difference" the run-7
    architect's analysis names). The exact numeric OLD-fails/NEW-passes
    contract is pinned directly against ``notch_excluded_tracking_error_db``
    in test_audio_measurement_harmonics.py; this test only pins that the
    pipeline actually WIRES the exclusion in (1/6-oct smoothing, the notch
    boundary applied) and that it measurably shrinks the max here too."""
    prog = build_verify_program(FC_HZ, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    null_delay = int(round(SR / FC_HZ))
    ir = np.zeros(8192)
    ir[200] = 1.0
    ir[200 + null_delay] = -1.0
    spectrum = np.fft.rfft(ir)
    freqs_ir = np.fft.rfftfreq(ir.size, 1.0 / SR)
    spectrum[(freqs_ir < 150.0) | (freqs_ir > 20000.0)] = 0.0
    ir = np.fft.irfft(spectrum, ir.size)
    mono = fftconvolve(pcm[:, 0], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(11).normal(0.0, 1e-4, cap.size)

    pred_freqs = np.geomspace(100.0, 20000.0, 400)
    # The predicted null assumes a slightly different delay -- the analytic
    # comb-filter magnitude for the SAME shape, a hair off in frequency.
    predicted_mag = 2.0 * np.abs(np.sin(np.pi * pred_freqs * (null_delay + 0.4) / SR))
    pred_db = 20.0 * np.log10(np.maximum(predicted_mag, 1e-6))

    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, predicted_sum=(pred_freqs, pred_db)),
    )
    assert res.verify_tracking is not None
    tracking = res.verify_tracking
    # The raw full-band max is dominated by the notch-position mismatch
    # (the run-7 bug shape); excluding the predicted notch's own interior
    # measurably shrinks it.
    assert tracking["max_db_notch_excluded"] < tracking["max_db"]
    assert tracking["rms_db_notch_excluded"] < tracking["rms_db"]


# --------------------------------------------------------------------------- #
# W6.9 — gating-consistent prediction + validity-floor tracking clamp
# --------------------------------------------------------------------------- #
#
# W6.9 forensics (2026-07-19) numerically reproduced a W6 run-7/8 hardware
# VERIFY failure and traced it to two compounding bugs: (1) the MEASURE-side
# prediction composed each branch's TF from a FIXED 65 ms window
# (`IR_PRE_MS` + `IR_POST_MS`), so a 15 cm desk-bounce reflection at the mic
# position was baked into the predicted sum (a spurious ~1125 Hz null) even
# though (2) VERIFY's own measured sum is adaptively reflection-gated
# (`_driver_response`) and never had that reflection to begin with — and the
# VERIFY tracking comparator's band never clamped to that adaptive gate's own
# validity floor (`gating.f_valid_floor_hz`), so sub-validity bins decided the
# verdict. Fix 1 (validity-floor clamp) and Fix 2 (gating-consistent
# `_aligned_branch_tf`) are exercised together below; the mechanism was also
# validated against the actual forensics hardware capture (run-8 a5:
# `analyze_program_capture` on the real WAV yields tracking
# rms=1.496/max=5.115 dB against the ≈1.50/5.12 forensics target — see the
# W6.9 commit message for the reproduction, not re-run here since the WAV/npz
# fixtures are hardware artifacts, not part of this hardware-free suite).


def _reflection_branch(peak: int, f_lo: float, f_hi: float, amp: float,
                        *, reflection_delay_s: float = 0.0, reflection_amp: float = 0.0,
                        n: int = 8192) -> np.ndarray:
    """A band-passed direct-arrival impulse, optionally with a genuine near
    reflection: a scaled, delayed COPY of the direct arrival's own shape
    (causal — zeroed before the reflection onset), the same construction
    ``gate_test.py`` (W6.9 forensics) used to reproduce the hardware desk
    bounce. ``reflection_delay_s`` must be ≥ ``gating.SEARCH_T_MIN_MS`` (0.5
    ms) to be detectable at all — a reflection that close to the direct
    arrival's own tail is structurally invisible to
    ``gating.detect_first_reflection``'s search window by design."""
    direct = _band_impulse(peak, f_lo, f_hi, amp, n=n)
    if reflection_amp == 0.0:
        return direct
    delay_samples = int(round(reflection_delay_s * SR))
    reflection = reflection_amp * np.roll(direct, delay_samples)
    reflection[:delay_samples] = 0.0
    return direct + reflection


def _old_fixed_window_branch_tf(full_ir: np.ndarray, n_fft: int):
    """Byte-for-byte the pre-W6.9 ``_aligned_branch_tf``: fixed
    ``IR_PRE_MS``/``IR_POST_MS`` window, no reflection gating."""
    peak_idx = int(np.argmax(np.abs(full_ir)))
    window = deconv.direct_arrival_window(
        full_ir, SR, direct_peak_idx=peak_idx,
        pre_arrival_ms=IR_PRE_MS, post_arrival_ms=IR_POST_MS,
    )
    ir = deconv.apply_arrival_window(full_ir, window)
    return _complex_tf(ir, SR, n_fft=n_fft, calibration=None)


def _reflection_fixture(fc_hz: float, *, peak: int = 300, n: int = 8192):
    """Woofer branch with a genuine near reflection at +0.70 ms (a real
    ``gating.detect_first_reflection`` hit — comfortably inside its [0.5, 7]
    ms search window, standing in for the forensics' ~0.6-0.7 ms hardware
    desk bounce); tweeter branch clean. Returns
    ``(woofer_ir, tweeter_ir, n_fft)``."""
    woofer_ir = _reflection_branch(
        peak, 150.0, 6000.0, 1.0,
        reflection_delay_s=0.70e-3, reflection_amp=1.0, n=n,
    )
    tweeter_ir = _reflection_branch(peak, 300.0, 20000.0, 0.7, n=n)
    return woofer_ir, tweeter_ir, _n_fft_for(woofer_ir, tweeter_ir)


def test_aligned_branch_tf_applies_the_same_adaptive_gate_as_driver_response():
    """Fix 2, isolated: ``_aligned_branch_tf`` must reflection-gate exactly
    like ``_driver_response`` does, not use the fixed 65 ms window alone."""
    fc_hz = 2000.0
    woofer_ir, _tweeter_ir, n_fft = _reflection_fixture(fc_hz)

    freqs, W_new, fragment = _aligned_branch_tf(woofer_ir, SR, n_fft, calibration=None)
    assert fragment["floor_source"] == "measured_reflection"
    floor_hz = _gate_floor_hz(fragment)
    assert floor_hz is not None and floor_hz > fc_hz / 2.0  # gate is tighter than the nominal band

    _f2, W_old = _old_fixed_window_branch_tf(woofer_ir, n_fft)
    # Around the injected reflection's comb notch, the OLD fixed-window TF is
    # badly depressed while the NEW gated TF stays flat — the collapse the
    # forensics cited (a fixed-window null vs. a gated peak at the same bin).
    lo, hi = fc_hz / 2.0, fc_hz * 2.0
    band = (freqs >= lo) & (freqs <= hi)
    old_db = 20.0 * np.log10(np.maximum(np.abs(W_old[band]), 1e-12))
    new_db = 20.0 * np.log10(np.maximum(np.abs(W_new[band]), 1e-12))
    old_ripple = float(old_db.max() - old_db.min())
    new_ripple = float(new_db.max() - new_db.min())
    assert old_ripple > 5.0
    assert new_ripple < 1.0
    assert new_ripple < old_ripple - 4.0


def test_gating_consistent_candidate_removes_reflection_notch_end_to_end():
    """Fix 2, end to end: a fixed-window prediction bakes a real near
    reflection into a notch that fails VERIFY tolerance; the gating-
    consistent prediction is clean and the SAME real capture passes."""
    fc_hz = 2000.0
    lo, hi = fc_hz / 2.0, fc_hz * 2.0
    woofer_ir, tweeter_ir, n_fft = _reflection_fixture(fc_hz)

    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )
    candidate, (pred_freqs, pred_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter", alignment, None,
    )
    # NEW: gating-consistent prediction is clean.
    assert candidate.predicted_ripple_db < 1.5

    # OLD: byte-for-byte the pre-fix fixed-window path bakes in a notch that
    # would fail the same ±1.5 dB VERIFY tolerance the run-7/8 bug tripped.
    freqs_old, W_old = _old_fixed_window_branch_tf(woofer_ir, n_fft)
    _f2, T_old = _old_fixed_window_branch_tf(tweeter_ir, n_fft)
    trim_w_old, trim_t_old, _lw, _lt = _solve_trims(freqs_old, W_old, T_old, fc_hz)
    old_ripple = _ripple_db(
        freqs_old, _predicted_sum(W_old, T_old, trim_w_old, trim_t_old, +1), lo, hi,
    )
    assert old_ripple > 5.0
    assert old_ripple > candidate.predicted_ripple_db + 4.0
    old_pred_db = 20.0 * np.log10(np.maximum(
        np.abs(_predicted_sum(W_old, T_old, trim_w_old, trim_t_old, +1)), 1e-12,
    ))

    # The REAL physical system (the reflection is reality — it doesn't get
    # gated away by wishing) is the raw time-domain sum of both branches at
    # the candidate's own solved trims; VERIFY captures this same system.
    g_w = 10.0 ** (candidate.trim_db["woofer"] / 20.0)
    g_t = 10.0 ** (candidate.trim_db["tweeter"] / 20.0)
    combined_ir = g_w * woofer_ir + alignment.polarity_sign * g_t * tweeter_ir

    prog = build_verify_program(fc_hz, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    mono = fftconvolve(pcm[:, 0], combined_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(42).normal(0.0, 1e-5, cap.size)

    res_new = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=fc_hz, predicted_sum=(pred_freqs, pred_db)),
    )
    assert res_new.verify_tracking is not None
    assert res_new.verify_tracking["max_db_notch_excluded"] < 1.5  # NEW: tracking passes

    res_old = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=fc_hz, predicted_sum=(freqs_old, old_pred_db)),
    )
    assert res_old.verify_tracking is not None
    assert res_old.verify_tracking["max_db_notch_excluded"] > 1.5  # OLD: same capture, false fail


def test_validity_floor_clamp_excludes_only_sub_floor_divergence():
    """Fix 1: the VERIFY tracking comparator drops bins below THIS capture's
    own gate-derived validity floor. A predicted-sum divergence placed
    entirely below that floor must not move the gated numbers (though it
    still shows up in the raw ``*_full_band`` diagnostics); the identical
    divergence placed above the floor must still fail the gate."""
    fc_hz = 2000.0
    woofer_ir, tweeter_ir, n_fft = _reflection_fixture(fc_hz)
    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )
    candidate, (pred_freqs, pred_db) = _build_candidate(
        woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter", alignment, None,
    )
    g_w = 10.0 ** (candidate.trim_db["woofer"] / 20.0)
    g_t = 10.0 ** (candidate.trim_db["tweeter"] / 20.0)
    combined_ir = g_w * woofer_ir + alignment.polarity_sign * g_t * tweeter_ir

    prog = build_verify_program(fc_hz, sweep_s=1.5)
    pcm = render_program_pcm(prog)
    mono = fftconvolve(pcm[:, 0], combined_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(800), mono, np.zeros(5000)])
    cap = cap + np.random.default_rng(42).normal(0.0, 1e-5, cap.size)

    # This capture's OWN reflection gate clamps the tracking band above the
    # nominal Fc/2 — confirm the fixture actually exercises the clamp before
    # trusting the below/above assertions that depend on it.
    baseline = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=fc_hz, predicted_sum=(pred_freqs, pred_db)),
    )
    band_lo = baseline.verify_tracking["tracking_band_hz"][0]
    assert band_lo > fc_hz / 2.0

    def _predicted_with_bump(center_hz: float, *, width_octave: float = 0.06) -> tuple[np.ndarray, np.ndarray]:
        # A narrow (0.06-octave) log-Gaussian bump so it decays to
        # negligible well before the clamped band edge — a wide bump would
        # leak enough tail across the floor to confound the below/above
        # assertions below.
        with np.errstate(divide="ignore"):
            ratio = np.where(pred_freqs > 0, pred_freqs / center_hz, 1.0)
        bump = 10.0 * np.exp(-0.5 * (np.log2(ratio) / width_octave) ** 2)
        return pred_freqs, pred_db + bump

    below_center = band_lo * 0.75  # inside the nominal [Fc/2, 2Fc] band, below the clamped floor
    assert below_center > fc_hz / 2.0
    res_below = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=fc_hz, predicted_sum=_predicted_with_bump(below_center),
        ),
    )
    tracking_below = res_below.verify_tracking
    assert tracking_below["max_db_notch_excluded"] < 1.5  # excluded: not gated
    assert tracking_below["max_db_full_band"] > 5.0  # still visible as a diagnostic

    above_center = band_lo * 1.3  # comfortably above the clamped floor
    res_above = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(
            crossover_fc_hz=fc_hz, predicted_sum=_predicted_with_bump(above_center),
        ),
    )
    tracking_above = res_above.verify_tracking
    assert tracking_above["max_db_notch_excluded"] > 1.5  # not excluded: still gates


def test_build_candidate_raises_when_validity_floor_consumes_whole_band():
    """If a branch's reflection gate is tight enough that the clamped low
    edge reaches/exceeds the band's high edge, `_solve_trims`/`_ripple_db`
    raise on the now-empty mask rather than silently computing over nothing.
    This is deliberate: `jasper.web.correction_crossover_v2`'s existing
    catch-all seam already classifies an analyze-time ValueError as
    `internal_error` (its own comment names this exact case), so a
    degenerate floor surfaces through that EXISTING signal instead of a new,
    invented reason code."""
    fc_hz = 100.0  # hi = 200 Hz — easy for a close reflection's floor to exceed
    woofer_ir = _reflection_branch(
        300, 50.0, 500.0, 1.0,
        reflection_delay_s=1.5e-3, reflection_amp=1.0,  # floor ~ 1/0.0015 = 667 Hz > 200 Hz
    )
    tweeter_ir = _reflection_branch(300, 100.0, 500.0, 0.7)
    n_fft = _n_fft_for(woofer_ir, tweeter_ir)
    alignment = AlignmentEstimate(
        delay_us=0.0, raw_delay_us=0.0, parallax_us=0.0,
        polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
        confidence=0.9, status=ALIGNMENT_OK,
    )
    with pytest.raises(ValueError):
        _build_candidate(
            woofer_ir, tweeter_ir, SR, n_fft, fc_hz, "woofer", "tweeter", alignment, None,
        )


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def test_measure_requires_fc_prior():
    prog = build_measure_program({"woofer": -11.0, "tweeter": -13.0}, _roles())
    cap = _synthesize(
        prog,
        woofer_ir=_band_impulse(200, 150.0, 6000.0, 1.0),
        tweeter_ir=_band_impulse(225, 300.0, 20000.0, 0.7),
    )
    with pytest.raises(ValueError):
        analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())


def test_rate_mismatch_is_rejected():
    prog = build_verify_program(FC_HZ, sweep_s=0.6)
    cap = np.zeros(10_000)
    with pytest.raises(ValueError):
        analyze_program_capture(prog, cap, 44_100)
