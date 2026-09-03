# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #2291 Phase 0 — capture-sequence and retry-ledger characterization.

Two gaps, both found by inventorying what the ~26 existing crossover test
files already pin. Most of this territory is covered and is NOT re-pinned
here:

* The shipped stage-1 phase ORDER is authoritatively pinned by
  ``tests/test_crossover_v2_lateral_evidence.py``'s
  ``test_the_walk_is_on_and_stage_1_is_the_pinned_six_pose_shape`` — it reads
  both stage-1 flags live, asserts the entry order, and pins the plan's wire
  bytes by SHA.
* Which phases are summed vs per-driver, and VERIFY's program identity with
  the position groups that follow it, are pinned three independent ways
  (conductor object-identity, retention labelling, and the
  ``summed_program.wav`` alias trio).
* The bounded-retry ruling's own bound and terminal shapes — pooled
  ``MAX_EXTRA_ATTEMPTS_PER_POSITION`` across initiators, the exhaustion
  outcomes, the honest refusal copy — are richly pinned in the conductor
  suite's bounded-retry block.

What follows is only what none of that reaches.
"""

from __future__ import annotations

import inspect

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE
from jasper.active_speaker.crossover_v2_flow import (
    MAX_EXTRA_ATTEMPTS_PER_POSITION,
    STAGE1_INCLUDES_CLOUD_MEASURE,
    STAGE1_INCLUDES_ENTRY_BASELINE,
    CrossoverV2Session,
    build_v2_cloud_index_phase_map,
    build_v2_session_spec,
)
from jasper.web import correction_crossover_v2 as v2host

from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    SESSION,
    SESSION_VOLUME_DB,
    FakeSeams,
    _conductor,
    _measure_analysis,
    _preset,
    _roles,
    _run_phase,
)


# --- gap 1: the lateral walk's own wiring into the shipped session -----------


def test_the_session_preparer_threads_the_lateral_walk_into_both_surfaces():
    """The ``include_lateral`` half of an existing guard that only checks
    ``include_cloud_measure``.

    ``prepare_v2_session`` decides each stage-1 inclusion once and threads it
    into TWO surfaces: the session spec the phone renders, and the conductor's
    index→phase map. That pairing is the module's own stated invariant —
    "an entry's prompt can never address a different phase than the conductor
    believes it is running" (``build_v2_cloud_index_phase_map``'s docstring).

    ``test_the_session_preparer_threads_one_tier_into_the_spec_and_the_map``
    in ``tests/test_correction_crossover_v2_endpoints.py`` guards exactly this
    for ``include_cloud_measure`` — including the ``== 2`` call count. There is
    no ``include_lateral`` equivalent anywhere, and ``include_lateral`` is not
    read from a module flag at all: the lateral walk is not a stage-1 group, so
    the preparer sets a local literal instead, and an operator's staged angle
    walk is the only thing that flips it (to ``True``) later in the same
    function. Both builders still default the *parameter* to ``False``
    (asserted below), so dropping either thread is silent in the same way it
    always was: the session would still build, quietly reverting to the
    no-walk shape while the local says otherwise.

    Source-inspected rather than driven, mirroring the sibling guard's own
    choice — trusting the call site to stay wired is the desync it guards.

    A behavioral version is possible and is strictly stronger: both call sites
    live in ``prepare_v2_session``'s ``_open`` closure, and
    ``tests/test_crossover_v2_stage_bridge.py``'s ``_stage_1`` harness drives
    that real ``_open``, so asserting ``PHASE_LATERAL in
    conductor.session_phases`` catches wiring breaks this textual guard cannot
    see (a hard-coded ``True``, the wrong flag read). It is deliberately NOT
    added here: running that harness's fixtures in this module makes it fail 36
    tests of ``tests/test_correction_crossover_v2_endpoints.py`` in a shared
    process — a residue of the real ``_open`` that also afflicts the
    stage-bridge module itself, with or without any production mutation. Adding
    it would be a second instance of a defect that belongs to that harness, so
    it is reported there instead of duplicated here.
    """

    source = inspect.getsource(v2host.prepare_v2_session)

    # The preparer sets this decision ONCE, as a local literal rather than a
    # module flag — the lateral walk is not a stage-1 group, per the comment
    # beside this line in production — and that one decision is what gets
    # threaded below. The literal is stated here so a reversion back to
    # reading a module flag is visible in this pin too.
    assert "include_lateral = False" in source
    # …and threads that one decision into every surface: the map, the same map
    # rebuilt when #2732's angle-walk take supplies one, and the spec. Three
    # since that take, and the count is about the LOCAL being threaded rather
    # than about how many builders read it — a literal at any of the three is
    # the drift this catches.
    assert source.count("include_lateral=include_lateral") == 3

    # Stated beside the sibling flag it was written next to, so a change that
    # turned the pre-apply cloud back on without revisiting the walk is
    # visible here rather than only in the plan's byte pin.
    assert "include_cloud_measure = STAGE1_INCLUDES_CLOUD_MEASURE" in source
    assert STAGE1_INCLUDES_CLOUD_MEASURE is False

    # #2291's entry baseline is the third stage-1 flag and takes the identical
    # guard, because a dropped thread is silent in exactly the same way: the
    # session would build the no-baseline shape while the flag says otherwise,
    # and the first surface to notice would be a household's benefit verdict
    # reading ``entry_baseline_unavailable`` a stage later.
    assert "include_entry_baseline = STAGE1_INCLUDES_ENTRY_BASELINE" in source
    assert STAGE1_INCLUDES_ENTRY_BASELINE is True
    assert source.count("include_entry_baseline=include_entry_baseline") == 3


def test_both_builders_default_the_walk_off_which_is_why_a_dropped_thread_is_silent():
    """The premise the guard above rests on, stated rather than assumed.

    If either builder defaulted ``include_lateral=True``, a dropped
    ``include_lateral=include_lateral`` would be harmless and the guard above
    would be theatre. They both default it ``False``, so the omission
    downgrades the shipped session to the no-walk shape with no error. #2291's
    ``include_entry_baseline`` follows the same convention for the same reason.
    """

    for builder in (build_v2_cloud_index_phase_map, build_v2_session_spec):
        for flag in ("include_lateral", "include_entry_baseline"):
            default = inspect.signature(builder).parameters[flag].default
            assert default is False, (
                f"{builder.__name__} no longer defaults {flag} off"
            )


# --- gap 2: the retry ledger does not survive a conductor rebuild ------------


def _hydrated(snapshot, session_id: str, fakes: FakeSeams) -> CrossoverV2Session:
    return CrossoverV2Session.hydrate(
        snapshot,
        session_id=session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=fakes.seams(),
        driver_spacing_m=0.15,
    )


def test_the_snapshot_carries_no_retry_ledger_so_a_resume_restores_full_extras():
    """Characterization, not endorsement: what a conductor rebuild does to a
    partially-spent position's retry pool today.

    ``SlotAttempts`` lives only in ``self._slot_attempts``; it is not a field
    of ``V2ConductorSnapshot``. So the §5.6 same-session resume restores the
    accepted phases and the gain plan but NOT the per-position extras — the
    rebuilt conductor offers a position its full
    ``MAX_EXTRA_ATTEMPTS_PER_POSITION`` again.

    **Reachability, stated honestly so this is not read as a live defect
    report.** ``prepare_v2_session`` is the only production caller of
    ``hydrate``, and it always passes a NEW capture session id, so production
    takes the different-session branch (a fresh start at CHECK, where a reset
    pool is simply correct). The same-session branch exercised here is
    reachable machinery with its own documented resume semantics; what is
    unasserted is which side of the §5.6 line the retry pool falls on.

    Pinned because #2291 Phase 4 extracts the journey state, and "the ledger
    is in-memory only" is a property that a state aggregate would silently
    change in either direction. Whether the bound SHOULD survive a rebuild is
    a product decision for that phase; this records what it does now.
    """

    fakes = FakeSeams()
    conductor = _conductor(fakes)
    _run_phase(conductor, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, linearity=False)
    _run_phase(conductor, 2, 2)
    spent = _run_phase(conductor, 2, 3)

    # One extra really was spent before the rebuild.
    assert spent["attempts"]["used"] == 1
    assert spent["attempts"]["left"] == MAX_EXTRA_ATTEMPTS_PER_POSITION - 1

    snapshot = conductor.snapshot()
    # The mechanism: the durable shape has no room for the ledger.
    assert "slot_attempts" not in snapshot.to_dict()
    assert not hasattr(snapshot, "slot_attempts")

    resumed = _hydrated(snapshot, SESSION, fakes)

    # The resume really is a resume — accepted phases DID survive, so the
    # reset below is the ledger's own behaviour and not a fresh session.
    assert resumed.session_id == SESSION
    assert PHASE_MEASURE not in resumed.accepted_phases
    assert "check" in resumed.accepted_phases
    assert resumed._slot_attempts == {}

    after = _run_phase(resumed, 2, 4)
    assert after["attempts"]["used"] == 0
    assert after["attempts"]["left"] == MAX_EXTRA_ATTEMPTS_PER_POSITION


def test_a_different_session_starts_the_ledger_fresh_as_section_5_6_requires():
    """The other branch, for contrast — here a reset pool is the CORRECT
    answer, because §5.6 invalidates a prior session's evidence outright and
    the household is starting the walk again at CHECK.

    Written so the pin above cannot be read as "any reset is wrong": the two
    branches are asserted side by side, and only one of them is an open
    question for #2291 Phase 4.
    """

    fakes = FakeSeams()
    conductor = _conductor(fakes)
    _run_phase(conductor, 1, 1)
    fakes.measure = lambda program: _measure_analysis(program, linearity=False)
    _run_phase(conductor, 2, 2)

    fresh = _hydrated(conductor.snapshot(), "cap_a_different_session", fakes)

    assert fresh.accepted_phases == frozenset()
    assert fresh._slot_attempts == {}


def test_the_ledger_is_reachable_only_through_the_conductors_own_memory():
    """Why no durable-state test could have caught the above.

    ``_slot_attempts`` is the single owner of the pooled count, and nothing
    projects it into the snapshot. Asserted against the live field set so a
    future migration that DOES persist it fails here and has to update the
    characterization above rather than leaving it stale.
    """

    assert "slot_attempts" not in flow.V2ConductorSnapshot.__dataclass_fields__
    assert isinstance(_conductor(FakeSeams())._slot_attempts, dict)
