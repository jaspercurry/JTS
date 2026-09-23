# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One summed capture reduced for comparison, and the entry baseline a round records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np

from .contracts import CrossoverV2ContractError, ResponseCurve, _text

if TYPE_CHECKING:  # pragma: no cover - typing only
    # ``program_analysis`` is a 5,500-line scipy-backed module and should not
    # be dragged into every package import.
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

__all__ = [
    "BENEFIT_CURVE_MAX_BINS",
    "ENTRY_BASELINE_KIND",
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

#: ``kind`` stamped on the persisted record, matching the package's convention.
ENTRY_BASELINE_KIND = "jts_crossover_v2_entry_baseline"


# --------------------------------------------------------------------------
# one capture, reduced
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredResponse:
    """One summed at-the-mark capture, reduced to what a comparison needs."""

    program_id: str
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
    program_id = str(getattr(analysis, "program_id", "") or "")
    if summed is None or not program_id:
        return None

    from jasper.audio_measurement.analysis import smooth_fractional_octave
    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

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
        program_id=program_id,
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


# --------------------------------------------------------------------------
# the entry baseline, as it crosses the stage bridge
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryBaseline:
    """The pre-apply side of the round, and the graph it was measured on.

    Persisted by the host between stage 1 (which captures it) and stage 2 (which
    grades against it), so it is JSON-shaped by construction. The graph
    fingerprint travels with it because a later round binds the currently-active
    profile as its own entry graph, which a curve with no record of its graph
    cannot support.

    The flow state file's copy lives exactly as long as the round; the copy that
    outlives it is the write-once retained take
    (``spatial.entry_baseline_record``). Both are written from one
    :class:`MeasuredResponse`; neither is derived from the other.
    """

    program_id: str
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
            program_id=measured.program_id,
            reference_mark=measured.reference_mark,
            curve=measured.curve,
            excluded=measured.excluded,
            graph_fingerprint=_text(
                graph_fingerprint, field_name="graph_fingerprint"
            ),
            captured_at=_text(captured_at, field_name="captured_at"),
            artifact_ref=str(artifact_ref or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ENTRY_BASELINE_KIND,
            "program_id": self.program_id,
            "reference_mark": self.reference_mark,
            "freqs_hz": list(self.curve.hz),
            "magnitude_db": list(self.curve.db),
            "excluded": [bool(flag) for flag in self.excluded],
            "graph_fingerprint": self.graph_fingerprint,
            "captured_at": self.captured_at,
            "artifact_ref": self.artifact_ref,
        }

    @classmethod
    def from_dict(cls, record: Any) -> "EntryBaseline | None":
        """Rehydrate, or ``None`` for anything this build did not write.

        ``None`` rather than a raise or a partially-trusted record: a pre-key
        state file, a truncated write and a hand-edited file all mean "there is
        no comparable baseline".
        """

        if not isinstance(record, Mapping):
            return None
        freqs = record.get("freqs_hz")
        levels = record.get("magnitude_db")
        excluded = record.get("excluded")
        if not isinstance(freqs, (list, tuple)) or not isinstance(
            levels, (list, tuple)
        ):
            return None
        if not isinstance(excluded, (list, tuple)) or len(excluded) != len(freqs):
            return None
        try:
            curve = ResponseCurve(freqs, levels)
            return cls(
                program_id=_text(
                    record.get("program_id"), field_name="program_id"
                ),
                reference_mark=_text(
                    record.get("reference_mark"), field_name="reference_mark"
                ),
                curve=curve,
                excluded=tuple(bool(flag) for flag in excluded),
                graph_fingerprint=_text(
                    record.get("graph_fingerprint"),
                    field_name="graph_fingerprint",
                ),
                captured_at=_text(
                    record.get("captured_at"), field_name="captured_at"
                ),
                artifact_ref=str(record.get("artifact_ref") or ""),
            )
        except CrossoverV2ContractError:
            return None
