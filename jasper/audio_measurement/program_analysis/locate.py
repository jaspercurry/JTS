# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Timeline anchor and per-segment location, with each segment's own integrity."""

from __future__ import annotations

import logging
import math

import numpy as np
from scipy.signal import correlate, resample_poly

from jasper.audio_measurement.alignment import _bandlimit
from jasper.audio_measurement.branch_program import is_branch_program
from jasper.audio_measurement.program import (
    ExcitationProgram,
    KIND_SUMMED_SWEEP,
    ProgramSegment,
    render_program_pcm,
    segment_stimulus,
    STIMULUS_KINDS,
)
from jasper.log_event import log_event
from .model import (
    ANCHOR_DISCRIMINATION_RATIO,
    AnchorEvidence,
    LOCATOR_RATE_HZ,
    logger,
    SEGMENT_SEARCH_S,
    SegmentLocation,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS,
    WITNESS_BAND_FLOOR_HZ,
)
from .signals import _has_clipped_run, _locate, _peak_dbfs


def _earliest_strong_peak(
    capture: np.ndarray,
    stimulus: np.ndarray,
    *,
    frac: float = 0.6,
    band_hz: tuple[float | None, float | None] | None = None,
    sample_rate: int | None = None,
    repeat_offsets_samples: tuple[int, ...] = (),
) -> int:
    """Index of the EARLIEST normalized-correlation peak within ``frac`` of max.

    A locally energy-normalized matched filter (cosine similarity per lag),
    so a quieter-but-identical first occurrence (MEASURE's woofer repeat) or
    a shape-sharing different-level segment (CHECK's lo/hi pilot pair)
    scores the same as a louder later one; taking the earliest lag within
    ``frac`` of the max picks the true first occurrence.

    For repeats, a first occurrence must also rank on the product of the
    correlations at its scheduled repeat offsets.

    ``band_hz`` restricts similarity to the stimulus's OWN declared band —
    without it, room noise the stimulus never occupied suppresses a quiet
    member's score (a quiet pilot once scored below gate despite better
    in-band SNR than a passing round, latching onto the wrong pilot and
    sliding every analysis window one pilot spacing). A caller with no band
    to declare keeps the full-band behavior.
    """
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
    if repeat_offsets_samples:
        first = ncc[:-repeat_offsets_samples[-1]]
        paired = first.copy()
        for offset in repeat_offsets_samples:
            paired *= ncc[offset:offset + first.size]
        paired[first < frac * float(ncc.max())] = 0.0
        ncc = paired
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


def _cached_stimulus(stimuli: dict[str, np.ndarray], segment: ProgramSegment) -> np.ndarray:
    if segment.segment_id not in stimuli:
        stimuli[segment.segment_id] = segment_stimulus(segment)
    return stimuli[segment.segment_id]


# One timeline reading: (witness presence, witness confidence, anchor segment, global offset).
_Reading = tuple[float, float, ProgramSegment, int]


def _score_anchor_witness(
    witness: ProgramSegment, candidates: list[ProgramSegment], capture: np.ndarray,
    sample_rate: int, arrival: int, stimuli: dict[str, np.ndarray],
) -> tuple[_Reading, _Reading, bool]:
    witness_stim = _cached_stimulus(stimuli, witness)
    scored: list[_Reading] = []
    for seg in candidates:
        offset = arrival - seg.start_sample
        _located, confidence, presence = _locate_in_window(
            capture, witness_stim, offset + witness.start_sample,
            witness.n_samples, sample_rate=sample_rate,
        )
        scored.append((presence, confidence, seg, offset))
    # Presence measures similarity; confidence alone can rank empty windows highly.
    # `max` keeps the first candidate on an exact tie.
    best_index, best = max(enumerate(scored), key=lambda item: item[1][0])
    runner_up = max(
        (row for index, row in enumerate(scored) if index != best_index),
        key=lambda item: item[0],
    )
    # Two candidates above the floor with presence within the ratio: the argmax
    # carries no information. Multiplication, so a zero runner-up presence resolves.
    ambiguous = (
        best[1] >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        and runner_up[1] >= SWEEP_LOCATE_CONFIDENCE_FLOOR
        and best[0] < runner_up[0] * ANCHOR_DISCRIMINATION_RATIO
    )
    return best, runner_up, ambiguous


def _witness_twinned(program: ExcitationProgram, witness: ProgramSegment, shift: int, sample_rate: int) -> bool:
    """Whether a same-shape copy of ``witness`` sits one rival ``shift`` away (CHECK's pilot pairs, #2644).

    Then the rival reading's witness window can land on a real stimulus that
    correlates exactly like the witness, and only the witness-only guard applies.
    """
    search = SEGMENT_SEARCH_S * sample_rate
    return any(
        seg is not witness and seg.kind in STIMULUS_KINDS and _stimulus_shape(seg) == _stimulus_shape(witness)
        and abs(abs(seg.start_sample - witness.start_sample) - abs(shift)) <= search
        for seg in program.segments
    )


def _pair_presences(
    capture: np.ndarray, stim: np.ndarray, best: _Reading, runner_up: _Reading, sample_rate: int,
) -> tuple[float, float] | None:
    """Each reading's schedule asked of the other reading's anchor segment.

    Returns the presence of the runner-up's segment where the best reading
    schedules it, then of the best's segment where the runner-up reading
    schedules it; ``None`` when either window leaves the recording, since a slot
    the capture never held is no evidence against a reading.
    """
    _, _, best_seg, best_offset = best
    _, _, runner_up_seg, runner_up_offset = runner_up
    slots = (best_offset + runner_up_seg.start_sample, runner_up_offset + best_seg.start_sample)
    search = int(round(SEGMENT_SEARCH_S * sample_rate))
    if min(slots) < search or max(slots) + best_seg.n_samples + search > capture.size:
        return None
    for_best, for_runner_up = (
        _locate_in_window(capture, stim, slot, best_seg.n_samples, sample_rate=sample_rate)[2] for slot in slots
    )
    return for_best, for_runner_up


def _resolve_anchor(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    arrival: int,
    first: ProgramSegment,
    stimuli: dict[str, np.ndarray],
) -> tuple[ProgramSegment, int, AnchorEvidence | None]:
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
    flip. That holds where the schedule can put a copy of the witness in the
    rival's window (CHECK's twin pilots, #2644). Where it cannot, the rival
    window reads an empty window's floor, which a reverberant seat can bring
    within the ratio of a present witness (#5632), so the anchor pair's own
    schedule is scored jointly with it: each reading predicts where the
    other's anchor segment plays, and the near-tie clears only when the
    witness and the pair each separate the readings by the root of the ratio.
    The returned evidence carries that ambiguity and whether the witness
    corroborated the anchor at all.

    Branch programs try equally long witnesses in schedule order only on
    ambiguity; if none resolves it, the first witness's evidence stands.
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
        return first, arrival - first.start_sample, None

    best, second, ambiguous = _score_anchor_witness(
        witness, candidates, capture, sample_rate, arrival, stimuli,
    )
    witnesses_tried = 1
    # CHECK's pilot_tweeter_hi is 1.3003 s after its witness vs 1.3048 s pilot spacing: 4.5 ms within ±30 ms can confirm the wrong anchor.
    if ambiguous and is_branch_program(program):
        for alternate in program.segments:
            if (alternate == witness or alternate.kind not in STIMULUS_KINDS
                    or alternate.n_samples != witness.n_samples or _stimulus_shape(alternate) == shape):
                continue
            witnesses_tried += 1
            resolved = _score_anchor_witness(alternate, candidates, capture, sample_rate, arrival, stimuli)
            if not resolved[2]:
                witness, (best, second, ambiguous) = alternate, resolved
                break
    best_presence, best_confidence, best_seg, best_offset = best
    runner_up_presence, runner_up, runner_up_seg, runner_up_offset = second
    pair = None
    if ambiguous and not _witness_twinned(program, witness, runner_up_offset - best_offset, sample_rate):
        pair = _pair_presences(capture, _cached_stimulus(stimuli, first), best, second, sample_rate)
        if pair is not None:
            # Each by the root of the ratio on its own: two empty pilot slots (1-5x measured)
            # or an unscheduled copy of the witness (about 2x) leave the take un-attributed.
            root = math.sqrt(ANCHOR_DISCRIMINATION_RATIO)
            ambiguous = not (best_presence >= runner_up_presence * root and pair[0] > pair[1] * root)
    assert witness.f1_hz is not None and witness.f2_hz is not None
    witness_stim = stimuli[witness.segment_id]
    # Filtering can prove the timeline but inflates empty-window presence,
    # so ranking and ambiguity keep the full-band scores.
    _, band_confidence, _ = _locate_in_window(
        capture, witness_stim, best_offset + witness.start_sample,
        witness.n_samples, sample_rate=sample_rate,
        band_hz=(max(WITNESS_BAND_FLOOR_HZ, witness.f1_hz), witness.f2_hz),
    )
    corroborated = max(best_confidence, band_confidence) >= SWEEP_LOCATE_CONFIDENCE_FLOOR
    if not corroborated:
        best_seg, best_offset = first, arrival - first.start_sample
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
        witnesses_tried=witnesses_tried,
        candidates=len(candidates),
        presence=round(best_presence, 6),
        runner_up_presence=round(runner_up_presence, 6),
        pair_presence=None if pair is None else round(pair[0], 6),
        pair_runner_up_presence=None if pair is None else round(pair[1], 6),
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
    return best_seg, best_offset, AnchorEvidence(
        ambiguous=ambiguous, presence=float(best_presence),
        confidence=float(best_confidence), corroborated=bool(corroborated),
        runner_up_presence=float(runner_up_presence), runner_up_confidence=float(runner_up),
        witnesses_tried=witnesses_tried,
        pair_presence=None if pair is None else float(pair[0]),
        pair_runner_up_presence=None if pair is None else float(pair[1]),
    )


def _global_offset(
    program: ExcitationProgram, capture: np.ndarray, sample_rate: int
) -> tuple[int, ProgramSegment, dict[str, np.ndarray], AnchorEvidence | None]:
    """Locate the anchor stimulus -> integer global offset G. Caches stimuli.

    The whole-capture matched filter runs at :data:`LOCATOR_RATE_HZ`; the
    coarse arrival is then refined at the full rate inside a tiny window, so
    the returned offset is full-rate-exact. That locate answers WHERE, not
    WHICH occurrence. Repeated summed sweeps use their scheduled spacing;
    other programs use :func:`_resolve_anchor`. The fourth return value
    carries the measured evidence.
    """
    stimuli: dict[str, np.ndarray] = {}
    sweeps = [seg for seg in program.segments if seg.kind == KIND_SUMMED_SWEEP]
    repeated = len(sweeps) > 1 and all(
        _stimulus_shape(seg) == _stimulus_shape(sweeps[0])
        and seg.start_sample >= previous.start_sample + previous.n_samples
        for previous, seg in zip(sweeps, sweeps[1:])
    )
    first = sweeps[0] if repeated else next(
        (seg for seg in program.segments if seg.kind in STIMULUS_KINDS), None,
    )
    if first is None:
        raise ValueError("program has no stimulus segment to locate against")
    stim = segment_stimulus(first)
    stimuli[first.segment_id] = stim
    arrival = _arrival(
        capture, stim, sample_rate, band_hz=(first.f1_hz, first.f2_hz),
        repeat_offsets_samples=tuple(seg.start_sample - first.start_sample
                                     for seg in sweeps[1:]) if repeated else (),
    )
    if repeated:
        offset = arrival - first.start_sample
        sweep_evidence = _resolve_sweep_anchor(program, capture, sample_rate, offset, first, sweeps[1], stim)
        return offset, first, stimuli, sweep_evidence
    anchor, global_offset, evidence = _resolve_anchor(
        program, capture, sample_rate, arrival, first, stimuli
    )
    return global_offset, anchor, stimuli, evidence


def _arrival(
    capture: np.ndarray, stim: np.ndarray, sample_rate: int, *,
    band_hz: tuple[float | None, float | None], repeat_offsets_samples: tuple[int, ...] = (),
) -> int:
    """Where ``stim`` first arrives in ``capture``: matched at :data:`LOCATOR_RATE_HZ`,
    then refined at the full rate inside a tiny window."""
    down = max(1, int(round(sample_rate / LOCATOR_RATE_HZ)))
    if down > 1:
        capture_lo = resample_poly(capture, 1, down)
        stim_lo = resample_poly(np.asarray(stim, dtype=np.float64), 1, down)
    else:
        capture_lo = capture
        stim_lo = np.asarray(stim, dtype=np.float64)
    coarse = _earliest_strong_peak(
        capture_lo, stim_lo, band_hz=band_hz, sample_rate=sample_rate // down,
        repeat_offsets_samples=tuple(round(offset / down) for offset in repeat_offsets_samples),
    ) * down

    # Full-rate refinement in a +/-4*down window: bounded cost, full-rate precision.
    margin = 4 * down
    lo = max(0, coarse - margin)
    hi = min(capture.size, coarse + stim.size + margin)
    window = capture[lo:hi]
    if window.size < stim.size:
        return coarse
    return lo + _earliest_strong_peak(window, stim, band_hz=band_hz, sample_rate=sample_rate)


def _staircase_offset(program: ExcitationProgram, capture: np.ndarray, sample_rate: int) -> int:
    """A level probe's global offset, its whole program one matched filter (ADR-0365).

    Its bursts differ in length, so only the true alignment lines up every burst the
    capture holds, whichever the room buried or the stop cut short. The capture is
    padded so a stopped probe still spans its program.
    """
    template = render_program_pcm(program).sum(axis=1)
    burst = next(seg for seg in program.segments if seg.kind in STIMULUS_KINDS)
    return _arrival(np.pad(capture, (0, template.size)), template, sample_rate,
                    band_hz=(burst.f1_hz, burst.f2_hz))


def _resolve_sweep_anchor(
    program: ExcitationProgram, capture: np.ndarray, sample_rate: int,
    offset: int, first: ProgramSegment, witness: ProgramSegment, stimulus: np.ndarray,
) -> AnchorEvidence:
    """Distinguish a displaced sweep witness from an unlocated one.

    Search through the inter-pass quiet span so an off-schedule copy can
    establish ambiguity beyond the normal segment search window.
    """
    # A clean template can locate a sweep when two noisy captures cannot be
    # aligned reliably. align_summed_capture's shared-power rule controls
    # sample shifts, not audibility; both use SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
    # for the timing tolerance.
    scheduled = offset + witness.start_sample
    search_samples = max(round(SEGMENT_SEARCH_S * sample_rate),
                         witness.start_sample - first.start_sample - first.n_samples)
    located, confidence, presence = _locate_sweep(
        capture, stimulus, scheduled, witness, sample_rate=sample_rate, search_samples=search_samples,
    )
    residual_ms = (located - scheduled) / sample_rate * 1000.0
    found = confidence >= SWEEP_LOCATE_CONFIDENCE_FLOOR
    displaced = abs(residual_ms) > SWEEP_SCHEDULE_RESIDUAL_CEILING_MS
    corroborated = found and not displaced
    evidence = AnchorEvidence(
        anchor=first.segment_id, witness=witness.segment_id, shift_ms=offset / sample_rate * 1000.0,
        witness_residual_ms=residual_ms, ambiguous=found and displaced,
        presence=presence, confidence=confidence, corroborated=corroborated,
    )
    log_event(
        logger, "program_analysis.anchor", level=logging.INFO if corroborated else logging.WARNING,
        phase=program.phase, program_id=program.program_id, anchor=evidence.anchor,
        witness=evidence.witness, shift_ms=evidence.shift_ms,
        witness_residual_ms=residual_ms, presence=presence, confidence=confidence,
        corroborated=corroborated, ambiguous=evidence.ambiguous,
    )
    return evidence


def _locate_in_window(
    capture: np.ndarray,
    stim: np.ndarray,
    scheduled: int,
    n_samples: int,
    *,
    sample_rate: int,
    band_hz: tuple[float, float] | None = None,
    search_samples: int | None = None,
) -> tuple[int, float, float]:
    """Matched-filter ``stim`` at ``scheduled`` +/- :data:`SEGMENT_SEARCH_S` by default.

    Returns BOTH scores, since they answer different questions: ``confidence``
    is the peakedness margin (is the winning lag sharp against its own
    neighbourhood — :data:`SWEEP_LOCATE_CONFIDENCE_FLOOR` grades this, NOT
    whether ``stim`` is here at all, since over ~61 ms of lags two room-noise
    correlations already ratio 0.6-0.8); ``presence`` is the normalized
    correlation similarity, which does say. A window too short to hold
    ``stim`` yields ``(scheduled, 0.0, 0.0)``, never a located claim.
    """
    search = int(round(SEGMENT_SEARCH_S * sample_rate)) if search_samples is None else search_samples
    lo = max(0, scheduled - search)
    hi = min(capture.size, scheduled + n_samples + search)
    window = capture[lo:hi]
    if window.size < stim.size:
        return scheduled, 0.0, 0.0
    if band_hz is not None:
        window_b = _bandlimit(window, sample_rate, *band_hz)
        stim_b = _bandlimit(stim, sample_rate, *band_hz)
        if float(np.linalg.norm(stim_b)) > 0.0 and float(np.linalg.norm(window_b)) > 0.0:
            window, stim = window_b, stim_b
    res = _locate(
        window, stim, sample_rate=sample_rate,
        max_capture_s=window.size / sample_rate + 1.0,
    )
    return lo + int(res.lag_samples), float(res.confidence), float(res.peak)


def _locate_sweep(
    capture: np.ndarray, stim: np.ndarray, scheduled: int, sweep: ProgramSegment, *,
    sample_rate: int, search_samples: int | None = None,
) -> tuple[int, float, float]:
    """:func:`_locate_in_window` for a sweep: full band, or above the room's modal
    tails (:data:`WITNESS_BAND_FLOOR_HZ`) when only that view clears the locate floor."""
    located = _locate_in_window(capture, stim, scheduled, sweep.n_samples,
                                sample_rate=sample_rate, search_samples=search_samples)
    assert sweep.f1_hz is not None and sweep.f2_hz is not None
    band_start = max(WITNESS_BAND_FLOOR_HZ, sweep.f1_hz)
    if located[1] >= SWEEP_LOCATE_CONFIDENCE_FLOOR or band_start >= sweep.f2_hz:
        return located
    banded = _locate_in_window(capture, stim, scheduled, sweep.n_samples, sample_rate=sample_rate,
                               band_hz=(band_start, sweep.f2_hz), search_samples=search_samples)
    return banded if banded[1] >= SWEEP_LOCATE_CONFIDENCE_FLOOR else located


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
            stim = _cached_stimulus(stimuli, seg)
            # `presence` is the anchor arbitration's term, not this one's: every
            # gate on `SegmentLocation.confidence` is calibrated on the
            # peakedness margin, so recording the other would move all of them.
            # A summed sweep uses the sweep-witness rule, so a sweep the anchor heard is heard here (#5632).
            located, confidence, _presence = (
                _locate_sweep(capture, stim, scheduled, seg, sample_rate=sample_rate)
                if seg.kind == KIND_SUMMED_SWEEP else
                _locate_in_window(capture, stim, scheduled, seg.n_samples, sample_rate=sample_rate)
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
