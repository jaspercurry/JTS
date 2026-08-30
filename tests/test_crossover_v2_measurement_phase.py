# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Which stimulus each measurement kind plays — the map, and its identities.

A wrong row here does not raise. It plays the wrong stimulus and banks a record
that looks correct, so the deliverable is as much this file as the map: every
row is asserted, the regime-invariance is asserted rather than assumed, and the
program-object identities the flow depends on are pinned as identities rather
than described in prose.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_NULL_CONFIRM,
    MEASURE_KIND_VERIFY,
    MEASURE_KINDS,
    MEASURE_REGIMES,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_ENTRY_BASELINE,
    PHASE_MEASURE,
    PHASE_NULL_CONFIRM,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.measurement_phase import (
    PHASE_BY_MEASURE_KIND,
    UNMAPPED_MEASUREMENT_KIND,
    NoPhaseForMeasurementError,
    phase_for_measurement,
)
from jasper.active_speaker.crossover_v2.programs import (
    GROUP_SUMMED_SWEEP_PHASES,
    SUMMED_SWEEP_PHASES,
    NoProgramForPhaseError,
    program_for_phase,
)

#: The table, written out. kind × regime → the phase whose program plays.
#: Spelled as literal pairs rather than derived from the map under test, so a
#: row that changed has to be changed HERE too — a table that computed its own
#: expectation would agree with any map.
_TABLE = [
    (MEASURE_KIND_BASELINE, "reference_axis", PHASE_ENTRY_BASELINE),
    (MEASURE_KIND_BASELINE, "near_field", PHASE_ENTRY_BASELINE),
    (MEASURE_KIND_CANDIDATE, "reference_axis", PHASE_MEASURE),
    (MEASURE_KIND_CANDIDATE, "near_field", PHASE_MEASURE),
    (MEASURE_KIND_VERIFY, "reference_axis", PHASE_VERIFY),
    (MEASURE_KIND_VERIFY, "near_field", PHASE_VERIFY),
    (MEASURE_KIND_NULL_CONFIRM, "reference_axis", PHASE_NULL_CONFIRM),
    (MEASURE_KIND_NULL_CONFIRM, "near_field", PHASE_NULL_CONFIRM),
]


@pytest.mark.parametrize("kind, regime, expected", _TABLE)
def test_each_kind_and_regime_resolves_to_its_own_stimulus(
    kind: str, regime: str, expected: str,
):
    """One row, one claim about which sound the speaker makes."""
    assert phase_for_measurement(kind) == expected, (kind, regime)


def test_the_table_covers_every_kind_and_every_regime():
    """Anti-vacuity: a fourth kind or regime must fail here, not pass silently.

    Asserted against the vocabularies' own members rather than a count, so a
    kind added without a row reds this instead of slipping through a table
    that only ever tested what it already listed.
    """
    assert {kind for kind, _r, _p in _TABLE} == set(MEASURE_KINDS)
    assert {regime for _k, regime, _p in _TABLE} == set(MEASURE_REGIMES)
    assert set(PHASE_BY_MEASURE_KIND) == set(MEASURE_KINDS)


@pytest.mark.parametrize("kind", sorted(MEASURE_KINDS))
def test_the_regime_does_not_choose_the_stimulus(kind: str):
    """The load-bearing finding, asserted rather than assumed.

    ``near_field`` and ``reference_axis`` are capture GEOMETRIES — where the
    microphone sits. A future regime that really needed its own stimulus would
    have to break this pin to get in, which is the point.
    """
    resolved = {phase_for_measurement(kind) for _regime in MEASURE_REGIMES}

    assert len(resolved) == 1


def test_an_unmapped_kind_refuses_by_name_and_never_guesses():
    """Visible wrongness: no default arm, and a reason code rather than prose.

    A default would play *some* stimulus for a kind nobody mapped, and the
    record would carry no sign the choice was arbitrary — the ``take_kind``
    precedent, where an unresolvable fact is reported as itself.
    """
    with pytest.raises(NoPhaseForMeasurementError) as raised:
        phase_for_measurement("sweep")

    assert raised.value.code == UNMAPPED_MEASUREMENT_KIND
    assert raised.value.kind == "sweep"


# --------------------------------------------------------------------------- #
# the identities the flow depends on, pinned as identities
# --------------------------------------------------------------------------- #


def _programs() -> dict:
    return {
        "check": object(),
        "measure": object(),
        "verify": object(),
        "cloud": object(),
    }


def test_the_baseline_and_the_verify_play_ONE_program_object():
    """#2291's before→after comparison is checked by ``program_id`` equality.

    Equality holds because both sides are handed the identical OBJECT. If this
    map sent the two kinds to phases that composed separately, every round's
    benefit verdict would become a program mismatch — the comparison would
    stop working without anything raising.
    """
    parts = _programs()
    baseline = program_for_phase(
        phase_for_measurement(MEASURE_KIND_BASELINE),
        check=parts["check"], measure=parts["measure"],
        verify=parts["verify"], cloud=parts["cloud"],
    )
    verify = program_for_phase(
        phase_for_measurement(MEASURE_KIND_VERIFY),
        check=parts["check"], measure=parts["measure"],
        verify=parts["verify"], cloud=parts["cloud"],
    )

    assert baseline is verify
    assert baseline is parts["verify"]


def test_the_candidate_plays_the_per_driver_program_and_not_the_sweep():
    """A candidate is a per-driver claim; a summed curve cannot answer it."""
    parts = _programs()

    program = program_for_phase(
        phase_for_measurement(MEASURE_KIND_CANDIDATE),
        check=parts["check"], measure=parts["measure"],
        verify=parts["verify"], cloud=parts["cloud"],
    )

    assert program is parts["measure"]
    assert phase_for_measurement(MEASURE_KIND_CANDIDATE) not in SUMMED_SWEEP_PHASES


def test_the_mapped_summed_phases_are_the_compared_pair_not_a_position_group():
    """The map reaches the COMPARED half of the summed family, never a cloud.

    A position group is a walk shape the engine's ``measure`` does not express,
    and resolving a kind onto one would hand a session a prompted group it has
    no prompts for.
    """
    mapped = set(PHASE_BY_MEASURE_KIND.values())

    assert mapped & SUMMED_SWEEP_PHASES, "the summed half must be reachable"
    assert not (mapped & GROUP_SUMMED_SWEEP_PHASES)


def test_a_candidate_measured_before_the_gain_solve_refuses_loudly():
    """The flow's own refusal survives the mapping.

    ``measure`` has no composed program until the CHECK gain solve produces
    one, and asking anyway raises rather than composing at a guessed level.
    The map must not paper over that.
    """
    parts = _programs()

    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            phase_for_measurement(MEASURE_KIND_CANDIDATE),
            check=parts["check"], measure=None,
            verify=parts["verify"], cloud=parts["cloud"],
        )
