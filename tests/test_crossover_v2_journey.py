# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 4: the journey state machine, and the conductor over it.

Two halves, matching the extraction:

* **The aggregate on its own** — plan derivation, the two transitions, and the
  derivations that read them. Public surface only; there is no private field to
  reach for, which is the point.
* **The conductor and the host through it** — that the delegation is a
  delegation and not a copy. A shim that returned a stale snapshot would satisfy
  every assertion in the first half and still ship the bug Phase 4 exists to
  remove, so the conductor's phase readers are asserted to MOVE when the journey
  moves.
"""

from __future__ import annotations

import ast
import dataclasses
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2.journey import (
    CAPABILITY_COMMANDED_DELTA,
    CAPABILITY_ENTRY_BASELINE,
    CAPABILITY_FINDINGS,
    CAPABILITY_PREDICTED_SUM,
    CAPABILITY_ROLLBACK,
    GROUP_PHASES,
    PHASE_APPLYING,
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_DONE,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
    STAGE_MEASURE_CAPABILITIES,
    STAGE_VERIFY_CAPABILITIES,
    CommissionJourney,
    JourneyPlan,
    available_stage_priors,
    open_stage,
)

# A stage-1-shaped map: the anchor pair, a two-pose lateral group, a two-
# position cloud group, and the baseline last. Deliberately NOT built by
# ``build_v2_cloud_index_phase_map`` — this suite pins what the journey does
# with a map, and borrowing the flow's builder would make a change there able
# to move these expectations with it.
STAGE1_MAP = {
    1: PHASE_CHECK,
    2: PHASE_MEASURE,
    3: PHASE_LATERAL,
    4: PHASE_LATERAL,
    5: PHASE_CLOUD_MEASURE,
    6: PHASE_CLOUD_MEASURE,
    7: PHASE_ENTRY_BASELINE,
}

#: The three-entry pre-cloud shape, and the one-entry verify re-arm.
THREE_ENTRY_MAP = {1: PHASE_CHECK, 2: PHASE_MEASURE, 3: PHASE_VERIFY}
VERIFY_ONLY_MAP = {1: PHASE_VERIFY}


def _journey(mapping=None, **kwargs) -> CommissionJourney:
    return CommissionJourney(
        JourneyPlan.from_index_map(mapping or THREE_ENTRY_MAP), **kwargs
    )


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------


def test_the_plan_orders_phases_canonically_not_by_map_iteration():
    """The walk is :data:`CAPTURE_PHASES` order, whatever order the map is in."""

    scrambled = dict(reversed(list(STAGE1_MAP.items())))
    plan = JourneyPlan.from_index_map(scrambled)
    assert plan.phases == (
        PHASE_CHECK,
        PHASE_MEASURE,
        PHASE_LATERAL,
        PHASE_CLOUD_MEASURE,
        PHASE_ENTRY_BASELINE,
    )
    # And the entry baseline really is last, which is the whole of its
    # comparability argument — it is the capture immediately before apply.
    assert plan.phases[-1] == PHASE_ENTRY_BASELINE


def test_a_session_walks_only_the_phases_its_map_addresses():
    """The verify-only re-arm must not sit forever pending on a group it never
    runs — the reason the walk is derived from the map rather than assumed."""

    plan = JourneyPlan.from_index_map(VERIFY_ONLY_MAP)
    assert plan.phases == (PHASE_VERIFY,)
    assert plan.group_indexes == {}
    assert plan.group_offsets(PHASE_CLOUD_MEASURE) == ()
    assert plan.is_group(PHASE_CLOUD_MEASURE) is False


def test_group_index_spans_come_from_the_map_and_are_ascending():
    plan = JourneyPlan.from_index_map(STAGE1_MAP)
    assert plan.group_indexes[PHASE_LATERAL] == (3, 4)
    assert plan.group_indexes[PHASE_CLOUD_MEASURE] == (5, 6)
    # A single-capture phase is not a group even though it is in the map.
    assert plan.is_group(PHASE_ENTRY_BASELINE) is False
    assert plan.is_group(PHASE_LATERAL) is True
    assert plan.is_last_index_of_group(PHASE_CLOUD_MEASURE, 6) is True
    assert plan.is_last_index_of_group(PHASE_CLOUD_MEASURE, 5) is False
    # Never "yes" for a phase with no group — an empty span has no last member.
    assert plan.is_last_index_of_group(PHASE_ENTRY_BASELINE, 7) is False


def test_is_group_asks_about_this_session_not_the_vocabulary():
    """``PHASE_LATERAL`` is a group phase in general and not in a session that
    walks no lateral poses; per-index bookkeeping must follow the session."""

    assert PHASE_LATERAL in GROUP_PHASES
    assert JourneyPlan.from_index_map(THREE_ENTRY_MAP).is_group(PHASE_LATERAL) is False


def test_phase_for_index_answers_none_for_an_unplanned_index():
    plan = JourneyPlan.from_index_map(THREE_ENTRY_MAP)
    assert plan.phase_for_index(2) == PHASE_MEASURE
    assert plan.phase_for_index(99) is None


def test_the_plan_is_frozen_and_owns_its_own_copy_of_the_map():
    """A caller's dict cannot reach in and change the walk after the fact."""

    source = dict(THREE_ENTRY_MAP)
    plan = JourneyPlan.from_index_map(source)
    source[4] = PHASE_CLOUD_VERIFY
    assert 4 not in plan.index_phase_map
    assert PHASE_CLOUD_VERIFY not in plan.phases
    with pytest.raises(TypeError):
        plan.index_phase_map[5] = PHASE_CHECK  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.phases = ()  # type: ignore[misc]


# --------------------------------------------------------------------------
# post_apply_verifies — the boost-permission evidence gate
# --------------------------------------------------------------------------


def test_post_apply_verifies_defaults_to_reading_the_walk():
    assert JourneyPlan.from_index_map(THREE_ENTRY_MAP).post_apply_verifies is True
    assert JourneyPlan.from_index_map(STAGE1_MAP).post_apply_verifies is False


def test_a_declaration_overrides_the_walk_in_both_directions():
    """The two-stage measuring session has no VERIFY of its own and IS verified;
    a shape that declares no post-apply positions is not."""

    assert JourneyPlan.from_index_map(
        STAGE1_MAP, post_apply_verifies=True
    ).post_apply_verifies is True
    assert JourneyPlan.from_index_map(
        THREE_ENTRY_MAP, post_apply_verifies=False
    ).post_apply_verifies is False


@pytest.mark.parametrize(
    "target,expected", [(0, False), (1, True), (6, True)]
)
def test_open_stage_reads_the_tiers_post_apply_position_count(target, expected):
    """``>= 1`` is the whole rule, and it lives here rather than in the host."""

    opening = open_stage(
        STAGE_MEASURE_CAPABILITIES,
        index_phase_map=STAGE1_MAP,
        verify_capture_target=target,
    )
    assert opening.plan.post_apply_verifies is expected


def test_open_stage_without_a_target_leaves_the_walk_derived_reading():
    opening = open_stage(
        STAGE_VERIFY_CAPABILITIES, index_phase_map=VERIFY_ONLY_MAP
    )
    assert opening.plan.post_apply_verifies is True


# --------------------------------------------------------------------------
# transitions
# --------------------------------------------------------------------------


def test_a_single_capture_phase_is_accepted_by_one_accept():
    journey = _journey()
    assert journey.phase_status(PHASE_CHECK) == "pending"
    journey.accept(PHASE_CHECK, 1)
    assert journey.phase_status(PHASE_CHECK) == "accepted"
    assert journey.accepted_phases == frozenset({PHASE_CHECK})


def test_a_group_phase_closes_only_when_every_position_is_resolved():
    """Accepting position 1 of 2 must not read as "the cloud is done"."""

    journey = _journey(STAGE1_MAP)
    journey.accept(PHASE_CLOUD_MEASURE, 5)
    assert journey.phase_status(PHASE_CLOUD_MEASURE) == "pending"
    assert PHASE_CLOUD_MEASURE in journey.pending_phases()
    journey.accept(PHASE_CLOUD_MEASURE, 6)
    assert journey.phase_status(PHASE_CLOUD_MEASURE) == "accepted"
    assert PHASE_CLOUD_MEASURE not in journey.pending_phases()


def test_re_accepting_a_position_does_not_close_a_group_early():
    """A geometry retake lands on an index already resolved; the group must
    still wait for the position nobody has walked."""

    journey = _journey(STAGE1_MAP)
    journey.accept(PHASE_CLOUD_MEASURE, 5)
    journey.accept(PHASE_CLOUD_MEASURE, 5)
    assert journey.phase_status(PHASE_CLOUD_MEASURE) == "pending"


def test_pending_phases_follows_the_walk_order_and_shrinks():
    journey = _journey(STAGE1_MAP)
    assert journey.pending_phases() == journey.plan.phases
    journey.accept(PHASE_CHECK, 1)
    journey.accept(PHASE_MEASURE, 2)
    assert journey.pending_phases() == (
        PHASE_LATERAL, PHASE_CLOUD_MEASURE, PHASE_ENTRY_BASELINE
    )


def test_unresolved_in_group_excludes_the_position_being_decided():
    """The "can this group still reach its floor" question: positions in hand
    plus positions not yet walked, never the count so far."""

    journey = _journey(STAGE1_MAP)
    assert journey.unresolved_in_group(PHASE_LATERAL, excluding=3) == (4,)
    journey.accept(PHASE_LATERAL, 4)
    assert journey.unresolved_in_group(PHASE_LATERAL, excluding=3) == ()
    # A phase with no group in this session has nothing unresolved.
    assert journey.unresolved_in_group(PHASE_ENTRY_BASELINE, excluding=7) == ()


def test_mark_applied_is_the_only_way_applied_becomes_true():
    journey = _journey()
    assert journey.applied is False
    journey.mark_applied()
    assert journey.applied is True


def test_a_journey_can_be_constructed_already_part_way_through():
    """Stage 2 is built with CHECK/MEASURE accepted and the apply observed."""

    journey = _journey(
        VERIFY_ONLY_MAP, accepted_phases=(PHASE_CHECK, PHASE_MEASURE), applied=True
    )
    assert journey.accepted_phases == frozenset({PHASE_CHECK, PHASE_MEASURE})
    assert journey.applied is True
    assert journey.current_phase == PHASE_VERIFY


# --------------------------------------------------------------------------
# current_phase, including the APPLYING interlude
# --------------------------------------------------------------------------


def test_current_phase_walks_to_the_first_unaccepted_phase():
    journey = _journey(STAGE1_MAP)
    assert journey.current_phase == PHASE_CHECK
    journey.accept(PHASE_CHECK, 1)
    assert journey.current_phase == PHASE_MEASURE


def test_current_phase_is_done_only_when_the_whole_walk_is_accepted():
    journey = _journey(VERIFY_ONLY_MAP)
    assert journey.current_phase == PHASE_VERIFY
    journey.accept(PHASE_VERIFY, 1)
    assert journey.current_phase == PHASE_DONE


def test_measure_accepted_and_verify_pending_and_unapplied_is_the_interlude():
    """The three-entry shape's machine-paced window between MEASURE-accepted and
    apply-observed. All three conditions are load-bearing, so each is dropped in
    turn below."""

    journey = _journey(THREE_ENTRY_MAP)
    journey.accept(PHASE_CHECK, 1)
    journey.accept(PHASE_MEASURE, 2)
    assert journey.current_phase == PHASE_APPLYING


def test_the_interlude_ends_the_moment_the_apply_is_observed():
    journey = _journey(THREE_ENTRY_MAP)
    journey.accept(PHASE_CHECK, 1)
    journey.accept(PHASE_MEASURE, 2)
    journey.mark_applied()
    assert journey.current_phase == PHASE_VERIFY


def test_the_interlude_needs_measure_accepted_not_merely_verify_pending():
    """CHECK still pending ⇒ CHECK, never "applying" — otherwise a session that
    had measured nothing would report an apply in progress."""

    journey = _journey(THREE_ENTRY_MAP)
    assert journey.current_phase == PHASE_CHECK
    journey.accept(PHASE_MEASURE, 2)
    assert journey.current_phase == PHASE_CHECK


def test_a_verify_only_walk_that_measured_nothing_reports_verify_not_applying():
    """The §5.2 recovery re-verify's shape, and the one place the "MEASURE is
    accepted" conjunct is load-bearing.

    Every other unaccepted-MEASURE case stops the walk at MEASURE before VERIFY
    is ever considered. Here MEASURE is not in the walk at all, so without that
    conjunct a re-verify session that has not yet observed an apply would
    announce an apply in progress and the wizard would render the machine-paced
    hold over a session with nothing to apply.
    """

    journey = _journey(VERIFY_ONLY_MAP)
    assert journey.applied is False
    assert PHASE_MEASURE not in journey.accepted_phases
    assert journey.current_phase == PHASE_VERIFY


def test_a_walk_with_no_verify_never_reports_the_interlude():
    """Stage 1's own shape: everything accepted ⇒ DONE, and the host's review
    interlude takes it from there."""

    journey = _journey(STAGE1_MAP)
    for phase, index in (
        (PHASE_CHECK, 1), (PHASE_MEASURE, 2), (PHASE_LATERAL, 3),
        (PHASE_LATERAL, 4), (PHASE_CLOUD_MEASURE, 5), (PHASE_CLOUD_MEASURE, 6),
    ):
        journey.accept(phase, index)
    assert journey.current_phase == PHASE_ENTRY_BASELINE
    journey.accept(PHASE_ENTRY_BASELINE, 7)
    assert journey.current_phase == PHASE_DONE


def test_accepted_capture_phases_is_canonically_ordered_for_the_snapshot():
    journey = _journey(STAGE1_MAP)
    journey.accept(PHASE_ENTRY_BASELINE, 7)
    journey.accept(PHASE_CHECK, 1)
    journey.accept(PHASE_MEASURE, 2)
    assert journey.accepted_capture_phases() == (
        PHASE_CHECK, PHASE_MEASURE, PHASE_ENTRY_BASELINE
    )


# --------------------------------------------------------------------------
# stage capabilities
# --------------------------------------------------------------------------


def test_the_two_stages_declare_the_capabilities_that_differ():
    assert STAGE_MEASURE_CAPABILITIES.stage == "measure"
    assert STAGE_MEASURE_CAPABILITIES.provides == {CAPABILITY_FINDINGS}
    assert STAGE_MEASURE_CAPABILITIES.requires == frozenset()
    assert STAGE_VERIFY_CAPABILITIES.stage == "verify"
    assert STAGE_VERIFY_CAPABILITIES.provides == {CAPABILITY_ROLLBACK}
    assert STAGE_VERIFY_CAPABILITIES.requires == {
        CAPABILITY_COMMANDED_DELTA,
        CAPABILITY_PREDICTED_SUM,
        CAPABILITY_ENTRY_BASELINE,
    }


def test_the_shortfall_is_requires_minus_available_sorted():
    opening = open_stage(
        STAGE_VERIFY_CAPABILITIES,
        index_phase_map=VERIFY_ONLY_MAP,
        available=(CAPABILITY_PREDICTED_SUM,),
    )
    assert opening.missing == (CAPABILITY_COMMANDED_DELTA, CAPABILITY_ENTRY_BASELINE)
    assert opening.stage == "verify"


def test_a_stage_handed_everything_it_requires_is_missing_nothing():
    opening = open_stage(
        STAGE_VERIFY_CAPABILITIES,
        index_phase_map=VERIFY_ONLY_MAP,
        available=STAGE_VERIFY_CAPABILITIES.requires,
    )
    assert opening.missing == ()


def test_a_stage_requiring_nothing_is_missing_nothing_when_handed_nothing():
    opening = open_stage(STAGE_MEASURE_CAPABILITIES, index_phase_map=STAGE1_MAP)
    assert opening.missing == ()


def test_a_missing_prior_does_not_refuse_the_opening():
    """Observability, not a gate — a stage that refused to open on a prior it
    could still run without would strand the household."""

    opening = open_stage(
        STAGE_VERIFY_CAPABILITIES, index_phase_map=VERIFY_ONLY_MAP
    )
    assert opening.missing == (
        CAPABILITY_COMMANDED_DELTA,
        CAPABILITY_ENTRY_BASELINE,
        CAPABILITY_PREDICTED_SUM,
    )
    assert opening.plan.phases == (PHASE_VERIFY,)


def test_available_stage_priors_names_only_the_facts_that_are_true():
    assert available_stage_priors(
        commanded_delta=True, predicted_sum=False, entry_baseline=True
    ) == (CAPABILITY_COMMANDED_DELTA, CAPABILITY_ENTRY_BASELINE)
    assert available_stage_priors(
        commanded_delta=False, predicted_sum=False, entry_baseline=False
    ) == ()
    assert set(
        available_stage_priors(
            commanded_delta=True, predicted_sum=True, entry_baseline=True
        )
    ) == STAGE_VERIFY_CAPABILITIES.requires


def test_no_capability_is_provided_by_both_stages():
    assert not (
        STAGE_MEASURE_CAPABILITIES.provides & STAGE_VERIFY_CAPABILITIES.provides
    )


# --------------------------------------------------------------------------
# the conductor over the journey — delegation, not a copy
# --------------------------------------------------------------------------


#: The journey names the flow no longer reaches at all: wave 0c pointed every
#: consumer at the owner, and the flow imports only the phases it reads itself.
#: Written out so a name that silently STOPS being re-exported still fails
#: below, and a name listed here that the flow starts importing again fails the
#: absence half.
JOURNEY_NAMES_THE_FLOW_NO_LONGER_READS = frozenset({
    "CAPTURE_PHASES",
    "PHASE_APPLYING",
    "PHASE_CLOSING",
    "PHASE_DONE",
    "PHASE_REVIEW",
})


def test_the_flow_re_exports_every_phase_name_the_journey_owns():
    """The flow's names ARE the journey's objects, so there is one vocabulary
    and a change here cannot leave the flow describing the old one.

    Enumerated from the journey module rather than from a hand-kept list: a
    name added there and not re-exported is exactly the drift this catches, and
    a list maintained here would have to be updated by the same commit that
    caused it.

    The names in :data:`JOURNEY_NAMES_THE_FLOW_NO_LONGER_READS` are checked the
    other way round — the flow must NOT reach them, because their consumers
    import from the journey directly and a flow binding could only be a second
    way to say the same thing.
    """

    from jasper.active_speaker import crossover_v2_flow as flow
    from jasper.active_speaker.crossover_v2 import journey

    phase_names = [n for n in vars(journey) if n.startswith("PHASE_")]
    assert len(phase_names) == 11, phase_names
    for name in [*phase_names, "CAPTURE_PHASES", "GROUP_PHASES"]:
        if name in JOURNEY_NAMES_THE_FLOW_NO_LONGER_READS:
            assert not hasattr(flow, name), (
                f"the flow reaches {name} again — repoint the consumer that "
                "needed it, or drop the name from "
                "JOURNEY_NAMES_THE_FLOW_NO_LONGER_READS"
            )
            continue
        assert hasattr(flow, name), f"{name} is not re-exported by the flow"
        assert getattr(flow, name) is getattr(journey, name), name


def test_the_host_re_exports_the_journeys_own_declarations():
    """The host publishes these names; they must not become a second copy."""

    from jasper.web import correction_crossover_v2 as v2host

    assert v2host.STAGE_MEASURE_CAPABILITIES is STAGE_MEASURE_CAPABILITIES
    assert v2host.STAGE_VERIFY_CAPABILITIES is STAGE_VERIFY_CAPABILITIES
    assert v2host.CAPABILITY_ROLLBACK is CAPABILITY_ROLLBACK
    assert v2host.CAPABILITY_ENTRY_BASELINE is CAPABILITY_ENTRY_BASELINE


def _conductor(**kwargs):
    """The shared conductor fixture, so this suite cannot drift from the corpus
    it is asserting about. Seams are the suite's fakes; this section reads
    phase state only."""
    from tests.crossover_v2_fixtures import FakeSeams
    from tests.crossover_v2_fixtures import _conductor as _build

    return _build(FakeSeams(), **kwargs)


def test_the_conductors_phase_readers_move_when_the_journey_moves():
    """The delegation is live. A shim that cached the plan or the accepted set
    would satisfy the aggregate tests above and still be the bug."""

    conductor = _conductor(index_phase_map=dict(THREE_ENTRY_MAP))
    assert conductor.current_phase == PHASE_CHECK
    assert conductor.phase_status(PHASE_CHECK) == "pending"

    conductor._journey.accept(PHASE_CHECK, 1)
    assert conductor.accepted_phases == frozenset({PHASE_CHECK})
    assert conductor.phase_status(PHASE_CHECK) == "accepted"
    assert conductor.current_phase == PHASE_MEASURE
    assert conductor.pending_phases() == (PHASE_MEASURE, PHASE_VERIFY)

    conductor._journey.accept(PHASE_MEASURE, 2)
    assert conductor.current_phase == PHASE_APPLYING
    conductor.note_apply_complete()
    assert conductor.applied is True
    assert conductor.current_phase == PHASE_VERIFY


def test_the_snapshot_reads_the_journey_and_not_a_constructor_echo():
    conductor = _conductor(index_phase_map=dict(STAGE1_MAP))
    conductor._journey.accept(PHASE_MEASURE, 2)
    conductor._journey.accept(PHASE_CHECK, 1)
    snapshot = conductor.snapshot()
    assert snapshot.accepted_phases == (PHASE_CHECK, PHASE_MEASURE)
    assert snapshot.session_phases == conductor.session_phases
    assert snapshot.applied is False
    conductor.note_apply_complete()
    assert conductor.snapshot().applied is True


def test_session_phases_and_post_apply_verifies_are_public_reads():
    """Both were reached as private conductor fields by tests before Phase 4."""

    stage1 = _conductor(
        index_phase_map=dict(STAGE1_MAP), post_apply_verifies=True
    )
    assert stage1.session_phases == JourneyPlan.from_index_map(STAGE1_MAP).phases
    assert PHASE_VERIFY not in stage1.session_phases
    assert stage1.post_apply_verifies is True
    assert _conductor(index_phase_map=dict(STAGE1_MAP)).post_apply_verifies is False


def test_a_conductor_with_no_index_map_walks_the_three_entry_default():
    assert _conductor().session_phases == (
        PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY
    )


# --------------------------------------------------------------------------
# architecture — dependency direction
# --------------------------------------------------------------------------

#: The two things a module in this package may not import.
FORBIDDEN_IMPORT_ROOTS = ("jasper.web", "crossover_v2_flow")


def _forbidden_imports(module: Path) -> list[str]:
    """Every real import in ``module`` naming a forbidden root.

    Parsed rather than grepped. A line-regex reads the module's own PROSE — the
    docstring sentence that states this very rule begins "import and nothing
    from ...crossover_v2_flow" and matched, so the grep shape reported the
    package's promise as its violation. ``ast`` sees import STATEMENTS only, at
    any nesting depth, which is also what makes a lazy in-function import
    (the shape a domain module would actually smuggle a host dependency
    through) visible to this guard.
    """

    tree = ast.parse(module.read_text(), filename=str(module))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # ``level`` > 0 is a relative import, whose module is a suffix:
            # ``from .crossover_v2_flow import X`` inside the package would
            # read as ``crossover_v2_flow``.
            names = [node.module or ""]
        else:
            continue
        found += [
            name for name in names
            if any(root in name for root in FORBIDDEN_IMPORT_ROOTS)
        ]
    return found


def test_the_import_guard_sees_a_planted_violation(tmp_path):
    """The guard's own positive control. Without it, an ``ast`` walk that
    matched nothing — a changed node type, a typo'd root — would report every
    module clean and read exactly like compliance.

    Planted in ``tmp_path`` rather than beside this file: the probes were
    collectable ``tests/*.py`` modules for the length of the write, and a run
    interrupted between the write and the ``unlink`` left them in the source
    tree (#2291 Phase 5 ledger). The guard reads a path, so it neither knows
    nor cares which directory.
    """

    planted = tmp_path / "_journey_guard_probe.py"
    planted.write_text(
        "def f():\n    from jasper.web import correction_crossover_v2\n"
    )
    assert _forbidden_imports(planted) == ["jasper.web"]

    planted_relative = tmp_path / "_journey_guard_probe2.py"
    planted_relative.write_text("from ..crossover_v2_flow import PHASE_CHECK\n")
    assert _forbidden_imports(planted_relative) == ["crossover_v2_flow"]


def test_no_domain_module_imports_the_host_or_the_legacy_flow():
    """#2291's dependency direction, asserted over the WHOLE package.

    ``test_crossover_v2_verification.py`` pins the same rule for one file. This
    walks the package instead, because the per-file shape cannot see a module
    that does not exist yet — journey.py was exactly such a module, and Phase 5
    adds more. Over-covering is free; the narrow guard is left where it is
    rather than deleted, since a suite that stops asserting its own module's
    purity is a worse trade than one redundant read.
    """

    package = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2"
    )
    modules = sorted(package.glob("*.py"))
    assert len(modules) >= 6, f"expected the package's modules, saw {modules}"
    assert package / "journey.py" in modules

    offenders = {
        module.name: bad
        for module in modules
        if (bad := _forbidden_imports(module))
    }
    assert offenders == {}


def test_no_test_module_imports_the_conductor_test_file():
    """#2291 5c-i's own result, held: the conductor test files have no importers.

    It had eighteen. They reached it for twenty-five fixture symbols — including
    all three Phase-0 characterization pins — which is what made a file of
    conductor-specific tests undeletable while the conductor is being dissolved.
    The fixtures now live in ``tests/crossover_v2_fixtures.py``; this asserts
    nobody re-creates the blocker, when a new importer would be an easy and
    invisible thing to add; the guard matches the whole
    ``test_crossover_v2_conductor*`` family by prefix.

    Prose mentions are fine and deliberately not matched — several modules cite
    the file(s) in a docstring to say where a behaviour is pinned. Only a real
    ``import`` counts.

    **Every ``*.py`` under ``tests/``, not just ``test_*.py``.** The highest-
    probability way to re-create the blocker is an import from
    ``crossover_v2_fixtures.py`` itself — eighteen modules import that, so one
    line there restores the blocker for all eighteen at once — and a
    ``test_*`` glob cannot see it. Helper modules and ``conftest.py`` are in
    scope for the same reason.

    Both import spellings count: the absolute ``tests.test_crossover_v2_conductor*``
    and the relative ``from .test_crossover_v2_conductor* import …``. The
    relative form has no precedent in this suite, which is exactly why a guard
    keyed only to the absolute one would be the easy thing to slip past.
    """

    tests_dir = Path(__file__).resolve().parent
    modules = sorted(tests_dir.glob("*.py"))
    assert len(modules) >= 50, f"expected the test suite, saw {len(modules)}"
    assert tests_dir / "crossover_v2_fixtures.py" in modules

    absolute_prefix = "tests.test_crossover_v2_conductor"
    relative_prefix = "test_crossover_v2_conductor"

    def names_a_conductor_file(node: ast.AST) -> bool:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            prefix = relative_prefix if node.level else absolute_prefix
            return module.startswith(prefix)
        if isinstance(node, ast.Import):
            return any(alias.name.startswith(absolute_prefix) for alias in node.names)
        return False

    offenders: dict[str, list[int]] = {}
    for module in modules:
        if module.name.startswith("test_crossover_v2_conductor"):
            continue
        text = module.read_text()
        # Coverage-preserving fast path: an import that names a conductor file
        # has to spell the prefix, in either spelling. Parsing all ~825
        # modules unconditionally cost this one guard several seconds; a
        # dynamically-built import would escape the AST check below with or
        # without this line.
        if relative_prefix not in text:
            continue
        tree = ast.parse(text, filename=str(module))
        lines = [
            node.lineno for node in ast.walk(tree)
            if names_a_conductor_file(node)
        ]
        if lines:
            offenders[module.name] = lines

    assert offenders == {}


# --------------------------------------------------------------------------
# architecture — the package's own shape
# --------------------------------------------------------------------------

#: How a module inside the package spells itself from the outside.
PACKAGE_DOTTED = "jasper.active_speaker.crossover_v2"


def _package_suffix(dotted_name: str, package: str) -> list[str]:
    """``["contracts"]`` for ``<package>.contracts.X``, ``[]`` for anything else."""

    prefix = f"{package}."
    if not dotted_name.startswith(prefix):
        return []
    return [dotted_name[len(prefix):].split(".")[0]]


def _intra_package_edges(package: Path, dotted: str) -> dict[str, set[str]]:
    """``{module: the sibling modules it imports}``, by bare module name.

    Both spellings the package actually uses are read: the relative
    ``from .contracts import X`` / ``from . import priors``, and the absolute
    ``from jasper.active_speaker.crossover_v2.contracts import X`` that two
    lazy in-function imports use today. A guard blind to the second would miss
    the exact shape a cycle arrives in, since deferring an import into a
    function body is how a developer works around one.

    ``__init__.py`` is deliberately not a node. Importing any submodule
    executes the package ``__init__``, and this one re-exports most of the
    package — so a graph containing it is cyclic by construction and would say
    nothing about whether the MODULES depend on each other in one direction.

    Names that are not modules of this package are dropped rather than
    trusted: ``from . import GEOMETRY_RETRY_POSITIONS`` would otherwise invent
    an edge to a module that does not exist.
    """

    modules = {path.stem for path in package.glob("*.py")} - {"__init__"}
    edges: dict[str, set[str]] = {}
    for path in sorted(package.glob("*.py")):
        if path.stem == "__init__":
            continue
        found: set[str] = set()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    named = (
                        [node.module.split(".")[0]] if node.module
                        else [alias.name for alias in node.names]
                    )
                elif node.level == 0 and node.module:
                    named = _package_suffix(node.module, dotted)
                else:  # ``from ..flat_spec import X`` — outside the package
                    named = []
            elif isinstance(node, ast.Import):
                named = [
                    name
                    for alias in node.names
                    for name in _package_suffix(alias.name, dotted)
                ]
            else:
                continue
            found |= {n for n in named if n in modules and n != path.stem}
        edges[path.stem] = found
    return edges


def _import_cycle(edges: dict[str, set[str]]) -> tuple[str, ...] | None:
    try:
        TopologicalSorter(edges).prepare()
    except CycleError as exc:
        return tuple(exc.args[1])
    return None


def test_the_cycle_guard_sees_a_planted_cycle(tmp_path):
    """The acyclicity guard's own positive control.

    Two ways this guard could pass while asserting nothing, and the plant
    catches both: an edge walk that matches no import shape returns an empty
    graph, which is trivially acyclic and reads exactly like a healthy
    package; and a cycle detector wired to the wrong end of the graph never
    raises. The planted package uses ONE spelling per direction — relative one
    way, absolute the other — so a walker that understands only one of them
    finds a single edge and no cycle.
    """

    package = tmp_path / "tmpkg"
    package.mkdir()
    (package / "__init__.py").write_text("from .a import A\n")
    (package / "a.py").write_text("from .b import B\n")
    (package / "b.py").write_text("from tmpkg.a import A\n")

    edges = _intra_package_edges(package, "tmpkg")
    assert edges == {"a": {"b"}, "b": {"a"}}, "the __init__ is not a node"
    assert _import_cycle(edges) is not None


def test_the_package_import_graph_stays_acyclic():
    """#2662's G1: the DAG the package happens to be becomes the DAG it is.

    ``test_no_domain_module_imports_the_host_or_the_legacy_flow`` above forbids
    two imports by name. It says nothing about the package's INTERNAL shape,
    so nothing stopped ``contracts`` — which eight nodes of this graph import
    (nine files do; the package ``__init__`` is not a node) and which
    imports none of them — from importing ``coordinator`` tomorrow. The
    layering was an accident of how the extraction happened to land; this
    makes it a contract, at the cost of one walk.

    The edge floor is not decoration. An assertion that a graph has no cycle
    is satisfied by a graph with no edges, so a walker broken by a Python
    grammar change would report perfect health.
    """

    package = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2"
    )
    edges = _intra_package_edges(package, PACKAGE_DOTTED)

    assert len(edges) >= 15, f"expected the package's modules, saw {len(edges)}"
    assert sum(len(deps) for deps in edges.values()) >= 20

    cycle = _import_cycle(edges)
    assert cycle is None, f"crossover_v2 import cycle: {' -> '.join(cycle or ())}"


# --------------------------------------------------------------------------
# architecture — the renderer speaks the vocabulary by symbol
# --------------------------------------------------------------------------


def _guarded_vocabulary() -> dict[str, str]:
    """``{value: owning symbol}`` for the two code vocabularies the renderer
    speaks: ``refusal_copy.REASON_*`` and ``verification.RESULT_*``."""

    from jasper.active_speaker.crossover_v2 import refusal_copy, verification

    owners: dict[str, str] = {}
    for module, prefix in ((refusal_copy, "REASON_"), (verification, "RESULT_")):
        for name in dir(module):
            value = getattr(module, name)
            if name.startswith(prefix) and isinstance(value, str):
                owners[value] = name
    return owners


def _retyped_vocabulary(module: Path, owners: dict[str, str]) -> list[str]:
    """Every string literal in ``module`` that re-types a guarded code."""

    tree = ast.parse(module.read_text(), filename=str(module))
    return [
        f"{module.name}:{node.lineno}: {node.value!r} is {owners[node.value]}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in owners
    ]


def test_the_retyping_guard_sees_a_planted_literal(tmp_path):
    """The conventions guard's own positive control.

    A value-matching walk that matched nothing — a changed node type, a
    vocabulary read that came back empty — would report the renderer clean and
    read exactly like compliance, which is the state this guard exists to tell
    apart from the real thing.
    """

    owners = _guarded_vocabulary()
    assert len(owners) >= 40, f"the vocabulary read came back thin: {len(owners)}"

    planted = tmp_path / "_retyping_probe.py"
    planted.write_text('BADGE = {"keep_previous": "Keep the previous sound."}\n')

    # Asserted by MARKER, not by the whole formatted string: the message is
    # diagnostic prose, and a control that breaks when someone improves the
    # wording teaches the next person to loosen the control.
    found = _retyped_vocabulary(planted, owners)
    assert len(found) == 1
    assert "_retyping_probe.py:1" in found[0]
    assert "RESULT_KEEP_PREVIOUS" in found[0]


def test_the_envelope_renderer_never_re_types_a_code_it_could_import():
    """#2662's G3: the domain renderer imports the vocabulary it renders.

    ``crossover_envelope_v2`` may not import ``jasper.web`` (the rule above),
    and the four ``RESULT_*`` codes lived there — so it spelled all four by
    hand in twelve places, with nothing holding the two sets equal. The codes
    moved to ``verification`` where the renderer can import them; this stops
    the next one from being re-typed instead.

    **One guarded value is skipped, and the skip is only as alive as its
    reason.** ``RESULT_INCONCLUSIVE`` and the host's ``GRADE_INCONCLUSIVE``
    are both ``"inconclusive"`` while answering different questions about the
    same round — what the result WAS, versus whether the check finished. The
    renderer legitimately compares a grade state against the second, so a bare
    ``"inconclusive"`` cannot be attributed to one of the two by its value.
    The assertion below fails if the collision ever ends, which is when this
    skip should be deleted rather than inherited.
    """

    from jasper.active_speaker.crossover_v2.verification import RESULT_INCONCLUSIVE
    from jasper.web.correction_crossover_v2 import GRADE_INCONCLUSIVE

    assert GRADE_INCONCLUSIVE == RESULT_INCONCLUSIVE, (
        "the value collision this skip exists for has ended — delete the skip"
    )
    owners = _guarded_vocabulary()
    del owners[RESULT_INCONCLUSIVE]

    renderer = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_envelope_v2.py"
    )
    assert _retyped_vocabulary(renderer, owners) == []


def test_the_journey_holds_no_dsp_no_filesystem_and_no_rendering():
    """Phase 4's explicit boundary: bookkeeping only."""

    module = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2" / "journey.py"
    )
    tree = ast.parse(module.read_text(), filename=str(module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    # Nothing numeric, nothing on disk, nothing that journals: the journey
    # derives, and its callers measure, persist, and narrate.
    assert imported == {"__future__", "dataclasses", "types", "typing"}


def test_the_journey_is_pure_the_same_inputs_give_the_same_plan():
    first = JourneyPlan.from_index_map(STAGE1_MAP)
    second = JourneyPlan.from_index_map(dict(STAGE1_MAP))
    assert first == second
    assert open_stage(
        STAGE_VERIFY_CAPABILITIES, index_phase_map=VERIFY_ONLY_MAP
    ) == open_stage(
        STAGE_VERIFY_CAPABILITIES, index_phase_map=VERIFY_ONLY_MAP
    )


def test_mark_restored_is_mark_applieds_inverse():
    """#2616 — the flag can go back, so the journey can own it in both
    directions."""
    journey = _journey()
    journey.mark_applied()
    assert journey.applied is True
    journey.mark_restored()
    assert journey.applied is False


def test_applied_cannot_go_false_to_true_without_a_fresh_apply():
    """Only ``mark_applied`` sets it, and a restore does not un-set itself.

    The defect #2616 fixes was a SECOND owner writing the flag back: the
    durable state's restore cleared it, and the next conductor persist
    re-asserted it from a snapshot taken before the restore. This pins the half
    that lives here — nothing but a fresh apply raises the flag, so once the
    journey is told about a restore, only a real apply can undo that.
    """
    journey = _journey()
    journey.mark_applied()
    journey.mark_restored()
    assert journey.applied is False

    # Every other transition this object has, exercised against a restored
    # journey: none of them may raise the flag.
    for phase, index in ((PHASE_CHECK, 1), (PHASE_MEASURE, 2)):
        journey.accept(phase, index)
        assert journey.applied is False
    journey.mark_restored()
    assert journey.applied is False

    # ...and the one transition that may, does.
    journey.mark_applied()
    assert journey.applied is True


def test_restoring_a_journey_that_never_applied_is_a_no_op():
    """No illegal-transition guard, matching the class's stated posture."""
    journey = _journey()
    journey.mark_restored()
    assert journey.applied is False


def test_the_restore_re_opens_the_applying_interlude():
    """The derivation ``applied`` feeds moves back with it.

    ``mark_applied`` collapses the PHASE_APPLYING interlude into VERIFY; its
    inverse has to re-open it, or the flag would be reversible while the screen
    the household sees would not.
    """
    journey = _journey(
        VERIFY_ONLY_MAP, accepted_phases=(PHASE_CHECK, PHASE_MEASURE),
        applied=True,
    )
    applied_phase = journey.current_phase
    journey.mark_restored()
    assert journey.current_phase != applied_phase
    assert journey.current_phase == PHASE_APPLYING
