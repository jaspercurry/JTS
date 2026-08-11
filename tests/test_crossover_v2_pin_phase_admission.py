# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #2291 Phase 0 — preserved-behavior pin for one phase-admission rule.

#2291 lists "phase admission" among the strong bones the strangler migration
must not change, and its acceptance criteria say existing "phase-admission …
behavior remains pinned unless a separately located defect requires a
deliberate change." This file pins the one admission rule that is written at
five sites and asserted at two.

**The rule.** A capture whose analyzer reports ``linearity_ok is False`` is
refused as ``agc_behavioral_fail`` — the phone's own recording chain moved its
gain mid-sweep, so the curve does not describe the speaker. Four phases carry
it as the identical two-line branch::

    if analysis.linearity_ok is False:
        return PhaseVerdict(False, REASON_AGC_BEHAVIORAL_FAIL)

CHECK carries a fifth, DIFFERENT version — it first asks whether the room's
ambient floor explains the non-linearity and blames ``noisy_room_linearity``
instead when it does — so CHECK is deliberately out of this file's class.

**Why it needs a pin.** Of the four plain sites, only the LATERAL one is
asserted today (``test_a_pose_runs_measures_own_capture_integrity_screens`` in
``tests/test_crossover_v2_lateral_evidence.py``). MEASURE, CLOUD_MEASURE, and
VERIFY have no test that fires this branch and reads its code back. The
nearest existing tests set ``linearity=False`` only *alongside*
``pilot_snr_ok=False``, to prove the pilot branch wins the ordering race
(``test_measure_low_pilot_snr_wins_over_the_linearity_branch``) — they assert
the OTHER branch's code, so they pass whether or not the linearity branch
exists. Tests in ``tests/test_crossover_envelope_v2.py`` that mention
``agc_behavioral_fail`` inject a pre-decided ``failure={"code": ...}`` and
exercise the copy layer, not the predicate.

That is the half-guarded shape: two of five sites pinned reads, from a
distance, like the rule is covered. A migration that reimplemented these
verdicts could drop or mis-code the branch at three of them and take a green
suite with it.

The harness is the conductor suite's own, imported rather than rebuilt — the
same convention ``tests/test_crossover_v2_lateral_evidence.py`` states: two
copies of a conductor factory is two definitions of a session.
"""

from __future__ import annotations

import inspect

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2_flow import (
    PHASE_CLOUD_MEASURE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
    REASON_AGC_BEHAVIORAL_FAIL,
    build_v2_cloud_index_phase_map,
)

from tests.test_crossover_v2_conductor import (
    CLOUD_MEASURE_INDEXES,
    FakeSeams,
    _cloud_conductor,
    _conductor,
    _measure_analysis,
    _run_phase,
    _verify_analysis,
)

# Every phase whose admission predicate carries the PLAIN rule. CHECK is
# excluded on purpose (it has the room-vs-microphone split); the four below
# are the identical branch.
PLAIN_LINEARITY_PHASES = (
    PHASE_MEASURE,
    PHASE_LATERAL,
    PHASE_CLOUD_MEASURE,
    PHASE_VERIFY,
)

# The four plain sites plus CHECK's split version.
LINEARITY_SITE_COUNT = len(PLAIN_LINEARITY_PHASES) + 1


def _refuse_at_measure() -> dict:
    """Drive MEASURE with a non-linear capture and return its relay verdict."""

    fakes = FakeSeams()
    conductor = _conductor(fakes)
    _run_phase(conductor, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, linearity=False)
    return _run_phase(conductor, 2, 2)


def _refuse_at_lateral() -> dict:
    """The POSITIVE CONTROL — the one site an existing test already pins.

    Its row proves this file's harness can observe the rule at all, so a
    silent all-rows-pass caused by a broken fixture (an analysis whose
    ``linearity_ok`` never reaches the predicate, say) shows up as every row
    passing including one that must fail under mutation.
    """

    fakes = FakeSeams()
    conductor = _conductor(
        fakes,
        index_phase_map=build_v2_cloud_index_phase_map(
            tier="full", include_cloud_measure=False, include_lateral=True,
        ),
    )
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 1)
    fakes.measure = lambda program: _measure_analysis(program, linearity=False)
    return _run_phase(conductor, 3, 1)


def _refuse_at_cloud_measure() -> dict:
    """Drive one prompted pre-apply cloud position with a non-linear capture.

    A cloud position plays the summed VERIFY program, so ``FakeSeams`` routes
    it through the ``verify`` analysis factory — see ``_program_for_phase``.
    """

    fakes = FakeSeams()
    conductor = _cloud_conductor(fakes)
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 2)
    fakes.verify = lambda program: _verify_analysis(program, linearity=False)
    return _run_phase(conductor, CLOUD_MEASURE_INDEXES[0], 3)


def _refuse_at_verify() -> dict:
    """Drive the post-apply VERIFY anchor with a non-linear capture."""

    fakes = FakeSeams()
    conductor = _conductor(fakes)
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 2)
    conductor.note_apply_complete()
    fakes.verify = lambda program: _verify_analysis(program, linearity=False)
    return _run_phase(conductor, 3, 3)


DRIVERS = {
    PHASE_MEASURE: _refuse_at_measure,
    PHASE_LATERAL: _refuse_at_lateral,
    PHASE_CLOUD_MEASURE: _refuse_at_cloud_measure,
    PHASE_VERIFY: _refuse_at_verify,
}


@pytest.mark.parametrize("phase", PLAIN_LINEARITY_PHASES)
def test_a_non_linear_capture_is_refused_as_agc_behavioral_fail(phase: str):
    """One row per site that carries the plain rule.

    Parameterized rather than written as one long test so a migration that
    keeps the branch for MEASURE and drops it for CLOUD_MEASURE fails on the
    row that names the phase, instead of on a line whose failure message says
    nothing about which phase lost the gate.
    """

    verdict = DRIVERS[phase]()

    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_AGC_BEHAVIORAL_FAIL


def test_every_linearity_admission_site_is_covered_by_a_row_above():
    """The adjacency tripwire: a SIXTH site must be classified, not ignored.

    The gap this file closes was not one missing test — it was the same rule
    written at five places and asserted at two, which reads as covered from
    any one of them. Counting the sites in the source keeps that from
    recurring: a new verdict function that copies the branch fails here until
    its author either adds a row to ``PLAIN_LINEARITY_PHASES`` (with a driver)
    or records it as another deliberate variant like CHECK's.

    Counted against the module source rather than a list of function names
    because the branch is what varies — a site could be renamed, split, or
    moved between methods and still be the same unasserted rule.
    """

    source = inspect.getsource(flow)
    sites = source.count("if analysis.linearity_ok is False:")

    assert sites == LINEARITY_SITE_COUNT, (
        f"crossover_v2_flow.py has {sites} linearity admission sites but "
        f"{LINEARITY_SITE_COUNT} are classified: "
        f"{len(PLAIN_LINEARITY_PHASES)} plain ({', '.join(PLAIN_LINEARITY_PHASES)}) "
        "plus CHECK's room-vs-microphone variant. Classify the new one and give "
        "it a row in PLAIN_LINEARITY_PHASES, or document why it is a variant."
    )


def test_checks_own_linearity_rule_is_deliberately_not_the_plain_one():
    """Why CHECK is excluded above, stated rather than assumed.

    ``_check_verdict`` asks whether the room's ambient floor already explains
    the non-linearity and answers ``noisy_room_linearity`` when it does. That
    split is what makes CHECK a variant rather than a fifth plain site, and it
    is separately pinned by
    ``test_check_linearity_fail_blames_the_room_when_ambient_is_elevated``.
    Pinned here as a bare vocabulary fact so the exclusion in
    ``PLAIN_LINEARITY_PHASES`` cannot quietly become wrong.
    """

    assert flow.REASON_NOISY_ROOM_LINEARITY != REASON_AGC_BEHAVIORAL_FAIL
    assert flow.REASON_NOISY_ROOM_LINEARITY in flow.REASON_REGISTRY
