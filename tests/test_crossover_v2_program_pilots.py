# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a composer extension: leading pilot pairs + woofer-repeat level agreement.

docs/historical/crossover-measurement-productization-design.md §5.2: every MEASURE and
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

import json
import logging
import math

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
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.audio_measurement.program_analysis import (
    AMBIENT_MIN_USABLE_FRACTION,
    INTEGRITY_CHECK_CLIPPED_RUN,
    INTEGRITY_CHECK_DISCONTINUITY_STEP,
    INTEGRITY_CHECK_FRAME_LEDGER,
    INTEGRITY_CHECK_RENDER_GAP,
    INTEGRITY_CHECK_REPEAT_EPSILON,
    INTEGRITY_CHECK_REPEAT_LEVEL,
    INTEGRITY_CHECK_SWEEP_HEARD,
    INTEGRITY_CHECK_SWEEP_SCHEDULE,
    INTEGRITY_CHECK_WITHIN_ROLE_DESYNC,
    INTEGRITY_FAIL,
    INTEGRITY_NOT_EVALUATED,
    INTEGRITY_PASS,
    PILOT_MIN_SNR_DB,
    REPEAT_LEVEL_TOLERANCE_DB,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    CaptureIntegrity,
    MeasurementGeometry,
    MeasurementPriors,
    SegmentLocation,
    _global_offset,
    _locate_segments,
    _pilot_ambient_samples,
    _verify_capture_integrity,
    analysis_diagnostic_summary,
    analyze_program_capture,
)

SR = 48_000
FC_HZ = 1600.0
GLOBAL_OFFSET = 900
_ANALYSIS_LOGGER = "jasper.audio_measurement.program_analysis"


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
    # The pair is preceded by its own room-listening window (issue #1810) —
    # without it `_pilot_observations` has no noise floor to judge the pilots
    # against and the SNR guard on the linearity verdict cannot fire.
    assert ids[:6] == [
        "ambient",
        "pilot_woofer_lo", "pilot_gap_woofer_lo",
        "pilot_woofer_hi", "pilot_gap_woofer_hi",
        "guard",
    ]
    assert ids[6:] == _N3_MEASURE_TAIL_IDS
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
    assert ids[:5] == [
        "ambient",
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


# --- analysis: the pilot SNR guard, live on MEASURE and VERIFY (#1810) --------


def _verify_pilot_program():
    return build_verify_program(
        FC_HZ, sweep_s=1.5,
        leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
    )


@pytest.mark.parametrize("phase", ["measure", "verify"])
def test_pilot_snr_is_measured_not_infinite(phase):
    """Issue #1810's structural defect, pinned.

    ``_pilot_in_band_snr_db`` returns ``+inf`` when there is no ambient
    evidence — "nothing to validate against, so nothing to distrust". Before
    the pre-pilot ambient window shipped, that was the ONLY value MEASURE and
    VERIFY could ever produce, so ``snr_valid = snr_db >= PILOT_MIN_SNR_DB``
    was satisfied unconditionally and the guard was dead code on both phases.

    A finite number here is the proof the window is actually being read.
    """
    prog = _measure_program() if phase == "measure" else _verify_pilot_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.pilot_snr_ok is True
    assert math.isfinite(res.pilots[0].snr_db)
    assert res.pilots[0].snr_db > PILOT_MIN_SNR_DB


@pytest.mark.parametrize("phase", ["measure", "verify"])
def test_pilots_drowned_in_room_noise_fail_snr_not_linearity(phase):
    """The JTS3 shape of 2026-07-28, reproduced.

    The correction had dropped the pilot band, so the quiet pilot sat just
    over the room floor and the noise compressed the captured two-pilot delta
    from 10 dB toward 6 dB. With the guard dead that read as a linearity
    failure and the household was told its phone's microphone had misbehaved.

    With the guard live the verdict is ``pilot_snr_ok=False``, and
    ``linearity_ok`` is UNKNOWN (``None``) — an untrustworthy estimate must
    never register as a linearity failure. Both halves are asserted: the
    second is what makes the conductor's routing honest rather than merely
    reordered.

    ``None``, not ``True``, since D7 of issue #1838: forcing True kept the
    verdict out of the FAILURE branch but made an unreadable capture read as
    a PASS to anything that did not also check ``pilot_snr_ok`` — which is
    how a session published ``linearity_ok=true`` beside a -60.9 dB captured
    delta against a programmed 10.0 dB. Both consumers of this flag branch on
    ``is False``, so the FAILURE-suppression property is unchanged.
    """
    prog = _measure_program() if phase == "measure" else _verify_pilot_program()
    cap = _synthesize(prog)
    # Attenuate the pilot pair (the correction dropping their band) and raise
    # the room floor across the whole capture — the ambient window included,
    # which is exactly why that window has to live next to the pilots. The
    # 30 dB drop is deeper than the session's measured 14-18 dB because this
    # fixture's synthetic "room" is far quieter than a real one; what the test
    # pins is the crossing of `PILOT_MIN_SNR_DB`, not the drop that gets there.
    for suffix in ("lo", "hi"):
        role = "woofer" if phase == "measure" else "summed"
        seg = prog.segment(f"pilot_{role}_{suffix}")
        start = GLOBAL_OFFSET + seg.start_sample
        cap[start:start + seg.n_samples] *= 10.0 ** (-30.0 / 20.0)
    cap = cap + np.random.default_rng(7).normal(0.0, 1e-2, cap.size)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.pilot_snr_ok is False
    assert res.pilots[0].snr_db < PILOT_MIN_SNR_DB
    assert res.linearity_ok is None
    assert res.linearity_ok is not False  # never the mic accusation


@pytest.mark.parametrize("phase", ["measure", "verify"])
def test_short_ambient_window_does_not_feed_the_channel_map_rise_test(phase):
    """The window is threaded into the level/SNR path ONLY.

    ``_channel_map_ok``'s TARGET test asks for a ``CHANNEL_MAP_TARGET_RISE_DB``
    (12 dB) rise over ambient. Feeding it this ~1 s spot estimate would
    reclassify the exact failure #1810 is about — a pilot pair sitting a few
    dB over the floor — as ``channel_map_mismatch``, a HARD STOP whose copy
    blames the speaker wiring. MEASURE/VERIFY therefore keep the
    total-in-band-energy-fraction fallback, whose tell is that neither rise
    number is computed.

    A CLEAN capture on that fallback publishes ``None``, not ``True`` (issue
    #2052): clearing the fraction says the window's energy sits in the band
    the pilot was scheduled in, which room noise alone can do, so it is not
    evidence the right driver produced it. The rise numbers stay the tell
    either way.
    """
    prog = _measure_program() if phase == "measure" else _verify_pilot_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    pilot = res.pilots[0]
    assert pilot.channel_map_ok is None
    assert pilot.channel_map_target_rise_db is None
    assert pilot.channel_map_cross_rise_db is None
    # ...and the session verdict is UNKNOWN, never a PASS these phases did
    # not measure and never the hard stop `all()` would have folded it into.
    assert res.channel_map_ok is None


@pytest.mark.parametrize("phase", ["measure", "verify"])
def test_measure_summary_omits_the_channel_map_flag_it_cannot_judge(phase):
    """A consequence with a live consumer, so it gets a pin of its own.

    `analysis_diagnostic_summary` drops a tri-state flag whose value is
    ``None``, and these phases never thread an ambient window into the
    channel-map check — so since #2052 a healthy MEASURE/VERIFY sidecar
    records no ``channel_map_ok`` at all, where it used to record ``true``.
    That is the honest record (the phase did not measure the map), but
    `harmonic_evidence.FIDELITY_FIELDS` treats an absent field as a
    reconstruction infidelity: keeping it would fail every banked corpus AND
    every new sidecar, reporting a deliberate semantic change as a broken
    replay. That tuple dropped the field; this pins the fact that made it
    necessary, so a change re-arming the flag on these phases has to come
    back through here.

    ``linearity_ok`` and ``pilot_snr_ok`` stay in the record, which is what
    makes the omission specific rather than the summary going quiet.
    """
    prog = _measure_program() if phase == "measure" else _verify_pilot_program()
    res = analyze_program_capture(
        prog, _synthesize(prog), SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    summary = analysis_diagnostic_summary(res)
    assert "channel_map_ok" not in summary
    assert summary["linearity_ok"] is True
    assert summary["pilot_snr_ok"] is True


@pytest.mark.parametrize("kept_samples_delta,expect_evidence", [(0, True), (-1, False)])
def test_pilot_ambient_min_usable_fraction_boundary(
    kept_samples_delta, expect_evidence,
):
    """`AMBIENT_MIN_USABLE_FRACTION` pinned AT its boundary, inclusive.

    Driven through `_pilot_ambient_samples` directly rather than through a
    truncated capture: the end-to-end path recovers ``global_offset`` by
    correlation, and a ±1-sample location error would flip a test that turns
    on an exact sample count. Here the offset is supplied, so "exactly at the
    fraction is KEPT, one sample under is dropped" is an exact statement
    about the constant.
    """
    prog = _measure_program()
    ambient = prog.segment("ambient")
    kept = int(AMBIENT_MIN_USABLE_FRACTION * ambient.n_samples) + kept_samples_delta
    # A capture that began exactly ``ambient.n_samples - kept`` samples into
    # the window: its schedule position is negative by that much.
    global_offset = -(ambient.start_sample + ambient.n_samples - kept)
    capture = np.zeros(prog.total_samples, dtype=np.float64)
    got = _pilot_ambient_samples(prog, capture, global_offset)
    if expect_evidence:
        assert got is not None and got.size == kept
    else:
        assert got is None


def test_pilot_ambient_samples_is_none_without_a_window():
    """A program with no room-listening window at all (legacy, or composed
    without leading pilots) yields no evidence rather than raising."""
    prog = _measure_program(with_pilots=False)
    capture = np.zeros(prog.total_samples, dtype=np.float64)
    assert _pilot_ambient_samples(prog, capture, 0) is None


@pytest.mark.parametrize(
    "keep_fraction,expect_evidence", [(0.7, True), (0.1, False)],
)
def test_late_started_capture_clips_the_ambient_window_never_slides_it(
    keep_fraction, expect_evidence,
):
    """A capture that began after the program did clips the window's HEAD.

    Two things must hold. (1) The window is clipped, not slid: computing its
    end from the clamped start instead of its own schedule position would
    walk it forward onto the first pilot and read that pilot as the room
    floor — a fabricated loud ambient, i.e. a false ``pilot_level_collapse``
    on a perfectly good capture. Above
    ``AMBIENT_MIN_USABLE_FRACTION`` the shortened window is still an
    honest floor, because RMS is length-independent. (2) Below that fraction
    there is nothing left to measure, and the analysis falls back to "no
    ambient evidence" — ``+inf`` SNR, pilots trusted — never to a guess.
    """
    prog = _measure_program()
    cap = _synthesize(prog)
    ambient = prog.segment("ambient")
    dropped = int((1.0 - keep_fraction) * ambient.n_samples)
    cut = GLOBAL_OFFSET + ambient.start_sample + dropped
    res = analyze_program_capture(
        prog, cap[cut:], SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.pilot_snr_ok is True
    if expect_evidence:
        assert math.isfinite(res.pilots[0].snr_db)
        assert res.pilots[0].snr_db > PILOT_MIN_SNR_DB
    else:
        assert res.pilots[0].snr_db == math.inf


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

    off_plain, _first_plain, stim_plain, _amb = _global_offset(plain, cap_plain, SR)
    locs_plain = {
        loc.segment_id: loc
        for loc in _locate_segments(plain, cap_plain, SR, off_plain, stim_plain)
    }
    off_prelude, _first_prelude, stim_prelude, _amb = _global_offset(prelude, cap_prelude, SR)
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
    global_offset, _first, stimuli, _amb = _global_offset(prog, cap, SR)
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
    global_offset, first, _stimuli, _amb = _global_offset(prog, cap, SR)
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


# --- VERIFY capture integrity (issue #1971) -----------------------------------
#
# Before this, ``glitch_detected`` was structurally False on EVERY verify-shaped
# analysis: it came from ``_estimate_drift``, which needs a repeat pair a mono
# summed sweep does not have, and the flow's two splice gates
# (``_sweep_schedule_ok`` / ``_sweep_locate_confidence_ok``) both filter
# ``KIND_SWEEP`` while VERIFY's sweep is ``KIND_SUMMED_SWEEP``. The checks
# below are the substitutes the 2026-07-31 P0 repeat-floor bench had to
# assemble by hand.


def _unreported_ledger(received_frames: int = 1_000_000):
    """The frame ledger of a capture whose page sent no report (issue #2094).

    Every builder test below is asking a question about the SIGNAL checks, so
    each hands in the shape that leaves the two frame-accounting checks
    unevaluated — which is also what the single-capture runner and any
    pre-#2094 page bundle actually produce.
    """
    return reconcile_capture_frames(None, received_frames=received_frames)


def _statuses(integrity) -> dict[str, str]:
    return {c.name: c.status for c in integrity.checks}


def _reasons(integrity) -> dict[str, str]:
    return {c.name: c.reason for c in integrity.checks}


def _splice(cap: np.ndarray, at: int, samples: int) -> np.ndarray:
    """Insert ``samples`` of silence at ``at`` -- one discrete timeline step,
    the 2026-07-27 hardware shape (everything after it arrives LATE)."""
    return np.concatenate([cap[:at], np.zeros(samples), cap[at:]])


def test_verify_clean_capture_records_a_real_integrity_verdict():
    """The three evaluable checks pass, and the four that CANNOT run here say
    so by name instead of quietly reading as a pass."""
    prog = _verify_pilot_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    integrity = res.capture_integrity
    assert integrity is not None
    assert _statuses(integrity) == {
        # #2094: this fixture hands in raw samples, so there is no capture-page
        # report to reconcile and both frame-accounting checks say so by name
        # rather than passing on evidence nobody supplied.
        INTEGRITY_CHECK_RENDER_GAP: INTEGRITY_NOT_EVALUATED,
        INTEGRITY_CHECK_FRAME_LEDGER: INTEGRITY_NOT_EVALUATED,
        INTEGRITY_CHECK_SWEEP_HEARD: INTEGRITY_PASS,
        INTEGRITY_CHECK_SWEEP_SCHEDULE: INTEGRITY_PASS,
        INTEGRITY_CHECK_CLIPPED_RUN: INTEGRITY_PASS,
        INTEGRITY_CHECK_REPEAT_EPSILON: INTEGRITY_NOT_EVALUATED,
        INTEGRITY_CHECK_REPEAT_LEVEL: INTEGRITY_NOT_EVALUATED,
        INTEGRITY_CHECK_WITHIN_ROLE_DESYNC: INTEGRITY_NOT_EVALUATED,
        INTEGRITY_CHECK_DISCONTINUITY_STEP: INTEGRITY_NOT_EVALUATED,
    }
    # Every not-evaluated check says WHY, and an evaluated one carries no
    # reason (the field is the unevaluated-only explanation, not a comment).
    reasons = _reasons(integrity)
    assert all(reasons[name] for name in integrity.not_evaluated)
    assert reasons[INTEGRITY_CHECK_SWEEP_HEARD] == ""
    # The measured figures the verdicts were drawn from, disclosed on a PASS
    # too -- a clean corpus needs the same fields a failing one has.
    assert integrity.locate_confidence_min is not None
    assert integrity.locate_confidence_min >= SWEEP_LOCATE_CONFIDENCE_FLOOR
    assert integrity.schedule_residual_ms_worst is not None
    assert (
        abs(integrity.schedule_residual_ms_worst) <= SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
    )
    assert integrity.clipped_segments == ()
    assert integrity.glitched is False
    assert res.glitch_detected is False


def test_verify_splice_fails_the_schedule_check_and_flips_glitch_detected():
    """The defect #1971 names, reproduced: a mid-capture insertion between the
    leading pilots and the summed sweep. It used to leave ``glitch_detected``
    False because nothing on this path ever looked."""
    prog = _verify_pilot_program()
    cap = _synthesize(prog)
    sweep = prog.segment("sweep_verify")
    inserted = int(round(2.0 * SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * SR))
    spliced = _splice(cap, GLOBAL_OFFSET + sweep.start_sample - 100, inserted)
    res = analyze_program_capture(
        prog, spliced, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    integrity = res.capture_integrity
    assert integrity is not None
    assert integrity.failed == (INTEGRITY_CHECK_SWEEP_SCHEDULE,)
    assert integrity.glitched is True
    assert res.glitch_detected is True
    # The sweep was heard perfectly well -- this is a splice, not a level
    # problem, and the record must not confuse the two.
    assert _statuses(integrity)[INTEGRITY_CHECK_SWEEP_HEARD] == INTEGRITY_PASS
    # SIGNED, and in the right direction: everything after the insertion
    # arrived LATE.
    assert integrity.schedule_residual_ms_worst is not None
    assert integrity.schedule_residual_ms_worst > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS


def test_verify_clip_fails_the_clipped_run_check():
    prog = _verify_pilot_program()
    cap = _synthesize(prog)
    sweep = prog.segment("sweep_verify")
    clipped = cap.copy()
    at = GLOBAL_OFFSET + sweep.start_sample + 5_000
    clipped[at:at + 8] = 1.0
    res = analyze_program_capture(
        prog, clipped, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    integrity = res.capture_integrity
    assert integrity is not None
    assert integrity.failed == (INTEGRITY_CHECK_CLIPPED_RUN,)
    assert integrity.clipped_segments == ("sweep_verify",)
    assert res.glitch_detected is True


def test_verify_analysis_always_carries_an_integrity_record():
    """``None`` means "no evidence" to the conductor's gate, so the live
    analyze seam must never produce it -- on either VERIFY program shape."""
    for prog in (
        _verify_pilot_program(),
        build_verify_program(FC_HZ, sweep_s=1.5),  # legacy, pilot-less
    ):
        res = analyze_program_capture(
            prog, _synthesize(prog), SR,
            priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        )
        assert res.capture_integrity is not None
        # The one-bit projection can never disagree with the record it
        # summarizes -- both are assigned from the same expression.
        assert res.glitch_detected == res.capture_integrity.glitched


def test_measure_analysis_carries_no_verify_integrity_record():
    """Scope: MEASURE keeps ``_estimate_drift`` as its glitch owner. A record
    here would be a second answer to one question."""
    prog = _measure_program()
    res = analyze_program_capture(
        prog, _synthesize(prog), SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.capture_integrity is None
    assert res.drift is not None


def test_verify_integrity_rides_the_retained_capture_sidecar():
    """The operator forensic dump carries the record flat, like every other
    block in ``analysis_diagnostic_summary``."""
    prog = _verify_pilot_program()
    cap = _synthesize(prog)
    sweep = prog.segment("sweep_verify")
    clipped = cap.copy()
    at = GLOBAL_OFFSET + sweep.start_sample + 5_000
    clipped[at:at + 8] = 1.0
    summary = analysis_diagnostic_summary(analyze_program_capture(
        prog, clipped, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    ))
    assert summary["integrity_failed"] == INTEGRITY_CHECK_CLIPPED_RUN
    assert INTEGRITY_CHECK_REPEAT_EPSILON in summary["integrity_not_evaluated"]
    assert summary["integrity_clipped_segments"] == "sweep_verify"
    assert summary["integrity_locate_confidence_min"] is not None
    assert summary["integrity_schedule_residual_ms_worst"] is not None


# --- the not-evaluated paths, at the builder (deterministic locations) --------


def _sweep_location(*, confidence: float, residual: float) -> SegmentLocation:
    return SegmentLocation(
        segment_id="sweep_verify", kind="summed_sweep", role=None,
        scheduled_start=1_000, located_start=int(1_000 + residual),
        residual_samples=residual, confidence=confidence,
        peak_dbfs=-12.0, clipped=False,
    )


def test_unheard_sweep_leaves_the_schedule_check_not_evaluated():
    """#1838's D3 rule, carried onto VERIFY: a sweep the locator could barely
    find lands in the wrong place and THEN shows a huge residual. Reporting
    that residual as a splice would name the symptom, not the cause -- so the
    schedule check records not-evaluated while the measured residual is still
    disclosed as evidence."""
    prog = _verify_pilot_program()
    huge = 10.0 * SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * SR
    integrity = _verify_capture_integrity(prog, SR, [
        _sweep_location(
            confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR - 0.01, residual=huge,
        ),
    ], _unreported_ledger())
    statuses = _statuses(integrity)
    assert statuses[INTEGRITY_CHECK_SWEEP_HEARD] == INTEGRITY_FAIL
    assert statuses[INTEGRITY_CHECK_SWEEP_SCHEDULE] == INTEGRITY_NOT_EVALUATED
    assert "not confidently located" in (
        _reasons(integrity)[INTEGRITY_CHECK_SWEEP_SCHEDULE]
    )
    # Failed, but only on the check that was actually asked.
    assert integrity.failed == (INTEGRITY_CHECK_SWEEP_HEARD,)
    assert integrity.schedule_residual_ms_worst == pytest.approx(huge / SR * 1000.0)


def test_sweep_exactly_at_the_confidence_floor_is_heard():
    """``>=`` at the floor -- the same direction ``_sweep_locate_confidence_ok``
    trusts it. The two copies are pinned equal by
    tests/test_measurement_integrity_floor_contracts.py, so their comparison
    senses have to agree too or the shared number means two things."""
    prog = _verify_pilot_program()
    integrity = _verify_capture_integrity(prog, SR, [
        _sweep_location(confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR, residual=0.0),
    ], _unreported_ledger())
    assert _statuses(integrity)[INTEGRITY_CHECK_SWEEP_HEARD] == INTEGRITY_PASS


def test_no_summed_sweep_located_is_not_evaluated_never_a_pass():
    prog = _verify_pilot_program()
    integrity = _verify_capture_integrity(prog, SR, [], _unreported_ledger())
    statuses = _statuses(integrity)
    assert statuses[INTEGRITY_CHECK_SWEEP_HEARD] == INTEGRITY_NOT_EVALUATED
    assert statuses[INTEGRITY_CHECK_SWEEP_SCHEDULE] == INTEGRITY_NOT_EVALUATED
    # Nothing to inspect for a clipped run either -- "no stimulus, so nothing
    # was clipped" would be the same didn't-look pass in a new place.
    assert statuses[INTEGRITY_CHECK_CLIPPED_RUN] == INTEGRITY_NOT_EVALUATED
    assert integrity.failed == ()
    # A record of nothing but not-evaluated is UNEXAMINED, not clean -- and
    # ``glitched`` is not what tells the two apart; ``not_evaluated`` is.
    assert integrity.glitched is False
    assert set(integrity.not_evaluated) == set(statuses)
    assert integrity.locate_confidence_min is None
    assert integrity.schedule_residual_ms_worst is None


def test_default_capture_integrity_is_empty_not_clean():
    """A bare ``CaptureIntegrity()`` reports nothing at all: no checks, no
    failures, no not-evaluated. A consumer asking "was this examined" reads
    the checks, never the bool."""
    empty = CaptureIntegrity()
    assert empty.checks == ()
    assert empty.failed == () and empty.not_evaluated == ()
    assert empty.glitched is False


def test_integrity_to_dict_is_json_safe_and_carries_the_reasons():
    prog = _verify_pilot_program()
    integrity = _verify_capture_integrity(prog, SR, [
        _sweep_location(confidence=0.9, residual=3.0),
    ], _unreported_ledger())
    record = json.loads(json.dumps(integrity.to_dict()))
    assert record["glitched"] is False
    by_name = {c["name"]: c for c in record["checks"]}
    assert by_name[INTEGRITY_CHECK_SWEEP_HEARD]["status"] == INTEGRITY_PASS
    assert "reason" not in by_name[INTEGRITY_CHECK_SWEEP_HEARD]
    assert by_name[INTEGRITY_CHECK_REPEAT_EPSILON]["reason"]
    assert record["clipped_segments"] == []


def test_integrity_warns_only_on_a_failure_and_names_both_lists(caplog):
    """The stable ``event=`` line a corpus sweep greps -- the VERIFY twin of
    ``program_analysis.glitch``. It names BOTH lists, because a reader has to
    be able to tell "a check failed" from "a check never ran"."""
    prog = _verify_pilot_program()
    over_ceiling = SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * 1e-3 * SR * 3
    with caplog.at_level(logging.WARNING, logger=_ANALYSIS_LOGGER):
        _verify_capture_integrity(prog, SR, [
            _sweep_location(confidence=0.9, residual=over_ceiling),
        ], _unreported_ledger())
    assert "event=program_analysis.capture_integrity" in caplog.text
    assert f"failed={INTEGRITY_CHECK_SWEEP_SCHEDULE}" in caplog.text
    assert INTEGRITY_CHECK_REPEAT_EPSILON in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_ANALYSIS_LOGGER):
        _verify_capture_integrity(prog, SR, [
            _sweep_location(confidence=0.9, residual=0.0),
        ], _unreported_ledger())
    assert "event=program_analysis.capture_integrity" not in caplog.text
