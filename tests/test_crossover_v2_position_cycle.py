# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The pose index derived from a banked round."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from jasper.active_speaker.crossover_v2 import position_cycle
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_CYCLE_FILENAME,
    POSITION_CYCLE_KIND,
    POSITION_EVIDENCE_KIND,
    SCHEMA_VERSION,
    PositionCycleError,
    position_cycle_document,
    select_pose_curve_pair,
    write_position_cycle,
)
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.measurement_programs import PURPOSE_SPEAKER
from jasper.audio_measurement.evidence_reasons import TAKE_CURVES_NOT_BANKED, EvidenceUnavailable
from jasper.active_speaker.crossover_v2.spatial import (
    MARK_DISTANCE_M,
    POSITION_AXIS_HORIZONTAL,
    PositionGeometry,
)
from tests.crossover_v2_banked_round import (
    LateralPose,
    TakeClaim,
    lateral_pose_record,
)
from tests.run_manifest_fixture import write_manifest


def _record(
    index: int, position_deg: int, *, attempt: int = 1, vertical_deg: int = 0,
    candidate_id: str = "",
) -> dict:
    """One take, built by the shared take-record builder.

    Not a hand-written dict: ``lateral_pose_record`` is the thing whose fields
    this index projects, so a test that spelled them itself would keep passing
    the day that record changed shape.
    """
    pose = LateralPose(
        pose_id=f"lateral_{index:02d}",
        index=index,
        attempt=attempt,
        prompt=f"{position_deg:+d} deg",
        role="onax" if position_deg == 0 else "offax",
        offset_cm=float(position_deg),
        at_mark=position_deg == 0,
        curves=(),
    )
    return lateral_pose_record(
        pose,
        geometry=PositionGeometry(
            POSITION_AXIS_HORIZONTAL, position_deg, MARK_DISTANCE_M, vertical_deg,
        ),
        lateral_consumer="forward_model",
        run_id="sess-1", graph_fingerprint="fp-applied",
        captured_at="2026-08-26T00:00:00Z",
        wav_sha256=f"sha-{index}-{attempt}",
        claim=TakeClaim(candidate_id=candidate_id),
    )


#: The banked layout, spelled out as a LITERAL on purpose.
#:
#: Deriving it from ``position_cycle``'s own glob would make every test below
#: pass against a wrong path as happily as against the right one — which is
#: exactly what happened: the first version of this file built
#: ``bundle/<session>/crossover_v2/…``, the module globbed the same wrong shape,
#: 32 tests agreed with each other, and a real bank was reported as a walk that
#: never ran. So the fixture states the tree independently, and
#: ``test_the_glob_matches_a_record_the_REAL_store_wrote`` binds this literal to
#: the actual writer.
_BANKED_ARTIFACTS = "evidence/v1/artifacts/crossover_v2"


def _bank(root: Path, records, *, capture: str = "capture-1") -> Path:
    """A banked round: the bundle tree ``bank-crossover-round.sh`` untars.

    ``<round>/bundle/<session>/evidence/v1/artifacts/crossover_v2/<capture>/positions/``
    — the same tree ``test_active_speaker_crossover_v2_round_views``'s
    ``_make_round_dir`` builds, because both model one bank's output.
    """
    positions = root / "bundle" / "sess-1" / _BANKED_ARTIFACTS / capture / "positions"
    positions.mkdir(parents=True, exist_ok=True)
    for record in records:
        payload = {
            "schema_version": 1,
            "kind": POSITION_EVIDENCE_KIND,
            "capture_session_id": capture,
            **record,
        }
        (positions / f"{record['take_id']}.json").write_text(json.dumps(payload))
    return root


STAMP = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def test_the_index_projects_the_speakers_own_take_records(tmp_path):
    _bank(tmp_path, [_record(1, 0), _record(2, 7), _record(3, -7)])

    document = position_cycle_document(tmp_path, derived_at=STAMP)

    assert document["kind"] == POSITION_CYCLE_KIND
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["derived_at"] == "2026-08-21T12:00:00Z"
    assert document["sources"] == [
        f"bundle/sess-1/{_BANKED_ARTIFACTS}/capture-1/positions"
    ]
    assert document["takes"] == [
        {"index": 1, "attempt": 1, "take_id": "lateral_01_a01", "candidate_id": "",
         "position_deg": 0, "vertical_deg": 0, "role": "onax", "wav_sha256": "sha-1-1"},
        {"index": 2, "attempt": 1, "take_id": "lateral_02_a01", "candidate_id": "",
         "position_deg": 7, "vertical_deg": 0, "role": "offax", "wav_sha256": "sha-2-1"},
        {"index": 3, "attempt": 1, "take_id": "lateral_03_a01", "candidate_id": "",
         "position_deg": -7, "vertical_deg": 0, "role": "offax", "wav_sha256": "sha-3-1"},
    ]


def test_each_take_at_a_cycled_pose_names_the_candidate_it_measured(tmp_path):
    """Three candidates at ONE bearing are three rows; the id is what tells
    them apart, and without it the curves at that pose are anonymous."""
    _bank(tmp_path, [
        _record(1, 0, candidate_id="cand-a"),
        _record(2, 0, candidate_id="cand-b"),
        _record(3, 0, candidate_id="cand-c"),
    ])

    takes = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert [take["candidate_id"] for take in takes] == [
        "cand-a", "cand-b", "cand-c",
    ]


@pytest.mark.parametrize("vertical_deg", [0, 20, -20])
def test_a_raised_pose_carries_its_elevation_into_the_index(
    tmp_path, vertical_deg
):
    """The projection carries BOTH bearings — a two-axis walk indexed on one
    would read as a walk taken entirely at mark height."""
    _bank(tmp_path, [_record(1, 22, vertical_deg=vertical_deg)])

    take, = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert take["vertical_deg"] == vertical_deg
    assert take["position_deg"] == 22


def test_a_take_banked_without_its_candidate_is_refused(tmp_path):
    """No reader defaults a field a take was banked without (#2902)."""
    _bank(tmp_path, [{k: v for k, v in _record(1, 7).items() if k != "candidate_id"}])

    with pytest.raises(PositionCycleError):
        position_cycle_document(tmp_path, derived_at=STAMP)


def test_a_take_banked_without_its_elevation_refuses_by_that_field(tmp_path):
    _bank(tmp_path, [{k: v for k, v in _record(1, 7).items() if k != "vertical_deg"}])

    with pytest.raises(EvidenceUnavailable) as excinfo:
        position_cycle_document(tmp_path, derived_at=STAMP)
    assert (excinfo.value.reason, excinfo.value.detail["field"]) == (TAKE_CURVES_NOT_BANKED, "vertical_deg")


def test_every_indexed_value_is_present_in_the_banked_record(tmp_path):
    """The document is DERIVED, never authored: no field may be computed here.

    Mechanical rather than by eye — the day someone adds a field this fails
    unless that field, too, came off the speaker's record.
    """
    records = [_record(1, 0), _record(2, 22)]
    _bank(tmp_path, records)
    banked = {record["take_id"]: record for record in records}

    for take in position_cycle_document(tmp_path, derived_at=STAMP)["takes"]:
        source = banked[take["take_id"]]
        assert all(source[field] == value for field, value in take.items())


def test_takes_are_sorted_by_index_then_attempt_however_they_were_globbed(tmp_path):
    _bank(tmp_path, [_record(3, -7), _record(1, 0, attempt=2), _record(1, 0),
                     _record(2, 7)])

    takes = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert [(t["index"], t["attempt"]) for t in takes] == [
        (1, 1), (1, 2), (2, 1), (3, 1),
    ]


def test_a_superseded_take_is_listed_beside_the_one_that_replaced_it(tmp_path):
    """The speaker keeps both on disk deliberately — "the superseded one stays
    on disk as the honest walk record" — so an index that hid one would be a
    third opinion about which take counted."""
    _bank(tmp_path, [_record(1, 0), _record(1, 0, attempt=2)])

    takes = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert [t["take_id"] for t in takes] == ["lateral_01_a01", "lateral_01_a02"]


def test_the_clouds_positions_are_not_takes_of_this_walk(tmp_path):
    """The same directory holds the CLOUD group's positions — one publisher
    serves both — so the phase is what separates them."""
    _bank(tmp_path, [_record(1, 0)])
    positions = tmp_path / "bundle/sess-1" / _BANKED_ARTIFACTS / "capture-1/positions"
    (positions / "cloud_02_a01.json").write_text(json.dumps({
        "schema_version": 1, "kind": POSITION_EVIDENCE_KIND,
        "capture_session_id": "capture-1", "phase": "cloud_measure",
        "index": 2, "attempt": 1, "take_id": "cloud_02_a01", "vertical_deg": 0, "pose_kind": "bearing",
    }))

    takes = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert [t["take_id"] for t in takes] == ["lateral_01_a01"]


def test_the_index_and_the_evidence_packet_share_one_accept_rule(tmp_path):
    """"What is a lateral take" is answered in ONE place, from both directions.

    This index globs the sidecars from the BANKED ROUND root; the evidence
    packet's ``lateral_poses`` block reaches the same files from the session
    bundle's round directory. Two starting points, one rule — and if either
    grew its own filter, they would disagree the first time a record shape
    moved. Proven by making the shared reader refuse everything and watching
    the index empty out, rather than by reading the call site.
    """
    _bank(tmp_path, [_record(1, 0), _record(2, 7)])

    with mock.patch.object(
        position_cycle, "read_lateral_take", return_value=None
    ) as refuse:
        with pytest.raises(PositionCycleError, match="no lateral take records"):
            position_cycle_document(tmp_path, derived_at=STAMP)

    assert refuse.call_count == 2


def test_a_foreign_json_file_in_the_positions_dir_is_not_a_take(tmp_path):
    _bank(tmp_path, [_record(1, 0)])
    positions = tmp_path / "bundle/sess-1" / _BANKED_ARTIFACTS / "capture-1/positions"
    (positions / "notes.json").write_text(json.dumps({"phase": PHASE_LATERAL}))

    assert len(position_cycle_document(tmp_path, derived_at=STAMP)["takes"]) == 1


def test_one_corrupt_sidecar_does_not_cost_the_index_the_takes_that_are_fine(
    tmp_path
):
    _bank(tmp_path, [_record(1, 0), _record(2, 7)])
    positions = tmp_path / "bundle/sess-1" / _BANKED_ARTIFACTS / "capture-1/positions"
    (positions / "lateral_03_a01.json").write_text("{ truncated")

    assert len(position_cycle_document(tmp_path, derived_at=STAMP)["takes"]) == 2


def test_takes_from_two_capture_sessions_name_both_sources(tmp_path):
    _bank(tmp_path, [_record(1, 0)], capture="capture-1")
    _bank(tmp_path, [_record(2, 7)], capture="capture-2")

    document = position_cycle_document(tmp_path, derived_at=STAMP)

    assert document["sources"] == [
        f"bundle/sess-1/{_BANKED_ARTIFACTS}/capture-1/positions",
        f"bundle/sess-1/{_BANKED_ARTIFACTS}/capture-2/positions",
    ]


# --------------------------------------------------------------------------- #
# the layout contract — this glob against the REAL writer
# --------------------------------------------------------------------------- #


def test_the_glob_matches_a_record_the_REAL_store_wrote(tmp_path):
    """The one test that could have caught the wrong path, and the reason it is
    written against the store instead of against a fixture.

    A record does NOT land at the relative path its writer passes:
    ``publish_json_artifact`` runs it through ``_artifact_path``, which prefixes
    ``evidence/v1/artifacts/``. Every other test here builds the tree itself, so
    all of them agreed with a glob that was missing that prefix — matching
    nothing against a real bank and reporting a walk that was never refused.

    So this one publishes through the REAL store, copies the bundle exactly as
    ``bank-crossover-round.sh`` untars it, and derives from the result. If either
    side of the layout moves — the store's root, its ``artifacts/`` namespace, or
    the ``crossover_v2/{capture}/positions/{take_id}.json`` shape the web host
    passes — this fails instead of the derivation silently globbing nothing.
    """
    import shutil

    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )
    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"],
    )

    capture, record = "cap1", _record(1, 7)
    # The EXACT write ``record_store.BankedRecordStore.bank`` makes for a
    # position take — same relative path expression, same envelope.
    store.publish_json_artifact(
        f"crossover_v2/{capture}/positions/{record['take_id']}.json",
        {
            "schema_version": 1,
            "kind": POSITION_EVIDENCE_KIND,
            "capture_session_id": capture,
            **record,
        },
    )

    # `bank-crossover-round.sh` untars <sessions>/<BUNDLE> into <round>/bundle/.
    round_dir = tmp_path / "round"
    bundle_dir = Path(info["bundle_dir"])
    shutil.copytree(bundle_dir, round_dir / "bundle" / bundle_dir.name)

    document = position_cycle_document(round_dir, derived_at=STAMP)

    assert [take["take_id"] for take in document["takes"]] == [record["take_id"]]
    assert document["takes"][0]["position_deg"] == 7
    assert document["sources"] == [
        f"bundle/{bundle_dir.name}/{_BANKED_ARTIFACTS}/{capture}/positions"
    ]


def test_the_evidence_packet_finds_a_record_the_REAL_store_wrote(tmp_path):
    """The packet reaches the same sidecars from the SESSION BUNDLE instead.

    Its `lateral_poses` block globs `<round-dir>/positions/*.json`, where the
    round dir is `round_artifact_dir`'s own return value — one segment, not the
    full path this module's glob spells. That segment is the half a fixture
    cannot pin: every packet test builds the tree itself, so all of them would
    agree with a wrong subdirectory name as happily as with the right one, and
    the block would report a walk that ran as a round with no bearings.

    So this publishes through the REAL store and asks the packet, on the same
    reasoning as the derivation test above.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )
    from jasper.active_speaker.crossover_v2.evidence_packet import (
        build_crossover_evidence_packet,
    )
    from tests.active_speaker_fixtures import mono_output_topology

    info = open_bundle(
        mono_output_topology(mode="active_3_way"),
        calibration_id="calibration-test",
        sessions_dir=tmp_path / "sessions",
    )
    assert info is not None
    store = CommissioningEvidenceStore.open(
        info["bundle_dir"], expected_session_id=info["session_id"],
    )
    capture, record = "cap1", _record(1, -22)
    store.publish_json_artifact(
        f"crossover_v2/{capture}/positions/{record['take_id']}.json",
        {
            "schema_version": 1,
            "kind": POSITION_EVIDENCE_KIND,
            "capture_session_id": capture,
            **record,
        },
    )

    block = build_crossover_evidence_packet(
        Path(info["bundle_dir"])
    )["lateral_poses"]

    assert block["status"] == "available"
    assert [take["take_id"] for take in block["takes"]] == [record["take_id"]]
    assert block["angles_deg"] == [-22]


def test_the_fixture_tree_and_the_real_store_agree_on_the_layout(tmp_path):
    """The fixture literal above is the same path the store actually writes.

    Stated separately from the derivation so a failure says WHICH of the two
    drifted: this one compares the tree ``_bank`` builds against the tree the
    store produces, with the reader out of the picture entirely.
    """
    from jasper.active_speaker.commissioning_evidence_store import _artifact_path

    written = _artifact_path("crossover_v2/capture-1/positions/lateral_01_a01.json")

    assert written == f"{_BANKED_ARTIFACTS}/capture-1/positions/lateral_01_a01.json"


# --------------------------------------------------------------------------- #
# what is missing is NAMED — never filled in from intent
# --------------------------------------------------------------------------- #


def test_a_round_with_no_bundle_is_refused_by_name(tmp_path):
    with pytest.raises(PositionCycleError, match="no bundle/ was banked"):
        position_cycle_document(tmp_path)


def test_a_bundle_with_no_lateral_takes_is_refused_by_name(tmp_path):
    """The walk was refused at take time, or its poses were never accepted.
    Either way the honest answer is to say so, not to write down the angles the
    round MEANT to visit."""
    _bank(tmp_path, [])

    with pytest.raises(PositionCycleError, match="no lateral take records"):
        position_cycle_document(tmp_path)


def test_the_refusal_names_where_it_looked(tmp_path):
    _bank(tmp_path, [])

    with pytest.raises(PositionCycleError, match=r"positions/\*\.json"):
        position_cycle_document(tmp_path)


def test_a_non_numeric_ordinal_refuses_as_this_modules_error(tmp_path):
    """A corrupt sidecar costs the round its index, never the caller's whole
    operation: a bare ``ValueError`` out of the sort would unwind a bank."""
    _bank(tmp_path, [dict(_record(1, 0), index="1a")])

    with pytest.raises(PositionCycleError, match="non-numeric"):
        position_cycle_document(tmp_path, derived_at=STAMP)


def test_the_writer_puts_the_index_in_the_round(tmp_path):
    """The one writer of the file writes the document it returns."""
    _bank(tmp_path / "round", [_record(1, 0), _record(2, 7)])

    path, document = write_position_cycle(tmp_path / "round")

    assert path == tmp_path / "round" / POSITION_CYCLE_FILENAME
    assert json.loads(path.read_text()) == document


# --------------------------------------------------------------------------- #
# the pose key: a bearing AND a height
# --------------------------------------------------------------------------- #


_BOTH_ROLES = [{"role": "woofer", "window": "gated"}, {"role": "tweeter", "window": "gated"}]


def _pose_bank(tmp_path: Path) -> Path:
    """One walk at 0 deg: a mark-height take, then a NEWER raised one."""
    speaker = {"measurement_purpose": PURPOSE_SPEAKER, "curves": _BOTH_ROLES}
    _bank(tmp_path, [{**_record(1, 0), **speaker}, {**_record(2, 0), "vertical_deg": 10, **speaker}])
    write_manifest(tmp_path)
    return tmp_path / "bundle" / "sess-1"


def _pair_take(bundle_dir: Path, **pose) -> list[str]:
    found = select_pose_curve_pair(
        bundle_dir, phases=(PHASE_LATERAL,), roles=("woofer", "tweeter"), **pose
    )
    return [] if found is None else [found.take.path]


def _indexed(bundle_dir: Path, **filters) -> list[str]:
    return [row.path for row in bundle_measurements(bundle_dir, **filters)]


@pytest.mark.parametrize(
    ("select", "expected"),
    [
        pytest.param(
            lambda d: _pair_take(d, position_deg=0),
            ["lateral_01_a01"],
            id="the_design_axis_pair_is_the_mark_height_take",
        ),
        pytest.param(
            lambda d: _pair_take(d, position_deg=0, vertical_deg=10),
            ["lateral_02_a01"],
            id="the_raised_pose_answers_only_when_its_height_is_named",
        ),
        pytest.param(
            lambda d: _indexed(d, vertical_deg=10),
            ["lateral_02_a01"],
            id="the_index_selects_the_raised_take_alone",
        ),
    ],
)
def test_a_pose_is_selected_by_its_bearing_AND_its_height(tmp_path, select, expected):
    """A raised seat and a mark-height one share a bearing and are NOT the
    same pose.

    "Latest attempt wins" walks the takes at a pose newest-first, so a bearing-
    only key hands the newer raised take to the forward model and the delay
    landscape as their design-axis basis — the wrong measurement, silently.
    """
    assert [Path(path).stem for path in select(_pose_bank(tmp_path))] == expected
