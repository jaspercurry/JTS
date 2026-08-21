# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The staged-walk expansion, and the pose index derived from a banked round."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_CYCLE_KIND,
    POSITION_EVIDENCE_KIND,
    SCHEMA_VERSION,
    PositionCycleError,
    expand_angle_spec,
    position_cycle_document,
    read_position_cycle,
    staged_stops,
    takes_by_position,
)
from jasper.active_speaker.crossover_v2.spatial import (
    LATERAL_POSE_REGIME,
    LateralPose,
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


def test_there_is_no_ceiling_here_because_the_relay_owns_that_bound():
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


def _record(index: int, position_deg: int, *, attempt: int = 1) -> dict:
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
        pose, position_deg=position_deg, lateral_consumer="forward_model",
        session_id="sess-1", wav_sha256=f"sha-{index}-{attempt}",
    )


def _bank(root: Path, records, *, relay: str = "relay-1") -> Path:
    """A banked round: the bundle tree ``bank-crossover-round.sh`` untars."""
    positions = root / "bundle" / "sess-1" / "crossover_v2" / relay / "positions"
    positions.mkdir(parents=True, exist_ok=True)
    for record in records:
        payload = {
            "schema_version": 1,
            "kind": POSITION_EVIDENCE_KIND,
            "relay_session_id": relay,
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
        "bundle/sess-1/crossover_v2/relay-1/positions"
    ]
    assert document["takes"] == [
        {"index": 1, "attempt": 1, "take_id": "lateral_01_a01", "position_deg": 0,
         "role": "onax", "regime": LATERAL_POSE_REGIME, "wav_sha256": "sha-1-1"},
        {"index": 2, "attempt": 1, "take_id": "lateral_02_a01", "position_deg": 7,
         "role": "offax", "regime": LATERAL_POSE_REGIME, "wav_sha256": "sha-2-1"},
        {"index": 3, "attempt": 1, "take_id": "lateral_03_a01", "position_deg": -7,
         "role": "offax", "regime": LATERAL_POSE_REGIME, "wav_sha256": "sha-3-1"},
    ]


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
    positions = tmp_path / "bundle/sess-1/crossover_v2/relay-1/positions"
    (positions / "cloud_02_a01.json").write_text(json.dumps({
        "schema_version": 1, "kind": POSITION_EVIDENCE_KIND,
        "relay_session_id": "relay-1", "phase": "cloud_measure",
        "index": 2, "attempt": 1, "take_id": "cloud_02_a01",
    }))

    takes = position_cycle_document(tmp_path, derived_at=STAMP)["takes"]

    assert [t["take_id"] for t in takes] == ["lateral_01_a01"]


def test_a_foreign_json_file_in_the_positions_dir_is_not_a_take(tmp_path):
    _bank(tmp_path, [_record(1, 0)])
    positions = tmp_path / "bundle/sess-1/crossover_v2/relay-1/positions"
    (positions / "notes.json").write_text(json.dumps({"phase": PHASE_LATERAL}))

    assert len(position_cycle_document(tmp_path, derived_at=STAMP)["takes"]) == 1


def test_one_corrupt_sidecar_does_not_cost_the_index_the_takes_that_are_fine(
    tmp_path
):
    _bank(tmp_path, [_record(1, 0), _record(2, 7)])
    positions = tmp_path / "bundle/sess-1/crossover_v2/relay-1/positions"
    (positions / "lateral_03_a01.json").write_text("{ truncated")

    assert len(position_cycle_document(tmp_path, derived_at=STAMP)["takes"]) == 2


def test_takes_from_two_relay_sessions_name_both_sources(tmp_path):
    _bank(tmp_path, [_record(1, 0)], relay="relay-1")
    _bank(tmp_path, [_record(2, 7)], relay="relay-2")

    document = position_cycle_document(tmp_path, derived_at=STAMP)

    assert document["sources"] == [
        "bundle/sess-1/crossover_v2/relay-1/positions",
        "bundle/sess-1/crossover_v2/relay-2/positions",
    ]


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
        0: ("lateral_01_a01", "lateral_02_a01", "lateral_03_a01"),
        7: ("lateral_04_a01", "lateral_05_a01", "lateral_06_a01"),
    }


def test_an_uncycled_walk_groups_to_one_take_per_pose(tmp_path):
    _bank(tmp_path, [_record(1, 0), _record(2, 7)])

    assert takes_by_position(
        position_cycle_document(tmp_path, derived_at=STAMP)
    ) == {0: ("lateral_01_a01",), 7: ("lateral_02_a01",)}


def test_a_pose_revisited_later_in_the_walk_keeps_both_visits(tmp_path):
    """``0,7,0`` is a legal walk — the arm returns to the axis — and its two
    on-axis takes are two takes at that pose, not one."""
    _bank(tmp_path, [_record(1, 0), _record(2, 7), _record(3, 0)])

    assert takes_by_position(
        position_cycle_document(tmp_path, derived_at=STAMP)
    ) == {0: ("lateral_01_a01", "lateral_03_a01"), 7: ("lateral_02_a01",)}
