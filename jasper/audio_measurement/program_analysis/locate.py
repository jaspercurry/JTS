# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Timeline anchor and per-segment location, with each segment's own integrity."""

from __future__ import annotations

import logging

import numpy as np

from jasper.audio_measurement.alignment import _bandlimit
from jasper.audio_measurement.program import (
    ExcitationProgram,
    ProgramSegment,
    segment_stimulus,
    STIMULUS_KINDS,
)
from jasper.log_event import log_event
from .model import (
    ANCHOR_DISCRIMINATION_RATIO,
    LOCATOR_RATE_HZ,
    logger,
    SEGMENT_SEARCH_S,
    SegmentLocation,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
)
from .signals import _has_clipped_run, _locate, _peak_dbfs


def _earliest_strong_peak(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    frac: float = 0.6,
    band_hz: tuple[float | None, float | None] | None = None,
    sample_rate: int | None = None,
) -> int:
    """Index of the EARLIEST normalized-correlation peak within ``frac`` of max.

    A locally energy-normalized matched filter (cosine similarity per lag),
    so a quieter-but-identical first occurrence (MEASURE's woofer repeat) or
    a shape-sharing different-level segment (CHECK's lo/hi pilot pair)
    scores the same as a louder later one; taking the earliest lag within
    ``frac`` of the max picks the true first occurrence.

    ``band_hz`` restricts similarity to the stimulus's OWN declared band —
    without it, room noise the stimulus never occupied suppresses a quiet
    member's score (a quiet pilot once scored below gate despite better
    in-band SNR than a passing round, latching onto the wrong pilot and
    sliding every analysis window one pilot spacing). A caller with no band
    to declare keeps the full-band behavior.
    """
    from scipy.signal import correlate

    cap = np.asarray(capture, dtype=np.float64)
    stim = np.asarray(stimulus, dtype=np.float64)
    cap = cap - cap.mean()
    stim = stim - stim.mean()
    L = stim.size
    if cap.size < L or L == 0:
        return 0
    if (
        band_hz is not None
        and sample_rate
        and band_hz[0] is not None
        and band_hz[1] is not None
    ):
        cap_b = _bandlimit(cap, sample_rate, band_hz[0], band_hz[1])
        stim_b = _bandlimit(stim, sample_rate, band_hz[0], band_hz[1])
        # A band with no surviving bin zeroes both sides; fall back rather
        # than correlate silence against silence.
        if float(np.linalg.norm(stim_b)) > 0.0 and float(np.linalg.norm(cap_b)) > 0.0:
            cap, stim = cap_b, stim_b
    stim_norm = float(np.linalg.norm(stim))
    if stim_norm <= 0.0:
        return 0
    num = correlate(cap, stim, mode="valid", method="fft")
    local_energy = correlate(cap * cap, np.ones(L), mode="valid", method="fft")
    local_norm = np.sqrt(np.maximum(local_energy, 0.0))
    # Floor the denominator so silent (near-zero-energy) windows don't blow the
    # ratio up; a floor at a small fraction of the loudest window is enough.
    floor = 1e-6 * float(local_norm.max()) + 1e-12
    ncc = np.abs(num) / (local_norm * stim_norm + floor)
    peak = float(ncc.max()) if ncc.size else 0.0
    if peak <= 0.0:
        return 0
    return int(np.argmax(ncc >= frac * peak))


def _stimulus_shape(segment: ProgramSegment) -> tuple[float | None, float | None, int]:
    """A stimulus segment's waveform identity — everything
    :func:`segment_stimulus` regenerates it from EXCEPT its level.

    Two segments sharing this triple differ only by amplitude, and
    :func:`_earliest_strong_peak`'s correlation is scale-invariant by
    design, so it cannot distinguish them — the exact ambiguity set
    :func:`_resolve_anchor` arbitrates.
    """
    return (segment.f1_hz, segment.f2_hz, segment.n_samples)


def _resolve_anchor(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    arrival: int,
    first: ProgramSegment,
    stimuli: dict[str, np.ndarray],
) -> tuple[ProgramSegment, int, bool]:
    """Decide WHICH shape-identical stimulus the located ``arrival`` really is,
    and say so when the evidence cannot decide.

    ``_earliest_strong_peak`` answers "where is a stimulus of this shape?"
    but not "which occurrence?", and is level-blind by construction
    (:func:`_stimulus_shape`). Its earliest-lag tie-break is robust for
    equal-level shape-siblings, but the v2 pilot pair is deliberately
    UNEQUAL (VERIFY lo is 10 dB under hi), so the quiet member's local SNR
    can snap the anchor onto the wrong sibling and shift the whole timeline
    by one pilot spacing — beyond the ±30 ms per-segment search window, so
    the rest of the program then reads "not found" on an audible capture.

    So rather than trust one level-blind gate, this enumerates the (few)
    interpretations the schedule permits and asks the capture which one the
    REST of the program agrees with: for each shape-sibling of ``first``,
    reinterpret ``arrival`` as that segment and score the resulting timeline
    by locating an independent WITNESS (the longest stimulus outside the
    ambiguity set) through the same :func:`_locate_in_window` the
    downstream locate uses. Readings are ranked by ``presence``, never
    confidence, since only presence says whether the witness is there.

    This CANNOT manufacture a passing capture: it only changes WHERE the
    analyzer looks; every downstream gate reads the real measured
    correlation. Re-anchoring requires POSITIVE evidence — the winning
    candidate's witness locate must clear ``SWEEP_LOCATE_CONFIDENCE_FLOOR``
    — so a capture with no locatable program declines to move. A program
    with no shape-sibling or no independent witness keeps the unarbitrated
    behavior.

    When the witness cannot tell the interpretations apart, this says so:
    if a near-tie pair (both above the confidence floor, presence within
    :data:`ANCHOR_DISCRIMINATION_RATIO` of each other) separates far less
    than a genuine witness reading does, an argmax between them is a coin
    flip. The committed anchor is left unchanged, but the third return
    value is True, and the CHECK ladder refuses the capture as retriable
    rather than reading a verdict off that flip.
    """
    shape = _stimulus_shape(first)
    candidates = [
        seg for seg in program.segments
        if seg.kind in STIMULUS_KINDS and _stimulus_shape(seg) == shape
    ]
    # Longest wins (correlation SNR grows with length); `max` holds its
    # FIRST maximum, so an equal-length tie keeps the earliest segment in
    # schedule order — load-bearing, since a witness confusable with itself
    # under the shift being arbitrated (CHECK's same-duration pilot pairs)
    # would score both hypotheses alike and coin-flip the timeline. Taking
    # the earliest of a tied pair avoids that pair (`_append_leading_pilot_pair`
    # always appends lo-then-hi). This covers only one of the two shift
    # directions; the near-tie guard below covers the other.
    witness = max(
        (seg for seg in program.segments
         if seg.kind in STIMULUS_KINDS and _stimulus_shape(seg) != shape),
        key=lambda seg: seg.n_samples,
        default=None,
    )
    if len(candidates) < 2 or witness is None:
        return first, arrival - first.start_sample, False

    witness_stim = stimuli.get(witness.segment_id)
    if witness_stim is None:
        witness_stim = segment_stimulus(witness)
        stimuli[witness.segment_id] = witness_stim
    scored: list[tuple[float, float, ProgramSegment, int]] = []
    for seg in candidates:
        offset = arrival - seg.start_sample
        _located, confidence, presence = _locate_in_window(
            capture, witness_stim, offset + witness.start_sample,
            witness.n_samples, sample_rate=sample_rate,
        )
        scored.append((presence, confidence, seg, offset))
    # Ranked on PRESENCE, not peakedness margin (see docstring); the margin
    # is still read at `corroborated` below. `max` keeps the FIRST maximum
    # so an exact tie holds the structurally first candidate.
    best_index, (best_presence, best_confidence, best_seg, best_offset) = max(
        enumerate(scored), key=lambda item: item[1][0]
    )
    runner_up_presence, runner_up, runner_up_seg, runner_up_offset = max(
        (row for index, row in enumerate(scored) if index != best_index),
        key=lambda item: item[0],
    )
    # Re-anchoring requires POSITIVE evidence that the winning witness locate
    # is a sharp lag, not room noise (a silent driver never played, so
    # nothing in the window is sharp and re-anchoring on noise would shift
    # the timeline for no reason). NOT redundant with the presence ranking
    # (which prefers the later candidate on a garbage capture) and NOT
    # sufficient alone — a sharp lag is not the witness.
    corroborated = best_confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR
    if not corroborated:
        best_seg, best_offset = first, arrival - first.start_sample
    # Corroboration alone is not discrimination: two candidates both above
    # the floor with presence within ANCHOR_DISCRIMINATION_RATIO of each
    # other means the argmax carries no information (CHECK's witness has a
    # same-shape twin one gap later). The commitment is left unchanged, but
    # flagged un-attributed so a consuming phase refuses it as retriable.
    # Multiplication rather than subtraction so a zero runner-up presence
    # resolves rather than divides by zero.
    ambiguous = (
        corroborated
        and runner_up >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        and best_presence < runner_up_presence * ANCHOR_DISCRIMINATION_RATIO
    )
    corrected = best_seg.segment_id != first.segment_id
    # One line per analyzed capture, naming the losing interpretation too —
    # a reader triaging an ambiguous anchor needs to know which timeline
    # nearly won. `presence=` is the term the choice is made on;
    # `confidence=` is the peakedness margin.
    runner_up_shift_ms = round(
        (runner_up_offset - (arrival - first.start_sample)) / sample_rate * 1000.0, 1
    )
    log_event(
        logger,
        "program_analysis.anchor",
        level=logging.WARNING if (corrected or ambiguous) else logging.INFO,
        phase=program.phase,
        program_id=program.program_id,
        anchor=best_seg.segment_id,
        witness=witness.segment_id,
        candidates=len(candidates),
        presence=round(best_presence, 6),
        runner_up_presence=round(runner_up_presence, 6),
        confidence=round(best_confidence, 4),
        runner_up=round(runner_up, 4),
        runner_up_anchor=runner_up_seg.segment_id,
        corroborated=corroborated,
        corrected=corrected,
        ambiguous=ambiguous,
        shift_ms=round(
            (best_offset - (arrival - first.start_sample)) / sample_rate * 1000.0, 1
        ),
        runner_up_shift_ms=runner_up_shift_ms,
    )
    return best_seg, best_offset, ambiguous


def _global_offset(
    program: ExcitationProgram, capture: np.ndarray, sample_rate: int
) -> tuple[int, ProgramSegment, dict[str, np.ndarray], bool]:
    """Locate the anchor stimulus -> integer global offset G. Caches stimuli.

    The whole-capture matched filter runs at :data:`LOCATOR_RATE_HZ`; the
    coarse arrival is then refined at the full rate inside a tiny window, so
    the returned offset is full-rate-exact. That locate answers WHERE, not
    WHICH occurrence — :func:`_resolve_anchor` arbitrates that and owns the
    returned segment. The fourth return value is its honesty flag: True
    when the evidence could not tell interpretations apart.
    """
    from scipy.signal import resample_poly

    stimuli: dict[str, np.ndarray] = {}
    first = None
    for seg in program.segments:
        if seg.kind in STIMULUS_KINDS:
            first = seg
            break
    if first is None:
        raise ValueError("program has no stimulus segment to locate against")
    stim = segment_stimulus(first)
    stimuli[first.segment_id] = stim
    band_hz = (first.f1_hz, first.f2_hz)

    down = max(1, int(round(sample_rate / LOCATOR_RATE_HZ)))
    if down > 1:
        capture_lo = resample_poly(capture, 1, down)
        stim_lo = resample_poly(np.asarray(stim, dtype=np.float64), 1, down)
    else:
        capture_lo = capture
        stim_lo = np.asarray(stim, dtype=np.float64)
    coarse = _earliest_strong_peak(
        capture_lo, stim_lo, band_hz=band_hz, sample_rate=sample_rate // down
    ) * down

    # Full-rate refinement in a +/-4*down window: bounded cost, full-rate precision.
    margin = 4 * down
    lo = max(0, coarse - margin)
    hi = min(capture.size, coarse + stim.size + margin)
    window = capture[lo:hi]
    if window.size >= stim.size:
        arrival = lo + _earliest_strong_peak(
            window, stim, band_hz=band_hz, sample_rate=sample_rate
        )
    else:
        arrival = coarse
    anchor, global_offset, ambiguous = _resolve_anchor(
        program, capture, sample_rate, arrival, first, stimuli
    )
    return global_offset, anchor, stimuli, ambiguous


def _locate_in_window(
    capture: np.ndarray,
    stim: np.ndarray,
    scheduled: int,
    n_samples: int,
    *,
    sample_rate: int,
) -> tuple[int, float, float]:
    """Matched-filter ``stim`` at ``scheduled`` +/- :data:`SEGMENT_SEARCH_S`.

    The ONE place the per-segment search geometry lives: both
    :func:`_locate_segments` and :func:`_resolve_anchor` score through it,
    so the chosen anchor is by construction the anchor segments actually
    locate under.

    Returns BOTH scores, since they answer different questions: ``confidence``
    is the peakedness margin (is the winning lag sharp against its own
    neighbourhood — :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR` grades this, NOT
    whether ``stim`` is here at all, since over ~61 ms of lags two room-noise
    correlations already ratio 0.6-0.8); ``presence`` is the normalized
    correlation similarity, which does say. A window too short to hold
    ``stim`` yields ``(scheduled, 0.0, 0.0)``, never a located claim.
    """
    search = int(round(SEGMENT_SEARCH_S * sample_rate))
    lo = max(0, scheduled - search)
    hi = min(capture.size, scheduled + n_samples + search)
    window = capture[lo:hi]
    if window.size < stim.size:
        return scheduled, 0.0, 0.0
    res = _locate(
        window, stim, sample_rate=sample_rate,
        max_capture_s=window.size / sample_rate + 1.0,
    )
    return lo + int(res.lag_samples), float(res.confidence), float(res.peak)


def _locate_segments(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    global_offset: int,
    stimuli: dict[str, np.ndarray],
) -> list[SegmentLocation]:
    """Locate every segment at scheduled offset ± window; record integrity."""
    out: list[SegmentLocation] = []
    for seg in program.segments:
        scheduled = global_offset + seg.start_sample
        if seg.kind in STIMULUS_KINDS:
            stim = stimuli.get(seg.segment_id)
            if stim is None:
                stim = segment_stimulus(seg)
                stimuli[seg.segment_id] = stim
            # `presence` is the anchor arbitration's term, not this one's: every
            # gate on `SegmentLocation.confidence` is calibrated on the
            # peakedness margin, so recording the other would move all of them.
            located, confidence, _presence = _locate_in_window(
                capture, stim, scheduled, seg.n_samples, sample_rate=sample_rate,
            )
            seg_samples = capture[located:located + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=located,
                residual_samples=float(located - scheduled),
                confidence=confidence,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
        else:
            seg_samples = capture[max(0, scheduled):scheduled + seg.n_samples]
            out.append(SegmentLocation(
                segment_id=seg.segment_id,
                kind=seg.kind,
                role=seg.role,
                scheduled_start=scheduled,
                located_start=scheduled,
                residual_samples=0.0,
                confidence=1.0,
                peak_dbfs=_peak_dbfs(seg_samples),
                clipped=_has_clipped_run(seg_samples),
            ))
    return out
