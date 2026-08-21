# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The staged-walk expansion and the manifest that records it."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_CYCLE_KIND,
    SCHEMA_VERSION,
    PositionCycleError,
    expand_angle_spec,
    position_cycle_document,
    read_position_cycle,
    staged_stops,
    stops_by_position,
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


@pytest.mark.parametrize("per_position", [0, -1])
def test_fewer_than_one_take_is_refused(per_position):
    with pytest.raises(PositionCycleError, match="at least 1"):
        expand_angle_spec("0,7", per_position)


def test_an_empty_token_is_refused_rather_than_staged():
    """A typed comma is not a stop; the seam would read it as an angle."""
    with pytest.raises(PositionCycleError, match="empty angle"):
        expand_angle_spec("0,,7", 2)


def test_there_is_no_ceiling_here_because_the_relay_owns_that_bound():
    """A second, lower ceiling invented on the laptop would refuse walks the
    speaker would have taken — ``session_lateral_walk`` refuses by name."""
    assert staged_stops(expand_angle_spec("0", 64)) == 64


def test_staged_stops_counts_what_the_walk_will_serve():
    assert staged_stops("0,7,-7") == 3
    assert staged_stops(expand_angle_spec("0,7,-7", 4)) == 12


# --------------------------------------------------------------------------- #
# the manifest
# --------------------------------------------------------------------------- #


def _document(angles: str = "0,7", per_position: int = 3) -> dict:
    return position_cycle_document(
        angles=angles, per_position=per_position, regime="per_driver",
        created_at=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
    )


def test_the_document_maps_every_stop_to_a_pose_and_a_take():
    document = _document()
    assert document["stops"] == [
        {"stop": 1, "angle": "0", "take": 1},
        {"stop": 2, "angle": "0", "take": 2},
        {"stop": 3, "angle": "0", "take": 3},
        {"stop": 4, "angle": "7", "take": 1},
        {"stop": 5, "angle": "7", "take": 2},
        {"stop": 6, "angle": "7", "take": 3},
    ]


def test_the_stop_order_is_the_order_the_walk_serves_them():
    """Same 1-based running order ``ResolvedStop.index`` is built in, so a stop
    ordinal here and a stop ordinal there are the same number."""
    document = _document(angles="0,7,-7", per_position=2)
    assert [stop["stop"] for stop in document["stops"]] == [1, 2, 3, 4, 5, 6]
    assert document["staged_angles"] == expand_angle_spec("0,7,-7", 2)


def test_both_what_was_asked_and_what_was_staged_are_recorded():
    """"Was this round cycled" and "what did stop 4 measure" are different
    questions; neither reader should have to re-derive the other's half."""
    document = _document()
    assert document["angles"] == "0,7"
    assert document["staged_angles"] == "0,0,0,7,7,7"
    assert document["per_position"] == 3


def test_the_document_names_itself_and_its_schema():
    document = _document()
    assert document["kind"] == POSITION_CYCLE_KIND
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["created_at"] == "2026-08-21T12:00:00Z"


def test_no_config_fingerprint_is_written_because_the_runner_never_played_one():
    """The graph a capture went through is the SPEAKER's fact, recorded per
    capture as ``provenance.graph.fingerprint``. A fingerprint written here
    would be intent, which is the wrong fact and the one that is wrong when it
    matters."""
    assert not [key for key in _document() if "fingerprint" in key]
    assert all("fingerprint" not in stop for stop in _document()["stops"])


def test_an_empty_angle_list_is_not_a_walk():
    with pytest.raises(PositionCycleError):
        position_cycle_document(angles="", per_position=1, regime="per_driver")


# --------------------------------------------------------------------------- #
# the reader
# --------------------------------------------------------------------------- #


def _written(tmp_path: Path, document) -> Path:
    path = tmp_path / "position_cycle.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path


def test_a_written_document_reads_back_identical(tmp_path):
    document = _document(angles="0,7,-7,22,-22", per_position=3)
    assert read_position_cycle(_written(tmp_path, document)) == document


def test_an_unknown_key_is_an_error_not_a_silent_drop(tmp_path):
    """A key this reader does not know is either a newer schema or a hand edit,
    and reading a NEWER document as an older one silently is the failure this
    document's whole job — being believed later — cannot survive."""
    document = dict(_document(), graph_fingerprint="cand-4f2a9b")
    with pytest.raises(PositionCycleError, match="unknown keys"):
        read_position_cycle(_written(tmp_path, document))


def test_a_missing_key_is_an_error(tmp_path):
    document = _document()
    document.pop("per_position")
    with pytest.raises(PositionCycleError, match="missing keys"):
        read_position_cycle(_written(tmp_path, document))


def test_another_documents_kind_is_refused(tmp_path):
    document = dict(_document(), kind="jts_crossover_v2_round_receipt")
    with pytest.raises(PositionCycleError, match="kind is"):
        read_position_cycle(_written(tmp_path, document))


def test_a_future_schema_version_is_refused(tmp_path):
    document = dict(_document(), schema_version=SCHEMA_VERSION + 1)
    with pytest.raises(PositionCycleError, match="schema_version"):
        read_position_cycle(_written(tmp_path, document))


def test_stops_out_of_walk_order_are_refused(tmp_path):
    """The join a comparison makes is positional, so a renumbered list would
    put a curve at a pose it was never measured at."""
    document = _document()
    document["stops"][2]["stop"] = 9
    with pytest.raises(PositionCycleError, match="1-based and in walk order"):
        read_position_cycle(_written(tmp_path, document))


def test_a_stop_carrying_an_extra_field_is_refused(tmp_path):
    document = _document()
    document["stops"][0]["degrees"] = 0
    with pytest.raises(PositionCycleError, match="must carry exactly"):
        read_position_cycle(_written(tmp_path, document))


def test_an_unreadable_file_is_this_modules_error_not_an_oserror(tmp_path):
    with pytest.raises(PositionCycleError):
        read_position_cycle(tmp_path / "absent.json")


def test_a_json_array_is_refused(tmp_path):
    path = tmp_path / "position_cycle.json"
    path.write_text("[]")
    with pytest.raises(PositionCycleError, match="not a JSON object"):
        read_position_cycle(path)


# --------------------------------------------------------------------------- #
# the split
# --------------------------------------------------------------------------- #


def test_the_takes_that_share_one_pose_are_grouped_in_walk_order():
    assert stops_by_position(_document(angles="0,7,-7", per_position=3)) == {
        "0": (1, 2, 3),
        "7": (4, 5, 6),
        "-7": (7, 8, 9),
    }


def test_an_uncycled_walk_groups_to_one_stop_per_pose():
    assert stops_by_position(_document(angles="0,7", per_position=1)) == {
        "0": (1,), "7": (2,),
    }


def test_a_pose_visited_twice_in_one_list_keeps_both_visits():
    """``0,7,0`` is a legal walk — the arm returns to the axis — and its two
    on-axis stops are two takes at that pose, not one."""
    assert stops_by_position(_document(angles="0,7,0", per_position=1)) == {
        "0": (1, 3), "7": (2,),
    }
