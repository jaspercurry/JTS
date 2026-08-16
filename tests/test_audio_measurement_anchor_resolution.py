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

**Issue #2644 is the same defect one layer down, and the last section of this
file owns it.** #2093 fixed WHICH occurrence a located arrival is taken to be;
it left untouched the question of whether the locate found the right occurrence
in the first place, and left the arbitration itself unguarded in one of its two
shift directions. On 2026-08-16 a CHECK capture hit both gaps at once: the
quiet ``pilot_woofer_lo`` scored 0.3932 against a 0.4176 gate on a FULL-BAND
correlation whose denominator the room's out-of-band noise dominated -- its
IN-BAND SNR, 27.6 dB, was BETTER than the 26.9 dB of the round that had passed
two hours earlier on the same unchanged speaker. The anchor snapped to
``pilot_woofer_hi``; the arbitration that exists to catch exactly that lost by
**0.0034** (0.9007 vs 0.8973) because CHECK's witness has a same-shape twin one
gap LATER; every per-driver window slid one pilot spacing; and the verdict --
a HARD STOP with no retry -- told the household to check its speaker wiring.
Those captures are on a lab Pi this checkout cannot reach, so the last section
reconstructs the decision geometry the same way the #2093 fixtures above do,
and pins BOTH production verdicts: the shipped analyzer's
``channel_map_mismatch`` on a 0.0006 anchor gap, and this branch's.
"""
from __future__ import annotations

import logging

import numpy as np
import pytest
from scipy.signal import fftconvolve, resample_poly

from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
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
    ANCHOR_DISCRIMINATION_MARGIN,
    CHANNEL_MAP_TARGET_RISE_DB,
    INTEGRITY_CHECK_SWEEP_HEARD,
    LOCATOR_RATE_HZ,
    SEGMENT_SEARCH_S,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    _band_rms_dbfs,
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
    offset, anchor, _stimuli, _amb = _global_offset(program, capture, SR)
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


#: Every program shape that ships, in the one place both witness-confusability
#: tests read it from -- the two directions of one claim must be asked of the
#: same programs or the pair proves nothing about either.
_SHIPPING_PROGRAMS = {
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
}


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
    repaired_offset, anchor, _stimuli, _amb = _global_offset(prog, cap, SR)
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
    offset, _anchor, stimuli, _amb = _global_offset(prog, cap, SR)
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
    offset, anchor, _stimuli, _amb = _global_offset(prog, garbage, SR)

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

    _offset, anchor, _stimuli, _amb = _global_offset(chk, cap, SR)
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

    It is ONE of the two shift directions. The other -- one gap LATER, where
    CHECK's witness does have a twin -- is issue #2644's, and
    ``test_the_witness_is_confusable_one_gap_LATER_and_only_on_check`` owns it.
    """
    program = _SHIPPING_PROGRAMS[phase]()

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

    offset, anchor, _stimuli, _amb = _global_offset(prog, _pristine(prog), SR)
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


# --------------------------------------------------------------------------- #
# issue #2644 -- the 2026-08-16 CHECK capture that was called a wiring fault
#
# Two defects, one capture. The reconstruction below is synthetic for the same
# reason every fixture above it is (``tests/`` carries no binary fixtures, and
# the retained WAVs sit on a lab Pi this checkout has no key for), and it is
# built to reproduce the two things the session records name: the SHIPPED
# analyzer's ``channel_map_mismatch`` verdict, and the coin-flip anchor gap
# underneath it. Where it is deliberately NOT faithful is stated on each
# helper.
# --------------------------------------------------------------------------- #


def _incident_program():
    """The 2026-08-16 CHECK program shape: two roles, the session's own per-role
    bases (-12.0 woofer / -45.01 tweeter), full-length pilots and gaps so the
    pilot spacing is the session's 1.310 s.

    The ambient window is 2 s rather than the shipped 12 s -- it feeds the
    channel-map rise test and the gain solve, both of which only need a floor
    estimate, and 10 s of silence per fixture capture is runtime this suite
    does not need to spend. ``build_check_program`` clamps a role band up to
    ``MEASURE_SWEEP_F_LO_HZ``, so the woofer's declared 60 Hz becomes a 150 Hz
    pilot band; that clamp is production behaviour and the session's own
    program carried it too.
    """
    return build_check_program(
        [
            RoleBand("woofer", 0, FrequencyBand(60.0, 4000.0)),
            RoleBand("tweeter", 1, FrequencyBand(1600.0, 18000.0)),
        ],
        ambient_s=2.0,
        role_base_peak_dbfs={"woofer": -12.0, "tweeter": -45.01},
    )


def _incident_room(program, *, tone_rms: float, seed: int = 7) -> np.ndarray:
    """A CHECK capture through two band-passed drivers, in a room with a floor.

    ``tone_rms`` is a 20-100 Hz rumble -- OUTSIDE both pilots' declared bands,
    so it is invisible to every band-limited judgment this module makes and can
    only reach a verdict through a FULL-BAND correlation. That is the whole
    mechanism of #2644, isolated: turn it up and the quiet pilot's full-band
    score collapses while its in-band SNR does not move.
    """
    pcm = render_program_pcm(program)
    irs = [
        _band_impulse(180, 150.0, 4000.0, 1.0),
        _band_impulse(150, 1600.0, 18000.0, 1.0),
    ]
    mono = np.zeros(pcm.shape[0])
    for ch in range(pcm.shape[1]):
        mono += fftconvolve(pcm[:, ch], irs[ch])[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(GLOBAL_OFFSET), mono, np.zeros(20_000)])
    cap = cap + _band_noise(cap.size, 20.0, 100.0, tone_rms, seed)
    return cap + _band_noise(cap.size, 20.0, 20_000.0, 2e-4, seed + 1)


#: The rumble level at which the reconstruction reproduces the incident. Below
#: ~0.10 the full-band gate still finds the quiet pilot; the value is a plateau
#: (0.10 through 0.25 all mis-lock), not an edge fitted to one number.
INCIDENT_TONE_RMS = 0.13
#: The same room without the rumble -- the "two hours earlier" round that passed.
QUIET_TONE_RMS = 3e-3


def _full_band_locate(capture, stimulus, *, frac=0.6, band_hz=None, sample_rate=None):
    """``_earliest_strong_peak`` as it scored before #2644: band argument dropped."""
    return _earliest_strong_peak(capture, stimulus, frac=frac)


def _check_screen(analysis, *, honour_ambiguity: bool = True) -> str | None:
    """CHECK's real ladder over a real analysis -- the household-facing answer.

    ``honour_ambiguity=False`` feeds the ladder the pre-#2644 fact set (there
    was no such field), so one call site can ask both "what did the shipped
    build say" and "what does this one say" without a second copy of the ladder.
    """
    from jasper.active_speaker.crossover_v2 import capture_dispatch as _dispatch
    from jasper.active_speaker.crossover_v2_flow import _stimulus_locate_ok

    plan = analysis.gain_plan
    return _dispatch.check_screens(_dispatch.CheckScreens(
        stimulus_located=_stimulus_locate_ok(analysis),
        anchor_ambiguous=analysis.anchor_ambiguous and honour_ambiguity,
        channel_map_ok=analysis.channel_map_ok,
        pilot_snr_ok=analysis.pilot_snr_ok,
        linearity_ok=analysis.linearity_ok,
        gain_plan_present=plan is not None,
        gain_plan_snr_floor_ok=bool(plan.snr_floor_ok) if plan is not None else False,
    ))


def _anchor_margin(program, capture, monkeypatch=None) -> float:
    """The winning anchor's lead over its runner-up, read off the event line
    rather than recomputed -- a second derivation of the margin here would keep
    passing while the real one drifted."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger(_ANALYSIS_LOGGER)
    log.addHandler(handler)
    level = log.level
    log.setLevel(logging.INFO)
    try:
        _global_offset(program, capture, SR)
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    line = next(r.getMessage() for r in records
                if "event=program_analysis.anchor" in r.getMessage())
    tokens = dict(tok.split("=", 1) for tok in line.split() if "=" in tok)
    return float(tokens["confidence"]) - float(tokens["runner_up"])


def test_the_witness_is_confusable_one_gap_LATER_and_only_on_check():
    """The direction ``test_chosen_witness_is_never_confusable_with_itself...``
    does not assert, and the reason the near-tie guard has to exist.

    That test checks the rival window landing one gap BEFORE the witness --
    the geometry when the coarse locate was RIGHT. When the coarse locate is
    itself one spacing LATE (the #2644 shape) both hypotheses shift the OTHER
    way, and CHECK's chosen witness ``pilot_tweeter_lo`` has its own twin
    ``pilot_tweeter_hi`` sitting one gap after it -- inside
    ``SEGMENT_SEARCH_S``, so both readings land the witness on a real pilot and
    score alike. No witness CHOICE fixes that: the only non-sibling stimuli
    CHECK owns are the other role's pair, and both members are twins.

    Asserted as an exact map rather than "at least CHECK", so gaining the
    exposure on MEASURE or VERIFY -- or losing it on CHECK, which would make
    the guard dead code -- both fail here.
    """
    exposed_later = {}
    for phase, build in _SHIPPING_PROGRAMS.items():
        program = build()
        stimuli = [s for s in program.segments if s.kind in STIMULUS_KINDS]
        first = stimuli[0]
        shape = _stimulus_shape(first)
        candidates = [s for s in stimuli if _stimulus_shape(s) == shape]
        witness = program.segment(_witness_chosen_for(program))
        search = SEGMENT_SEARCH_S * SR
        shifts = {s.start_sample - first.start_sample for s in candidates} - {0}
        twins = [s.start_sample for s in stimuli
                 if _stimulus_shape(s) == _stimulus_shape(witness)]
        # The direction the witness ORDERING already buys, restated here so the
        # two halves of the claim sit together.
        for shift in shifts:
            for twin in twins:
                assert abs((witness.start_sample - shift) - twin) > search, (
                    f"{phase}: the EARLIER direction regressed"
                )
        exposed_later[phase] = any(
            abs((witness.start_sample + shift) - twin) <= search
            for shift in shifts for twin in twins
        )
    assert exposed_later == {"check": True, "measure": False, "verify": False}


def test_the_incident_capture_really_mis_locks_the_full_band_gate():
    """The precondition every #2644 test below rests on, kept permanently:
    scoring FULL BAND lands the anchor on the loud pilot, scoring in the
    pilot's OWN band lands it on the quiet one -- on the same samples.

    The two locates are the only difference. If this fixture ever stops
    reproducing the mis-lock, this fails and the repair tests below stop being
    able to pass vacuously.
    """
    prog = _incident_program()
    cap = _incident_room(prog, tone_rms=INCIDENT_TONE_RMS)
    stim = np.asarray(segment_stimulus(prog.segment("pilot_woofer_lo")), np.float64)
    down = max(1, int(round(SR / LOCATOR_RATE_HZ)))
    cap_lo = resample_poly(cap, 1, down)
    stim_lo = resample_poly(stim, 1, down)

    true_lo = GLOBAL_OFFSET + prog.segment("pilot_woofer_lo").start_sample
    spacing = (prog.segment("pilot_woofer_hi").start_sample
               - prog.segment("pilot_woofer_lo").start_sample)
    # The session's own 1.310 s spacing, which is what the timeline slid by.
    assert abs(spacing / SR - 1.310) < 0.001

    full_band = _earliest_strong_peak(cap_lo, stim_lo) * down
    in_band = _earliest_strong_peak(
        cap_lo, stim_lo,
        band_hz=(prog.segment("pilot_woofer_lo").f1_hz,
                 prog.segment("pilot_woofer_lo").f2_hz),
        sample_rate=SR // down,
    ) * down
    assert abs(full_band - (true_lo + spacing)) < 0.030 * SR, "no mis-lock to repair"
    assert abs(in_band - true_lo) < 0.030 * SR


def test_the_quiet_pilots_in_band_snr_never_moved():
    """The falsifying fact, and the reason a full-band score is the wrong
    instrument: the noisy capture's quiet pilot is as far above the room IN ITS
    OWN BAND as the accepted round's is. The session measured 27.6 dB against
    26.9 dB and refused the better one.
    """
    prog = _incident_program()
    seg = prog.segment("pilot_woofer_lo")
    ambient = prog.segment(AMBIENT_SEGMENT_ID)
    snrs = []
    for tone in (INCIDENT_TONE_RMS, QUIET_TONE_RMS):
        cap = _incident_room(prog, tone_rms=tone)
        pilot = cap[GLOBAL_OFFSET + seg.start_sample:
                    GLOBAL_OFFSET + seg.start_sample + seg.n_samples]
        room = cap[GLOBAL_OFFSET + ambient.start_sample:
                   GLOBAL_OFFSET + ambient.start_sample + ambient.n_samples]
        snrs.append(
            _band_rms_dbfs(pilot, SR, seg.f1_hz, seg.f2_hz)
            - _band_rms_dbfs(room, SR, seg.f1_hz, seg.f2_hz)
        )
    noisy, quiet = snrs
    assert noisy > 25.0
    assert abs(noisy - quiet) < 1.0, (
        f"the rumble must be out-of-band: {noisy:.1f} vs {quiet:.1f} dB"
    )


def test_the_shipped_analyzer_called_this_capture_a_wiring_fault(monkeypatch):
    """The incident, end to end: with the pre-#2644 full-band locate AND the
    pre-#2644 fact set, CHECK answers ``channel_map_mismatch`` -- a hard stop
    with no retry whose sentence tells the household to check its wiring --
    and the anchor it rests on is a coin flip.

    This is the mutation guard for the whole section. Revert either half of the
    fix and the tests below stop describing a repair; this one keeps describing
    the bug, so the pair cannot both stay green.
    """
    monkeypatch.setattr(
        "jasper.audio_measurement.program_analysis._earliest_strong_peak",
        _full_band_locate,
    )
    prog = _incident_program()
    cap = _incident_room(prog, tone_rms=INCIDENT_TONE_RMS)

    margin = _anchor_margin(prog, cap)
    assert margin < ANCHOR_DISCRIMINATION_MARGIN
    assert margin < 0.01, f"the coin flip is the point: margin {margin:.4f}"

    analysis = analyze_program_capture(prog, cap, SR)
    assert analysis.channel_map_ok is False
    assert _check_screen(analysis, honour_ambiguity=False) == "channel_map_mismatch"
    # ...and the numbers that verdict rested on are physically impossible for
    # ANY wiring: the program commanded +10 dB between the two pilots.
    woofer = next(p for p in analysis.pilots if p.role == "woofer")
    assert woofer.programmed_delta_db == pytest.approx(10.0)
    assert woofer.captured_delta_db < -50.0


def test_the_near_tie_guard_alone_turns_that_verdict_into_a_retake(monkeypatch):
    """Half the fix, in isolation. Keep the pre-#2644 locate, so the anchor is
    still wrong and the channel map still reads False -- the guard alone must
    already make the wiring copy unreachable, because a capture the analyzer
    could not attribute is not evidence about how a speaker is wired.
    """
    monkeypatch.setattr(
        "jasper.audio_measurement.program_analysis._earliest_strong_peak",
        _full_band_locate,
    )
    prog = _incident_program()
    analysis = analyze_program_capture(
        prog, _incident_room(prog, tone_rms=INCIDENT_TONE_RMS), SR
    )
    assert analysis.anchor_ambiguous is True
    assert analysis.channel_map_ok is False, "the guard must not repair the map"
    assert _check_screen(analysis) == "anchor_ambiguous"


def test_the_band_limited_locate_puts_the_whole_timeline_back():
    """The other half, and the flip proof restated: on the SAME samples, with
    nothing monkeypatched, the anchor is the quiet pilot, the two hypotheses
    are two orders of magnitude apart, and the channel map -- the thing the
    household was refused on -- passes.
    """
    prog = _incident_program()
    cap = _incident_room(prog, tone_rms=INCIDENT_TONE_RMS)

    _offset, anchor, _stimuli, ambiguous = _global_offset(prog, cap, SR)
    assert anchor.segment_id == "pilot_woofer_lo"
    assert ambiguous is False
    assert _anchor_margin(prog, cap) > 0.5

    analysis = analyze_program_capture(prog, cap, SR)
    assert analysis.channel_map_ok is True
    assert analysis.anchor_ambiguous is False
    # Every per-driver window is back where its driver actually played.
    for pilot in analysis.pilots:
        assert pilot.captured_delta_db == pytest.approx(10.0, abs=0.5)
    # The room in this reconstruction is genuinely loud, so the gain solve may
    # still refuse it -- as a RETRIABLE room finding. What must be gone is the
    # wiring instruction, which is the claim this issue is about; asserting an
    # acceptance here would claim more than the original session ever reached
    # (its ladder stopped at the channel-map rung).
    screen = _check_screen(analysis)
    assert screen != "channel_map_mismatch"
    if screen is not None:
        from jasper.active_speaker.crossover_v2 import vocabulary as _vocab
        code = _vocab.SCREEN_KIND_REASONS[screen]
        assert code not in _vocab.NON_RETRIABLE_CODES


def test_the_quiet_room_sibling_is_untouched_by_both_halves():
    """The accepted round, before and after. A capture the shipped build read
    correctly must read identically here -- same anchor, same wide margin, same
    acceptance -- or the fix bought its repair with a regression.
    """
    prog = _incident_program()
    cap = _incident_room(prog, tone_rms=QUIET_TONE_RMS)

    _offset, anchor, _stimuli, ambiguous = _global_offset(prog, cap, SR)
    assert anchor.segment_id == "pilot_woofer_lo"
    assert ambiguous is False
    assert _anchor_margin(prog, cap) > 0.5

    analysis = analyze_program_capture(prog, cap, SR)
    assert analysis.channel_map_ok is True
    assert _check_screen(analysis) is None


@pytest.mark.parametrize("miswired", [False, True])
def test_a_genuinely_miswired_capture_still_gets_the_wiring_verdict(miswired):
    """The regression the guard must not buy its repair with, plus its control.

    A quiet room, a confidently-resolved anchor, and one lead landed on the
    wrong terminal for real: the tweeter's channel drives the woofer's cone, so
    the tweeter's own band never rises. CHECK must still reach the hard stop
    and still tell the household to check the wiring -- the ambiguity rung sits
    ABOVE that one, so an over-eager guard would swallow a real fault, and this
    is what would catch it.

    ONE lead rather than both, deliberately. Cross both and the woofer's own
    pilots -- the timeline anchor -- go inaudible too, and production answers
    the honest ``locate_failed`` instead; there would be no wiring verdict left
    to regress. Crossing one leaves the anchor pristine and puts the fault
    exactly where ``_channel_map_ok`` can see it. The arm that fires is
    asserted below by name so this cannot quietly start passing for another
    reason.

    ``miswired=False`` is the no-op control on the identical fixture: same
    program, same room, same noise seed, leads correct, accepted. Without it a
    fixture that refused everything would read as a pass.
    """
    program = build_check_program(
        [
            RoleBand("woofer", 0, FrequencyBand(60.0, 1000.0)),
            RoleBand("tweeter", 1, FrequencyBand(4000.0, 18000.0)),
        ],
        ambient_s=2.0,
    )
    # Exact spectral masking rather than the 4096-tap ``_band_impulse`` the
    # fixtures above convolve with: an ideal-rect IR of that length leaks its
    # own sidelobes about 25 dB down, which is enough to keep a silent driver's
    # band "risen" and would make this pass for a reason unrelated to wiring.
    def radiate(signal, f_lo, f_hi):
        spectrum = np.fft.rfft(signal)
        freqs = np.fft.rfftfreq(signal.size, 1.0 / SR)
        spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
        return np.fft.irfft(spectrum, signal.size)

    woofer_cone, tweeter_dome = (150.0, 1000.0), (4000.0, 18000.0)
    pcm = render_program_pcm(program)
    mono = radiate(pcm[:, 0], *woofer_cone)
    mono += radiate(pcm[:, 1], *(woofer_cone if miswired else tweeter_dome))
    cap = np.concatenate([np.zeros(GLOBAL_OFFSET), mono, np.zeros(20_000)])
    cap = cap + _band_noise(cap.size, 20.0, 20_000.0, 2e-4, seed=3)

    _offset, anchor, _stimuli, ambiguous = _global_offset(program, cap, SR)
    assert anchor.segment_id == "pilot_woofer_lo"
    assert ambiguous is False, "a mis-wired capture is not an un-attributed one"

    analysis = analyze_program_capture(program, cap, SR)
    assert analysis.anchor_ambiguous is False
    if not miswired:
        assert analysis.channel_map_ok is True
        assert _check_screen(analysis) is None
        return
    assert analysis.channel_map_ok is False
    assert _check_screen(analysis) == "channel_map_mismatch"
    # Named: the TARGET arm on the tweeter (its own band never rose over the
    # room), with the woofer -- still correctly wired -- passing beside it.
    by_role = {p.role: p for p in analysis.pilots}
    assert by_role["tweeter"].channel_map_target_rise_db < CHANNEL_MAP_TARGET_RISE_DB
    assert by_role["woofer"].channel_map_ok is True


def test_the_anchor_event_reports_the_ambiguity_it_found(monkeypatch):
    """The margin was already logged; whether it was judged SMALL was not. A
    population of near-ties is the only way the constant gets re-derived from
    the field rather than from this fixture, so the judgment has to be on the
    line -- at WARNING, like a correction, because both mean the timeline is
    not what the locate said it was.
    """
    monkeypatch.setattr(
        "jasper.audio_measurement.program_analysis._earliest_strong_peak",
        _full_band_locate,
    )
    prog = _incident_program()
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger(_ANALYSIS_LOGGER)
    log.addHandler(handler)
    level = log.level
    log.setLevel(logging.INFO)
    try:
        _global_offset(prog, _incident_room(prog, tone_rms=INCIDENT_TONE_RMS), SR)
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    line = next(r.getMessage() for r in records
                if "event=program_analysis.anchor" in r.getMessage())
    assert "ambiguous=true" in line
    assert any(r.levelno == logging.WARNING for r in records)
