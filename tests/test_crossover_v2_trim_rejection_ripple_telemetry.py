# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The trim rejection's ripple pair, made commensurable (#2541).

**The defect this module pins.** ``decide_trim`` branches on drift in dB of
gain; ripple is telemetry, and the comment at the rejection site says the
ripple fields exist so live evidence can tell "legitimate flatter optimum
rejected" from "garbage correctly caught" before anyone widens the guard.
The event could not do that: it sat ``resolved_ripple_db`` (LINEARIZED
branches, REJECTED trim) beside ``raw_predicted_ripple_db`` (RAW branches,
MEASURE trim). Two variables moved at once, so the gap read as flatness thrown
away when most of it was the linearization itself — which ships under this
outcome either way. ``anchored_ripple_db`` is the missing third number: the
same linearized branches over the same band at the trim that actually SHIPS,
so the first two differ in exactly one variable.

**The fixture is synthetic and hardware-agnostic.** It is
``tests/crossover_v2_fixtures.py``'s analytic branch pair (a tilt, an in-band
dip, an overlap-band bump around a 1600 Hz corner) — no banked jts3 numbers,
and no assertion here hardcodes a ripple value. Every expected number is
RE-DERIVED from the curves the planner actually handed the scan, which is what
makes these assertions sensitive to the wiring rather than to the fixture.

**Why the scan is stubbed.** The guard only fires past
``LINEARIZATION_TRIM_SANITY_MARGIN_DB``, and this fixture's real ripple
optimum sits comfortably inside it (deliberately — see ``_fixture_branch_db``,
which added the tilt so a genuine interior optimum exists). So the scan is
replaced by one that returns a trim a known distance BELOW the anchor, in the
incident's own direction, and reports the honest ripple at that trim through
the production helper. The scan's own use of that helper is pinned separately
and unstubbed, so nothing about "both numbers share one owner" rests on the
stub.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2.contracts import (
    CandidateAcousticContext,
    TrimStrategy,
)
from jasper.audio_measurement import program_analysis as pa
from jasper.audio_measurement.program_analysis import ripple_at_trim
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _candidate_sections,
    _conductor,
    _eligible_measure_analysis,
    _run_phase,
)

REJECTION_EVENT = "correction.crossover_v2_linearization_trim_rejected"

#: How far below the anchor the stubbed scan lands. Past the 6.0 dB margin so
#: the guard fires, and DOWNWARD because that is the degenerate direction the
#: margin exists to catch — the 2026-08-10 incident's scan drifted down too.
FORCED_SCAN_DRIFT_DB = 8.0

#: The candidate's raw pre-fit ``predicted_ripple_db``, overridden to a value
#: no linearized ripple on this fixture can coincide with. A field that logged
#: the raw number instead of the anchored one must be unmistakable, not merely
#: improbable.
RAW_PREDICTED_RIPPLE_SENTINEL_DB = 42.5


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


@dataclass
class ScanCall:
    """What the planner handed the ripple scan — the frame both reads share.

    The arrays are stored as handed, not copied or coerced, so a test can ask
    whether the anchored read got the SAME objects rather than merely equal
    ones.
    """

    freqs: Any = None
    w_lin: Any = None
    t_lin: Any = None
    kwargs: dict = field(default_factory=dict)

    def ripple_at(self, trim_t_db: float) -> float:
        """This capture's own ripple at one tweeter trim.

        Everything except ``trim_t_db`` comes off the call the planner made,
        so a test comparing two of these is holding every other variable at
        whatever production chose rather than at a value the test picked.
        """
        return ripple_at_trim(
            self.freqs,
            self.w_lin,
            self.t_lin,
            lo_hz=self.kwargs["lo_hz"],
            hi_hz=self.kwargs["hi_hz"],
            trim_w_db=self.kwargs["trim_w_db"],
            trim_t_db=float(trim_t_db),
            sign=self.kwargs["sign"],
        )


def _walked_to_measure():
    """A conductor past CHECK, with an eligible synthetic MEASURE analysis."""
    fakes = FakeSeams()
    fakes.measure = lambda program: _eligible_measure_analysis(program)
    conductor = _conductor(fakes)
    _run_phase(conductor, 1, 1)
    analysis = _eligible_measure_analysis(
        conductor.program_for_phase(flow.PHASE_MEASURE)
    )
    # A distinctive raw ripple, so "did this field come from the RAW branches"
    # is answerable by looking at the number.
    analysis = replace(
        analysis,
        candidate=replace(
            analysis.candidate,
            predicted_ripple_db=RAW_PREDICTED_RIPPLE_SENTINEL_DB,
        ),
    )
    return conductor, analysis


def _planner_request(conductor, analysis) -> iv.LinearizationRequest:
    """The planner's inputs, built the way the conductor builds them."""
    program = conductor.program_for_phase(flow.PHASE_MEASURE)
    seg_w, seg_t = program.segment("sweep_w"), program.segment("sweep_t")
    return iv.request_from_analysis(
        analysis,
        analysis.candidate,
        context=CandidateAcousticContext.from_sections(
            _candidate_sections(conductor, conductor._fc_hz)
        ),
        roles=(conductor._woofer.role, conductor._tweeter.role),
        excited_band_hz={
            conductor._woofer.role: (seg_w.f1_hz, seg_w.f2_hz),
            conductor._tweeter.role: (seg_t.f1_hz, seg_t.f2_hz),
        },
        driver_class_by_role=conductor._driver_class_by_role,
        post_apply_verifies=conductor.post_apply_verifies,
        cloud_phase_planned=False,
        cloud=None,
    )


def _install_drifting_scan(monkeypatch) -> ScanCall:
    """Force a beyond-margin scan, and capture the frame it was handed.

    The returned ripple is the TRUE ripple at the trim the stub returns —
    computed through the same production helper the real scan evaluates every
    candidate with — so ``resolved_ripple_db`` stays an honest number and the
    only thing the stub fabricates is WHERE the optimum landed.
    """
    call = ScanCall()

    def scan(freqs, w_lin, t_lin, fc_hz, **kwargs):
        call.freqs = freqs
        call.w_lin = w_lin
        call.t_lin = t_lin
        call.kwargs = dict(kwargs)
        resolved_t = float(kwargs["seed_trim_db"]) - FORCED_SCAN_DRIFT_DB
        return resolved_t, call.ripple_at(resolved_t), float(kwargs["seed_trim_db"])

    monkeypatch.setattr(iv, "solve_ripple_optimal_trim", scan)
    return call


def _rejection_fields(plan) -> dict:
    records = [r for r in plan.journal if r.event == REJECTION_EVENT]
    assert len(records) == 1, [r.event for r in plan.journal]
    return dict(records[0].fields)


def _rejected_plan(monkeypatch):
    """One planner run whose guard fired, plus the scan's captured frame."""
    conductor, analysis = _walked_to_measure()
    call = _install_drifting_scan(monkeypatch)
    plan = iv.plan_linearization(_planner_request(conductor, analysis))
    assert plan.outcome == "trim_rejected"
    assert plan.trim.strategy is TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
    return plan, call


# --------------------------------------------------------------------------- #
# the field itself
# --------------------------------------------------------------------------- #


def test_the_rejection_logs_the_ripple_at_the_trim_that_actually_ships(monkeypatch):
    """``anchored_ripple_db`` is the linearized pair's ripple at the ANCHOR.

    Not at the rejected trim, and not the raw pre-fit number — the two values
    already in the event, and the two a mis-wiring would most plausibly log.
    The expectation is re-derived from the branches the planner handed the
    scan, so this fails on any change of curve family, band, statistic, or
    trim.
    """
    plan, call = _rejected_plan(monkeypatch)
    fields = _rejection_fields(plan)
    anchored_t = float(plan.trim.anchored_db["tweeter"])
    resolved_t = float(plan.trim.resolved_db["tweeter"])

    # The anchor is what shipped, so this field is the flatness the household
    # actually got.
    assert plan.role_attenuations_db["tweeter"] == pytest.approx(anchored_t)

    assert fields["anchored_ripple_db"] == pytest.approx(
        round(call.ripple_at(anchored_t), 3)
    )
    # …and the three numbers are pairwise distinct on this fixture, so none of
    # the assertions above could pass by coincidence.
    assert fields["resolved_ripple_db"] == pytest.approx(
        round(call.ripple_at(resolved_t), 3)
    )
    assert fields["raw_predicted_ripple_db"] == pytest.approx(
        RAW_PREDICTED_RIPPLE_SENTINEL_DB
    )
    trio = (
        fields["anchored_ripple_db"],
        fields["resolved_ripple_db"],
        fields["raw_predicted_ripple_db"],
    )
    assert len(set(trio)) == 3, trio

    # The question the pair now answers, asserted as the reading it enables:
    # on this fixture the rejected optimum is WORSE, so the guard threw away
    # nothing — "garbage correctly caught", legible from the event alone.
    assert fields["resolved_ripple_db"] > fields["anchored_ripple_db"]


def test_the_two_linearized_ripples_differ_in_exactly_one_variable(monkeypatch):
    """Same helper, same curves, same band, same sign, same woofer trim.

    This is the whole point of the field: a pair that moves one variable is
    evidence about the guard, and a pair that moves two is not. Asserted on
    the CALL rather than on the numbers, so a future edit that reaches the
    right value through a second computation still fails.
    """
    conductor, analysis = _walked_to_measure()
    call = _install_drifting_scan(monkeypatch)

    anchored_calls: list[dict] = []
    real_helper = iv.ripple_at_trim

    def spy(freqs, w_tf, t_tf, **kwargs):
        anchored_calls.append({"args": (freqs, w_tf, t_tf), **kwargs})
        return real_helper(freqs, w_tf, t_tf, **kwargs)

    monkeypatch.setattr(iv, "ripple_at_trim", spy)
    plan = iv.plan_linearization(_planner_request(conductor, analysis))
    fields = _rejection_fields(plan)

    assert len(anchored_calls) == 1, anchored_calls
    anchored_call = anchored_calls[0]
    freqs, w_tf, t_tf = anchored_call["args"]

    # Identity, not equality: the anchored read is on the very arrays the scan
    # was handed — the LINEARIZED branches — never a re-derived pair.
    assert freqs is call.freqs
    assert w_tf is call.w_lin
    assert t_tf is call.t_lin

    for key in ("lo_hz", "hi_hz", "trim_w_db", "sign"):
        assert anchored_call[key] == call.kwargs[key], key

    # …and the one thing that does differ.
    assert anchored_call["trim_t_db"] == pytest.approx(
        float(plan.trim.anchored_db["tweeter"])
    )
    assert anchored_call["trim_t_db"] != pytest.approx(
        float(plan.trim.resolved_db["tweeter"])
    )
    assert fields["anchored_ripple_db"] == pytest.approx(
        round(real_helper(freqs, w_tf, t_tf, **{
            k: v for k, v in anchored_call.items() if k != "args"
        }), 3)
    )


def test_the_scans_reported_ripple_is_that_same_helper_at_its_own_trim():
    """The resolved half's owner, pinned unstubbed.

    ``resolved_ripple_db`` is whatever ``solve_ripple_optimal_trim`` returns,
    so "both fields come from one helper" is only true while the scan itself
    evaluates through :func:`ripple_at_trim`. Run for real on a synthetic pair
    and checked two ways: the helper is called during the scan, and the
    returned ripple reproduces exactly from it at the returned trim.
    """
    freqs = np.linspace(200.0, 6400.0, 4096)
    fc_hz = 1600.0
    # A minimum-effort straddling pair: complementary first-order shelves plus
    # a little ripple, enough that the scan has something to optimize.
    w_tf = 1.0 / (1.0 + 1j * freqs / fc_hz)
    t_tf = (1j * freqs / fc_hz) / (1.0 + 1j * freqs / fc_hz)

    seen: list[float] = []
    real_helper = pa.ripple_at_trim

    def spy(*args, **kwargs):
        value = real_helper(*args, **kwargs)
        seen.append(value)
        return value

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pa.response, "ripple_at_trim", spy)
        trim_t, ripple_db, _seed = pa.solve_ripple_optimal_trim(
            freqs, w_tf, t_tf, fc_hz,
            lo_hz=fc_hz / 2.0, hi_hz=fc_hz * 2.0,
            seed_trim_db=-3.0, trim_w_db=0.0, sign=+1,
        )

    assert seen, "the scan did not evaluate through ripple_at_trim"
    assert ripple_db == pytest.approx(
        real_helper(
            freqs, w_tf, t_tf,
            lo_hz=fc_hz / 2.0, hi_hz=fc_hz * 2.0,
            trim_w_db=0.0, trim_t_db=trim_t, sign=+1,
        )
    )


# --------------------------------------------------------------------------- #
# and it decides nothing
# --------------------------------------------------------------------------- #


def test_the_anchored_ripple_is_telemetry_and_moves_no_decision(monkeypatch):
    """A wildly different anchored ripple changes the logged number and
    NOTHING else.

    The guard branches on drift in dB of gain; ripple has never been an input
    and this field does not make it one. Run twice — once honestly, once with
    the helper forced to a value no real capture would produce — and compare
    the committed pair, the strategy, the drift and the margin.
    """
    honest_plan, _call = _rejected_plan(monkeypatch)
    honest_fields = _rejection_fields(honest_plan)

    conductor, analysis = _walked_to_measure()
    _install_drifting_scan(monkeypatch)
    monkeypatch.setattr(iv, "ripple_at_trim", lambda *a, **k: 999.0)
    forced_plan = iv.plan_linearization(_planner_request(conductor, analysis))
    forced_fields = _rejection_fields(forced_plan)

    assert forced_fields["anchored_ripple_db"] == pytest.approx(999.0)
    assert honest_fields["anchored_ripple_db"] != pytest.approx(999.0)

    assert dict(forced_plan.role_attenuations_db) == pytest.approx(
        dict(honest_plan.role_attenuations_db)
    )
    assert forced_plan.trim.strategy is honest_plan.trim.strategy
    assert forced_plan.trim.anchor_drift_db == pytest.approx(
        honest_plan.trim.anchor_drift_db
    )
    assert forced_plan.trim.sanity_margin_db == pytest.approx(
        honest_plan.trim.sanity_margin_db
    )
    assert forced_plan.outcome == honest_plan.outcome
    # The margin is derived (2 × the realized-level tolerance) and #2313
    # measured what moving it costs; this PR does not touch it.
    assert honest_plan.trim.sanity_margin_db == pytest.approx(
        iv.LINEARIZATION_TRIM_SANITY_MARGIN_DB
    )
