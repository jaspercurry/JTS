# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Gate sensitivity on each band of the round spec."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from jasper.active_speaker import flat_spec
from jasper.active_speaker.crossover_v2.gate_sweep import (
    DEFAULT_RUNGS_MS,
    GRID_HI_HZ,
    GRID_LO_HZ,
    REFUSE_SINGLE_POSE,
    analysis_grid,
    frame_descriptor,
    sweep_features,
)
from jasper.active_speaker.crossover_v2.round_captures import (
    RoundCapturesRefused,
    discover_captures,
)
from jasper.active_speaker.flat_spec import FlatSpecReport

from .banked import BankedRound

#: Why a band carries no gate sensitivity when the LADDER NEVER RAN on it. The
#: ``not_swept_`` prefix tells these apart from the sweep's own refusals
#: (``gate_sweep.NULL_*``), which mean the ladder ran and then declined to
#: publish; both land in the one ``BandResult.gate_sensitivity_note`` field.
NOT_SWEPT_SINGLE_POSE = "not_swept_single_pose"
NOT_SWEPT_BAND_NOT_EVALUABLE = "not_swept_band_not_evaluable"
#: Every ``RoundCapturesRefused`` the ladder can raise EXCEPT the single-pose
#: one. They are one word here because the answer is the same — this round's
#: captures did not become curves — and the refusal carries the detail.
NOT_SWEPT_CAPTURES_UNREADABLE = "not_swept_captures_unreadable"
NOT_SWEPT_BIN_OFF_ANALYSIS_GRID = "not_swept_bin_outside_analysis_grid"


def _stamped_band(
    band: flat_spec.BandResult,
    feature: Mapping[str, Any] | None,
    note: str | None,
    detail: dict[str, Any] | None,
) -> flat_spec.BandResult:
    """One band plus the ladder's read at its own worst bin, or the reason why not.

    ``n_valid_rungs`` and ``gate_window_verdict`` are stamped whenever the ladder
    RAN, including on a null: ``"unresolved"`` rather than absent, so silence is
    never mistaken for "never swept". ``detail`` carries only beside a
    capture-refusal note and is ``None`` for every other note.
    """
    if feature is None:
        return replace(band, gate_sensitivity_note=note, gate_sensitivity_detail=detail)
    sensitivity = feature.get("sensitivity")
    return replace(
        band,
        gate_sensitivity_db=(
            None if sensitivity is None else float(sensitivity["corrected_delta_db"])
        ),
        sigma_growth_ratio=(
            None if sensitivity is None else float(sensitivity["sigma_growth_ratio"])
        ),
        n_valid_rungs=int(feature["n_valid_rungs"]),
        gate_sensitivity_note=feature.get("sensitivity_null_reason"),
        gate_window_verdict=feature["window_verdict"],
        gate_window_verdict_reasons=tuple(feature["window_verdict_reasons"]),
    )


def spec_with_gate_sensitivity(
    banked: BankedRound, *, rungs_ms: Sequence[float] = DEFAULT_RUNGS_MS,
) -> FlatSpecReport:
    """The round's graded spec, with "room or speaker" answered at every band's
    own worst bin.

    The spec verdict names the bin; :mod:`.gate_sweep` says whether that bin
    moves with the analysis window. Each
    :class:`~jasper.active_speaker.flat_spec.BandResult` carries the ladder's
    headline at its own ``max_deviation_hz``, and the report carries the frame
    those numbers are stated in. **Disclosure only: no grade moves.**

    It reads a BANKED round rather than the live combine because the cloud
    pipeline's seam keeps each position's magnitude and drops the ``ir``. The IR
    reachable there has already been through ``deconv.direct_arrival_window``
    and the adaptive reflection gate, whose search stops at
    ``gating.SEARCH_T_MAX_MS`` — 7 ms — so the ladder's 9, 12 and 20 ms rungs
    would read a window closed before they got there and ``sigma_growth_ratio``
    would come back at ~1.0 by construction. Only a banked round's raw
    ``summed_*.wav``, deconvolved here against its own program, can answer what
    a longer window admits.

    Cost is one ladder pass at up to three bins. Every way there can be no
    number is named in ``gate_sensitivity_note`` and none of them raises; a
    capture refusal additionally stamps ``gate_sensitivity_detail`` with the
    specific input that was missing.
    """
    report = banked.graded_report
    targets: list[tuple[int, float]] = []
    notes: dict[int, str] = {}
    for index, band in enumerate(report.bands):
        worst_hz = band.max_deviation_hz
        if worst_hz is None:
            notes[index] = NOT_SWEPT_BAND_NOT_EVALUABLE
        elif not GRID_LO_HZ <= float(worst_hz) <= GRID_HI_HZ:
            # Named per band rather than allowed to raise: ``sweep_features``
            # rejects the whole CALL on one off-grid bin.
            notes[index] = NOT_SWEPT_BIN_OFF_ANALYSIS_GRID
        else:
            targets.append((index, float(worst_hz)))

    features: dict[int, Mapping[str, Any]] = {}
    details: dict[int, dict[str, Any]] = {}
    frame: dict[str, Any] | None = None
    if targets:
        rungs = tuple(sorted(float(rung) for rung in rungs_ms))
        try:
            swept = sweep_features(
                discover_captures(banked.round_dir),
                rungs_ms=rungs,
                at_hz=[hz for _index, hz in targets],
            )
        except RoundCapturesRefused as exc:
            # The engine's own bar, echoed rather than re-judged: it refuses
            # fewer than two captures.
            refused = (
                NOT_SWEPT_SINGLE_POSE
                if exc.reason == REFUSE_SINGLE_POSE
                else NOT_SWEPT_CAPTURES_UNREADABLE
            )
            notes.update({index: refused for index, _hz in targets})
            # The bucket slug names only the shape; what was missing rides on
            # the exception the engine already raised.
            details.update(
                {index: {"reason": exc.reason, **exc.detail} for index, _hz in targets}
            )
        else:
            # Positional, never keyed by frequency: two bands asking about one
            # bin is a coincidence, not a reason to share an answer.
            features = {
                index: feature
                for (index, _hz), feature in zip(targets, swept, strict=True)
            }
            frame = frame_descriptor(rungs, analysis_grid())

    return replace(
        report,
        bands=tuple(
            _stamped_band(band, features.get(index), notes.get(index), details.get(index))
            for index, band in enumerate(report.bands)
        ),
        gate_sweep_frame=frame,
    )
