# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""In-capture clock drift, estimated from the MEASURE sweep repeats."""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

import numpy as np

from jasper.audio_measurement.program import ExcitationProgram, KIND_SWEEP
from jasper.audio_measurement.timeline_slip import (
    fit_timeline_step,
    GLITCH_INPUT_TIMELINE_SLIP,
    slip_rejects_capture,
    TimelineStepFit,
)
from jasper.log_event import log_event
from .check import _band_rms_dbfs, _pilot_trim_fade
from .model import (
    DISCONTINUITY_UNRESOLVED,
    DriftEstimate,
    GLITCH_RESIDUAL_SAMPLES,
    logger,
    MAX_DRIFT_PPM,
    REPEAT_LEVEL_TOLERANCE_DB,
    SegmentLocation,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
)
from .signals import _subsample_separation


# A MEASURE sweep segment ID's occurrence suffix (build_measure_program's
# _occurrence_suffix): bare = first/primary, "_rep" = second, "_repN" = the
# (N+1)-th. Anchored at the END of the id so a driver token embedded earlier
# (today "w"/"t") never matters here.
_SWEEP_OCCURRENCE_SUFFIX_RE = re.compile(r"_rep(\d*)$")


def _sweep_occurrence_index(segment_id: str) -> int:
    """0-based occurrence index encoded in a MEASURE sweep segment ID's
    suffix (mirrors ``program.build_measure_program``'s ``_occurrence_suffix``):
    bare id ⇒ 0 (first/primary), ``_rep`` ⇒ 1, ``_rep{n}`` ⇒ n (n ≥ 2).
    """
    m = _SWEEP_OCCURRENCE_SUFFIX_RE.search(segment_id)
    if m is None:
        return 0
    digits = m.group(1)
    return 1 if digits == "" else int(digits)


def _sweep_occurrences_by_role(
    locations: Sequence[SegmentLocation],
) -> dict[str, list[SegmentLocation]]:
    """Group MEASURE ``KIND_SWEEP`` locations by driver role, each list
    ordered first->last by ID-encoded occurrence index
    (:func:`_sweep_occurrence_index`), not physical schedule position — the
    N=3 layout interleaves w1,t1,w2,t2,... (design §5.4).
    """
    by_role: dict[str, list[tuple[int, SegmentLocation]]] = {}
    for loc in locations:
        if loc.kind != KIND_SWEEP or not loc.role:
            continue
        by_role.setdefault(loc.role, []).append(
            (_sweep_occurrence_index(loc.segment_id), loc)
        )
    return {
        role: [loc for _idx, loc in sorted(pairs, key=lambda pair: pair[0])]
        for role, pairs in by_role.items()
    }


def _repeat_epsilon(
    capture: np.ndarray,
    program: ExcitationProgram,
    first: SegmentLocation,
    last: SegmentLocation,
) -> tuple[float, float] | None:
    """Sub-sample + integer-only clock-drift epsilon from a role's FIRST vs
    LAST located sweep occurrence (design §3.1/§5.6.3). ``None`` when the
    two share a degenerate scheduled start.
    """
    seg_first = program.segment(first.segment_id)
    seg_last = program.segment(last.segment_id)
    scheduled_sep = seg_last.start_sample - seg_first.start_sample
    if scheduled_sep <= 0:
        return None
    measured_sep = _subsample_separation(
        capture, first.located_start, last.located_start, seg_first.n_samples
    )
    epsilon = measured_sep / scheduled_sep - 1.0
    eps_int = (last.located_start - first.located_start) / scheduled_sep - 1.0
    return epsilon, eps_int


def _locate_discontinuity(
    program: ExcitationProgram,
    capture: np.ndarray,
    stimulus_locs: Sequence[SegmentLocation],
) -> tuple[float | str, str, TimelineStepFit]:
    """Fit a single discrete timeline STEP across the located sweeps.

    Returns ``(step_samples, after_segment_id, fit)``: ``(0.0, "")`` when no
    step is resolved (clean capture); ``(DISCONTINUITY_UNRESOLVED, "")``
    when any ``stimulus_locs`` falls below
    ``SWEEP_LOCATE_CONFIDENCE_FLOOR``, since a step fitted from a barely-found
    sweep is invented from noise. The model lives in
    :mod:`jasper.audio_measurement.timeline_slip`; the sharp input is each
    occurrence placed against its role's first by
    :func:`_subsample_separation` (measured scatter 0.038-0.299 samples)
    rather than the integer ``located_start`` (2.00-3.13 samples on clean
    hardware) — this also cancels the global offset and constant acoustic
    delay structurally.
    """
    ordered = sorted(
        stimulus_locs, key=lambda loc: program.segment(loc.segment_id).start_sample
    )
    # Gate BEFORE the fit, on the confidence the fit is about to trust.
    if any(loc.confidence < SWEEP_LOCATE_CONFIDENCE_FLOOR for loc in ordered):
        return DISCONTINUITY_UNRESOLVED, "", TimelineStepFit()

    # Sub-sample positions, per role, referenced to that role's first
    # occurrence; only WITHIN-role placement needs to be sharp. Keyed by
    # POSITION in `ordered`, not object identity, since locations can compare equal.
    by_role: dict[str, list[int]] = {}
    for index, loc in enumerate(ordered):
        by_role.setdefault(loc.role or "", []).append(index)
    placed: list[float] = [0.0] * len(ordered)
    for members in by_role.values():
        reference = ordered[members[0]]
        ref_n = program.segment(reference.segment_id).n_samples
        placed[members[0]] = float(reference.located_start)
        for index in members[1:]:
            placed[index] = float(reference.located_start) + _subsample_separation(
                capture,
                reference.located_start,
                ordered[index].located_start,
                ref_n,
            )

    fit = fit_timeline_step(
        [float(program.segment(loc.segment_id).start_sample) for loc in ordered],
        placed,
        [loc.role or "" for loc in ordered],
    )
    if not slip_rejects_capture(fit):
        return 0.0, "", fit
    return fit.step_samples, ordered[fit.cut_index - 1].segment_id, fit


def _estimate_drift(
    program: ExcitationProgram,
    capture: np.ndarray,
    sample_rate: int,
    locations: Sequence[SegmentLocation],
) -> DriftEstimate:
    occurrences_by_role = _sweep_occurrences_by_role(locations)
    # Only SWEEP-kind stimuli anchor the drift baselines; a leading pilot
    # pair's short/quiet windows locate more coarsely and would manufacture
    # spurious desync (pilots are judged separately, on their own verdict).
    stimulus_locs = [loc for loc in locations if loc.kind == KIND_SWEEP]

    # Primary gate: the WOOFER's first-vs-LAST located occurrence — the one
    # literal anchor kept, since a MEASURE program always contains "sweep_w".
    woofer_role = program.segment("sweep_w").role
    assert woofer_role is not None, "a MEASURE sweep segment always carries a role"
    woofer_occurrences = occurrences_by_role.get(woofer_role, [])
    w1 = woofer_occurrences[0] if woofer_occurrences else None
    w2 = woofer_occurrences[-1] if len(woofer_occurrences) >= 2 else None

    epsilon = 0.0
    if w1 is not None and w2 is not None:
        result = _repeat_epsilon(capture, program, w1, w2)
        if result is not None:
            epsilon = result[0]

    # Per-driver-demeaned schedule residual after applying epsilon. A
    # driver's own acoustic delay is a constant offset (removed by
    # demeaning), so this catches a within-driver desync (a dropped buffer
    # between repeats), not the tweeter-vs-woofer delay. Only activates for
    # a role with >=2 located sweeps. Placed against its group's FIRST by
    # `_subsample_separation` (resolution argument owned by
    # `GLITCH_RESIDUAL_SAMPLES`), never `located_start`.
    groups: dict[Any, list[SegmentLocation]] = {}
    for loc in stimulus_locs:
        groups.setdefault(loc.role, []).append(loc)
    max_residual = 0.0
    for members in groups.values():
        reference = members[0]
        ref_seg = program.segment(reference.segment_id)
        resids = [0.0]
        for loc in members[1:]:
            scheduled_sep = (
                program.segment(loc.segment_id).start_sample - ref_seg.start_sample
            )
            measured_sep = _subsample_separation(
                capture, reference.located_start, loc.located_start, ref_seg.n_samples,
            )
            resids.append(measured_sep - scheduled_sep * (1.0 + epsilon))
        mean = sum(resids) / len(resids)
        for r in resids:
            max_residual = max(max_residual, abs(r - mean))

    # Woofer-repeat LEVEL agreement (design §5.2): first and last sweeps are
    # bit-identical, so a clean capture reproduces the same level. Measured
    # band-relative in-band RMS, never full-band peak — two hardware mics
    # measured identical sweeps 0.64 dB apart by peak but only 0.06-0.24 dB
    # apart by in-band RMS. A larger delta REUSES the
    # drift-baselines-disagree verdict rather than a new code.
    repeat_level_delta_db = 0.0
    repeat_level_disagrees = False
    if w1 is not None and w2 is not None:
        level_seg_w = program.segment("sweep_w")
        if level_seg_w.f1_hz is None or level_seg_w.f2_hz is None:
            raise ValueError("woofer sweep segment has no declared band")
        w1_samples = _pilot_trim_fade(
            capture[w1.located_start:w1.located_start + level_seg_w.n_samples], sample_rate,
        )
        w2_samples = _pilot_trim_fade(
            capture[w2.located_start:w2.located_start + level_seg_w.n_samples], sample_rate,
        )
        level_w1 = _band_rms_dbfs(w1_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        level_w2 = _band_rms_dbfs(w2_samples, sample_rate, level_seg_w.f1_hz, level_seg_w.f2_hz)
        repeat_level_delta_db = abs(level_w1 - level_w2)
        repeat_level_disagrees = repeat_level_delta_db > REPEAT_LEVEL_TOLERANCE_DB

    # Per-role diagnostics; NEVER gates `glitch_detected` (only the woofer pair does).
    per_role_epsilon_ppm: dict[str, float] = {}
    for role, occurrences in occurrences_by_role.items():
        if len(occurrences) < 2:
            continue
        result = _repeat_epsilon(capture, program, occurrences[0], occurrences[-1])
        if result is not None:
            per_role_epsilon_ppm[role] = result[0] * 1e6

    # Computed on EVERY capture, not just a failing one, for corpus telemetry.
    discontinuity_samples, discontinuity_after, slip_fit = _locate_discontinuity(
        program, capture, stimulus_locs
    )

    # WHICH bound tripped, fixed order — the verdict stays one reason code
    # (§5.2), this is telemetry's disambiguator.
    glitch_inputs = tuple(
        name
        for name, tripped in (
            ("epsilon_out_of_bound", abs(epsilon) * 1e6 > MAX_DRIFT_PPM),
            ("residual_desync", max_residual > GLITCH_RESIDUAL_SAMPLES),
            ("repeat_level_disagree", repeat_level_disagrees),
            (GLITCH_INPUT_TIMELINE_SLIP, slip_rejects_capture(slip_fit)),
        )
        if tripped
    )
    glitch = bool(glitch_inputs)

    if glitch:
        log_event(
            logger,
            "program_analysis.glitch",
            level=logging.WARNING,
            phase=program.phase,
            program_id=program.program_id,
            glitch_inputs=",".join(glitch_inputs),
            epsilon_ppm=round(epsilon * 1e6, 2),
            max_residual_samples=round(max_residual, 2),
            repeat_level_delta_db=round(repeat_level_delta_db, 3),
            # `discontinuity_samples` may be `DISCONTINUITY_UNRESOLVED` (a str).
            discontinuity_samples=(
                round(discontinuity_samples, 2)
                if isinstance(discontinuity_samples, (int, float))
                else discontinuity_samples
            ),
            discontinuity_after_segment=discontinuity_after,
        )
    return DriftEstimate(
        epsilon_ppm=epsilon * 1e6,
        max_residual_samples=max_residual,
        glitch_detected=glitch,
        repeat_level_delta_db=repeat_level_delta_db,
        per_role_epsilon_ppm=per_role_epsilon_ppm,
        glitch_inputs=glitch_inputs,
        discontinuity_samples=discontinuity_samples,
        discontinuity_after_segment=discontinuity_after,
    )
