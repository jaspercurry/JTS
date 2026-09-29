# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One summed capture reduced, and the session's timing prior built from it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.spatial_combine import decimate_curve_to_analysis_grid

from .contracts import CrossoverV2ContractError, ResponseCurve, _text

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``program_analysis`` is a 5,500-line scipy-backed module and should not
    # be dragged into every package import.
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

__all__ = [
    "BENEFIT_CURVE_MAX_BINS",
    "ITERATION_PLATEAU_DB",
    "MEASURED_BENEFIT_MARGIN_DB",
    "EntryBaseline",
    "MeasuredResponse",
    "measured_response_from_analysis",
]


# --------------------------------------------------------------------------
# the margin
# --------------------------------------------------------------------------

#: dB. How much flatter the speaker must measure before the round may say so.
#:
#: Fallback until a pooled repeat study is banked; the frozen tracking study
#: measured a different metric. See repeat_floor.stopping_thresholds.
MEASURED_BENEFIT_MARGIN_DB = 0.5

#: dB. Advisory threshold for objective size and inter-round movement.
#: A banked repeat floor supplies the measured value through stopping_thresholds.
ITERATION_PLATEAU_DB = 0.25


#: Resolution of BOTH sides of the benefit comparison — the same 512 the host
#: applies to ``verify_priors.predicted_sum``, since the entry baseline crosses
#: the durable stage bridge. Named here rather than imported because
#: :mod:`jasper.web.correction_crossover_v2` is the wrong direction for this
#: package, and because governing both sides puts them on one grid by
#: construction rather than by luck.
BENEFIT_CURVE_MAX_BINS = 512


# --------------------------------------------------------------------------
# one capture, reduced
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredResponse:
    """One summed at-the-mark capture, reduced to what a comparison needs."""

    stimulus_id: str
    reference_mark: str
    curve: ResponseCurve
    excluded: tuple[bool, ...]


def measured_response_from_analysis(
    analysis: "ProgramAnalysis | None", *, reference_mark: str,
) -> MeasuredResponse | None:
    """Reduce one VERIFY-program analysis to a comparison side, or ``None``.

    ``None`` for every honest "there is nothing here to compare": no analysis,
    no summed response, a degenerate curve.

    Block-average onto the analysis grid first (the smoother is an
    O(bins x window) Python loop and a raw grid costs seconds on a Pi), then
    1/3-octave smooth. Another order or fraction produces a curve
    ``flat_spec.evaluate_flat_spec`` grades differently from every other curve
    in this subsystem.
    """

    if analysis is None:
        return None
    summed = getattr(analysis, "summed_response", None)
    stimulus_id = str(getattr(analysis, "stimulus_id", "") or "")
    if summed is None or not stimulus_id:
        return None

    try:
        grid, coarse_db = decimate_curve_to_analysis_grid(
            np.asarray(summed.freqs_hz, dtype=float),
            np.asarray(summed.magnitude_db, dtype=float),
            max_bins=BENEFIT_CURVE_MAX_BINS,
        )
        smoothed = smooth_fractional_octave(grid, coarse_db, fraction=3)
        curve = ResponseCurve(grid, smoothed)
    except (ValueError, TypeError, IndexError, AttributeError,
            CrossoverV2ContractError):
        # A malformed or non-finite capture is a "cannot compare this", not a
        # crash to propagate into a household decision.
        return None
    return MeasuredResponse(
        stimulus_id=stimulus_id,
        reference_mark=_text(reference_mark, field_name="reference_mark"),
        curve=curve,
        excluded=_validity_clamp(grid, getattr(summed, "validity_floor_hz", None)),
    )


def _validity_clamp(grid: np.ndarray, validity_floor_hz: Any) -> tuple[bool, ...]:
    """Bins below this capture's own reflection gate, as a per-bin flag.

    Below ``gating.f_valid_floor_hz`` the response is an artifact of a
    truncated gate window. An absent or non-finite floor screens nothing — "no
    evidence of a floor" is not "the floor is at zero".
    """

    floor = validity_floor_hz
    if floor is None or not isinstance(floor, (int, float)) or isinstance(floor, bool):
        return (False,) * int(grid.size)
    floor = float(floor)
    if not np.isfinite(floor):
        return (False,) * int(grid.size)
    return tuple(bool(value) for value in (np.asarray(grid, dtype=float) < floor))


@dataclass(frozen=True)
class EntryBaseline:
    """The session's timing take (ADR-0319), the prior MEASURE reads its summed
    alignment from. It lives only in the session: nothing persists it (ADR-0390).
    """

    stimulus_id: str
    reference_mark: str
    curve: ResponseCurve
    excluded: tuple[bool, ...]
    graph_fingerprint: str
    captured_at: str
    artifact_ref: str = ""

    @classmethod
    def from_measurement(
        cls,
        measured: MeasuredResponse,
        *,
        graph_fingerprint: str,
        captured_at: str,
        artifact_ref: str = "",
    ) -> "EntryBaseline":
        return cls(
            stimulus_id=measured.stimulus_id,
            reference_mark=measured.reference_mark,
            curve=measured.curve,
            excluded=measured.excluded,
            graph_fingerprint=_text(
                graph_fingerprint, field_name="graph_fingerprint"
            ),
            captured_at=_text(captured_at, field_name="captured_at"),
            artifact_ref=str(artifact_ref or ""),
        )
