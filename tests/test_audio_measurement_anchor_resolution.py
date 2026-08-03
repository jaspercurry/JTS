# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Timeline-anchor resolution (issue #2093): a knife-edge peak gate must not
fabricate ``locate_failed`` on a pristine capture.

``_global_offset`` pins the WHOLE capture timeline on the program's first
stimulus, which for the v2 programs is the deliberately QUIETEST thing in
them (VERIFY's ``pilot_summed_lo``, 10 dB under ``pilot_summed_hi``).
``_earliest_strong_peak``'s "earliest lag within 0.6x of the max" gate is
level-blind by construction, so which side of that gate the quiet pilot lands
on is decided by its local SNR rather than by its waveform. Miss it and the
anchor snaps to the loud pilot, the whole timeline shifts by the pilot
spacing, and -- because every segment is then searched only at
``scheduled +/- SEGMENT_SEARCH_S`` (+/-30 ms) -- every segment reads as "not
found".

Live evidence this suite encodes (2026-08-03 cloud VERIFY, 11 captures of one
speaker, all retained; measured by re-running the analyzer's own code over the
banked WAVs):

===========  ==============  ==============  ================  ==============
capture      NCC @ pilot_lo  gate (0.6xpk)   margin            sweep confidence
===========  ==============  ==============  ================  ==============
8 healthy    0.4725-0.6523   0.4091-0.4677   +0.0188..+0.1846  0.7206-0.8190
3 "failed"   0.3491-0.4276   0.3936-0.4324   -0.0048..-0.0490  0.0194-0.0967
===========  ==============  ==============  ================  ==============

The nearest pass and the nearest fail are 0.024 apart -- a knife edge. On all
three failures the anchor error was identically +1296.5 ms (the independently
measured pilot spacing, 1.2972 s), and re-anchoring them correctly scores the
same sweeps at **0.7313 / 0.8202 / 0.6671** -- healthy, and squarely among the
eight that passed. The audio never distinguished pass from fail; the anchor
did. Households were told "Couldn't hear the speaker clearly. Check the
volume..." about recordings in which the speaker is plainly audible.

The captures themselves are ~1.65 MB each of household room audio and are NOT
shipped here (``tests/`` carries no binary fixtures at all). Instead the
fixtures below reproduce the decision GEOMETRY: a quiet first pilot pushed
just past the gate while the summed sweep stays pristine, which is exactly the
live shape -- the analyzer inventing "I could not hear the sweep" about a
sweep sitting untouched in the capture.

``test_pre_fix_offset_would_have_failed_this_capture`` is the mutation guard
kept permanently in the suite: it recomputes the offset the pre-#2093 way and
asserts that timeline DOES fail, so the repair tests can never pass vacuously
on a fixture that quietly stopped reproducing the bug.
"""
from __future__ import annotations

import logging

import numpy as np
import pytest
from scipy.signal import fftconvolve, resample_poly

from jasper.audio_measurement.program import (
    KIND_SUMMED_SWEEP,
    STIMULUS_KINDS,
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
    render_program_pcm,
    segment_stimulus,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program_analysis import (
    INTEGRITY_CHECK_SWEEP_HEARD,
    LOCATOR_RATE_HZ,
    SEGMENT_SEARCH_S,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    _earliest_strong_peak,
    _global_offset,
    _locate_segments,
    _stimulus_shape,
    analyze_program_capture,
)

SR = 48_000
FC_HZ = 1600.0
GLOBAL_OFFSET = 900
_ANALYSIS_LOGGER = "jasper.audio_measurement.program_analysis"

# Burst level (dB relative to the captured quiet pilot) that carries the pilot
# past the 0.6 gate. Measured on this fixture: +0 dB stays on pilot_lo, +3 dB
# and above snaps to pilot_hi. +6 dB sits clear of the edge on both sides --
# far enough in to be stable, far enough from the +15 dB level where the burst
# starts tripping the unrelated clipped-run check.
KNIFE_EDGE_BURST_DB = 6.0


def _band_impulse(delay: int, f_lo: float, f_hi: float, amp: float, n: int = 4096):
    imp = np.zeros(n)
    imp[delay] = amp
    spectrum = np.fft.rfft(imp)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    return np.fft.irfft(spectrum, n)


def _band_noise(n: int, f_lo: float, f_hi: float, rms: float, seed: int):
    x = np.random.default_rng(seed).normal(0.0, 1.0, n)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    y = np.fft.irfft(spectrum, n)
    return y / (np.sqrt(np.mean(y**2)) + 1e-18) * rms


def _check_roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _verify_program(*, with_pilots: bool = True):
    kwargs = {}
    if with_pilots:
        kwargs = {"leading_pilot_gains_db": (-22.0, -12.0), "pilot_duration_s": 0.5}
    return build_verify_program(FC_HZ, sweep_s=1.5, courtesy_prelude=True, **kwargs)


def _pristine(program, *, noise: float = 1e-4, seed: int = 0) -> np.ndarray:
    """A clean capture: the program through one band-passed driver IR."""
    pcm = render_program_pcm(program)
    ir = _band_impulse(200, 150.0, 6000.0, 1.0)
    mono = np.zeros(pcm.shape[0], dtype=np.float64)
    for ch in range(pcm.shape[1]):
        mono += fftconvolve(pcm[:, ch], ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(GLOBAL_OFFSET), mono, np.zeros(20_000)])
    return cap + np.random.default_rng(seed).normal(0.0, noise, cap.size)


def _knife_edge(program, *, burst_db: float = KNIFE_EDGE_BURST_DB) -> np.ndarray:
    """A capture that is pristine EXCEPT for an in-band noise burst confined to
    the quiet pilot -- the household shape (someone speaks, the HVAC kicks in)
    that pushes the quiet pilot past the level-blind gate. The summed sweep is
    bit-identical to :func:`_pristine`'s.
    """
    cap = _pristine(program)
    pilot = program.segment("pilot_summed_lo")
    start = GLOBAL_OFFSET + pilot.start_sample
    window = cap[start:start + pilot.n_samples]
    rms = float(np.sqrt(np.mean(window**2)))
    cap[start:start + pilot.n_samples] += _band_noise(
        pilot.n_samples, 150.0, 1000.0, rms * 10 ** (burst_db / 20.0), seed=11
    )
    return cap


def _pilot_spacing(program) -> int:
    return (program.segment("pilot_summed_hi").start_sample
            - program.segment("pilot_summed_lo").start_sample)


def _coarse_peak(program, capture) -> int:
    """What the level-blind gate alone reports, before any interpretation --
    the raw ``_earliest_strong_peak`` locate at :data:`LOCATOR_RATE_HZ`."""
    stim = np.asarray(
        segment_stimulus(program.segment("pilot_summed_lo")), dtype=np.float64
    )
    down = max(1, int(round(SR / LOCATOR_RATE_HZ)))
    return _earliest_strong_peak(
        resample_poly(capture, 1, down), resample_poly(stim, 1, down)
    ) * down


def _located_arrival(program, capture) -> int:
    """The refined arrival sample, read back out of ``_global_offset``'s own
    return values (offset + the anchor it chose) rather than re-implementing
    its coarse/refine pair here -- a copy of that would drift.

    The fix changed only which SEGMENT this sample is taken to be, never the
    sample, so this is also exactly the pre-#2093 arrival.
    """
    offset, anchor, _stimuli = _global_offset(program, capture, SR)
    return offset + anchor.start_sample


def _witness_chosen_for(program) -> str:
    """The witness ``_resolve_anchor`` actually selected, read off its event
    line on a pristine capture of ``program``."""
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger(_ANALYSIS_LOGGER)
    log.addHandler(handler)
    level = log.level
    log.setLevel(logging.INFO)
    try:
        _global_offset(program, _pristine(program), SR)
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    line = next(r.getMessage() for r in records
                if "event=program_analysis.anchor" in r.getMessage())
    return next(tok.split("=", 1)[1] for tok in line.split()
                if tok.startswith("witness="))


def _sweep_confidence(analysis) -> float:
    return min(loc.confidence for loc in analysis.locations
               if loc.kind == KIND_SUMMED_SWEEP)


# --------------------------------------------------------------------------- #
# the fixture genuinely reproduces the bug (precondition for everything below)
# --------------------------------------------------------------------------- #


def test_knife_edge_fixture_really_mis_locks_the_raw_locator():
    """The level-blind gate lands on the LOUD pilot, off by exactly the pilot
    spacing -- the live signature (+1296.5 ms on all three 2026-08-03
    failures, which is that session's 1.2972 s spacing).

    Without this the repair tests could pass vacuously on a fixture that had
    quietly stopped reproducing the bug.
    """
    prog = _verify_program()
    spacing = _pilot_spacing(prog)
    true_lo = GLOBAL_OFFSET + prog.segment("pilot_summed_lo").start_sample

    assert abs(_coarse_peak(prog, _pristine(prog)) - true_lo) < 0.030 * SR
    mis = _coarse_peak(prog, _knife_edge(prog)) - true_lo
    # Off by the pilot spacing, to within the locator's own coarse resolution.
    assert abs(mis - spacing) < 0.030 * SR
    # ...and that is far outside the per-segment search window, which is the
    # whole reason a mis-anchor reads as "every segment missing".
    assert abs(mis) > 0.030 * SR


def test_pre_fix_offset_would_have_failed_this_capture():
    """The mutation, kept in the suite: recompute the offset the pre-#2093 way
    (``arrival - first.start_sample``, no cross-check) and confirm THAT
    timeline collapses -- sub-floor sweep confidence, the same
    ``summed_sweep_heard`` failure households were shown.

    This is what makes the repair tests load-bearing: if someone reverts
    ``_resolve_anchor``, the assertions above/below stop describing a repair
    and this one stops describing a bug, so the pair cannot both stay green.
    """
    prog = _verify_program()
    cap = _knife_edge(prog)
    first = prog.segment("pilot_summed_lo")
    pre_fix_offset = _located_arrival(prog, cap) - first.start_sample

    locations = _locate_segments(prog, cap, SR, pre_fix_offset, {})
    confidence = min(loc.confidence for loc in locations
                     if loc.kind == KIND_SUMMED_SWEEP)
    assert confidence < SWEEP_LOCATE_CONFIDENCE_FLOOR

    # And the repaired offset on the SAME capture is a different timeline.
    repaired_offset, anchor, _stimuli = _global_offset(prog, cap, SR)
    assert repaired_offset != pre_fix_offset
    assert anchor.segment_id == "pilot_summed_hi"


# --------------------------------------------------------------------------- #
# the repair
# --------------------------------------------------------------------------- #


def test_knife_edge_capture_locates_the_sweep_exactly_as_the_pristine_one():
    """The point of the whole fix: the sweep is untouched between the two
    captures, so the analyzer must read it identically. Before #2093 the
    burst-on-the-quiet-pilot capture reported a sub-floor confidence and a
    ``summed_sweep_heard`` failure about a sweep that had not changed by one
    sample.
    """
    prog = _verify_program()
    pristine = analyze_program_capture(prog, _pristine(prog), SR)
    repaired = analyze_program_capture(prog, _knife_edge(prog), SR)

    assert _sweep_confidence(repaired) == pytest.approx(
        _sweep_confidence(pristine), abs=1e-4
    )
    assert _sweep_confidence(repaired) > SWEEP_LOCATE_CONFIDENCE_FLOOR
    assert INTEGRITY_CHECK_SWEEP_HEARD not in repaired.capture_integrity.failed
    assert repaired.capture_integrity.failed == ()


def test_repaired_anchor_puts_the_sweep_back_at_its_true_position():
    """Not just "confident": the sweep is located where it ACTUALLY is in the
    capture.

    Asserted against the fixture's known true position, deliberately NOT
    against ``residual_samples`` -- a located start is confined to
    ``scheduled +/- SEGMENT_SEARCH_S`` by construction, so its residual can
    never exceed that window no matter how wrong the anchor is. A residual
    assertion here would be tautological and would stay green under a full
    revert of the fix.
    """
    prog = _verify_program()
    cap = _knife_edge(prog)
    offset, _anchor, stimuli = _global_offset(prog, cap, SR)
    locations = _locate_segments(prog, cap, SR, offset, stimuli)
    sweep = next(loc for loc in locations if loc.kind == KIND_SUMMED_SWEEP)
    true_start = GLOBAL_OFFSET + prog.segment(sweep.segment_id).start_sample
    assert abs(sweep.located_start - true_start) < 0.030 * SR


# --------------------------------------------------------------------------- #
# it must not become a "find the sweep anyway" machine
# --------------------------------------------------------------------------- #


def test_garbage_capture_still_fails_and_the_anchor_does_not_move():
    """Noise only -- no program in it at all. Every candidate anchor scores in
    the noise, so the cross-check declines to move (no corroboration), and the
    capture is refused exactly as it was before the fix. Re-anchoring only ever
    changes WHERE the analyzer looks; it never invents a confidence.
    """
    prog = _verify_program()
    garbage = np.random.default_rng(4).normal(
        0.0, 1e-2, prog.total_samples + GLOBAL_OFFSET + 20_000
    )
    offset, anchor, _stimuli = _global_offset(prog, garbage, SR)

    assert anchor.segment_id == "pilot_summed_lo"
    assert offset == _located_arrival(prog, garbage) - anchor.start_sample

    res = analyze_program_capture(prog, garbage, SR)
    assert _sweep_confidence(res) < SWEEP_LOCATE_CONFIDENCE_FLOOR
    assert INTEGRITY_CHECK_SWEEP_HEARD in res.capture_integrity.failed


def test_silent_capture_still_fails():
    """The other end of "unlocatable": digital silence. Nothing to anchor on,
    nothing to corroborate with, and no claim that anything was heard."""
    prog = _verify_program()
    silence = np.zeros(prog.total_samples + GLOBAL_OFFSET + 20_000)
    res = analyze_program_capture(prog, silence, SR)
    assert _sweep_confidence(res) < SWEEP_LOCATE_CONFIDENCE_FLOOR
    assert INTEGRITY_CHECK_SWEEP_HEARD in res.capture_integrity.failed


def test_witness_that_never_played_cannot_move_the_anchor():
    """The corroboration gate. CHECK's realistic miswire -- one driver plays,
    the other is silent -- makes the witness (the silent role's pilot) absent,
    so both candidates score in the noise. Picking the argmax of two noise
    values would shift the timeline for no reason and break the very
    channel-map test that protects a miswired household, so below
    ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` the cross-check declines and the
    pre-#2093 reading stands.
    """
    chk = build_check_program(_check_roles(), ambient_s=1.0, pilot_duration_s=0.5)
    pcm = render_program_pcm(chk)
    # Woofer plays; the tweeter (whose pilots are the witness) never does.
    cap = np.concatenate([np.zeros(500), pcm[:, 0], np.zeros(5000)])
    cap = cap + np.random.default_rng(5).normal(0.0, 3e-5, cap.size)

    _offset, anchor, _stimuli = _global_offset(chk, cap, SR)
    assert anchor.segment_id == "pilot_woofer_lo"


@pytest.mark.parametrize("phase", ["check", "measure", "verify"])
def test_chosen_witness_is_never_confusable_with_itself_under_the_shift(phase):
    """A witness must not be confusable with ITSELF under the shift being
    arbitrated -- being a different shape from the ANCHOR is not enough.

    CHECK builds both roles' pilot pairs with the same duration and gap, so
    reinterpreting the anchor as ``pilot_woofer_hi`` moves the search window
    for ``pilot_tweeter_hi`` onto ``pilot_tweeter_lo``: same shape,
    scale-invariant correlation, so both hypotheses score within ~1e-3 of each
    other (a loudest-on-tie witness key measured 0.968 vs 0.968 and 0.9927 vs
    0.9926 on two CHECK fixtures) -- a coin flip that shifts the timeline a
    full pilot spacing.

    Today the longest-then-earliest witness rule never picks such a segment,
    because nothing of the same shape sits one gap BEFORE a pair's ``lo``
    member and the composer always appends lo-then-hi. That is a real
    invariant rather than luck, but it is an invariant SHARED between the
    composer's ordering and this selection rule, so it is asserted here
    directly on the witness actually chosen -- for every shipping program --
    instead of trusted. Change either side and this fails.
    """
    program = {
        "check": lambda: build_check_program(
            _check_roles(), ambient_s=1.0, pilot_duration_s=0.5
        ),
        "measure": lambda: build_measure_program(
            {"woofer": -11.0, "tweeter": -13.0}, _check_roles(),
            sweep_durations={"woofer": 1.0, "tweeter": 0.8},
            leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
            courtesy_prelude=True,
        ),
        "verify": _verify_program,
    }[phase]()

    stimuli = [s for s in program.segments if s.kind in STIMULUS_KINDS]
    first = stimuli[0]
    shape = _stimulus_shape(first)
    candidates = [s for s in stimuli if _stimulus_shape(s) == shape]
    assert len(candidates) >= 2, f"{phase}: nothing to arbitrate"

    # Read the witness PRODUCTION actually chose, off its own event line --
    # re-deriving the selection rule here would be a second copy of it, and
    # this assertion would then keep passing while the real choice drifted.
    witness = program.segment(_witness_chosen_for(program))

    search = SEGMENT_SEARCH_S * SR
    shifts = {s.start_sample - first.start_sample for s in candidates} - {0}
    twins = [s.start_sample for s in stimuli
             if _stimulus_shape(s) == _stimulus_shape(witness)]
    for shift in shifts:
        for twin in twins:
            assert abs((witness.start_sample - shift) - twin) > search, (
                f"{phase}: witness {witness.segment_id} is confusable with a "
                f"same-shape segment under a {shift}-sample rival anchor"
            )


def test_program_without_shape_siblings_is_left_exactly_alone():
    """A legacy pilot-less VERIFY: its first stimulus is the unique summed
    sweep, so there is no second interpretation to arbitrate and the offset is
    the plain pre-#2093 arrival. Nothing to decide means nothing to log."""
    prog = _verify_program(with_pilots=False)
    first = next(s for s in prog.segments if s.kind == KIND_SUMMED_SWEEP)

    offset, anchor, _stimuli = _global_offset(prog, _pristine(prog), SR)
    assert anchor.segment_id == first.segment_id
    assert abs(offset - GLOBAL_OFFSET) < 0.030 * SR


# --------------------------------------------------------------------------- #
# observability
# --------------------------------------------------------------------------- #


def test_anchor_decision_is_logged_with_its_margin(caplog):
    """The 2026-08-03 chain was invisible: nothing said which stimulus the
    timeline was pinned to, so three fabricated failures read exactly like real
    ones. One stable ``event=`` line per analyzed capture now carries the
    chosen anchor, the witness it was corroborated against, both confidences
    (the margin), and whether the choice moved."""
    prog = _verify_program()
    with caplog.at_level(logging.INFO, logger=_ANALYSIS_LOGGER):
        _global_offset(prog, _knife_edge(prog), SR)
    assert "event=program_analysis.anchor" in caplog.text
    assert "anchor=pilot_summed_hi" in caplog.text
    assert "witness=sweep_verify" in caplog.text
    assert "corroborated=true" in caplog.text
    assert "corrected=true" in caplog.text
    # A correction is a WARNING -- a mis-anchor was caught and is worth seeing.
    assert any(r.levelno == logging.WARNING for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_ANALYSIS_LOGGER):
        _global_offset(prog, _pristine(prog), SR)
    assert "event=program_analysis.anchor" in caplog.text
    assert "anchor=pilot_summed_lo" in caplog.text
    assert "corrected=false" in caplog.text
    # ...and an ordinary capture stays at INFO.
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_no_anchor_event_when_there_is_nothing_to_arbitrate(caplog):
    prog = _verify_program(with_pilots=False)
    with caplog.at_level(logging.INFO, logger=_ANALYSIS_LOGGER):
        _global_offset(prog, _pristine(prog), SR)
    assert "event=program_analysis.anchor" not in caplog.text
