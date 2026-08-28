# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The little measurement database: what it indexes, and what rebuilds it.

Two pins carry the suite: ``test_a_banked_take_is_findable_by`` fails if the
store stops indexing at all, and ``test_a_rebuild_by_rescan_reproduces_the_
banked_rows`` fails if the index and the files it derives from can disagree —
which is the whole claim that losing this database loses nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
    ROUND_RECEIPT_KIND,
)
from jasper.active_speaker.crossover_v2.record_index import (
    find_measurements,
    index_path,
    rebuild,
)
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from jasper.web import correction_crossover_v2 as host
from tests.test_crossover_v2_record_store import RELAY, _bundle, _take

ARTIFACTS = "evidence/v1/artifacts"


@pytest.fixture
def store(tmp_path):
    """The production store over a real bundle — the index's only writer."""
    info = _bundle(tmp_path)
    host.set_state_path_for_tests(tmp_path / "state.json")
    yield BankedRecordStore(
        evidence=CommissioningEvidenceStore.open(
            info["bundle_dir"], expected_session_id=info["session_id"],
        ),
        relay_session_id=RELAY,
        load_state=host.load_v2_state,
        save_state=host.save_v2_state,
    )
    host.set_state_path_for_tests(None)


def _db(store: BankedRecordStore) -> Path:
    return index_path(store.evidence.bundle_dir)


def _artifacts(store: BankedRecordStore) -> Path:
    return Path(store.evidence.bundle_dir) / ARTIFACTS


def _builder_take(**overrides: Any) -> dict[str, Any]:
    """A take as the four ``spatial`` builders bank one: with a clock on it."""
    return {**_take(), "captured_at": "2026-08-28T11:22:33Z", **overrides}


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "engine-session"),
        ("kind", MEASURE_KIND_CANDIDATE),
        ("position_deg", 30),
        ("candidate_id", "cand-7"),
    ],
)
async def test_a_banked_take_is_findable_by(store, field, value):
    """Every column the owner asked for is one the index can be asked about.

    The point of the database: one banked take, found without globbing a
    directory, by each of the four facts a reader has in hand.
    """
    record_id = await store.bank(_builder_take(
        kind=MEASURE_KIND_CANDIDATE, position_deg=30, candidate_id="cand-7",
    ))

    found = find_measurements(_db(store), **{field: value})

    assert [row.path for row in found] == [record_id]


async def test_a_rebuild_by_rescan_reproduces_the_banked_rows(store):
    """Delete the index, rebuild it from the files, get the same six columns.

    This is what makes the index derived rather than a second store: the
    banked records carry every column, so the database can be thrown away.
    """
    for index, kind in enumerate(
        (MEASURE_KIND_BASELINE, MEASURE_KIND_CANDIDATE, MEASURE_KIND_VERIFY)
    ):
        await store.bank(_builder_take(
            kind=kind, position_deg=index * 15,
            take_id=f"pose_{index:02d}_a01", candidate_id=f"cand-{index}",
        ))
    banked = find_measurements(_db(store))
    assert len(banked) == 3

    _db(store).unlink()
    assert rebuild(_db(store), _artifacts(store)) == 3

    assert find_measurements(_db(store)) == banked


@pytest.mark.parametrize(
    "captured_at,expected",
    [
        # The lateral pose, the entry baseline and the phase capture.
        ("2026-08-28T11:22:33Z", "2026-08-28T11:22:33Z"),
        # The cloud position, which emits a Unix epoch float for that same
        # instant instead — the disagreement is the type, not the moment.
        (1787916153.0, "2026-08-28T11:22:33Z"),
        # The engine's own ``_record()``, which banks no clock at all.
        (None, None),
    ],
)
async def test_the_indexed_clock_is_the_records_own(store, captured_at, expected):
    """Two builder types normalize to one spelling; an absent one stays absent.

    The types genuinely disagree upstream, and normalizing here rather than at
    the builders is what keeps the store from rewriting records it already
    wrote once.
    """
    take = _take() if captured_at is None else _builder_take(
        captured_at=captured_at,
    )

    await store.bank(take)

    assert [row.captured_at for row in find_measurements(_db(store))] == [expected]


async def test_an_artifact_that_is_not_a_measurement_is_not_indexed(store):
    """Five of the six banked kinds are not takes, and none of them get a row."""
    await store.bank({
        "kind": ROUND_RECEIPT_KIND, "session_id": "engine-session",
        "phase": "verify",
    })

    assert find_measurements(_db(store)) == ()
    assert rebuild(_db(store), _artifacts(store)) == 0


async def test_the_index_never_lands_in_the_byte_budgeted_subtree(store):
    """The database is derived, so it stays out of ``evidence/v1``.

    That subtree is walked file-by-file against the session byte limit and
    refuses anything that is not a regular file, which a mutable database and
    its journal sidecars are not.
    """
    await store.bank(_builder_take())
    evidence = Path(store.evidence.bundle_dir) / "evidence"

    assert _db(store).exists()
    assert _db(store).parent == Path(store.evidence.bundle_dir)
    assert list(evidence.rglob("*.sqlite3*")) == []
