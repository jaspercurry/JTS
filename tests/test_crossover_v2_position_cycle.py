# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The staged-walk expansion, and the pose index derived from a banked round."""

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
    expand_angle_spec,
    position_cycle_document,
    read_entry_baseline_take,
    read_pose_curve_pair,
    read_position_cycle,
    staged_stops,
    takes_by_position,
    write_position_cycle,
)
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.spatial import (
    LATERAL_POSE_REGIME,
    LateralPose,
    TakeClaim,
    entry_baseline_record,
    lateral_pose_record,
)


# --------------------------------------------------------------------------- #
# the expansion
# --------------------------------------------------------------------------- #


def test_each_angle_is_repeated_n_times_adjacently():
    """Adjacent, not interleaved: the arm must not travel between takes."""
    assert expand_angle_spec("0,7,-7", 3) == "0,0,0,7,7,7,-7,-7,-7"


def test_one_take_per_position_is_the_list_unchanged():
    """The default runs on every staged round, so it must be a no-op."""
    assert expand_angle_spec("0,7,-7,22,-22", 1) == "0,7,-7,22,-22"


def test_tokens_are_repeated_verbatim_never_parsed():
    """The angle vocabulary has ONE validator and it is not this one.

    ``0.4`` truncates to ``0`` under ``int()`` — an off-axis pose silently
    becoming an on-axis capture — which is why ``_validated_angle`` refuses to
    coerce. An expansion that parsed to repeat would be a second reader of that
    vocabulary; it repeats the TEXT, and whatever the operator wrote still
    reaches the one validator that judges it.
    """
    assert expand_angle_spec("0.4,+7,007", 2) == "0.4,0.4,+7,+7,007,007"


def test_surrounding_whitespace_is_stripped_so_one_walk_is_one_walk():
    assert expand_angle_spec("0, 7 ,-7", 2) == "0,0,7,7,-7,-7"


def test_empty_fields_are_dropped_exactly_as_the_seam_drops_them():
    """``jasper.cli.angle_capture._parse_angles`` is
    ``[... for field in raw.split(",") if field.strip()]`` — a trailing comma is
    tolerated there by design, so refusing one here would be a second, stricter
    reader of the same field."""
    assert expand_angle_spec("0,7,", 2) == "0,0,7,7"
    assert expand_angle_spec("0,,7", 2) == "0,0,7,7"
    assert expand_angle_spec(",", 2) == ""


@pytest.mark.parametrize("per_position", [0, -1])
def test_fewer_than_one_take_is_refused(per_position):
    with pytest.raises(PositionCycleError, match="at least 1"):
        expand_angle_spec("0,7", per_position)


def test_there_is_no_ceiling_here_because_the_plan_owns_that_bound():
    """A second, lower ceiling invented on the laptop would refuse walks the
    speaker would have taken — ``session_lateral_walk`` refuses by name."""
    assert staged_stops(expand_angle_spec("0", 64)) == 64


def test_staged_stops_counts_what_the_walk_will_serve():
    assert staged_stops("0,7,-7") == 3
    assert staged_stops(expand_angle_spec("0,7,-7", 4)) == 12
    assert staged_stops("0,7,") == 2


# --------------------------------------------------------------------------- #
# the index — derived from the speaker's own records
# --------------------------------------------------------------------------- #


def _record(
    index: int, position_deg: int, *, attempt: int = 1, vertical_deg: int = 0,
    candidate_id: str = "",
) -> dict:
    """One take, built by the SPEAKER's own producer.

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
        pose, position_deg=position_deg, vertical_deg=vertical_deg,
        lateral_consumer="forward_model",
        session_id="sess-1", graph_fingerprint="fp-applied",
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
         "position_deg": 0, "vertical_deg": 0, "role": "onax",
         "regime": LATERAL_POSE_REGIME, "wav_sha256": "sha-1-1"},
        {"index": 2, "attempt": 1, "take_id": "lateral_02_a01", "candidate_id": "",
         "position_deg": 7, "vertical_deg": 0, "role": "offax",
         "regime": LATERAL_POSE_REGIME, "wav_sha256": "sha-2-1"},
        {"index": 3, "attempt": 1, "take_id": "lateral_03_a01", "candidate_id": "",
         "position_deg": -7, "vertical_deg": 0, "role": "offax",
         "regime": LATERAL_POSE_REGIME, "wav_sha256": "sha-3-1"},
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


def test_a_take_banked_before_elevation_existed_indexes_as_mark_height(tmp_path):
    """History reads, and reads HONESTLY: absent is 0, never ``None``.

    Asserted against a record with the key REMOVED, not one written 0 — a walk
    that could not state a rise did not take one, so the two are the same fact
    and a reader must not have to tell a missing number from an unstated one.
    Refusing the round instead would trade a whole banked walk for one number
    it never had.
    """
    legacy = {k: v for k, v in _record(1, 7).items() if k != "vertical_deg"}
    _bank(tmp_path, [legacy])

    take, = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert take["vertical_deg"] == 0


def test_a_take_banked_before_candidates_existed_names_no_candidate(tmp_path):
    """``""`` is the honest reading of a walk that cycled nothing — never
    ``None``, which would put a null in front of every packet reader."""
    legacy = {k: v for k, v in _record(1, 7).items() if k != "candidate_id"}
    _bank(tmp_path, [legacy])

    take, = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert take["candidate_id"] == ""


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
        "index": 2, "attempt": 1, "take_id": "cloud_02_a01",
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
# the round's "before" — the same directory, the other reader
# --------------------------------------------------------------------------- #


def _entry_take(tmp_path: Path, **overrides) -> Path:
    """One banked entry-baseline sidecar, from the SPEAKER's own producer."""
    fields = {
        "index": 9, "attempt": 1, "session_id": "sess-1",
        "program_id": "prog-entry", "reference_mark": "design_axis",
        "graph_fingerprint": "fp-entry", "captured_at": "2026-08-11T00:00:00Z",
        "freqs_hz": (200.0, 400.0), "magnitude_db": (-1.5, 0.5),
        "excluded": (True, False),
        "validity_floor_hz": 100.0, "gate_window_ms": 12.0,
        "summed_ripple_db": 1.0, "glitch_detected": False,
        "wav_sha256": "entry-sha",
    }
    record = entry_baseline_record(**{**fields, **overrides})
    path = tmp_path / f"{record['take_id']}.json"
    path.write_text(json.dumps({
        "schema_version": 1, "kind": POSITION_EVIDENCE_KIND, **record,
    }))
    return path


def test_the_before_reads_back_in_the_shape_its_record_type_rehydrates_from(
    tmp_path,
):
    """The field names are ``EntryBaseline.from_dict``'s, so one reader covers both.

    A reader that returned its own spelling would make every caller translate,
    and the translation is where a dropped exclusion mask hides.
    """
    take = read_entry_baseline_take(_entry_take(tmp_path))

    assert take == {
        "program_id": "prog-entry",
        "reference_mark": "design_axis",
        "graph_fingerprint": "fp-entry",
        "captured_at": "2026-08-11T00:00:00Z",
        "freqs_hz": [200.0, 400.0],
        "magnitude_db": [-1.5, 0.5],
        "excluded": [True, False],
        "artifact_ref": "entry_baseline_09_a01",
    }


def test_a_lateral_pose_is_never_read_as_the_round_s_before(tmp_path):
    """Two record shapes in one directory, and each reader takes one.

    ``BankedRecordStore.bank`` routes the lateral walk, the cloud group and the
    entry baseline into one directory. Reading a per-driver pose as the summed
    "before" would put the wrong capture on one side of a benefit comparison.
    """
    path = tmp_path / "pose.json"
    path.write_text(json.dumps({
        "schema_version": 1, "kind": POSITION_EVIDENCE_KIND, **_record(1, 0),
    }))

    assert read_entry_baseline_take(path) is None


@pytest.mark.parametrize(
    "missing", ["freqs_hz", "magnitude_db", "excluded"],
)
def test_a_take_banked_before_the_curve_rode_here_is_not_a_before(
    tmp_path, missing,
):
    """A baseline-shaped record with no bins cannot answer what it is asked.

    Rounds banked before the curve moved into the take carry the identity
    fields and none of the arrays. Returning one half-filled would hand a
    comparison a "before" with nothing to compare, which is worse than the
    honest absence the caller already knows how to report.
    """
    path = _entry_take(tmp_path)
    raw = json.loads(path.read_text())
    del raw[missing]
    path.write_text(json.dumps(raw))

    assert read_entry_baseline_take(path) is None


@pytest.mark.parametrize(
    "written", ["{ truncated", json.dumps([1, 2, 3])],
    ids=["truncated", "not-an-object"],
)
def test_an_unreadable_before_is_an_absence_rather_than_a_raise(tmp_path, written):
    """One corrupt sidecar must not cost a reader the round it is looking at."""
    path = tmp_path / "entry_baseline_09_a01.json"
    path.write_text(written)

    assert read_entry_baseline_take(path) is None


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

    assert block["available"] is True
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


def test_the_writer_puts_the_index_where_the_reader_looks(tmp_path):
    """The one writer of the file, round-tripped through the one reader."""
    _bank(tmp_path / "round", [_record(1, 0), _record(2, 7)])

    path, document = write_position_cycle(tmp_path / "round")

    assert path == tmp_path / "round" / POSITION_CYCLE_FILENAME
    assert read_position_cycle(path) == document


# --------------------------------------------------------------------------- #
# the reader
# --------------------------------------------------------------------------- #


def _written(tmp_path: Path, document) -> Path:
    path = tmp_path / "written.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path


@pytest.fixture
def document(tmp_path) -> dict:
    _bank(tmp_path / "round", [_record(1, 0), _record(2, 7)])
    return position_cycle_document(tmp_path / "round", derived_at=STAMP)


def test_a_written_index_reads_back_identical(tmp_path, document):
    assert read_position_cycle(_written(tmp_path, document)) == document


def test_an_unknown_key_is_an_error_not_a_silent_drop(tmp_path, document):
    """A key this reader does not know is either a newer schema or a hand edit,
    and reading a NEWER document as an older one silently is the failure this
    document's whole job — being believed later — cannot survive."""
    with pytest.raises(PositionCycleError, match="unknown keys"):
        read_position_cycle(_written(tmp_path, dict(document, per_position=3)))


def test_a_missing_key_is_an_error(tmp_path, document):
    document.pop("sources")
    with pytest.raises(PositionCycleError, match="missing keys"):
        read_position_cycle(_written(tmp_path, document))


def test_another_documents_kind_is_refused(tmp_path, document):
    with pytest.raises(PositionCycleError, match="kind is"):
        read_position_cycle(
            _written(tmp_path, dict(document, kind=POSITION_EVIDENCE_KIND))
        )


def test_a_future_schema_version_is_refused(tmp_path, document):
    with pytest.raises(PositionCycleError, match="schema_version"):
        read_position_cycle(
            _written(tmp_path, dict(document, schema_version=SCHEMA_VERSION + 1))
        )


def test_an_empty_take_list_is_refused(tmp_path, document):
    with pytest.raises(PositionCycleError, match="non-empty list"):
        read_position_cycle(_written(tmp_path, dict(document, takes=[])))


def test_a_take_carrying_an_extra_field_is_refused(tmp_path, document):
    document["takes"][0]["offset_cm"] = 0.0
    with pytest.raises(PositionCycleError, match="must carry exactly"):
        read_position_cycle(_written(tmp_path, document))


@pytest.mark.parametrize("field", ["vertical_deg", "candidate_id"])
def test_a_take_missing_a_DEFAULTED_field_still_reads(tmp_path, document, field):
    """The strict reader's exemptions, at the MISSING end only.

    Strictness exists so a NEWER document is never read as an older one, and
    the test above keeps that: an unknown key still refuses. What this exempts
    is the opposite direction — a document written before a defaulted field
    existed, which a newer reader understands completely. Refusing it would
    throw away a banked round to gain one number the round never had.
    """
    document["takes"] = [
        {k: v for k, v in take.items() if k != field}
        for take in document["takes"]
    ]

    assert read_position_cycle(_written(tmp_path, document)) == document


def test_an_unreadable_file_is_this_modules_error_not_an_oserror(tmp_path):
    with pytest.raises(PositionCycleError):
        read_position_cycle(tmp_path / "absent.json")


def test_a_json_array_is_refused(tmp_path):
    path = tmp_path / "written.json"
    path.write_text("[]")
    with pytest.raises(PositionCycleError, match="not a JSON object"):
        read_position_cycle(path)


# --------------------------------------------------------------------------- #
# the split
# --------------------------------------------------------------------------- #


def test_the_takes_that_share_one_pose_are_grouped_in_walk_order(tmp_path):
    _bank(tmp_path, [_record(1, 0), _record(2, 0), _record(3, 0),
                     _record(4, 7), _record(5, 7), _record(6, 7)])

    assert takes_by_position(
        position_cycle_document(tmp_path, derived_at=STAMP)
    ) == {
        (0, 0): ("lateral_01_a01", "lateral_02_a01", "lateral_03_a01"),
        (7, 0): ("lateral_04_a01", "lateral_05_a01", "lateral_06_a01"),
    }


def test_an_uncycled_walk_groups_to_one_take_per_pose(tmp_path):
    _bank(tmp_path, [_record(1, 0), _record(2, 7)])

    assert takes_by_position(
        position_cycle_document(tmp_path, derived_at=STAMP)
    ) == {(0, 0): ("lateral_01_a01",), (7, 0): ("lateral_02_a01",)}


def test_a_pose_revisited_later_in_the_walk_keeps_both_visits(tmp_path):
    """``0,7,0`` is a legal walk — the arm returns to the axis — and its two
    on-axis takes are two takes at that pose, not one."""
    _bank(tmp_path, [_record(1, 0), _record(2, 7), _record(3, 0)])

    assert takes_by_position(
        position_cycle_document(tmp_path, derived_at=STAMP)
    ) == {(0, 0): ("lateral_01_a01", "lateral_03_a01"), (7, 0): ("lateral_02_a01",)}


def test_a_raised_pose_is_its_own_group_at_the_same_bearing(tmp_path):
    """0/0 and 0/+10 are two poses, not one bearing measured twice."""
    _bank(tmp_path, [_record(1, 0), _record(2, 0, vertical_deg=10)])

    assert takes_by_position(
        position_cycle_document(tmp_path, derived_at=STAMP)
    ) == {(0, 0): ("lateral_01_a01",), (0, 10): ("lateral_02_a01",)}



# --------------------------------------------------------------------------- #
# the pose key: a bearing AND a height
# --------------------------------------------------------------------------- #


_BOTH_ROLES = [{"role": "woofer"}, {"role": "tweeter"}]


def _pose_bank(tmp_path: Path) -> Path:
    """One walk at 0 deg: a mark-height take, then a NEWER raised one.

    The mark-height record carries no ``vertical_deg`` KEY AT ALL — the shape
    every round banked before elevated walks shipped — so selecting it at
    ``vertical_deg=0`` also pins that absence reading as mark height rather
    than as "unknown height".
    """
    mark = {k: v for k, v in _record(1, 0).items() if k != "vertical_deg"}
    _bank(tmp_path, [
        {**mark, "curves": _BOTH_ROLES},
        {**_record(2, 0), "vertical_deg": 10, "curves": _BOTH_ROLES},
    ])
    return tmp_path / "bundle" / "sess-1"


def _pair_take(bundle_dir: Path, **pose) -> list[str]:
    found = read_pose_curve_pair(
        bundle_dir, phase=PHASE_LATERAL, roles=("woofer", "tweeter"), **pose
    )
    return [] if found is None else [found[2]]


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
        pytest.param(
            lambda d: _indexed(d, vertical_deg=0),
            ["lateral_01_a01"],
            id="a_record_lacking_the_key_indexes_as_mark_height",
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
