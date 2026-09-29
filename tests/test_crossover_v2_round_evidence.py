# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One summed capture reduced: the reduction is the SHIPPED owners'
arithmetic, not a second copy."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import round_evidence
from jasper.active_speaker.crossover_v2.round_evidence import (
    BENEFIT_CURVE_MAX_BINS,
    measured_response_from_analysis,
)

_MARK = "design_axis_mark"


def _grid(n: int = 2048) -> np.ndarray:
    """An rFFT-shaped frequency axis — starting at DC, as a real one does.

    Deliberately not ``linspace(20, …)``: ``DriverResponse.freqs_hz`` comes off
    an rFFT and its first bin is 0 Hz. Starting the fixture at 20 Hz would make
    the sub-1-Hz region untestable, and that is precisely the region a bool
    ``validity_floor_hz`` would silently clamp at (``float(True) == 1.0``).
    """
    return np.linspace(0.0, 24000.0, n)


def _analysis(
    *,
    stimulus_id: str = "prog-a",
    freqs: np.ndarray | None = None,
    magnitude_db: np.ndarray | None = None,
    validity_floor_hz: float | None = None,
    summed: object | None = ...,  # type: ignore[assignment]
):
    """A ProgramAnalysis-shaped double carrying only what the reducer reads.

    A double rather than a real analysis because building one needs a capture,
    a deconvolution, and a program — none of which this module touches. The
    three attributes read at runtime are named in the reducer's own body
    (``stimulus_id``, ``summed_response``, and the summed response's
    ``freqs_hz``/``magnitude_db``/``validity_floor_hz``), so a double that
    carries exactly those is the whole contract.
    """

    if summed is ...:
        hz = _grid() if freqs is None else freqs
        db = (
            np.sin(np.log10(np.maximum(hz, 1.0)) * 5.0)
            if magnitude_db is None
            else magnitude_db
        )
        summed = SimpleNamespace(
            freqs_hz=hz, magnitude_db=db, validity_floor_hz=validity_floor_hz
        )
    return SimpleNamespace(stimulus_id=stimulus_id, summed_response=summed)


# --------------------------------------------------------------------------- #
# 1. the reduction is the shipped owners'
# --------------------------------------------------------------------------- #


def test_the_curve_is_the_shipped_decimate_then_smooth_not_a_second_copy():
    """Asserted against the owners themselves, so a drifted copy cannot pass.

    The expected value is computed by CALLING those owners rather than by baking
    numbers, so the pin follows them if they change and fails if this module
    stops using them.
    """
    from jasper.audio_measurement.analysis import smooth_fractional_octave
    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    hz = _grid()
    db = np.sin(np.log10(np.maximum(hz, 1.0)) * 5.0)

    reduced = measured_response_from_analysis(
        _analysis(freqs=hz, magnitude_db=db), reference_mark=_MARK
    )
    assert reduced is not None

    grid, coarse = decimate_curve_to_analysis_grid(
        hz, db, max_bins=BENEFIT_CURVE_MAX_BINS
    )
    expected = smooth_fractional_octave(grid, coarse, fraction=3)

    assert reduced.curve.hz == tuple(float(f) for f in grid)
    assert reduced.curve.db == pytest.approx(list(expected))
    assert len(reduced.curve.hz) <= BENEFIT_CURVE_MAX_BINS


def test_the_stimulus_id_and_mark_ride_through_unchanged():
    """Comparability's two identity fields are carried, never re-derived.

    ``stimulus_id`` equality is the whole comparability check
    (:class:`~jasper.audio_measurement.program.ExcitationProgram`: a content
    hash over the schedule, so equal ids are a cryptographic guarantee of same
    program AND same level). A reducer that minted its own id would make that guarantee
    meaningless.
    """
    reduced = measured_response_from_analysis(
        _analysis(stimulus_id="prog-xyz"), reference_mark=_MARK
    )

    assert reduced is not None
    assert reduced.stimulus_id == "prog-xyz"
    assert reduced.reference_mark == _MARK


@pytest.mark.parametrize(
    "analysis",
    [
        None,
        _analysis(summed=None),
        _analysis(stimulus_id=""),
        # A non-finite bin: ``ResponseCurve`` refuses it rather than hashing
        # it, so the reduction cannot produce a comparand. "Cannot compare
        # this" is an answer, not a crash to propagate into a household
        # decision.
        _analysis(
            freqs=_grid(64),
            magnitude_db=np.concatenate([np.zeros(63), [np.nan]]),
        ),
    ],
    ids=["no_analysis", "no_summed_response", "no_stimulus_id", "non_finite_bin"],
)
def test_an_unreducible_capture_is_none_never_a_raise(analysis):
    assert measured_response_from_analysis(analysis, reference_mark=_MARK) is None


def test_a_curve_too_narrow_to_grade_still_reduces():
    """The reducer reduces; it does not grade, and it does not pre-judge. A
    two-bin curve reaches no spec band, but that is a grader's answer, not a
    reason for the reducer to withhold the capture."""
    reduced = measured_response_from_analysis(
        _analysis(
            freqs=np.array([100.0, 200.0]), magnitude_db=np.array([0.0, 1.0])
        ),
        reference_mark=_MARK,
    )
    assert reduced is not None


# --------------------------------------------------------------------------- #
# 2. the validity clamp
# --------------------------------------------------------------------------- #


def test_bins_below_the_captures_own_validity_floor_are_screened():
    """The same clamp the cloud pipeline unions into its spec mask.

    Below ``gating.f_valid_floor_hz`` the response is an artifact of a
    truncated gate window, so those bins must not decide a verdict either way.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=500.0), reference_mark=_MARK
    )

    assert reduced is not None
    flagged = [
        hz for hz, excluded in zip(reduced.curve.hz, reduced.excluded) if excluded
    ]
    unflagged = [
        hz for hz, excluded in zip(reduced.curve.hz, reduced.excluded) if not excluded
    ]
    assert flagged, "a 500 Hz floor must screen the bins beneath it"
    assert max(flagged) < 500.0
    assert min(unflagged) >= 500.0


@pytest.mark.parametrize(
    ("floor", "expected"),
    [
        (None, (False, False, False)),
        (float("nan"), (False, False, False)),
        (float("inf"), (False, False, False)),
        ("2.0", (False, False, False)),
        # ``True`` is an ``int``, so a clamp that forgot to reject bools would
        # read it as a 1.0 Hz floor. On the production grid the coarsest bin
        # already sits near 23 Hz, so that mistake is INVISIBLE end-to-end —
        # measured: an end-to-end version of this case survived deleting the
        # bool guard outright. Only a sub-1-Hz axis can see it, which is why
        # this one case reaches the clamp directly.
        (True, (False, False, False)),
        (2.0, (True, True, False)),
    ],
    ids=["absent", "nan", "inf", "string", "bool", "a_real_floor"],
)
def test_the_clamp_screens_only_for_a_finite_numeric_floor(floor, expected):
    """The clamp itself, on an axis fine enough to expose every branch.

    Reached by name because the fact under test has no public surface and the
    production grid cannot express it; the helper is named for the rule, so a
    future public accessor can replace the body without touching this test.
    """
    grid = np.array([0.25, 0.75, 5.0])

    assert round_evidence._validity_clamp(grid, floor) == expected


@pytest.mark.parametrize(
    "floor",
    [None, float("nan"), float("inf"), "500"],
    ids=["absent", "nan", "inf", "string"],
)
def test_a_floor_that_is_not_a_finite_number_screens_nothing(floor):
    """"No evidence of a floor" is not "the floor is at zero".

    Over-screening is not the safe direction here: it shrinks the graded
    denominator the whole before/after comparison depends on, which is the
    exact hazard ``spec_convergence_residual`` warns a loop about. ``True`` is
    listed because it is an ``int`` and would otherwise clamp at 1 Hz.
    """
    reduced = measured_response_from_analysis(
        _analysis(validity_floor_hz=floor), reference_mark=_MARK
    )

    assert reduced is not None
    assert not any(reduced.excluded)
