# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WO-1 storage contracts: bundle-lifetime retention.

Pins the acceptance items ``docs/historical/attribution-stage-plan.md`` §7 assigns to
WO-1 that are about *storage* — the Q-C retention model and provenance-marker
discipline — plus the flow seam that produces them. The schema itself is pinned
in ``tests/test_attribution_findings.py``.
"""

from __future__ import annotations

from jasper.active_speaker.crossover_v2 import durable_state as v2durable
from jasper.web import correction_crossover_v2_evidence as v2evidence

from tests.engine_twin import retained_take_writer

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from jasper.active_speaker.bundles import (
    BUNDLE_FILE_MODE,
    CAPTURE_KIND_SEQUENTIAL,
    enforce_retention,
    open_bundle,
)
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.attribution.findings import (
    EVIDENCE_STORE_BUNDLE,
    EvidenceRef,
    Finding,
    FindingSet,
)
from jasper.attribution.promotion import PRODUCED_BY
from jasper.attribution.session_identity import (
    SessionIdentity,
    read_session_identity,
)
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from jasper.attribution.storage import (
    FindingEvidenceMissing,
    FindingStorageError,
    bundle_evidence_ref,
    findings_relative_path,
    read_finding_set,
)
from tests.active_speaker_fixtures import mono_output_topology

CAPTURE = "cap_Ktm3xQ2p"
PHASE = "cloud_measure"


def _open_store(tmp_path: Path) -> tuple[CommissioningEvidenceStore, Path]:
    info = open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"]
    )
    return store, Path(info["bundle_dir"])


def _publish_finding_set(
    store: CommissioningEvidenceStore, finding_set: FindingSet
) -> None:
    """Bank one phase's findings the way the production seam does.

    ``phase`` rides the record to route it and the route takes it back off, so
    the file is exactly ``FindingSet.to_dict()`` (ADR-0227 §12).
    """

    asyncio.run(
        BankedRecordStore(evidence=store, capture_session_id=CAPTURE).bank(
            {**finding_set.to_dict(), "phase": PHASE}
        )
    )


def _seed_evidence(store: CommissioningEvidenceStore) -> tuple[SessionIdentity, EvidenceRef]:
    """Publish a stand-in cloud artifact and cite it, as the live seam does."""

    identity = SessionIdentity(session_id=store.session_id)
    artifact = store.publish_json_artifact(
        f"crossover_v2/{CAPTURE}/{PHASE}.json",
        {"schema_version": 1, "kind": "jts_crossover_v2_cloud_evidence"},
    )
    return identity, bundle_evidence_ref(artifact, identity)


def _finding(cite: EvidenceRef) -> Finding:
    return Finding(
        mechanism="M2",
        band_hz=(4200.0, 4600.0),
        evidence={"tau_us": 310.0},
        confidence="unsure",
        fix_class="carve",
        household_copy=(
            "A narrow range here cancels rather than plays quietly, and "
            "adding level cannot fill a cancellation."
        ),
        probes_run=("P2",),
        probes_recommended=("P4",),
        cites=(cite,),
    )


# --------------------------------------------------------------------------- #
# Q-C — bundle-lifetime retention (§7 acceptance)
# --------------------------------------------------------------------------- #


def test_a_finding_round_trips_through_the_real_evidence_store(tmp_path: Path) -> None:
    store, _ = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    built = FindingSet(
        session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
    )

    _publish_finding_set(store, built)
    reopened = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)

    assert reopened == built


def test_a_finding_does_not_outlive_the_bundle_that_holds_its_evidence(
    tmp_path: Path,
) -> None:
    """Q-C, ruled by the owner 2026-07-29 night: "A finding lives inside the
    session bundle whose evidence it cites and dies with it."

    Retention evicts a bundle with ``shutil.rmtree`` — the whole directory,
    findings and evidence together — so this half needs no retention code at
    all. That is exactly why it is pinned: a guarantee that holds only
    because of the storage shape is the kind that quietly stops holding when
    someone adds a per-file pruner.
    """

    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    _publish_finding_set(
        store,
        FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )
    findings_path = bundle_dir / "evidence/v1/artifacts" / findings_relative_path(
        CAPTURE, PHASE
    )
    assert findings_path.is_file()

    shutil.rmtree(bundle_dir)

    assert not findings_path.exists()
    # And the evidence it cited is gone in the same act — neither can survive
    # the other.
    assert not (bundle_dir / cite.locator).exists()


def test_bundle_eviction_takes_findings_and_evidence_together(
    tmp_path: Path,
) -> None:
    """The same rule through the real retention path rather than a hand
    ``rmtree``: ``enforce_retention`` is a two-axis ring (count and bytes) and
    deletes whole bundles, so there is no tier that could keep a finding
    whose evidence was pruned."""

    sessions = tmp_path / "sessions"
    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    _publish_finding_set(
        store,
        FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )
    # A newer bundle, so the one under test is no longer retention-protected.
    open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=sessions,
    )

    enforce_retention(sessions, max_bundles=1, max_bytes=10**9)

    assert not bundle_dir.exists()


def test_a_finding_may_not_silently_predecease_its_evidence(
    tmp_path: Path,
) -> None:
    """The loud half of the same rule. Nothing prunes *inside* a living
    bundle today, but a finding that cited a vanished artifact would be the
    more dangerous failure — a diagnosis that still reads fine and rests on
    nothing. Reading must say so, not return the finding."""

    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    _publish_finding_set(
        store,
        FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )

    (bundle_dir / cite.locator).unlink()

    with pytest.raises(FindingEvidenceMissing, match="no longer produce"):
        read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    # An explicit opt-out still exists for a caller that wants the record
    # without its support — but it has to ask.
    assert read_finding_set(
        store, capture_session_id=CAPTURE, phase=PHASE, verify_evidence=False
    ) is not None


def test_altered_evidence_is_as_loud_as_missing_evidence(tmp_path: Path) -> None:
    """The digest is the verifier. Evidence that is present but no longer the
    bytes the finding cited must not read as support."""

    store, _ = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    tampered = EvidenceRef(
        session=identity,
        store=EVIDENCE_STORE_BUNDLE,
        locator=cite.locator,
        sha256="b" * 64,
    )
    _publish_finding_set(
        store,
        FindingSet(
            session=identity,
            produced_by=PRODUCED_BY,
            findings=(_finding(tampered),),
        ),
    )

    with pytest.raises(FindingEvidenceMissing, match="but the bundle now holds"):
        read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)


# --------------------------------------------------------------------------- #
# Provenance marker discipline (§7 acceptance)
# --------------------------------------------------------------------------- #


def test_a_bundle_written_before_attribution_stays_readable(tmp_path: Path) -> None:
    """§7 acceptance: "provenance marker discipline (old bundles without
    findings readable)". Absence is a state, not an error."""

    store, _ = _open_store(tmp_path)
    _seed_evidence(store)

    assert read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE) is None


def test_ran_and_found_nothing_is_not_the_same_as_nobody_looked(
    tmp_path: Path,
) -> None:
    """The whole point of the marker. An empty ``findings`` list inside a
    present record says the speaker is clean; an absent record says nothing
    was ever run. Conflating them would make "no findings" unreadable."""

    store, _ = _open_store(tmp_path)
    identity, _cite = _seed_evidence(store)

    assert read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE) is None
    _publish_finding_set(
        store,
        FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=()
        ),
    )
    empty = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)

    assert empty is not None
    assert empty.findings == ()
    assert empty.produced_by == PRODUCED_BY


def test_a_corrupt_record_is_a_failure_not_a_legacy_bundle(tmp_path: Path) -> None:
    """An unreadable artifact must never be mistaken for "this bundle
    predates attribution" — that would turn a real fault into a shrug."""

    store, bundle_dir = _open_store(tmp_path)
    path = bundle_dir / "evidence/v1/artifacts" / findings_relative_path(CAPTURE, PHASE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema":"jts_attribution_finding_set/1"}', encoding="utf-8")

    with pytest.raises(FindingStorageError):
        read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)


def test_findings_inherit_the_bundle_s_group_readable_mode(tmp_path: Path) -> None:
    """§6's ownership posture: "no **root-owned** artifacts on a laptop-pull
    path … Group-readable ownership plus the existing modes is the shape."
    Findings are published through the same store as every other artifact, so
    they inherit ``BUNDLE_FILE_MODE`` and the parent's group rather than
    inventing a posture of their own."""

    store, bundle_dir = _open_store(tmp_path)
    identity, cite = _seed_evidence(store)
    _publish_finding_set(
        store,
        FindingSet(
            session=identity, produced_by=PRODUCED_BY, findings=(_finding(cite),)
        ),
    )
    path = bundle_dir / "evidence/v1/artifacts" / findings_relative_path(CAPTURE, PHASE)

    assert BUNDLE_FILE_MODE == 0o640
    assert path.stat().st_mode & 0o777 == BUNDLE_FILE_MODE
    assert path.stat().st_gid == path.parent.stat().st_gid


# --------------------------------------------------------------------------- #
# The cross-store identity, through the real web seams (§6, §7 acceptance)
# --------------------------------------------------------------------------- #


def _carve_outs() -> list[dict]:
    return [
        {
            "band_hz": [2000.0, 8000.0],
            "intervals": [
                {
                    "f_lo_hz": 4200.0,
                    "f_hi_hz": 4600.0,
                    "source": "identified_null",
                    "f_center_hz": 4400.0,
                    "n": 3,
                    "tau_us": 310.0,
                    "r_time": 0.28,
                    "r_freq": 0.31,
                    "depth_db": -6.4,
                    "classification": "position_invariant",
                    "reason": (
                        "This range is a cancellation between two arrivals. "
                        "Adding level cannot fill a cancellation, so it is "
                        "left out of correction and out of grading."
                    ),
                }
            ],
        }
    ]


def test_the_cloud_artifact_and_its_findings_share_one_identity(
    tmp_path: Path,
) -> None:
    """The hop that matters most: the finding set and the evidence it cites
    resolve to the same session, and the capture id rides as an alias rather
    than as a competing identity."""


    store, bundle_dir = _open_store(tmp_path)
    refs: dict = {}
    v2evidence.bind_cloud_publisher(store, CAPTURE, refs, asyncio.run)(
        PHASE, {"available": True, "carve_outs": _carve_outs()}
    )

    cloud = json.loads(
        (
            bundle_dir
            / "evidence/v1/artifacts/crossover_v2"
            / CAPTURE
            / f"{PHASE}.json"
        ).read_text()
    )
    cloud_identity = read_session_identity(cloud)
    assert cloud_identity is not None
    assert cloud_identity.session_id == store.session_id
    assert cloud_identity.aliases["capture_session_id"] == CAPTURE

    findings = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert findings is not None
    assert findings.session == cloud_identity


def test_the_live_seam_promotes_carve_outs_and_cites_the_cloud_artifact(
    tmp_path: Path,
) -> None:
    """End to end through the real publisher: the excluded-band records become
    findings, and each cites the exact cloud artifact its numbers were read
    from — so ``read_finding_set``'s default verification has something real
    to check."""


    store, _ = _open_store(tmp_path)
    refs: dict = {}
    v2evidence.bind_cloud_publisher(store, CAPTURE, refs, asyncio.run)(
        PHASE, {"available": True, "carve_outs": _carve_outs()}
    )

    findings = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert findings is not None
    assert [f.mechanism for f in findings.findings] == ["M2"]
    assert findings.findings[0].confidence == "unsure"
    assert findings.findings[0].fix_class == "carve"
    assert refs["finding_artifacts"][PHASE]
    cite = findings.findings[0].cites[0]
    assert cite.locator.endswith(f"crossover_v2/{CAPTURE}/{PHASE}.json")
    assert len(cite.sha256) == 64


def test_a_findings_failure_never_fails_the_cloud_publish(tmp_path: Path) -> None:
    """§3.4: findings are *optional* evidence artifacts — "a session with no
    findings behaves exactly as it does today". So this seam fails soft,
    unlike its two strict siblings, and the cloud artifact it rides behind is
    already durable by the time findings run."""


    store, _ = _open_store(tmp_path)
    refs: dict = {}

    # A carve-out block shaped in a way promotion cannot read at all.
    v2evidence.bind_cloud_publisher(store, CAPTURE, refs, asyncio.run)(
        PHASE, {"available": True, "carve_outs": "not-a-list"}
    )

    assert refs["cloud_artifacts"][PHASE]
    empty = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert empty is not None and empty.findings == ()


def test_a_carve_out_set_is_recorded_but_never_projected(tmp_path: Path) -> None:
    """The deliberate boundary: the store holds every finding; the household
    wire holds only sentences no other surface already owns.

    A carve-out finding's ``household_copy`` is COPIED from the carve-out record
    (``promote_carve_outs`` rule 3), so the copy already has an owner, and
    projecting it again would put one fact on one screen twice, from two owners.
    """

    store, _ = _open_store(tmp_path)
    refs: dict = {}
    v2evidence.bind_cloud_publisher(store, CAPTURE, refs, asyncio.run)(
        PHASE, {"available": True, "carve_outs": _carve_outs()}
    )
    # Recorded — the durable finding set exists and reopens.
    banked = read_finding_set(store, capture_session_id=CAPTURE, phase=PHASE)
    assert banked is not None and [f.mechanism for f in banked.findings] == ["M2"]
    # …and NOT on the household wire.
    assert v2durable.FINDING_HOUSEHOLD_REFS_KEY not in refs


def test_position_retention_puts_the_wav_path_and_digest_in_the_state(
    tmp_path: Path,
) -> None:
    """§6: "the per-position WAV path + SHA-256 **in the state itself** so the
    state alone is replayable", and the accepted-attempt mapping alongside it.
    ``refs`` is what the durable v2 state persists as its evidence block."""

    store, bundle_dir = _open_store(tmp_path)
    refs: dict = {}
    bank = retained_take_writer(store, CAPTURE, refs, asyncio.run)

    class _Result:
        wav = b"take-bytes"

    bank(
        _Result(),
        {
            "position_id": "cloud_measure_03",
            "phase": PHASE,
            "index": 3,
            "attempt": 2,
            "take_id": "cloud_measure_03_a02",
            "measure_kind": "",
            "prompt": "Two hand-widths LEFT of the mark.",
            "wide": False,
            "captured_at": 1.0,
            "session_id": CAPTURE,
            "wav_sha256": "c" * 64,
        },
    )

    entry = refs["position_artifacts"][0]
    assert entry["take_id"] == "cloud_measure_03_a02"
    assert entry["attempt"] == 2
    assert entry["wav_sha256"] == hashlib.sha256(b"take-bytes").hexdigest()
    assert entry["wav_path"]
    # The path is bundle-relative and really points at the retained bytes.
    assert (bundle_dir / entry["wav_path"]).read_bytes() == b"take-bytes"


@pytest.mark.parametrize(
    ("phase", "expected_kind"),
    [
        (PHASE_CHECK, CAPTURE_KIND_SEQUENTIAL),
        (PHASE_MEASURE, CAPTURE_KIND_SEQUENTIAL),
        (PHASE_LATERAL, CAPTURE_KIND_SEQUENTIAL),
        (PHASE_VERIFY, "summed"),
        (PHASE_CLOUD_MEASURE, "summed"),
    ],
)
def test_a_banked_take_records_the_kind_its_phase_actually_played(
    tmp_path: Path, phase: str, expected_kind: str,
) -> None:
    """CHECK, MEASURE and LATERAL play ONE recording that steps through every
    driver in turn, which is neither a single driver nor a simultaneous sum.
    They were banked as ``summed`` only because the taxonomy had no third
    value; now they are banked as what they are. A lateral pose belongs with
    the other two because ``programs.program_for_phase`` answers it with
    MEASURE's program OBJECT verbatim — the same stimulus under a third name.
    VERIFY and the cloud position groups really do play one summed sweep and
    keep the old label.
    """

    store, bundle_dir = _open_store(tmp_path)
    bank = retained_take_writer(store, CAPTURE, {}, asyncio.run)

    class _Result:
        wav = b"take-bytes"

    # A lateral pose names its prompted spot ``pose_id``; every other phase
    # calls it ``position_id``. The two vocabularies ``spatial._take_identity``
    # keeps apart, so the lateral row drives the shape a pose really banks.
    id_key = "pose_id" if phase == PHASE_LATERAL else "position_id"
    bank(
        _Result(),
        {
            id_key: f"{phase}_00",
            "phase": phase,
            "index": 0,
            "attempt": 1,
            "take_id": f"{phase}_00_a01",
            "measure_kind": "",
            "prompt": "",
            "wide": False,
            "captured_at": 1.0,
            "session_id": CAPTURE,
            "wav_sha256": "d" * 64,
        },
    )

    info = json.loads((bundle_dir / "info.json").read_text())
    # Every kind here shares one list — the recorded kind is the only thing
    # that tells the three apart, which is why it is written at all.
    assert [e["kind"] for e in info["summed_captures"]] == [expected_kind]
    assert info["captures"] == []
