# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Retry ledger behavior when a crossover session is restored."""

from __future__ import annotations

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE
from jasper.active_speaker.crossover_v2_flow import (
    MAX_EXTRA_ATTEMPTS_PER_POSITION,
    CrossoverV2Session,
)

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
    assert spent["attempts"]["by_household"] == 1
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
    assert after["attempts"]["by_household"] == 0
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
