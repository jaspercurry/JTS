# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The harmonic-distortion read-out, against a nonlinearity whose answer is known.

The synthetic case is the load-bearing one: a memoryless polynomial has an
ANALYTIC harmonic content, so passing a real program sweep through it and
running the shipped read gives a number that can be right or wrong rather than
merely plausible. The wrap fixtures pin the reason this module exists at all —
the production deconvolution window is far too short to contain the images.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from jasper.audio_measurement import deconv
from jasper.audio_measurement.distortion import (
    BAND_EDGE_TRIM_OCTAVES,
    DEFAULT_HARMONIC_ORDERS,
    DriveLevel,
    analysis_band_hz,
    harmonic_reading_from_ir,
    order_band_hz,
    preceding_silence_s,
    read_segment_distortion,
    required_pre_guard_s,
    segment_sweep_meta,
)
from jasper.audio_measurement.program import (
    FrequencyBand,
    RoleBand,
    build_measure_program,
    render_program_pcm,
)
from jasper.audio_measurement.program_analysis import DECONV_PRE_GUARD_S

# The bands and gains the owner's 2-way speaker actually measured with, taken
# from the banked series-2 rounds (`series2-state-r1b-preapply.json`'s
# `gain_plan_db`, and the sweep bands whose program_id reproduces that round's
# banked id). Real numbers rather than round ones, so the fixtures exercise the
# `L` values the corpus has.
WOOFER_BAND_HZ = (150.0, 4000.0)
TWEETER_BAND_HZ = (1600.0, 20000.0)
GAIN_PLAN_DB = {"woofer": -6.484436, "tweeter": -33.498763}
LEAD_SAMPLES = 24_000  # 0.5 s of capture before the program starts


def _program(repeat_count: int = 1):
    return build_measure_program(
        GAIN_PLAN_DB,
        (
            RoleBand("woofer", 0, FrequencyBand(*WOOFER_BAND_HZ)),
            RoleBand("tweeter", 1, FrequencyBand(*TWEETER_BAND_HZ)),
        ),
        repeat_count=repeat_count,
    )


def _captured_through(program, *, a: float, b: float) -> np.ndarray:
    """The program played through ``y = x + a·x² + b·x³`` and heard as mono.

    The two program channels are summed because a microphone hears one signal;
    the sweeps are scheduled sequentially, so no summing artefact lands inside
    a sweep window. The nonlinearity is applied to the summed signal exactly as
    a real (memoryless) amplifier/driver nonlinearity would act on it.
    """
    pcm = np.asarray(render_program_pcm(program), dtype=np.float64)
    mono = pcm.sum(axis=1)
    distorted = mono + a * mono**2 + b * mono**3
    return np.concatenate((np.zeros(LEAD_SAMPLES), distorted))


def _expected_relative_db(order: int, *, amplitude: float, a: float, b: float) -> float:
    """Analytic H-``order``-to-fundamental ratio for ``x + a·x² + b·x³``.

    For ``x = A·sin φ``: ``x²`` contributes ``a·A²/2`` at 2φ, ``x³`` contributes
    ``b·A³/4`` at 3φ **and** ``3b·A³/4`` back onto the fundamental — which is
    why the denominator is not simply ``A``. Ignoring that term would misstate
    both ratios by the same ~0.1 dB at these coefficients, so it is kept.
    """
    fundamental = amplitude * (1.0 + 3.0 * b * amplitude**2 / 4.0)
    harmonic = (
        a * amplitude**2 / 2.0 if order == 2 else abs(b) * amplitude**3 / 4.0
    )
    return 20.0 * math.log10(harmonic / fundamental)


@pytest.mark.parametrize("a,b", ((0.03, 0.01), (0.008, 0.004)))
def test_recovers_the_analytic_harmonics_of_a_known_nonlinearity(a, b):
    """End to end: real program → known nonlinearity → the shipped read.

    Tolerance is 1.0 dB, matching the bar
    ``tests/test_audio_measurement_harmonics.py`` already holds the underlying
    primitives to. The read adds 1/12-octave smoothing and a band mask on top of
    them; neither should move a ratio that is flat in frequency, and this asserts
    that it does not.
    """
    program = _program()
    capture = _captured_through(program, a=a, b=b)
    segment = program.segment("sweep_w")
    reading = read_segment_distortion(
        program, capture, "sweep_w", LEAD_SAMPLES + segment.start_sample,
    )
    amplitude = 10.0 ** (segment.gain_db / 20.0)

    # Graded away from the sweep edges, where the fade and the band limit make
    # the deconvolution's own shoulders — not the nonlinearity — dominant. The
    # top is 1200 Hz because H3 of the 4 kHz-topped woofer sweep leaves the
    # deconvolution passband at f2/3 = 1333 Hz (see `order_band_hz`).
    interior = (reading.freqs_hz >= 300.0) & (reading.freqs_hz <= 1200.0)
    assert interior.sum() > 100
    for order in DEFAULT_HARMONIC_ORDERS:
        want = _expected_relative_db(order, amplitude=amplitude, a=a, b=b)
        got = reading.relative_db[order][interior]
        assert np.all(np.isfinite(got))
        assert np.max(np.abs(got - want)) < 1.0, (
            f"H{order}: worst {np.max(np.abs(got - want)):.2f} dB from the "
            f"analytic {want:.2f} dB"
        )
        # The nonlinearity is well clear of the read's own floor, or the
        # agreement above would be a coincidence of two noise estimates.
        assert not np.any(reading.floor_limited(order)[interior])


def test_a_linear_path_reads_as_floor_not_as_distortion():
    """The negative control the parametrized case needs.

    With ``a = b = 0`` there is no harmonic content at all, so every point must
    come back floor-limited and ``worst()`` must refuse to name one. Without
    this, a read that simply returned its own noise would pass the test above
    for small coefficients.

    This is also the fixture that sized :data:`BAND_EDGE_TRIM_OCTAVES`: before
    the trim, the sweep's fade-in put a 25.8 dB excursion at ``f1`` on this
    provably-linear path — an artefact the read would have reported as the
    speaker's worst distortion. Both sweeps are checked because their band edges
    behave differently, and the top edge is checked with the bottom so an
    asymmetry that reversed would be caught rather than assumed away.
    """
    program = _program()
    capture = _captured_through(program, a=0.0, b=0.0)
    for segment_id in ("sweep_w", "sweep_t"):
        segment = program.segment(segment_id)
        reading = read_segment_distortion(
            program, capture, segment_id, LEAD_SAMPLES + segment.start_sample,
        )
        for order in DEFAULT_HARMONIC_ORDERS:
            separation = (
                reading.relative_db[order] - reading.floor_relative_db[order]
            )
            assert np.all(reading.floor_limited(order)), (
                f"{segment_id} H{order}: {np.nanmax(separation):.1f} dB above its "
                f"own floor at "
                f"{reading.freqs_hz[int(np.nanargmax(separation))]:.0f} Hz, on a "
                f"path with no nonlinearity at all"
            )
            assert math.isnan(reading.worst(order)[1])


def test_the_production_pre_guard_wraps_every_image_and_ours_does_not():
    """The trap this module exists to step around, pinned as a relationship.

    ``DECONV_PRE_GUARD_S`` is correct for its own job (containing the linear
    IR's non-causal shoulder) and is deliberately not changed. It is simply far
    shorter than ``L·ln N``, so at that window the images have wrapped off the
    front of the circular deconvolution and ``extract_harmonic_ir`` raises. If a
    future sweep shape ever made the production guard sufficient, this fixture
    fails and the extra deconvolution here stops being needed.
    """
    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)
    segment = program.segment("sweep_w")
    meta = segment_sweep_meta(segment)
    anchor = LEAD_SAMPLES + segment.start_sample
    rate = program.sample_rate_hz
    needed = required_pre_guard_s(meta, DEFAULT_HARMONIC_ORDERS)

    assert needed > DECONV_PRE_GUARD_S
    assert deconv.harmonic_time_advance_s(meta, 2) > DECONV_PRE_GUARD_S

    from jasper.audio_measurement.program_analysis import _deconvolve_window

    wrapped, pre = _deconvolve_window(
        capture, segment, anchor, rate, pre_guard_s=DECONV_PRE_GUARD_S,
    )
    peak = int(np.argmax(np.abs(wrapped)))
    for order in DEFAULT_HARMONIC_ORDERS:
        with pytest.raises(ValueError, match="crosses"):
            deconv.extract_harmonic_ir(wrapped, rate, peak, meta, order)
    assert pre == round(DECONV_PRE_GUARD_S * rate)

    # At the derived guard every window is reachable and the read succeeds.
    reading = read_segment_distortion(program, capture, "sweep_w", anchor)
    assert reading.pre_guard_s >= needed - 1.0 / rate
    assert set(reading.relative_db) == set(DEFAULT_HARMONIC_ORDERS)


def test_the_pre_guard_covers_every_window_the_read_cuts():
    """Images AND phantom floors, whichever reaches furthest back.

    :func:`required_pre_guard_s` is the single answer to "how much capture does
    this reading need", and it is only trustworthy if it covers the floor
    windows too — a floor that wrapped is worse than no floor, because it
    reports the array's tail as the instrument's noise. Asserted as a property
    over both window families rather than against a hardcoded number, so it
    keeps holding if either geometry moves.
    """
    from jasper.audio_measurement.distortion import (
        _image_half_width_s,
        _phantom_window_s,
    )

    for segment_id in ("sweep_w", "sweep_t"):
        meta = segment_sweep_meta(_program().segment(segment_id))
        guard = required_pre_guard_s(meta, DEFAULT_HARMONIC_ORDERS)
        for order in (1, *DEFAULT_HARMONIC_ORDERS):
            edge = deconv.harmonic_time_advance_s(meta, order) + _image_half_width_s(
                meta, order
            )
            assert guard > edge, f"{segment_id} H{order} image"
        for order in DEFAULT_HARMONIC_ORDERS:
            centre, half = _phantom_window_s(meta, order)
            assert half > 0.0
            assert guard > centre + half, f"{segment_id} H{order} floor"


def test_the_floor_window_sits_nearer_the_arrival_than_its_image():
    """The placement that makes the floor conservative rather than optimistic.

    Deconvolution artefacts decay away from the direct arrival, so a floor
    measured FURTHER out than the image under-states it — and a floor that is
    too low is how artefact gets reported as distortion. Pinned as a direction,
    which is the property ``test_a_linear_path_reads_as_floor_not_as_distortion``
    depends on and the reason that test failed under the opposite placement.
    """
    from jasper.audio_measurement.distortion import _phantom_window_s

    meta = segment_sweep_meta(_program().segment("sweep_w"))
    for order in DEFAULT_HARMONIC_ORDERS:
        centre, _half = _phantom_window_s(meta, order)
        assert centre < deconv.harmonic_time_advance_s(meta, order)
        assert centre > deconv.harmonic_time_advance_s(meta, order - 1)


def test_every_shipped_measure_sweep_has_room_for_its_own_images():
    """The schedule's gaps clear the windows this read cuts — for all six sweeps.

    MESM gaps are sized by the PRECEDING sweep's ``L``
    (``program.mesm_gap_samples``) while these windows are sized by the
    FOLLOWING sweep's own, so the two are not the same quantity and their
    ordering is a fact about the shipped program rather than a guarantee of its
    construction. The tightest margin is 0.322 s, on the woofer repeats (whose
    1.805 s gap is sized by the shorter tweeter sweep that precedes them). A
    duration or band change that inverted the ordering would put the affected
    sweep's H3 window inside the previous driver's audio, and this fixture is
    what says so.
    """
    program = _program(repeat_count=3)
    for segment in program.stimulus_segments():
        if not segment.segment_id.startswith(("sweep_w", "sweep_t")):
            continue
        meta = segment_sweep_meta(segment)
        needed = required_pre_guard_s(meta, DEFAULT_HARMONIC_ORDERS)
        assert preceding_silence_s(program, segment) > needed, segment.segment_id


def test_the_band_stops_at_the_deconvolution_passband_not_at_nyquist():
    """Order ``N`` is real to ``f2/N``, because the inversion kills the rest.

    The bound that actually binds is the sweep's own top, not Nyquist: nothing
    above ``f2`` survives division by ``|X|² + ε``. Getting this wrong is not a
    small error — a Nyquist-bounded band would have reported the woofer as
    superbly clean from 1.3 to 4 kHz, where in truth its H3 products are being
    annihilated by the regularizer. Both edges are pinned because both appear
    in the shipped MEASURE program.
    """
    program = _program()
    trim = 2.0**BAND_EDGE_TRIM_OCTAVES
    woofer = segment_sweep_meta(program.segment("sweep_w"))
    assert order_band_hz(woofer, 2) == (pytest.approx(150.0 * trim), 2000.0)
    assert order_band_hz(woofer, 3)[1] == pytest.approx(4000.0 / 3.0)
    assert analysis_band_hz(woofer, (2, 3)) == order_band_hz(woofer, 3)
    # Nyquist would have allowed far more; it is not the operative bound.
    assert order_band_hz(woofer, 3)[1] < woofer.sample_rate / 2.0 / 3.0

    tweeter = segment_sweep_meta(program.segment("sweep_t"))
    assert order_band_hz(tweeter, 2) == (pytest.approx(1600.0 * trim), 10000.0)
    assert order_band_hz(tweeter, 3)[1] == pytest.approx(20000.0 / 3.0)

    with pytest.raises(ValueError, match="at least one harmonic order"):
        analysis_band_hz(woofer, ())
    # A sweep narrower than the order's span has no honest band at all.
    with pytest.raises(ValueError, match="too narrow for order 13"):
        order_band_hz(tweeter, 13)


def test_each_order_reaches_its_own_edge_and_no_further():
    """H2 keeps the half-octave H3 has already lost, and the total keeps neither.

    The shared grid runs to the LOWEST order's edge, each order is NaN past its
    own, and THD — which needs every term — stops at the highest order's. Pinned
    together because the three extents are easy to collapse into one and the
    collapse is silent: a THD curve extended past H3's edge simply bends
    downwards, looking like a clean driver.
    """
    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)
    segment = program.segment("sweep_w")
    reading = read_segment_distortion(
        program, capture, "sweep_w", LEAD_SAMPLES + segment.start_sample,
    )
    # H2's edge is the top, because H2 is the lowest order requested.
    assert reading.band_hz == (pytest.approx(150.0 * 2.0**BAND_EDGE_TRIM_OCTAVES), 2000.0)

    h3_edge, h2_edge = 4000.0 / 3.0, 2000.0
    above_h3 = reading.freqs_hz > h3_edge
    assert above_h3.any()
    assert np.all(np.isnan(reading.relative_db[3][above_h3]))
    assert np.all(np.isfinite(reading.relative_db[2][reading.freqs_hz <= h2_edge]))
    assert np.all(np.isnan(reading.thd_percent[above_h3]))
    assert np.all(np.isfinite(reading.thd_percent[reading.freqs_hz <= h3_edge]))
    # NaN must read as "not a driver reading", never as "clear of the floor".
    assert np.all(reading.floor_limited(3)[above_h3])


def test_calibration_is_applied_at_each_curve_s_own_acoustic_frequency():
    """A harmonic is corrected at ``N·f``, never at ``f``.

    The ratio is NOT cal-invariant — it carries ``C(N·f) − C(f)`` — so a curve
    with a deliberate step between ``f`` and ``2f`` must move H2 by the step and
    leave a flat-region point alone. This is the fixture that would fail if the
    calibration were ever applied on the excitation axis by mistake, which is
    the easy error to make because that is the axis the arrays are keyed on.
    """
    from jasper.audio_measurement.calibration import CalibrationCurve

    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)
    segment = program.segment("sweep_w")
    anchor = LEAD_SAMPLES + segment.start_sample

    flat = read_segment_distortion(program, capture, "sweep_w", anchor)
    # A +6 dB step whose transition (700-750 Hz) is far from every probe below,
    # and far outside the 1/12-octave smoothing kernel around them, so the
    # assertions read the step and not the interpolation across it.
    stepped = read_segment_distortion(
        program, capture, "sweep_w", anchor,
        calibration=CalibrationCurve(
            freqs_hz=[10.0, 700.0, 750.0, 24000.0],
            correction_db=[0.0, 0.0, 6.0, 6.0],
        ),
    )
    # f = 500 Hz: the fundamental (500) is below the step, H2 (1000) above it,
    # so the ratio must rise by the full 6 dB.
    straddling = int(np.argmin(np.abs(flat.freqs_hz - 500.0)))
    # f = 1000 Hz: fundamental (1000) and H2 (2000) are both above it, so the
    # step cancels and the ratio must not move at all.
    both_above = int(np.argmin(np.abs(flat.freqs_hz - 1000.0)))
    assert stepped.relative_db[2][straddling] - flat.relative_db[2][
        straddling
    ] == pytest.approx(6.0, abs=0.2)
    assert stepped.relative_db[2][both_above] - flat.relative_db[2][
        both_above
    ] == pytest.approx(0.0, abs=0.2)


def test_a_capture_that_starts_too_close_to_the_sweep_is_refused():
    """No lead means the images wrapped, and a wrapped read must not be returned.

    The refusal is the whole safety property: with too little capture in front
    of the sweep the window clamps at the head, the images land at negative
    indices, and anything reported would be the array's tail.
    """
    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)[LEAD_SAMPLES:]
    segment = program.segment("sweep_w")
    with pytest.raises(ValueError, match="starts too close"):
        read_segment_distortion(
            program, capture, "sweep_w", segment.start_sample - 48_000,
        )


def test_harmonic_reading_rejects_orders_it_cannot_mean():
    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)
    segment = program.segment("sweep_w")
    anchor = LEAD_SAMPLES + segment.start_sample
    for orders, match in (
        ((1, 2), "at least 2"),
        ((2, 2), "distinct"),
        ((), "at least one"),
    ):
        with pytest.raises(ValueError, match=match):
            read_segment_distortion(program, capture, "sweep_w", anchor, orders=orders)


def test_a_band_outside_the_real_one_is_refused_not_silently_widened():
    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)
    segment = program.segment("sweep_w")
    anchor = LEAD_SAMPLES + segment.start_sample
    with pytest.raises(ValueError, match="does not overlap"):
        read_segment_distortion(
            program, capture, "sweep_w", anchor, band_hz=(8000.0, 20000.0),
        )
    narrowed = read_segment_distortion(
        program, capture, "sweep_w", anchor, band_hz=(0.0, 1000.0),
    )
    # Intersected, never widened: the caller's floor of 0 Hz cannot reach below
    # the band-edge trim, and its ceiling of 1 kHz binds over H2's 2 kHz edge.
    assert narrowed.band_hz == (
        pytest.approx(150.0 * 2.0**BAND_EDGE_TRIM_OCTAVES), 1000.0,
    )


def test_drive_level_names_every_reference_it_reports():
    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)
    segment = program.segment("sweep_w")
    reading = read_segment_distortion(
        program, capture, "sweep_w", LEAD_SAMPLES + segment.start_sample,
    )
    drive = reading.drive
    assert drive.stimulus_peak_dbfs == pytest.approx(segment.gain_db)
    assert drive.effective_peak_dbfs == pytest.approx(segment.effective_peak_dbfs)
    # The mic-side pair describes the recording, so it must differ from the
    # played-side pair and sit below full scale.
    assert drive.capture_rms_dbfs < drive.capture_peak_dbfs < 0.0
    assert reading.images_clean and reading.clearance_s > 0.0


def test_the_ir_seam_is_the_whole_read_and_records_what_it_is_told():
    """:func:`harmonic_reading_from_ir` alone, against the wrapper that cuts for it.

    The IR-level function is the seam an in-session caller lands on — it holds
    an already-deconvolved IR and no capture — so it must be the ENTIRE read:
    handed the same IR ``read_segment_distortion`` cuts, it must reproduce that
    reading bin for bin. The two context fields it cannot know (``pre_guard_s``,
    ``preceding_silence_s``) are recorded verbatim and never used — proved by
    the bin equality across DIFFERENT values of them — and a caller that leaves
    them unstated gets NaN back, whose clearance must not read as clean.
    """
    from jasper.audio_measurement.program_analysis import _deconvolve_window

    program = _program()
    capture = _captured_through(program, a=0.03, b=0.01)
    segment = program.segment("sweep_w")
    anchor = LEAD_SAMPLES + segment.start_sample
    rate = program.sample_rate_hz
    meta = segment_sweep_meta(segment)
    needed = required_pre_guard_s(meta, DEFAULT_HARMONIC_ORDERS)

    via_wrapper = read_segment_distortion(program, capture, "sweep_w", anchor)

    full_ir, _pre = _deconvolve_window(
        capture, segment, anchor, rate, pre_guard_s=needed,
    )
    drive = DriveLevel(0.0, 0.0, 0.0, 0.0)
    peak = int(np.argmax(np.abs(full_ir)))
    direct = harmonic_reading_from_ir(
        full_ir, rate, peak, meta,
        segment_id="sweep_w", role=None, drive=drive,
        pre_guard_s=needed, preceding_silence_s=needed + 0.25,
    )

    np.testing.assert_array_equal(direct.freqs_hz, via_wrapper.freqs_hz)
    np.testing.assert_array_equal(direct.fundamental_db, via_wrapper.fundamental_db)
    for order in DEFAULT_HARMONIC_ORDERS:
        np.testing.assert_array_equal(
            direct.relative_db[order], via_wrapper.relative_db[order]
        )
        np.testing.assert_array_equal(
            direct.floor_relative_db[order], via_wrapper.floor_relative_db[order]
        )
    np.testing.assert_array_equal(direct.thd_percent, via_wrapper.thd_percent)

    # Recorded verbatim; a stated clearance of +0.25 s reads clean.
    assert direct.pre_guard_s == needed
    assert direct.preceding_silence_s == needed + 0.25
    assert direct.images_clean and direct.clearance_s == pytest.approx(0.25)

    # Unstated context defaults to NaN — and a reading that cannot state its
    # window cleanliness must not claim it.
    unstated = harmonic_reading_from_ir(
        full_ir, rate, peak, meta, segment_id="sweep_w", role=None, drive=drive,
    )
    assert math.isnan(unstated.pre_guard_s)
    assert math.isnan(unstated.preceding_silence_s)
    assert not unstated.images_clean


# --------------------------------------------------------------------------- #
# corpus smoke — skipped wherever captures/ is not checked out (i.e. on CI)
# --------------------------------------------------------------------------- #

_CORPUS = Path(__file__).resolve().parents[1] / "captures" / "xover-series2-2026-08-17"


@pytest.mark.skipif(not _CORPUS.is_dir(), reason="capture corpus not present")
def test_the_banked_corpus_reconstructs_and_reads():
    """One banked capture, through the same path the replay CLI drives.

    Guards the join between this module and the real corpus: the program
    reconstruction still reproduces the round's banked ``program_id`` (so the
    sweep ``L`` is the played one), and the read comes back on the band and with
    the clearance the schedule implies. Deliberately one capture and one
    segment — this is a wiring check, not the campaign.
    """
    import json
    import wave

    state = json.loads((_CORPUS / "series2-state-r1b-preapply.json").read_text())
    wavs = sorted((_CORPUS / "e0-r1b").glob("*_measure.wav"))
    if not wavs:
        pytest.skip("round r1b holds no MEASURE capture")

    # The session volume is banked nowhere, so it is solved against the
    # program id exactly as `scripts/harmonic-distortion-replay.py` solves it.
    # Repeated here rather than imported: the CLI's filename is not a module
    # name, and renaming a lab tool to suit a test is the wrong direction.
    from jasper.active_speaker.crossover_v2_flow import (
        COURTESY_PRELUDE_ENABLED,
        PILOT_LEVEL_DELTA_DB,
    )

    gains = state["gain_plan_db"]
    program = None
    for downstream in np.arange(-40.0, 0.01, 0.5):
        candidate = build_measure_program(
            {r: float(gains[r]) for r in ("woofer", "tweeter")},
            (
                RoleBand("woofer", 0, FrequencyBand(*WOOFER_BAND_HZ)),
                RoleBand("tweeter", 1, FrequencyBand(*TWEETER_BAND_HZ)),
            ),
            downstream_gain_db=float(downstream),
            leading_pilot_gains_db=(
                float(gains["woofer"]) - PILOT_LEVEL_DELTA_DB, float(gains["woofer"]),
            ),
            leading_pilot_role="woofer",
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )
        if candidate.program_id == state["candidate"]["program_id"]:
            program = candidate
            break
    assert program is not None, "banked program_id no longer reconstructs"

    with wave.open(str(wavs[0])) as handle:
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    capture = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if channels > 1:
        capture = capture[::channels]

    from jasper.audio_measurement.program_analysis import _global_offset

    global_offset, _f, _stimuli, _amb = _global_offset(
        program, capture, program.sample_rate_hz
    )
    segment = program.segment("sweep_w")
    reading = read_segment_distortion(
        program, capture, "sweep_w", global_offset + segment.start_sample,
    )
    assert reading.role == "woofer"
    # H2's own edge: the 4 kHz sweep top over order 2, trimmed at the bottom.
    assert reading.band_hz == (
        pytest.approx(150.0 * 2.0**BAND_EDGE_TRIM_OCTAVES), 2000.0,
    )
    assert reading.images_clean
    # The woofer's own distortion is well clear of the floor in the bass, which
    # is the finding this whole path exists to produce.
    assert not np.all(reading.floor_limited(3))
