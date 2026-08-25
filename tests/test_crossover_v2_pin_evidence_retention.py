# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #2291 Phase 0 — preserved-behavior pins for retained-position evidence.

#2291 names "evidence retention/combining" among the strong bones, and its
target architecture moves retained-position assembly into a ``spatial.py``
adapter. These pin the two properties that move would most easily lose.

**Not re-pinned here**, because they already are: position COUNT and ORDER
through the combiner (``tests/test_spatial_combine.py``), the conductor's own
count/order/stable-id retention and its retake-replaces-by-index discipline
(``tests/test_crossover_v2_conductor.py``), the members-alongside-aggregate
serialization and its per-position scalars (``tests/test_attribution_persistence.py``),
and the lateral walk's in-memory count/order/identity
(``tests/test_crossover_v2_lateral_evidence.py``).

What follows is the boundary those leave open.
"""

from __future__ import annotations

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import (
    GROUP_PHASES,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
)
from jasper.active_speaker.crossover_v2_flow import build_v2_cloud_index_phase_map
from jasper.attribution.position_evidence import position_evidence_block

from tests.test_attribution_persistence import _combined, _records
from tests.crossover_v2_fixtures import FakeSeams, _conductor, _run_phase


# --- the identity join is by key, not by position ----------------------------


def test_each_position_keeps_its_own_metadata_when_the_records_arrive_scrambled():
    """The join that stops metadata being bound to the wrong curve.

    ``position_evidence_block`` builds a ``{position_id: record}`` dict and
    looks each row up by its own id, rather than walking ``position_records``
    in parallel with ``combined.position_ids``. That choice is what makes the
    block correct when the two sequences are not in the same order — and it
    is unasserted: every existing fixture builds both from the same
    ``range(n)``, so a ``zip(ids, position_records)`` "simplification" would
    produce identical output for all of them and pass the whole suite.

    The failure that would ship is silent and serious: a position's
    ``wav_sha256``, accepted ``attempt``, and mic ``role`` bound to a
    different position's curve. That is the evidence a later attribution
    round reads to say *which* microphone position heard *what*, so a
    misbinding is not a cosmetic ordering bug.

    Reversed rather than randomly shuffled so the case is deterministic and
    every row is displaced (a shuffle can leave elements in place).
    """

    combined = _combined(n=3)
    scrambled = list(reversed(_records(n=3)))

    block = position_evidence_block(combined, position_records=scrambled)

    assert block["available"] is True
    rows = {row["position_id"]: row for row in block["positions"]}
    assert set(rows) == {f"cloud_measure_{i:02d}" for i in range(3)}

    # Each row carries ITS OWN record's fields. The fixture makes these
    # distinguishable on purpose: a distinct digest per position, and an
    # accepted second attempt on exactly one of them.
    for i in range(3):
        row = rows[f"cloud_measure_{i:02d}"]
        assert row["wav_sha256"] == f"{i:064d}"
        assert row["index"] == i
        assert row["role"] == ("onax", "offax", "xovr")[i % 3]
    assert rows["cloud_measure_01"]["attempt"] == 2
    assert rows["cloud_measure_00"]["attempt"] == 1
    assert rows["cloud_measure_02"]["attempt"] == 1

    # …and the curves stay in the COMBINER's order regardless of how the
    # metadata arrived, so the aggregate and its members still line up.
    assert [row["position_id"] for row in block["positions"]] == list(
        combined.position_ids
    )


def test_a_position_with_no_record_still_gets_its_curve_rather_than_a_neighbours():
    """The same join, one step harder: a partial record set.

    A group that lost one position's metadata must degrade to "this row has
    a curve and no metadata", never to "this row borrowed the next row's
    metadata". A positional walk over a SHORTER ``position_records`` shifts
    every later row by one, which is the misbinding above with an
    off-by-one instead of a reversal.
    """

    combined = _combined(n=3)
    only_the_last = [_records(n=3)[2]]

    block = position_evidence_block(combined, position_records=only_the_last)

    rows = {row["position_id"]: row for row in block["positions"]}
    assert rows["cloud_measure_02"]["wav_sha256"] == f"{2:064d}"
    # The two without records carry curves and no borrowed identity.
    for missing in ("cloud_measure_00", "cloud_measure_01"):
        assert "wav_sha256" not in rows[missing]
        assert "attempt" not in rows[missing]
        assert rows[missing]["magnitude_db"]


# --- the lateral walk is a group phase that is NOT a cloud group -------------


def test_the_lateral_walk_keeps_its_own_retention_and_never_enters_a_cloud_group():
    """The asymmetry a reader of ``GROUP_PHASES`` would not predict.

    ``PHASE_LATERAL`` is a member of ``GROUP_PHASES`` — it spans many capture
    indexes like the two clouds — but its captures are per-driver MEASURE
    replays, not summed sweeps, so they are retained on their own
    ``lateral_poses`` list and never in ``_group_positions``. The flow says
    why in ``CrossoverV2Session.__init__``: putting the two in one list
    "would give the combiner an input it cannot combine."

    Nothing asserts that today, in either direction. The trap is symmetrical:
    a migration that treats ``GROUP_PHASES`` as "the phases the cloud
    pipeline owns" would route poses into the combiner, and one that splits
    lateral out of ``GROUP_PHASES`` entirely would lose the per-index
    accepted-capture bookkeeping that membership is actually for.
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
    poses = len(flow.LATERAL_POSE_PROMPTS)
    for index in range(3, 3 + poses):
        _run_phase(conductor, index, 1)

    # The walk really did retain its evidence…
    assert len(conductor.lateral_poses) == poses

    # …on its own list. The cloud accessors stay empty for the same phase.
    assert PHASE_LATERAL in GROUP_PHASES
    assert conductor.group_positions(PHASE_LATERAL) == ()
    assert conductor.group_position_takes(PHASE_LATERAL) == ()
    assert conductor.group_geometry(PHASE_LATERAL) is None

    # And it is not one of the summed-sweep groups the combiner does own.
    assert PHASE_LATERAL not in flow.SUMMED_SWEEP_PHASES
    assert GROUP_PHASES == frozenset(
        {PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_LATERAL}
    )
