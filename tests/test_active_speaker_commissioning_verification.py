# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The room-authority reader, driven against real on-disk evidence.

Every other test of this reader monkeypatches it, so which branch a real
receipt file lands in has never been pinned. That matters most for the branch
this file was added with: a receipt an older JTS minted must read as its own
disclosure, not as damaged bytes.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from jasper.active_speaker import _common
from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreErrorCode,
)
from jasper.active_speaker.commissioning_lifecycle import CommissioningTransition
from jasper.active_speaker.commissioning_receipt import (
    RECEIPT_KIND,
    RECEIPT_SCHEMA_VERSION,
)
from jasper.active_speaker.commissioning_run import (
    CommissioningRunHandle,
    CommissioningRunStore,
)
from jasper.active_speaker.commissioning_verification import (
    _artifact_relative_path,
    _bundle_forensics,
    read_commissioning_room_authority,
    receipt_source_path,
)
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology

# Reaching `verified` needs the whole ladder; the evidence fingerprints are
# synthetic because `transition` correlates the handle, never the artifact.
_LADDER = (
    ("unconfigured", "protected", "protection_evidence"),
    ("protected", "measured", "admitted_measurement_set"),
    ("measured", "candidate_ready", "candidate_artifact"),
    ("candidate_ready", "applied_unverified", "applied_candidate_proof"),
    ("applied_unverified", "verified", "commissioning_eligibility_receipt"),
)


VerifiedRun = tuple[
    OutputTopology, Path, Path, CommissioningEvidenceStore, CommissioningRunHandle
]


def _hash(char: str) -> str:
    return char * 64


@pytest.fixture
def verified_run(tmp_path: Path) -> VerifiedRun:
    topology = mono_output_topology(mode="active_2_way")
    sessions_root = tmp_path / "sessions"
    info = open_bundle(
        topology,
        calibration_id="verification-test-calibration",
        sessions_dir=sessions_root,
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    run_state_path = tmp_path / "run.json"
    run_store = CommissioningRunStore(path=run_state_path, owner_id="1" * 32)
    run = run_store.start(
        session_id=store.session_id,
        session_fingerprint=_hash("a"),
    )
    for index, (from_state, to_state, evidence_kind) in enumerate(_LADDER):
        assert run_store.transition(
            run,
            CommissioningTransition(
                from_state=from_state,
                to_state=to_state,
                evidence_kind=evidence_kind,
                evidence_fingerprint=_hash(f"{index}"),
            ),
        )
    assert run_store.lifecycle_state(run) == "verified"
    return topology, run_state_path, sessions_root, store, run


def _authority(
    topology: OutputTopology, run_state_path: Path, sessions_root: Path
) -> dict[str, object]:
    return read_commissioning_room_authority(
        topology,
        run_state_path=run_state_path,
        sessions_root=sessions_root,
    )


def test_verified_lifecycle_without_a_receipt_reads_as_absent(
    verified_run: VerifiedRun,
) -> None:
    topology, run_state_path, sessions_root, _store, _run = verified_run

    result = _authority(topology, run_state_path, sessions_root)

    assert result["allowed"] is False
    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_ABSENT
    assert result["receipt_fingerprint"] is None


def test_a_receipt_an_older_jts_minted_reads_as_superseded_not_damaged(
    verified_run: VerifiedRun,
) -> None:
    """The migration posture, stated as behavior.

    A schema bump makes every older receipt unparseable by the strict reader,
    and "unparseable" would otherwise reach the household as *malformed* —
    copy that tells someone their evidence is damaged when in fact their JTS
    grew a field. Nothing about the speaker changed, so nothing is refused
    (ruling S10) and the remedy is one unhurried re-run.
    """
    topology, run_state_path, sessions_root, store, run = verified_run
    store.publish_json_artifact(
        receipt_source_path(run),
        {
            "schema_version": RECEIPT_SCHEMA_VERSION - 1,
            "kind": RECEIPT_KIND,
            "fingerprint": _hash("b"),
        },
    )

    result = _authority(topology, run_state_path, sessions_root)

    assert result["allowed"] is False
    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_SUPERSEDED
    assert result["receipt_fingerprint"] is None


def test_bytes_that_are_not_a_receipt_still_read_as_malformed(
    verified_run: VerifiedRun,
) -> None:
    """The superseded branch narrows malformed; it must not swallow it."""

    topology, run_state_path, sessions_root, store, run = verified_run
    store.publish_json_artifact(
        receipt_source_path(run),
        {"schema_version": RECEIPT_SCHEMA_VERSION, "kind": "something_else"},
    )

    result = _authority(topology, run_state_path, sessions_root)

    assert result["allowed"] is False
    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_MALFORMED


def test_a_current_schema_receipt_that_will_not_parse_is_malformed(
    verified_run: VerifiedRun,
) -> None:
    """A receipt claiming THIS schema is held to it — no leniency leak."""

    topology, run_state_path, sessions_root, store, run = verified_run
    store.publish_json_artifact(
        receipt_source_path(run),
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "kind": RECEIPT_KIND,
            "fingerprint": _hash("c"),
        },
    )

    result = _authority(topology, run_state_path, sessions_root)

    assert result["allowed"] is False
    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_MALFORMED


def test_receipt_provenance_reads_the_bundle_forensics_it_records(
    verified_run: VerifiedRun,
) -> None:
    """Pinned here rather than through the mint, which nothing can reach yet.

    `_receipt()` is the receipt's only writer and needs three post-apply
    repeats that nothing writes, so the one new step in it — reading the
    session's own forensic fields — has no reachable caller to pin it through
    until wave 4g's producer half lands.
    """

    _topology, _run_state_path, _sessions_root, store, _run = verified_run
    recorded = json.loads((store.bundle_dir / "info.json").read_text())["fingerprints"]

    assert _bundle_forensics(store.bundle_dir) == (
        recorded["build_sha"] or None,
        recorded["mic"]["calibration_id"] or None,
        recorded["mic"]["calibration_sha256"] or None,
    )
    assert recorded["mic"]["calibration_id"] == "verification-test-calibration"


def test_unreadable_bundle_forensics_never_stop_a_receipt(tmp_path: Path) -> None:
    """Ruling S10: a forensic mirror that will not open is not a blocker."""

    assert _bundle_forensics(tmp_path / "not-a-bundle") == (None, None, None)


def _unconfigured_record(tmp_path: Path) -> Path:
    """A valid, schema-clean record at lifecycle `unconfigured`."""

    run_state_path = tmp_path / "run.json"
    store = CommissioningRunStore(path=run_state_path, owner_id="1" * 32)
    run = store.start(session_id="549d1cd82c91", session_fingerprint=_hash("a"))
    assert store.lifecycle_state(run) == "unconfigured"
    return run_state_path


def test_a_never_commissioned_run_record_reads_as_absent_not_malformed(
    tmp_path: Path,
) -> None:
    """The state most speakers are in is a true state, not a damaged one."""

    result = read_commissioning_room_authority(
        mono_output_topology(mode="active_2_way"),
        run_state_path=_unconfigured_record(tmp_path),
        sessions_root=tmp_path / "sessions",
    )

    assert result["allowed"] is False
    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_ABSENT


def test_a_status_read_does_not_need_a_lock_it_cannot_open(
    tmp_path: Path,
) -> None:
    """The mechanism fix, as behavior.

    A root-run status poll used to CREATE this record's sibling lock as
    root:root, after which no service account could open it and every later
    read failed. The record is published atomically, so a read takes no lock
    and an unopenable one cannot deny it. See ADR-0196.
    """

    run_state_path = _unconfigured_record(tmp_path)
    lock_path = run_state_path.with_name(f".{run_state_path.name}.lock")
    if lock_path.exists():
        lock_path.unlink()
    lock_path.mkdir()

    result = read_commissioning_room_authority(
        mono_output_topology(mode="active_2_way"),
        run_state_path=run_state_path,
        sessions_root=tmp_path / "sessions",
    )

    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_ABSENT


def _unopenable_record(run_state_path: Path) -> None:
    run_state_path.mkdir()


def _unparseable_record(run_state_path: Path) -> None:
    run_state_path.write_text('{"current": {', encoding="utf-8")


@pytest.mark.parametrize(
    ("break_record", "expected"),
    [
        pytest.param(
            _unopenable_record,
            _common.ROOM_AUTHORITY_RECEIPT_UNREADABLE,
            id="record_cannot_be_opened",
        ),
        pytest.param(
            _unparseable_record,
            _common.ROOM_AUTHORITY_RECEIPT_MALFORMED,
            id="record_will_not_parse",
        ),
    ],
)
def test_a_record_that_cannot_be_opened_is_not_a_record_that_will_not_parse(
    tmp_path: Path,
    break_record,
    expected: str,
) -> None:
    """Two different facts, two different remedies.

    The failures here are built from a directory rather than a mode bit so the
    test means the same thing when it runs as root.
    """

    run_state_path = tmp_path / "run.json"
    break_record(run_state_path)

    result = read_commissioning_room_authority(
        mono_output_topology(mode="active_2_way"),
        run_state_path=run_state_path,
        sessions_root=tmp_path / "sessions",
    )

    assert result["allowed"] is False
    assert result["reason"] == expected


def test_a_broken_evidence_bundle_names_the_store_code_that_refused_it(
    verified_run: VerifiedRun,
) -> None:
    """The disclosure carries the store's own structured verdict.

    Every bundle fault — a deleted session directory, an `info.json` that will
    not open — reaches this reader as the store's authority verdict rather than
    as the underlying errno, so the answer is read from that code and the code
    rides the denial. Without it the operator has a class name and no file.
    """

    topology, run_state_path, sessions_root, store, _run = verified_run
    shutil.rmtree(store.bundle_dir)

    result = _authority(topology, run_state_path, sessions_root)

    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_MALFORMED
    assert result["cause"] == CommissioningEvidenceStoreErrorCode.WRONG_AUTHORITY


def test_a_substituted_receipt_artifact_is_a_record_defect(
    verified_run: VerifiedRun,
) -> None:
    """A symlink in place of the receipt is not a file-access problem.

    The store opens artifacts `O_NOFOLLOW` and answers `NOT_REGULAR`; the
    remedy is a fresh mint, so it must not reach the household as the class
    that says nothing is wrong with the record.
    """

    topology, run_state_path, sessions_root, store, run = verified_run
    store.publish_json_artifact(
        receipt_source_path(run),
        {"schema_version": RECEIPT_SCHEMA_VERSION, "kind": RECEIPT_KIND},
    )
    artifact = store.bundle_dir / _artifact_relative_path(receipt_source_path(run))
    body = artifact.read_bytes()
    decoy = artifact.with_name("decoy.json")
    decoy.write_bytes(body)
    artifact.unlink()
    artifact.symlink_to(decoy)

    result = _authority(topology, run_state_path, sessions_root)

    assert result["reason"] == _common.ROOM_AUTHORITY_RECEIPT_MALFORMED


def test_two_reads_of_one_denial_disclose_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A denial is the steady state for most speakers; the change is the event."""

    run_state_path = _unconfigured_record(tmp_path)
    kwargs = {
        "run_state_path": run_state_path,
        "sessions_root": tmp_path / "sessions",
    }
    topology = mono_output_topology(mode="active_2_way")
    with caplog.at_level(logging.WARNING):
        read_commissioning_room_authority(topology, **kwargs)
        read_commissioning_room_authority(topology, **kwargs)

    assert sum(
        1 for record in caplog.records
        if "commissioning_receipt_unvouched" in record.getMessage()
    ) == 1
