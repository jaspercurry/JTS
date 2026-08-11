# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two measurements one correction round compares, and the margin (#2291).

#2291 Phase 3c's half of the honest loop that is neither a verdict nor a
capture: turning **one summed capture** into one side of a before→after
comparison, and holding the one number that says how big a difference has to
be before the round is allowed to call it a change.

The defect this closes is stated in the issue as a missing measurement, not a
missing verdict. The 2026-08-10 jts3 round retained CHECK/MEASURE, the lateral
candidate-selection walk, VERIFY, and five post-apply cloud positions — and
still could not say whether the speaker got better, because none of that
evidence was *the same summed acoustic question, at the same program and level,
before and after the graph change*. So Phase 3c adds exactly one capture
(``PHASE_ENTRY_BASELINE``, at the measurement mark, immediately before apply,
replaying the VERIFY program) and this module is what makes that capture and
the post-apply VERIFY comparable to each other.

**What this module does, and the one decision it makes.** It reduces a
:class:`~jasper.audio_measurement.program_analysis.ProgramAnalysis` to a
:class:`~.verification.MeasurementComparand` — the shape
:func:`~.verification.evaluate_benefit` grades — and, when it holds both sides,
it **unions their exclusion masks**. That union is the one judgement call here,
and :func:`~.verification.evaluate_benefit` names it as the caller's to make:
equal masks mean equal graded bins by construction, so the residual cannot fall
merely because the honesty screen grew. Union rather than intersection because
a bin either capture could not trust is a bin neither comparison should be
graded on.

Everything else is deferred:

* **comparability** is the evaluator's, and it is answered by ``program_id``
  equality — a SHA-256 over the whole excitation schedule including every
  segment's ``gain_db`` and ``effective_peak_dbfs``
  (:func:`jasper.audio_measurement.program._program_id`). Two captures that
  share it share the program *and* the level, cryptographically. That is why
  the entry baseline must be built by the VERIFY program builder rather than by
  a lookalike: equality of that id is the whole comparability check.
* **the curve reduction** is the shipped owners', in the shipped order
  (:func:`~jasper.audio_measurement.spatial_combine.decimate_curve_to_analysis_grid`
  then :func:`~jasper.audio_measurement.analysis.smooth_fractional_octave` at
  the spec fraction) — the same two steps, same owners,
  ``crossover_v2_flow.spec_report_for_predicted_sum`` already applies to put one
  curve in front of :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`.
* **the verdict** is :mod:`.verification`'s. Nothing here decides improved,
  regressed, or indeterminate.

**What the mask here is, and is not.** It is this capture's own
gate-validity clamp — bins below ``DriverResponse.validity_floor_hz``, which
are an artifact of a truncated gate window rather than a measurement. It is
*not* the spatial cloud's interference screen
(:func:`~jasper.audio_measurement.interference_nulls.identify_interference_nulls`
over a combined group), because a single at-the-mark capture is not a group and
there is nothing to screen across. So a residual computed here is optimistic
against the cloud's absolute spec verdict. That does not weaken the *difference*
this module exists to support: both sides are screened the same way and then
share one union, and the difference of two same-frame residuals is the claim.
The absolute spec answer stays the cloud's, which is exactly the separation
:func:`~.verification.evaluate_spec` and :func:`~.verification.evaluate_benefit`
keep apart.

No ``jasper.web`` import, no filesystem, no clock: the host supplies the graph
fingerprint, the mark, and the timestamp, and the host persists the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import numpy as np

from .contracts import CrossoverV2ContractError, ResponseCurve, _text
from .verification import MeasurementComparand

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Same rule as :mod:`.verification`: ``program_analysis`` is a 5,500-line
    # scipy-backed module, and a reduction helper should not drag it into every
    # import of the package. Only three attributes are read at runtime.
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

__all__ = [
    "BENEFIT_CURVE_MAX_BINS",
    "ENTRY_BASELINE_KIND",
    "MEASURED_BENEFIT_MARGIN_DB",
    "EntryBaseline",
    "MeasuredResponse",
    "benefit_comparands",
    "measured_response_from_analysis",
]


# --------------------------------------------------------------------------
# the margin
# --------------------------------------------------------------------------

#: How much flatter the speaker must measure before the round may say so.
#:
#: **Provisionally equal to
#: :func:`~jasper.active_speaker.attempts_loop.material_improvement_db`
#: (0.5 dB), and deliberately NOT that constant.** They answer different
#: questions and only happen to agree today:
#:
#: * ``material_improvement_db`` is model-vs-hardware error — "the gap between
#:   what the correction model predicts and what the hardware realizes on
#:   JTS3", in its own words. It bounds a *prediction*.
#: * this bounds two *measurements* of the same speaker taken minutes apart
#:   through the same program, mic, and analyzer. That is capture
#:   repeatability, and it is a strictly different quantity — it could be much
#:   smaller (same graph, same room, same mic, one program apart) or much
#:   larger (a mic nudged between the two captures).
#:
#: Inheriting the model number silently would have been the cheaper edit and
#: the dishonest one: the margin decides whether a measured regression is
#: "clear" enough to pull a graph off a household's speaker, and a threshold
#: whose justification is about something else is a threshold nobody can
#: defend when it fires.
#:
#: **Nothing in this repository measures the quantity this constant wants.**
#: The nearest thing is
#: :class:`~jasper.active_speaker.attempts_loop.FloorStats` — a real
#: repeatability floor with a ``median_db``/``p95_db`` and a basis — but it
#: grades ``max_db_notch_excluded`` (a measured-vs-predicted deviation), not a
#: pooled spec residual, and :func:`~jasper.active_speaker.attempts_loop.decide_next`
#: refuses a comparison whose metric does not match its floor's for exactly
#: that reason.
#:
#: **TODO(#2291, Definition-of-Done hardware run):** capture the same at-the-mark
#: VERIFY program twice in a row on jts3 against an unchanged graph, difference
#: the two pooled :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual`
#: values, repeat, and set this to that distribution's p95. Until that run
#: exists this value is an assumption wearing its own name, which is the point
#: of the name.
MEASURED_BENEFIT_MARGIN_DB = 0.5

#: Resolution of both sides of the benefit comparison.
#:
#: The entry baseline crosses the durable stage bridge (stage 1 captures it,
#: stage 2 grades against it), so it is bounded like every other curve that
#: does — this is the same 512 the host applies to ``verify_priors.
#: predicted_sum`` and ``verify_priors.commanded_delta``, and for the same
#: reason. It is named here rather than imported because
#: :mod:`jasper.web.correction_crossover_v2` is the wrong direction for this
#: package to import, and because this ceiling governs *both* sides of a
#: comparison rather than one side's persistence budget: the post-apply curve
#: is reduced to it too, so the two land on one grid by construction instead of
#: by luck.
BENEFIT_CURVE_MAX_BINS = 512

#: ``kind`` stamped on the persisted record, matching the package's convention.
ENTRY_BASELINE_KIND = "jts_crossover_v2_entry_baseline"


# --------------------------------------------------------------------------
# one capture, reduced
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredResponse:
    """One summed at-the-mark capture, reduced to what a comparison needs.

    Deliberately not a :class:`~.verification.MeasurementComparand` yet: a
    comparand carries the mask it will be *graded* on, and that mask is not
    known until both sides are in hand (see :func:`benefit_comparands`). This
    type carries the capture's OWN screen, which is the input to that union.
    """

    program_id: str
    reference_mark: str
    curve: ResponseCurve
    excluded: tuple[bool, ...]


def measured_response_from_analysis(
    analysis: "ProgramAnalysis | None", *, reference_mark: str,
) -> MeasuredResponse | None:
    """Reduce one VERIFY-program analysis to a comparison side, or ``None``.

    ``None`` for every honest "there is nothing here to compare": no analysis,
    no summed response, a degenerate curve. The evaluator turns a ``None`` side
    into :attr:`~.contracts.BenefitStatus.INDETERMINATE` with a reason naming
    which side was missing, which is the answer — a comparison with one side is
    not a comparison.

    The two reduction steps and their order are
    ``crossover_v2_flow.spec_report_for_predicted_sum``'s, not this module's:
    block-average onto the analysis grid first (the smoother is an
    O(bins x window) Python loop and a raw grid costs seconds on a Pi), then
    1/3-octave smooth. Doing it in the other order, or with a different
    smoothing fraction, would produce a curve
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` grades
    differently from every other curve in this subsystem.
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
        # crash to propagate into a household decision — the same bounded
        # family, for the same reason, as ``spec_report_for_predicted_sum``.
        return None
    return MeasuredResponse(
        program_id=program_id,
        reference_mark=_text(reference_mark, field_name="reference_mark"),
        curve=curve,
        excluded=_validity_clamp(grid, getattr(summed, "validity_floor_hz", None)),
    )


def _validity_clamp(grid: np.ndarray, validity_floor_hz: Any) -> tuple[bool, ...]:
    """Bins below this capture's own reflection gate, as a per-bin flag.

    The same clamp ``assemble_cloud_group_result`` unions into its spec mask:
    below ``gating.f_valid_floor_hz`` the response is an artifact of a
    truncated gate window, so those bins must not decide a verdict either way.
    An absent or non-finite floor screens nothing — "no evidence of a floor" is
    not "the floor is at zero", and over-screening would shrink the graded
    denominator this comparison depends on.
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

    Persisted by the host between stage 1 (which captures it, immediately
    before the household applies) and stage 2 (which grades against it), so it
    is JSON-shaped by construction. The graph fingerprint travels with it
    because the receipt's whole job is to let a *later* round bind the
    currently-active profile as its own entry graph: a baseline curve with no
    record of which graph produced it cannot do that.
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

    def as_measurement(self) -> MeasuredResponse:
        return MeasuredResponse(
            program_id=self.program_id,
            reference_mark=self.reference_mark,
            curve=self.curve,
            excluded=self.excluded,
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

        ``None`` rather than a raise, and rather than a partially-trusted
        record: a state file from a build before this key shipped, a truncated
        write, and a hand-edited file all mean the same thing to the round —
        there is no comparable baseline — and that already has an honest
        verdict (:data:`~.verification.BENEFIT_BASELINE_UNAVAILABLE`). Length
        agreement between the two curve arrays and the mask is checked here
        because :class:`~.verification.MeasurementComparand` would otherwise
        raise on it inside a verdict computation.
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


# --------------------------------------------------------------------------
# the one assembly decision: a shared mask
# --------------------------------------------------------------------------


def benefit_comparands(
    *,
    baseline: MeasuredResponse | None,
    post: MeasuredResponse | None,
) -> tuple[MeasurementComparand | None, MeasurementComparand | None]:
    """The two sides :func:`~.verification.evaluate_benefit` grades.

    The union of the two per-capture screens, applied to both sides — the
    assembly decision :func:`~.verification.evaluate_benefit` explicitly leaves
    to its caller, taken here once so no other surface may take it differently.
    A bin either capture could not trust is dropped from both, which keeps the
    denominators identical and the residual difference honest.

    Comparability is NOT decided here. A program, mark, or grid disagreement is
    passed straight through to the evaluator, which names which one broke; a
    caller that silently resampled one side onto the other's grid would be
    manufacturing the comparability the round is supposed to be checking. So
    when the grids differ the two per-capture masks ride unchanged and the
    evaluator returns
    :data:`~.verification.BENEFIT_GRID_MISMATCH`.
    """

    if baseline is None or post is None:
        return (
            None if baseline is None else _comparand(baseline, baseline.excluded),
            None if post is None else _comparand(post, post.excluded),
        )
    if baseline.curve.hz != post.curve.hz:
        return _comparand(baseline, baseline.excluded), _comparand(
            post, post.excluded
        )
    shared = tuple(
        bool(a or b) for a, b in zip(baseline.excluded, post.excluded, strict=True)
    )
    return _comparand(baseline, shared), _comparand(post, shared)


def _comparand(
    measured: MeasuredResponse, mask: Iterable[Any]
) -> MeasurementComparand:
    return MeasurementComparand(
        program_id=measured.program_id,
        reference_mark=measured.reference_mark,
        curve=measured.curve,
        exclusion_mask=mask,
    )
