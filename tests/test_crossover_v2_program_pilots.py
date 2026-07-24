# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a composer extension: leading pilot pairs + woofer-repeat level agreement.

docs/crossover-measurement-productization-design.md §5.2: every MEASURE and
VERIFY program also opens with a short two-level pilot pair so EACH capture
carries its own linearity evidence, and MEASURE acceptance additionally
requires the woofer repeat pair to agree in level within ±0.3 dB (a
gain-riding detector complementing the timing baselines). A repeat-level
failure REUSES the ``drift_baselines_disagree`` verdict (``glitch_detected``)
— never a new user-facing code.

Fixture style mirrors tests/test_audio_measurement_program_analysis.py:
captures are composed by convolving each program channel with a synthetic
band-passed driver IR, then perturbed (AGC on the hi pilot, a level step on
the repeat sweep).
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    KIND_COURTESY_TONE,
    KIND_PILOT,
    KIND_SILENCE,
    RoleBand,
    build_measure_program,
    build_verify_program,
    render_program_pcm,
)
from jasper.audio_measurement.program_analysis import (
    REPEAT_LEVEL_TOLERANCE_DB,
    MeasurementGeometry,
    MeasurementPriors,
    _global_offset,
    _locate_segments,
    analyze_program_capture,
)

SR = 48_000
FC_HZ = 1600.0
GLOBAL_OFFSET = 900


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _measure_program(with_pilots: bool = True, courtesy_prelude: bool = False):
    kwargs = {}
    if with_pilots:
        kwargs = {
            "leading_pilot_gains_db": (-22.0, -12.0),
            "pilot_duration_s": 0.5,
        }
    return build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 1.0, "tweeter": 0.8},
        courtesy_prelude=courtesy_prelude,
        **kwargs,
    )


def _band_impulse(delay: int, f_lo: float, f_hi: float, amp: float, n: int = 4096) -> np.ndarray:
    imp = np.zeros(n)
    imp[delay] = amp
    spectrum = np.fft.rfft(imp)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    return np.fft.irfft(spectrum, n)


def _synthesize(program, *, noise: float = 1e-4, seed: int = 0) -> np.ndarray:
    pcm = render_program_pcm(program)
    woofer_ir = _band_impulse(200, 150.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(225, 300.0, 20000.0, 0.7)
    mono = np.zeros(pcm.shape[0], dtype=np.float64)
    mono += fftconvolve(pcm[:, 0], woofer_ir)[: pcm.shape[0]]
    if pcm.shape[1] >= 2:
        mono += fftconvolve(pcm[:, 1], tweeter_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(GLOBAL_OFFSET), mono, np.zeros(20_000)])
    if noise:
        cap = cap + np.random.default_rng(seed).normal(0.0, noise, cap.size)
    return cap


# --- composer layout ----------------------------------------------------------


_N3_MEASURE_TAIL_IDS = [
    "sweep_w", "gap_w_t", "sweep_t", "gap_t_w",
    "sweep_w_rep", "gap_w_t_rep", "sweep_t_rep", "gap_t_w_rep",
    "sweep_w_rep2", "gap_w_t_rep2", "sweep_t_rep2",
    "tail",
]


def test_measure_default_layout_is_unchanged_without_pilots():
    """Opt-out (no leading pilot pair) still gets the N=3 interleaved shape
    (sweep-composition PR-A, #1668) — pilots are the only thing this opt-out
    controls, not the repeat count."""
    prog = _measure_program(with_pilots=False)
    ids = [seg.segment_id for seg in prog.segments]
    assert ids == ["guard", *_N3_MEASURE_TAIL_IDS]


def test_measure_leading_pilot_pair_layout_and_gains():
    prog = _measure_program()
    ids = [seg.segment_id for seg in prog.segments]
    assert ids[:5] == [
        "pilot_woofer_lo", "pilot_gap_woofer_lo",
        "pilot_woofer_hi", "pilot_gap_woofer_hi",
        "guard",
    ]
    assert ids[5:] == _N3_MEASURE_TAIL_IDS
    lo = prog.segment("pilot_woofer_lo")
    hi = prog.segment("pilot_woofer_hi")
    assert lo.kind == KIND_PILOT and hi.kind == KIND_PILOT
    assert lo.role == "woofer" and lo.channel == 0
    assert hi.gain_db - lo.gain_db == pytest.approx(10.0)
    # Pilots ride the woofer's measurement band.
    assert lo.f1_hz == pytest.approx(150.0)
    assert lo.f2_hz == pytest.approx(6000.0)


def test_measure_pilot_role_selectable_and_validated():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 1.0, "tweeter": 0.8},
        leading_pilot_gains_db=(-22.0, -12.0),
        leading_pilot_role="tweeter",
    )
    assert prog.segment("pilot_tweeter_lo").channel == 1
    with pytest.raises(ValueError):
        build_measure_program(
            {"woofer": -11.0, "tweeter": -13.0}, _roles(),
            leading_pilot_gains_db=(-22.0, -12.0),
            leading_pilot_role="mid",
        )
    with pytest.raises(ValueError):
        build_measure_program(
            {"woofer": -11.0, "tweeter": -13.0}, _roles(),
            leading_pilot_gains_db=(-22.0,),
        )


def test_verify_leading_pilot_pair_is_mono_summed():
    prog = build_verify_program(
        FC_HZ, sweep_s=1.5,
        leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
    )
    assert prog.channels == 1
    ids = [seg.segment_id for seg in prog.segments]
    assert ids[:4] == [
        "pilot_summed_lo", "pilot_gap_summed_lo",
        "pilot_summed_hi", "pilot_gap_summed_hi",
    ]
    pilot = prog.segment("pilot_summed_lo")
    assert pilot.channel == 0 and pilot.role == "summed"
    # Opt-out stays the legacy layout.
    legacy = build_verify_program(FC_HZ, sweep_s=1.5)
    assert [s.segment_id for s in legacy.segments] == ["guard", "sweep_verify", "tail"]


def test_pilot_gaps_are_silence():
    prog = _measure_program()
    for seg in prog.segments:
        if seg.segment_id.startswith("pilot_gap_"):
            assert seg.kind == KIND_SILENCE


# --- analysis: per-capture linearity ------------------------------------------


def test_measure_pilot_linearity_clean_capture_passes():
    prog = _measure_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        geometry=MeasurementGeometry(),
    )
    assert res.linearity_ok is True
    assert len(res.pilots) == 1
    assert res.pilots[0].captured_delta_db == pytest.approx(10.0, abs=0.5)
    # measurement-honesty gate G3's raw material (crossover_v2_flow.py): the
    # HI segment's own declared gain, published so the conductor's VERIFY
    # inter-attempt consistency check can compute a transfer without binding
    # back to this ExcitationProgram instance.
    hi = prog.segment("pilot_woofer_hi")
    assert res.pilots[0].programmed_hi_gain_db == pytest.approx(hi.gain_db)
    # The pilot pair does not perturb the sweep-anchored drift verdict.
    assert not res.glitch_detected
    assert res.candidate is not None


def test_measure_pilot_linearity_fails_under_simulated_agc():
    prog = _measure_program()
    cap = _synthesize(prog)
    # AGC-compress the HI pilot only: programmed 10 dB delta captured as ~4 dB.
    hi = prog.segment("pilot_woofer_hi")
    start = GLOBAL_OFFSET + hi.start_sample
    cap[start:start + hi.n_samples] *= 10.0 ** (-6.0 / 20.0)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.linearity_ok is False


def test_legacy_measure_program_reports_no_pilot_verdict():
    """No pilots ⇒ linearity is ``None`` (no evidence), not True/False."""
    prog = _measure_program(with_pilots=False)
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.linearity_ok is None
    assert res.pilots == ()


def test_verify_pilot_linearity_verdict_present():
    prog = build_verify_program(
        FC_HZ, sweep_s=1.5,
        leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
    )
    cap = _synthesize(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.linearity_ok is True
    assert res.summed_ripple_db is not None
    # measurement-honesty gate G3's raw material — VERIFY's own leading
    # pilot (role "summed") publishes its programmed HI gain too.
    hi = prog.segment("pilot_summed_hi")
    assert res.pilots[0].programmed_hi_gain_db == pytest.approx(hi.gain_db)


# --- analysis: woofer-repeat level agreement ------------------------------------
#
# The gate is first-vs-LAST located woofer occurrence (sweep-composition
# PR-A, #1668 — see `_estimate_drift`'s docstring): under the N=3 default the
# LAST occurrence is "sweep_w_rep2", not "sweep_w_rep" (the middle one) — the
# three tests below perturb "sweep_w_rep2" so they keep exercising the gate
# that actually runs against the shipped default, not a middle repeat the
# gate does not see (that coverage gap is a named, deferred scope note, not
# a bug — see the same docstring's "Scope note").


def test_repeat_level_step_is_flagged_as_glitch():
    """A >±0.3 dB level step between the woofer's first and last sweeps ⇒
    glitch verdict.

    Timing is untouched (pure amplitude scale on the repeat window), so the
    drift baselines agree — the LEVEL check alone must trip, and it reuses the
    same ``glitch_detected`` verdict (⇒ ``drift_baselines_disagree`` reason,
    §5.2)."""
    prog = _measure_program()
    cap = _synthesize(prog)
    rep = prog.segment("sweep_w_rep2")
    start = GLOBAL_OFFSET + rep.start_sample
    cap[start:start + rep.n_samples] *= 10.0 ** (-1.0 / 20.0)  # 1 dB quieter
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.drift is not None
    # The timing baselines still agree — the level detector is what fires.
    assert abs(res.drift.epsilon_ppm) < 100.0
    assert res.glitch_detected is True


def test_repeat_level_within_tolerance_is_clean():
    prog = _measure_program()
    cap = _synthesize(prog)
    rep = prog.segment("sweep_w_rep2")
    start = GLOBAL_OFFSET + rep.start_sample
    delta_db = REPEAT_LEVEL_TOLERANCE_DB * 0.5
    cap[start:start + rep.n_samples] *= 10.0 ** (-delta_db / 20.0)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.glitch_detected is False


def test_repeat_level_lf_transient_does_not_false_reject():
    """A brief sub-band (60 Hz) room-mode-style transient spikes one sweep's
    full-band single-sample PEAK without moving its in-band RMS — the fixed
    estimator must not false-reject it.

    Root cause (2026-07-20, real hardware): two Dayton iMM-6C and UMIK-2
    MEASURE captures each showed two genuinely-identical woofer sweeps 0.64 dB
    apart by full-band peak (a low-frequency room mode below the woofer's own
    150 Hz band dominates the raw single-sample peak) but only 0.06-0.24 dB
    apart by in-band RMS — see `REPEAT_LEVEL_TOLERANCE_DB`'s comment. This
    fixture reproduces that shape synthetically: the transient sits at 60 Hz,
    below the woofer's declared [150, 6000] Hz band, so `_band_power`'s
    FFT bandpass mask filters it out of the in-band RMS estimate entirely
    while it still dominates `_peak_dbfs`'s raw sample max.
    """
    prog = _measure_program()
    cap = _synthesize(prog)
    rep = prog.segment("sweep_w_rep2")
    start = GLOBAL_OFFSET + rep.start_sample
    n = 480  # 10 ms at 48 kHz, a handful of cycles at 60 Hz
    t = np.arange(n) / SR
    transient = 0.6 * np.sin(2 * np.pi * 60.0 * t)  # dwarfs the ~0.33 sweep peak
    mid = start + rep.n_samples // 2
    cap[mid:mid + n] += transient
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    by_id = {loc.segment_id: loc for loc in res.locations}
    # SegmentLocation.peak_dbfs is the untouched full-band peak (still used
    # for clip-run reporting / the pilot gain-solve reference) — confirms the
    # OLD estimator would have tripped the glitch on this same capture.
    old_style_delta = abs(by_id["sweep_w"].peak_dbfs - by_id["sweep_w_rep2"].peak_dbfs)
    assert old_style_delta > REPEAT_LEVEL_TOLERANCE_DB
    assert res.glitch_detected is False


def test_repeat_level_gate_stays_woofer_anchored_middle_and_tweeter_steps_pass():
    """Pins the scope note in `_estimate_drift`'s docstring (sweep-composition
    PR-A, #1668): the level gate is woofer first-vs-LAST only. A level step
    confined to the woofer's MIDDLE repeat, or to any tweeter occurrence
    (never covered by this gate, before or after #1668), must NOT trip
    ``glitch_detected`` — deferred to a future PR's G2 hardening, not a bug
    here."""
    prog = _measure_program()

    cap_middle = _synthesize(prog)
    middle = prog.segment("sweep_w_rep")  # occurrence 2 of 3 -- not first/last
    start = GLOBAL_OFFSET + middle.start_sample
    cap_middle[start:start + middle.n_samples] *= 10.0 ** (-1.0 / 20.0)
    res_middle = analyze_program_capture(
        prog, cap_middle, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res_middle.glitch_detected is False

    cap_tweeter = _synthesize(prog)
    tweeter_last = prog.segment("sweep_t_rep2")
    start = GLOBAL_OFFSET + tweeter_last.start_sample
    cap_tweeter[start:start + tweeter_last.n_samples] *= 10.0 ** (-1.0 / 20.0)
    res_tweeter = analyze_program_capture(
        prog, cap_tweeter, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res_tweeter.glitch_detected is False


def test_measure_predicted_sum_travels_for_verify():
    """MEASURE now exports the predicted applied sum VERIFY compares against."""
    prog = _measure_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.predicted_sum is not None
    freqs, mag_db = res.predicted_sum
    assert freqs.shape == mag_db.shape
    assert freqs.size > 0


# --------------------------------------------------------------------------- #
# courtesy-tone prelude (issue #1677): locate/schedule/analysis unaffected
# --------------------------------------------------------------------------- #
#
# The prelude adds a longer pre-roll ahead of everything ``_synthesize``
# already convolves/captures; these pins confirm the relative-offset locate
# math (``_global_offset``/``_locate_segments``) absorbs it exactly the way
# it already absorbed sweep-composition PR-A lengthening MEASURE, and that
# every downstream gate (linearity, drift/glitch, candidate build) reaches
# the SAME verdict on the SAME underlying capture with or without it.


def test_measure_courtesy_prelude_clean_capture_still_passes_every_gate():
    prog = _measure_program(courtesy_prelude=True)
    assert prog.segment("courtesy_tone_ch0").kind == KIND_COURTESY_TONE
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        geometry=MeasurementGeometry(),
    )
    assert res.linearity_ok is True
    assert res.glitch_detected is False
    assert res.candidate is not None


def test_measure_courtesy_prelude_sweep_residuals_match_no_prelude_capture():
    """The longer pre-roll must not degrade sweep-locate precision at all: on
    an otherwise-identical synthetic capture (same noise seed), every located
    sweep's residual-from-schedule and confidence are the SAME with or
    without the prelude -- the relative-offset locate math (``_global_offset``
    anchors on the first REAL stimulus either way) fully absorbs the extra
    pre-roll rather than merely tolerating it."""
    plain = _measure_program(courtesy_prelude=False)
    prelude = _measure_program(courtesy_prelude=True)
    cap_plain = _synthesize(plain, seed=7)
    cap_prelude = _synthesize(prelude, seed=7)

    off_plain, _first_plain, stim_plain = _global_offset(plain, cap_plain, SR)
    locs_plain = {
        loc.segment_id: loc
        for loc in _locate_segments(plain, cap_plain, SR, off_plain, stim_plain)
    }
    off_prelude, _first_prelude, stim_prelude = _global_offset(prelude, cap_prelude, SR)
    locs_prelude = {
        loc.segment_id: loc
        for loc in _locate_segments(prelude, cap_prelude, SR, off_prelude, stim_prelude)
    }
    for seg_id in ("sweep_w", "sweep_t", "sweep_w_rep", "sweep_t_rep", "sweep_w_rep2", "sweep_t_rep2"):
        plain_loc = locs_plain[seg_id]
        prelude_loc = locs_prelude[seg_id]
        # rel=1e-4: the two captures differ in overall array length (the
        # prelude's extra 3.6 s), so the downsample/resample step inside
        # ``_global_offset`` can round a few ULPs differently -- this is
        # floating-point noise, not a locate-precision regression, so the
        # tolerance is loose enough to absorb it while still being far
        # tighter than any value that would indicate a real behavior change.
        assert prelude_loc.residual_samples == pytest.approx(plain_loc.residual_samples, abs=0.01)
        assert prelude_loc.confidence == pytest.approx(plain_loc.confidence, rel=1e-4)
        # Sanity floor so this test would actually catch a real regression
        # (not just two equally-broken locates agreeing with each other).
        assert plain_loc.confidence > 0.5


def test_measure_courtesy_prelude_tone_segments_located_like_silence():
    """The tone segments themselves are recorded exactly like a silence
    segment in ``_locate_segments`` -- no search, residual 0, confidence 1 --
    because they are never in STIMULUS_KINDS."""
    prog = _measure_program(courtesy_prelude=True)
    cap = _synthesize(prog)
    global_offset, _first, stimuli = _global_offset(prog, cap, SR)
    locations = _locate_segments(prog, cap, SR, global_offset, stimuli)
    by_id = {loc.segment_id: loc for loc in locations}
    for seg_id in ("courtesy_tone_ch0", "courtesy_tone_ch1"):
        loc = by_id[seg_id]
        assert loc.kind == KIND_COURTESY_TONE
        assert loc.residual_samples == 0.0
        assert loc.confidence == 1.0
        assert loc.located_start == loc.scheduled_start


def test_measure_courtesy_prelude_first_stimulus_is_still_the_leading_pilot():
    """``_global_offset`` must keep correlating against the first REAL
    stimulus (the leading pilot), never the tone -- confirmed indirectly: the
    reported global offset lands the pilot at (or very near) its own
    scheduled position once the correlation is resolved."""
    prog = _measure_program(courtesy_prelude=True)
    cap = _synthesize(prog)
    global_offset, first, _stimuli = _global_offset(prog, cap, SR)
    assert first.segment_id == "pilot_woofer_lo"
    assert first.kind == KIND_PILOT


def test_verify_courtesy_prelude_clean_capture_still_passes():
    prog = build_verify_program(
        FC_HZ, sweep_s=1.5,
        leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
        courtesy_prelude=True,
    )
    assert prog.segment("courtesy_tone_ch0").kind == KIND_COURTESY_TONE
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.linearity_ok is True
    assert res.summed_ripple_db is not None
