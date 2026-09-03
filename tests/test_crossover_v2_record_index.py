# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Selecting banked takes: what a rescan reads off the files, and what it skips.

``test_a_banked_take_is_findable_by`` is the pin that carries the suite: it
fails if a take the store banked stops being selectable. There is no index file
to reconcile any more (ADR-0198), so what used to be the rebuild-agrees-with-
the-files claim is now structural — the files are the only thing read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    ROUND_RECEIPT_KIND,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
)
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from tests.test_crossover_v2_record_store import (
    RELAY,
    _bundle,
    _take,
)

ARTIFACTS = "evidence/v1/artifacts"


@pytest.fixture
def store(tmp_path):
    """The production store over a real bundle — what the rescan reads."""
    info = _bundle(tmp_path)
    return BankedRecordStore(
        evidence=CommissioningEvidenceStore.open(
            info["bundle_dir"], expected_session_id=info["session_id"],
        ),
        capture_session_id=RELAY,
    )


def _found(store: BankedRecordStore, **filters: Any):
    return bundle_measurements(store.evidence.bundle_dir, **filters)


def _artifacts(store: BankedRecordStore) -> Path:
    return Path(store.evidence.bundle_dir) / ARTIFACTS


def _builder_take(**overrides: Any) -> dict[str, Any]:
    """A take as the four ``spatial`` builders bank one: with a clock on it."""
    return {**_take(), "captured_at": "2026-08-28T11:22:33Z", **overrides}


@pytest.mark.parametrize(
    "field,value",
    [("kind", MEASURE_KIND_CANDIDATE), ("position_deg", 30)],
)
async def test_a_banked_take_is_findable_by(store, field, value):
    """The point of the reader: a take found without globbing a directory.

    Both axes the reader ships — the two ``position_cycle`` was asked for.
    """
    record_id = await store.bank(_builder_take(
        kind=MEASURE_KIND_CANDIDATE, position_deg=30, candidate_id="cand-7",
    ))

    found = _found(store, **{field: value})

    assert [row.path for row in found] == [record_id]


async def test_the_candidate_axis_separates_two_variants_of_one_pose(store):
    """The axis ``jasper-measure`` banks FOR — two takes, one pose, one label apart.

    The door refuses to bank a variant take (an inverted branch, a delayed one,
    a level match) without a ``--candidate-id``, and this is why: the two takes
    below differ in nothing a reader can otherwise select on, so a filter that
    ignored the label would return both and the comparison the variant was
    measured for could not be set up at all.
    """
    wanted = await store.bank(_builder_take(
        candidate_id="null_a1", take_id="candidate_00_a00",
    ))
    await store.bank(_builder_take(
        candidate_id="null_a2", take_id="candidate_01_a00",
    ))

    found = _found(store, candidate_id="null_a1")

    assert [row.path for row in found] == [wanted]
    assert [row.candidate_id for row in found] == ["null_a1"]


async def test_a_banked_walk_pose_is_selectable_by_the_candidate_it_measured(
    store,
):
    """The cycle's label reaches the reader through the WALK's own builder.

    Two poses at one bearing, one candidate apart: a per-pose cycle is only
    worth banking if a reader can afterwards ask for one variant's takes, and
    the pose record is where that label has to survive.
    """
    def _pose_record(index: int, candidate_id: str) -> dict[str, Any]:
        pose = spatial.LateralPose(
            pose_id=f"lateral_{index:02d}", index=index, attempt=1,
            prompt="+0 deg", role="onax", offset_cm=0.0, at_mark=True,
            curves=(),
        )
        return spatial.lateral_pose_record(
            pose, position_deg=0, lateral_consumer="forward_model",
            session_id="sess-1", graph_fingerprint="fp-applied",
            captured_at="2026-08-28T11:22:33Z", wav_sha256=f"sha-{index}",
            claim=spatial.TakeClaim(candidate_id=candidate_id),
        )

    wanted = await store.bank(_pose_record(1, "fp-a"))
    await store.bank(_pose_record(2, "fp-b"))

    found = _found(store, phase=PHASE_LATERAL, candidate_id="fp-a")

    assert [row.path for row in found] == [wanted]
    assert [row.candidate_id for row in found] == ["fp-a"]


@pytest.mark.parametrize("phase", [PHASE_LATERAL, PHASE_ENTRY_BASELINE])
async def test_the_phase_axis_selects_the_takes_that_ARE_that_phase(store, phase):
    """What a take IS, beside what it MEASURES — two columns, two questions.

    Two takes differing ONLY in phase, so a filter that narrowed by kind
    instead would return both: the axis has to select one and exclude the
    other, which is what the packet's lateral and entry-baseline blocks read
    it for.
    """
    wanted = await store.bank(_builder_take(phase=phase, take_id="pose_00_a01"))
    await store.bank(_builder_take(phase=PHASE_CHECK, take_id="pose_00_a02"))

    found = _found(store, phase=phase)

    assert [row.path for row in found] == [wanted]
    assert [row.phase for row in found] == [phase]


async def test_a_read_writes_nothing_into_the_bundle(store):
    """The reader has no side effect — no table, no file, nothing.

    ``jasper-crossover-prescriber status`` is pinned to leave a banked corpus
    byte-identical, which a reader that wrote an index could not promise.
    """
    await store.bank(_builder_take())
    before = {
        p: p.stat().st_mtime_ns
        for p in Path(store.evidence.bundle_dir).rglob("*")
        if p.is_file()
    }

    assert _found(store)

    after = {
        p: p.stat().st_mtime_ns
        for p in Path(store.evidence.bundle_dir).rglob("*")
        if p.is_file()
    }
    assert after == before
    assert list(Path(store.evidence.bundle_dir).rglob("*.sqlite3*")) == []


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
async def test_the_read_clock_is_the_records_own(store, captured_at, expected):
    """Two builder types normalize to one spelling; an absent one stays absent.

    The types genuinely disagree upstream, and normalizing at the read rather
    than at the builders is what keeps the store from rewriting records it
    already wrote once.
    """
    take = _take() if captured_at is None else _builder_take(
        captured_at=captured_at,
    )

    await store.bank(take)

    assert [row.captured_at for row in _found(store)] == [expected]


async def test_an_artifact_that_is_not_a_measurement_is_not_selected(store):
    """Five of the six banked kinds are not takes, and none of them get a row."""
    await store.bank({
        "kind": ROUND_RECEIPT_KIND, "session_id": "engine-session",
        "phase": "verify",
    })

    assert _found(store) == ()


async def test_a_rescanned_nan_clock_reads_no_timestamp(store):
    """``json.loads`` accepts a bare ``NaN``, so the rescan is where it lands.

    The banked file is edited under the store rather than written through it,
    because the store is what makes this shape unbankable in the first place.
    """
    record_id = await store.bank(_builder_take())
    banked = _artifacts(store) / record_id
    document = json.loads(banked.read_text())
    document["captured_at"] = float("nan")
    banked.write_text(json.dumps(document))

    assert [row.captured_at for row in _found(store)] == [None]


async def test_a_file_the_rescan_cannot_parse_costs_only_itself(store):
    """One unreadable take is not a reason to refuse the rest of the corpus."""
    good = await store.bank(_builder_take(take_id="pose_00_a01"))
    broken = await store.bank(_builder_take(take_id="pose_00_a02"))
    (_artifacts(store) / broken).write_text("{not json at all")

    assert [row.path for row in _found(store)] == [good]
