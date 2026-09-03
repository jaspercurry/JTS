# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #2291 Phase 0 — preserved-behavior pin for one phase-admission rule.

#2291 lists "phase admission" among the strong bones the strangler migration
must not change, and its acceptance criteria say existing "phase-admission …
behavior remains pinned unless a separately located defect requires a
deliberate change." This file pins the one admission rule that is written at
six sites and was asserted at two.

**The rule.** A capture whose analyzer reports ``linearity_ok is False`` is
refused as ``agc_behavioral_fail`` — the phone's own recording chain moved its
gain mid-sweep, so the curve does not describe the speaker. FIVE phases carry
it as the same two-line branch::

    if <screens>.linearity_ok is False:
        return <refusal>          # agc_behavioral_fail, however it is spelled

Since #2291 Phase 5a-iv those five live in **two modules** and take **two
spellings**: MEASURE and VERIFY still read it off the analysis in
``crossover_v2_flow``; CLOUD_MEASURE, LATERAL and ENTRY_BASELINE read it off a
stated ``CaptureScreens`` in ``crossover_v2.spatial``, and return a refusal
KIND the flow maps to the code. Same rule, same household sentence — see
:data:`LINEARITY_SITE_SPELLINGS`, which is what keeps the tripwire below
watching all of them.

CHECK carries a sixth, DIFFERENT version — it first asks whether the room's
ambient floor explains the non-linearity and blames ``noisy_room_linearity``
instead when it does — so CHECK is deliberately out of this file's class.

**Why it needs a pin.** Of the plain sites, only the LATERAL one was
asserted when this file was written
(``test_a_pose_runs_measures_own_capture_integrity_screens`` in
``tests/test_crossover_v2_lateral_evidence.py``). MEASURE, CLOUD_MEASURE, and
VERIFY had no test that fires this branch and reads its code back — every row
below is what closed that. The
nearest existing tests set ``linearity=False`` only *alongside*
``pilot_snr_ok=False``, to prove the pilot branch wins the ordering race
(``test_measure_low_pilot_snr_wins_over_the_linearity_branch``) — they assert
the OTHER branch's code, so they pass whether or not the linearity branch
exists. Tests in ``tests/test_crossover_envelope_v2.py`` that mention
``agc_behavioral_fail`` inject a pre-decided ``failure={"code": ...}`` and
exercise the copy layer, not the predicate.

That is the half-guarded shape: two of six sites pinned reads, from a
distance, like the rule is covered. A migration that reimplemented these
verdicts could drop or mis-code the branch at the others and take a green
suite with it.

**And the shape recurred here, in the tripwire itself.** Phase 5a-iv moved
three of the five plain sites into another module; the tripwire counted one
file, so it saw 3 against a declared 6 and failed — correctly, but only because
its declared total is a literal. Had it counted a *set* rather than a total it
would have gone quietly blind. That is why the counter is now a per-module
table with its own shape control below.

The harness is the conductor suite's own, imported rather than rebuilt — the
same convention ``tests/test_crossover_v2_lateral_evidence.py`` states: two
copies of a conductor factory is two definitions of a session.
"""

from __future__ import annotations

import inspect

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_AGC_BEHAVIORAL_FAIL
from jasper.active_speaker.crossover_v2_flow import build_v2_cloud_index_phase_map

from tests.crossover_v2_fixtures import (
    CLOUD_MEASURE_INDEXES,
    FakeSeams,
    _cloud_conductor,
    _conductor,
    _measure_analysis,
    _run_phase,
    _verify_analysis,
)

# Every phase whose admission predicate carries the PLAIN rule. CHECK is
# excluded on purpose (it has the room-vs-microphone split); the five below
# are the identical branch.
PLAIN_LINEARITY_PHASES = (
    PHASE_MEASURE,
    PHASE_LATERAL,
    PHASE_CLOUD_MEASURE,
    PHASE_VERIFY,
    # #2291's entry baseline. It copied the branch, which is exactly the
    # adjacency this file's tripwire exists to catch — so it gets a ROW and a
    # driver, not a bumped count.
    PHASE_ENTRY_BASELINE,
)

# The five plain sites plus CHECK's split version.
LINEARITY_SITE_COUNT = len(PLAIN_LINEARITY_PHASES) + 1


def _refuse_at_measure() -> dict:
    """Drive MEASURE with a non-linear capture and return its capture verdict."""

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
    it through the ``verify`` analysis factory — see ``program_for_phase``.
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


def _refuse_at_entry_baseline() -> dict:
    """Drive #2291's pre-apply capture with a non-linear one.

    Routed through the ``verify`` analysis factory for ``_refuse_at_cloud_measure``'s
    reason: the entry baseline replays the summed VERIFY program, so
    ``program.phase`` is ``"verify"`` there too.
    """

    fakes = FakeSeams()
    conductor = _conductor(
        fakes,
        index_phase_map=build_v2_cloud_index_phase_map(
            tier="full", include_cloud_measure=False, include_lateral=False,
            include_entry_baseline=True,
        ),
    )
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 1)
    fakes.verify = lambda program: _verify_analysis(program, linearity=False)
    return _run_phase(conductor, 3, 1)


DRIVERS = {
    PHASE_MEASURE: _refuse_at_measure,
    PHASE_LATERAL: _refuse_at_lateral,
    PHASE_CLOUD_MEASURE: _refuse_at_cloud_measure,
    PHASE_VERIFY: _refuse_at_verify,
    PHASE_ENTRY_BASELINE: _refuse_at_entry_baseline,
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


#: Where a linearity admission site can live, and the spellings it takes there.
#:
#: **Two modules since #2291 Phase 5a-iv**, and that is the whole reason this is
#: a table rather than one ``count`` call. The cloud, lateral and entry-baseline
#: ladders moved to ``crossover_v2.spatial``; counting only the flow after that
#: returned 3 against a declared 6 — the tripwire had gone BLIND to three of the
#: five plain sites while still reading, from its own green, like it was
#: watching them. A rule that can live in two files needs a guard that looks in
#: both, and the next vertical will move more of them.
#:
#: **Two spellings**, for the same reason: the moved ladders take their screens
#: as a stated :class:`~jasper.active_speaker.crossover_v2.spatial.CaptureScreens`
#: rather than reaching into the analysis, so the branch reads
#: ``screens.linearity_ok`` there. Both are the same rule and both are counted;
#: a site written in a third spelling is exactly the drift this file exists to
#: catch, and it will show up as an undercount here.
LINEARITY_SITE_SPELLINGS = (
    "if analysis.linearity_ok is False:",
    "if screens.linearity_ok is False:",
)


def _linearity_admission_sites() -> dict[str, int]:
    """Every module that can carry the rule → how many sites it carries.

    Counted against module SOURCE rather than a list of function names because
    the branch is what varies — a site can be renamed, split, or moved between
    methods and still be the same unasserted rule. Returned per module so a
    failure names which file drifted rather than only that the total did.
    """
    from jasper.active_speaker.crossover_v2 import capture_dispatch, spatial

    return {
        module.__name__.rsplit(".", 1)[-1]: sum(
            inspect.getsource(module).count(spelling)
            for spelling in LINEARITY_SITE_SPELLINGS
        )
        for module in (flow, spatial, capture_dispatch)
    }


def test_every_linearity_admission_site_is_covered_by_a_row_above():
    """The adjacency tripwire: a SIXTH site must be classified, not ignored.

    The gap this file closes was not one missing test — it was the same rule
    written at five places and asserted at two, which reads as covered from
    any one of them. Counting the sites in the source keeps that from
    recurring: a new verdict function that copies the branch fails here until
    its author either adds a row to ``PLAIN_LINEARITY_PHASES`` (with a driver)
    or records it as another deliberate variant like CHECK's.
    """

    by_module = _linearity_admission_sites()
    sites = sum(by_module.values())

    assert sites == LINEARITY_SITE_COUNT, (
        f"{sites} linearity admission sites ({by_module}) but "
        f"{LINEARITY_SITE_COUNT} are classified: "
        f"{len(PLAIN_LINEARITY_PHASES)} plain ({', '.join(PLAIN_LINEARITY_PHASES)}) "
        "plus CHECK's room-vs-microphone variant. Classify the new one and give "
        "it a row in PLAIN_LINEARITY_PHASES, or document why it is a variant."
    )


def test_the_tripwire_looks_in_every_module_that_carries_the_rule():
    """The control the count above needs, and the one it did not have.

    A total is not evidence that every module was searched: 6 could be six sites
    in one file and none in the other, which is what the guard would report the
    day someone moves the last ladder out and forgets this list.

    **That day arrived** — #2291 Phase 5a-vii moved CHECK's, MEASURE's and
    VERIFY's ladders into :mod:`.capture_dispatch`, and this test is how the
    move was noticed rather than announced.  The flow now carries ZERO sites,
    which is asserted rather than dropped from the search: the whole point of
    the list is that a rule reappearing in the conductor must fail here, and a
    module removed from it can never fail again.

    So: all three searched, the two CARRIERS non-empty, and the conductor
    empty — because "we looked there" is the property that failed in Phase
    5a-iv, not "the sum is right".
    """
    by_module = _linearity_admission_sites()

    assert set(by_module) == {"crossover_v2_flow", "spatial", "capture_dispatch"}
    carriers = {"spatial", "capture_dispatch"}
    assert all(by_module[name] > 0 for name in carriers), by_module
    assert by_module["crossover_v2_flow"] == 0, (
        "the conductor carries no linearity ladder since Phase 5a-vii; a site "
        "here is either a ladder that came back or a new unclassified rule"
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

    assert refusal_copy.REASON_NOISY_ROOM_LINEARITY != REASON_AGC_BEHAVIORAL_FAIL
    assert refusal_copy.REASON_NOISY_ROOM_LINEARITY in flow.REASON_REGISTRY
