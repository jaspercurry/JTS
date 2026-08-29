# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The production ``RecordStore``: what it writes, and what reads it back.

The round-trip pins run against BOTH implementations — ``FakeRecords`` from
the engine twin and the real store — because the twin is what every engine
test states its "before" through, and two banks that disagree about what
``read`` returns would make a green suite mean nothing about the real one.

The acceptance pin is :func:`test_prior_bank_rebuilds_over_the_store`: bank a
walk, ``persist``, drop the session, and rebuild a ``PriorBank`` over the
store. It is the only pin that catches a ``bank`` returning a useless id,
which empties ``record_ids`` and silently leaves a candidate check with no
"before" at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.candidate_bank import CANDIDATE_ARTIFACT_GLOB
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
    CommissioningEvidenceStoreErrorCode,
)
from jasper.active_speaker.crossover_v2 import position_cycle
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    ROUND_RECEIPT_KIND,
)
from jasper.active_speaker.crossover_v2.evidence_packet import round_artifact_dir
from jasper.active_speaker.crossover_v2.prior_bank import CapturePose, PriorBank
from jasper.active_speaker.crossover_v2.record_store import (
    CHECK_EVIDENCE_KIND,
    CLOUD_EVIDENCE_KIND,
    BankedRecordStore,
)
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    MeasuredCrossoverCandidateError,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.attribution.findings import FindingSet
from jasper.attribution.session_identity import (
    SESSION_IDENTITY_KEY,
    SessionIdentity,
)
from jasper.web import correction_crossover_v2 as host
from tests.active_speaker_fixtures import mono_output_topology
from tests.engine_twin import FakeRecords
from tests.test_active_speaker_profile import _two_way_preset

RELAY = "cap-relay-1"


def _bundle(tmp_path: Path) -> Mapping[str, Any]:
    info = open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    return info


@pytest.fixture
def real_store(tmp_path):
    """The production store over a real bundle and a temp state file."""
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


@pytest.fixture(params=["twin", "real"])
def store(request, real_store):
    """Both implementations of the seam, so they cannot drift apart."""
    return FakeRecords() if request.param == "twin" else real_store


def _take(
    *,
    kind: str = MEASURE_KIND_BASELINE,
    position_deg: int | None = 0,
    attempt: int = 1,
) -> dict[str, Any]:
    """One capture record, as the engine banks one.

    Not a mirror of ``session.py:_record``'s literal return — a store test
    that copied another module's dict would be a second copy of it, and would
    rot the first time that one grew a field. What matters here is what the
    STORE reads: the ``take_id`` every banked record owes (W1-a's bank-id
    ruling), the ``kind`` that routes it, and enough of the rest to prove the
    round trip is field-for-field.
    """
    return {
        "session_id": "engine-session",
        "kind": kind,
        "take_id": f"pose_{0 if position_deg is None else position_deg:02d}"
                   f"_a{attempt:02d}",
        "baseline_record_id": "",
        "position_deg": position_deg,
        "position_axis": "lateral",
        "prompt": "stand at the mark",
        "candidate_id": "",
        "regime": "reference_axis",
        "polarity": "normal",
        "graph_fingerprint": "graph-abc",
        "level_db": -18.0,
        "stimulus_dbfs": -12.0,
        "incident": "",
    }


def _as_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _banked_file(store: BankedRecordStore, record_id: str) -> dict[str, Any]:
    """The bytes on disk, read without going through the store's own reader."""
    path = Path(store.evidence.bundle_dir) / "evidence/v1/artifacts" / record_id
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# P1-P4 — the contract both implementations owe
# --------------------------------------------------------------------------- #


async def test_bank_then_read_round_trips(store):
    """P1: what comes back out is what went in, field for field."""
    record = _take()

    record_id = await store.bank(record)

    assert record_id
    assert await store.read(record_id) == record


async def test_read_of_an_unknown_id_is_none(store):
    """P2: a missing record is a fact ``analyze`` discloses, not a raise."""
    assert await store.read("crossover_v2/other/positions/nope_a01.json") is None


#: What the analyze seam carries onto a banked take, with a real value each.
#: The blocks themselves are ``correction_crossover_v2._capture_evidence_blocks``'
#: subject; here they are three nested mappings the store has to carry
#: whole — a shape it had never been handed before, since every other field on
#: a take record is a scalar.
_EVIDENCE_BLOCKS = {
    "diagnostic": {
        "phase": "measure", "epsilon_ppm": 1.5, "frames_received": 48000,
    },
    "capture_integrity": {
        "frames": 48000, "encoded_frames": 48000, "block_gaps": 0,
    },
    "frame_ledger": {
        "received_frames": 48000, "declared_frames": 48000,
        "encoded_frames": 48000, "render_gaps": 0, "render_gap_frames": 0,
        "lost_at": [],
    },
}


@pytest.mark.parametrize("block", sorted(_EVIDENCE_BLOCKS))
async def test_a_take_carries_its_capture_evidence_block(store, block):
    """The additive half of the reader flip: the blocks BANK.

    Since the capture-dump ring died, ``diagnostic``, ``capture_integrity``
    and ``frame_ledger`` are computed at the analyze seam and land in no file
    at all — the banked record is the only retention path left, so it has to
    carry them or a round banked from here on cannot be graded on them.

    Pinned at the store because the store is what a nested mapping has to
    survive: every other field on a take record is a scalar, and canonical
    JSON plus the envelope strip is where a whole sub-document would be
    flattened, reordered or dropped without anything upstream noticing.
    Round-tripped field for field, and asserted against the bytes so a reader
    opening the file — not this store — sees the same block.
    """
    record = {**_take(), block: _EVIDENCE_BLOCKS[block]}

    record_id = await store.bank(record)

    assert await store.read(record_id) == record


async def test_a_take_with_no_evidence_blocks_stays_exactly_as_readable(store):
    """Additive, not required: a record from before the carry still reads.

    Every take banked before this change carries none of the three, and the
    engine's own ``_record`` still banks none — it names its capture at PLAY
    time, before any analysis exists. Neither is a defect and neither may
    become one, so the absence is pinned rather than left to the round-trip
    pin's silence.
    """
    record_id = await store.bank(_take())

    read_back = await store.read(record_id)

    assert read_back is not None
    assert not set(read_back) & set(_EVIDENCE_BLOCKS)


async def test_a_banked_block_is_in_the_file_and_still_indexes(real_store):
    """The two things only the REAL store can be asked about the blocks.

    That they reach the bytes on disk whole — the store's canonical JSON is
    where a nested mapping would be flattened — and that a take carrying them
    is still a take: the measurement index reads six named columns off the
    file, so three new top-level keys must move no row.
    """
    from jasper.active_speaker.crossover_v2.record_index import (
        find_measurements,
        index_path,
    )

    record_id = await real_store.bank({**_take(), **_EVIDENCE_BLOCKS})

    banked = _banked_file(real_store, record_id)
    for name, block in _EVIDENCE_BLOCKS.items():
        assert banked[name] == block
    found = find_measurements(index_path(real_store.evidence.bundle_dir))
    assert [row.path for row in found] == [record_id]


async def test_persist_then_read_state_round_trips(store):
    """P4: the five keys ``save`` writes come back as they were written."""
    state = {
        "session_id": "engine-session",
        "graph_fingerprint": "graph-abc",
        "measurement_level_db": -18.0,
        "record_ids": ("a", "b"),
        "disclosures": ({"code": "mic_only_regime", "captured": False},),
    }

    state_id = await store.persist(state)
    read_back = await store.read_state(state_id)

    assert read_back is not None
    # Compared through JSON on both sides: the state crosses a file in
    # production, so a tuple coming back as a list is the contract holding,
    # not the twin and the real store disagreeing.
    for key, value in state.items():
        assert _as_json(read_back[key]) == _as_json(value)


async def test_prior_bank_rebuilds_over_the_store(store):
    """P8: bank a walk, ``save`` it, drop the session, read the bank back.

    The acceptance pin. A ``bank`` that returns a useless id passes every
    other pin here and fails this one, because ``session.py`` drops falsy ids
    out of ``record_ids`` and ``PriorBank`` is left with nothing to pair.
    """
    ids = [
        await store.bank(_take(position_deg=degrees))
        for degrees in (0, 30)
    ]
    ids.append(await store.bank(_take(kind=MEASURE_KIND_CANDIDATE, attempt=2)))
    state_id = await store.persist({
        "session_id": "engine-session",
        "graph_fingerprint": "graph-abc",
        "measurement_level_db": -18.0,
        "record_ids": tuple(identifier for identifier in ids if identifier),
        "disclosures": (),
    })

    prior = await PriorBank.read(store, state_id)

    # Asserted before the baselines, and load-bearing: a ``bank`` that returns
    # ``""`` drops every id out of ``record_ids``, and a baseline compared
    # against that same ``""`` would agree with itself and pass.
    assert all(ids)
    assert prior is not None
    assert prior.record_ids == tuple(ids)
    assert prior.measurement_level_db == -18.0
    assert prior.baseline_for(
        CapturePose(position_axis="lateral", position_deg=0, stimulus_dbfs=-12.0)
    ) == ids[0]
    assert prior.baseline_for(
        CapturePose(position_axis="lateral", position_deg=30, stimulus_dbfs=-12.0)
    ) == ids[1]


# --------------------------------------------------------------------------- #
# P3, P5-P7 — what only the real store can be asked
# --------------------------------------------------------------------------- #


async def test_read_state_of_an_outlived_id_is_none(real_store):
    """P3: the file is overwritten every persist, so an id can outlive it."""
    base = {
        "session_id": "engine-session",
        "graph_fingerprint": "graph-abc",
        "measurement_level_db": -18.0,
        "disclosures": (),
    }

    first = await real_store.persist({**base, "record_ids": ("a",)})
    second = await real_store.persist({**base, "record_ids": ("a", "b")})

    assert first != second
    assert await real_store.read_state(first) is None
    assert await real_store.read_state(second) is not None


async def test_two_persists_that_banked_nothing_are_still_two(real_store):
    """F7: the id counts PERSISTS, not the records the state accounts for.

    A walk can persist twice with nothing new banked between them — a capture
    that did not play, a save after a refusal — and an id derived from the
    state's own contents would be the same both times, handing back the newer
    document under the older id. That is the one thing this id detects.
    """
    state = {
        "session_id": "engine-session",
        "graph_fingerprint": "graph-abc",
        "measurement_level_db": -18.0,
        "record_ids": ("a",),
        "disclosures": (),
    }

    first = await real_store.persist(state)
    second = await real_store.persist(state)

    assert first != second
    assert await real_store.read_state(first) is None
    assert await real_store.read_state(second) is not None


async def test_the_banked_kind_is_the_readers_discriminator(real_store):
    """P5: the file names the artifact kind ``position_cycle`` accepts.

    Not the record's own measurement kind, which asks a different question and
    rides beside it — ``PriorBank`` selects on ``baseline``/``candidate``/
    ``verify`` while every bundle reader accepts a file only when its ``kind``
    is the position-evidence discriminator.
    """
    record_id = await real_store.bank(_take())

    banked = _banked_file(real_store, record_id)

    assert banked["kind"] == position_cycle.POSITION_EVIDENCE_KIND
    assert (await real_store.read(record_id))["kind"] == MEASURE_KIND_BASELINE


async def test_an_identical_re_bank_is_idempotent(real_store):
    """P6: the same bytes at the same path is a re-publish, not a conflict."""
    record = _take()

    first = await real_store.bank(record)
    second = await real_store.bank(record)

    assert first == second


async def test_different_bytes_at_one_path_refuse(real_store):
    """The other half of write-once: the store stays strict, and propagates."""
    await real_store.bank(_take())

    with pytest.raises(CommissioningEvidenceStoreError) as raised:
        await real_store.bank(_take(position_deg=None))

    assert raised.value.code == (
        CommissioningEvidenceStoreErrorCode.PATH_CONFLICT
    )


async def test_the_record_lands_under_the_relay_id(real_store):
    """P7: the directory keys on the RELAY session, not the record's own.

    ``round_artifact_dir`` reports that directory's name AS the relay id, so a
    store minting it from ``record["session_id"]`` files the record where no
    reader looks.
    """
    record_id = await real_store.bank(_take())

    assert record_id.startswith(f"crossover_v2/{RELAY}/positions/")
    found, why = round_artifact_dir(Path(real_store.evidence.bundle_dir))
    assert found is not None, why
    assert found.name == RELAY
    assert (found / "positions" / "pose_00_a01.json").is_file()


# --------------------------------------------------------------------------- #
# the fold — the five publishers' artifact kinds, on one seam
# --------------------------------------------------------------------------- #


def _candidate() -> MeasuredCrossoverCandidate:
    """A candidate that survives its own ``from_mapping``, which the store runs."""
    return MeasuredCrossoverCandidate(
        program_id="prog-abc123",
        analysis={"drift_ppm": 12.5},
        source_preset=ActiveSpeakerPreset.from_mapping(_two_way_preset()),
        role_attenuations_db={"woofer": 0.0, "tweeter": -13.0},
    )


def _finding_set() -> dict[str, Any]:
    """A finding set with the phase the CALLER injects for the route."""
    return {
        **FindingSet(
            session=SessionIdentity(session_id="bundle-1"),
            produced_by="test",
            findings=(),
        ).to_dict(),
        "phase": "cloud_measure",
    }


def _fold_records() -> list[tuple[str, dict[str, Any], str]]:
    """All SIX routes: the five publishers' kinds plus the position take.

    Parametrized together and not sampled, because the route table is where a
    kind gets forgotten — the findings route shipped with a ``phase`` nothing
    supplied, and only three of six routes had a pin to catch it.
    """
    return [
        ("position", _take(), f"positions/{_take()['take_id']}.json"),
        (
            "check",
            {"kind": CHECK_EVIDENCE_KIND, "gain_plan_db": {"woofer": -6.0}},
            "check.json",
        ),
        (
            "cloud",
            {"kind": CLOUD_EVIDENCE_KIND, "phase": "cloud_measure", "ripple_db": 3.0},
            "cloud_measure.json",
        ),
        ("candidate", _candidate().to_dict(), "candidate.json"),
        (
            "receipt",
            {"schema_version": 2, "kind": ROUND_RECEIPT_KIND, "round_id": "r1"},
            "round_receipt.json",
        ),
        ("findings", _finding_set(), "findings_cloud_measure.json"),
    ]


@pytest.mark.parametrize(
    "record, filename",
    [pytest.param(r, f, id=name) for name, r, f in _fold_records()],
)
async def test_a_folded_kind_lands_where_its_reader_looks(
    real_store, record, filename,
):
    """Every folded kind lands at the shipped publisher's path, and reads back.

    Whether the store envelopes differs per route and is not a style choice:
    ``MeasuredCrossoverCandidate.from_mapping`` refuses any key it does not
    know, so wrapping a candidate the way a position take is wrapped would
    make the file unreadable by its own reader.
    """
    record_id = await real_store.bank(record)

    assert record_id == f"crossover_v2/{RELAY}/{filename}"
    assert await real_store.read(record_id) == record

    found, why = round_artifact_dir(Path(real_store.evidence.bundle_dir))
    assert found is not None, why
    assert (found / filename).is_file()


async def test_a_banked_cloud_result_carries_the_session_identity(real_store):
    """F1: the cloud payload's BYTES, not only its path.

    ``publish_cloud`` stamps the session identity so a finding can cite the
    artifact across two id namespaces. The store is the writer now, so the
    stamp is the store's — and ``read`` takes it back off, because the caller
    never put it there.
    """
    record = {
        "kind": CLOUD_EVIDENCE_KIND, "phase": "cloud_measure", "ripple_db": 3.0,
    }

    record_id = await real_store.bank(record)

    stamped = _banked_file(real_store, record_id)[SESSION_IDENTITY_KEY]
    assert stamped["session_id"] == real_store.evidence.session_id
    assert RELAY in stamped["aliases"].values()
    assert await real_store.read(record_id) == record


async def test_a_banked_candidate_is_where_candidate_bank_globs(real_store):
    """The bank's own glob, run over the tree the store wrote."""
    record_id = await real_store.bank(_candidate().to_dict())

    sessions_root = Path(real_store.evidence.bundle_dir).parent

    assert [str(path) for path in sessions_root.glob(CANDIDATE_ARTIFACT_GLOB)] == [
        str(Path(real_store.evidence.bundle_dir)
            / "evidence/v1/artifacts" / record_id)
    ]


async def test_a_builder_shaped_record_routes_on_its_measure_kind(real_store):
    """B1: the measurement kind arrives under either spelling.

    The engine hands it as ``kind``; a record built by ``spatial``'s builders
    hands it as ``measure_kind`` and leaves the artifact kind to the store. A
    store that read only one spelling would refuse half its producers.
    """
    record = {**_take(), "measure_kind": MEASURE_KIND_BASELINE}
    del record["kind"]

    record_id = await real_store.bank(record)

    assert record_id == f"crossover_v2/{RELAY}/positions/{record['take_id']}.json"
    banked = _banked_file(real_store, record_id)
    assert banked["kind"] == position_cycle.POSITION_EVIDENCE_KIND
    assert banked["measure_kind"] == MEASURE_KIND_BASELINE
    assert (await real_store.read(record_id))["kind"] == MEASURE_KIND_BASELINE


async def test_a_take_whose_measure_kind_is_unresolved_still_banks(real_store):
    """An UNRESOLVED measurement kind is a value, not a missing key.

    ``spatial.take_kind`` returns ``""`` for a take whose graph names neither
    fingerprint — *"unresolvable is `""`, never a guess"* — so the builders
    are contracted to emit ``measure_kind: ""`` and a store that tested the
    value's truthiness rather than the key's presence would refuse a record
    they are required to produce. Caught by banking a real builder record
    through this store, not by either half's own suite.
    """
    record = {**_take(), "measure_kind": ""}
    del record["kind"]

    record_id = await real_store.bank(record)

    assert record_id == f"crossover_v2/{RELAY}/positions/{record['take_id']}.json"
    banked = _banked_file(real_store, record_id)
    assert banked["kind"] == position_cycle.POSITION_EVIDENCE_KIND
    assert banked["measure_kind"] == ""
    assert (await real_store.read(record_id))["kind"] == ""


async def test_a_candidate_that_changed_on_readback_refuses(real_store):
    """F4: the apply path's tamper check, carried into the store.

    A candidate that cannot survive exact reopen never becomes reviewable.
    Strictness belongs here; only fail-soft stays at the caller.

    ``from_mapping`` is the half this reaches — it re-derives the fingerprint
    from the persisted content and refuses a file that disagrees with itself.
    The store's own fingerprint compare behind it is the shipped publisher's
    second line, and it is near-unreachable while the path is write-once: a
    reopen that returned a DIFFERENT valid candidate would have been a
    ``PATH_CONFLICT`` first. Carried anyway, because dropping a shipped guard
    is not this PR's call to make.
    """
    with pytest.raises(MeasuredCrossoverCandidateError):
        await real_store.bank({**_candidate().to_dict(), "fingerprint": "not-mine"})


async def test_a_receipt_that_changed_on_readback_refuses(real_store):
    """F4's other half — R21's accept-receipt pattern, kept by the fold."""
    receipt = {
        "schema_version": 2,
        "kind": ROUND_RECEIPT_KIND,
        # A tuple is not what comes back out of canonical JSON, which is
        # exactly the class of change the guard exists to catch.
        "evidence_identities": ("a", "b"),
    }

    with pytest.raises(RuntimeError, match="changed on exact readback"):
        await real_store.bank(receipt)


async def test_a_record_carrying_a_store_owned_key_refuses(real_store):
    """F2: the strip is only exact while the store is the sole author.

    ``read`` takes ``schema_version``/``relay_session_id`` back off because
    ``bank`` put them on. A record that arrived carrying one would lose it
    silently, so it is refused instead.
    """
    with pytest.raises(ValueError, match="must not carry"):
        await real_store.bank({**_take(), "relay_session_id": "someone-else"})


async def test_a_take_without_an_id_is_refused(real_store):
    """The bank-id ruling: every banked record carries its own ``take_id``.

    Minted by the producer — through ``spatial.take_id_for`` where a prompted
    position exists — never re-derived here. A geometry retake reuses its
    position id, so a store that guessed would collide two takes on one
    write-once path.
    """
    record = _take()
    del record["take_id"]

    with pytest.raises(ValueError, match="take_id"):
        await real_store.bank(record)


async def test_an_unroutable_record_is_refused(real_store):
    """Strict, and loud: a kind with no place to land is a defect, not a drop."""
    with pytest.raises(ValueError):
        await real_store.bank({"kind": "jts_not_a_banked_artifact"})
