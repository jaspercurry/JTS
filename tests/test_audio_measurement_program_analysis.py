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
    MeasurementGeometry,
    MeasurementPriors,
    _gcc_phat,
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


def _check_capture(program, *, compress_hi: bool = False, seed: int = 3):
    pcm = render_program_pcm(program)
    woofer_ir = _band_impulse(200, 150.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(225, 300.0, 20000.0, 0.7)
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
    prog = build_check_program(_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.linearity_ok is True
    assert res.channel_map_ok is True
    for pilot in res.pilots:
        assert pilot.captured_delta_db == pytest.approx(10.0, abs=0.5)


def test_check_linearity_fails_under_simulated_agc():
    prog = build_check_program(_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog, compress_hi=True)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    # The programmed 10 dB delta is captured as ~4 dB ⇒ linearity fails loud.
    assert res.linearity_ok is False


def test_check_gain_plan_targets_measure_window_with_guard():
    prog = build_check_program(_roles(), ambient_s=2.0, pilot_duration_s=0.6)
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
    prog = build_check_program(_roles(), ambient_s=2.0, pilot_duration_s=0.6)
    cap = _check_capture(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors())
    assert res.ambient_report is not None
    assert res.ambient_report["bands"]


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
