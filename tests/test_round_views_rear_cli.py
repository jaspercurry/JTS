# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-round-views rear`` reads the banked comparison back exactly as
``packet.json`` carries it, and ``index.md`` names it (issue #5330)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from jasper.active_speaker.round_packet_report import INDEX_FILENAME
from jasper.active_speaker.round_view_artifacts import REASON_REFUSED
from jasper.cli import round_views
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED
from tests.crossover_v2_banked_round import bank_seat_round
from tests.run_manifest_fixture import write_manifest
from tests.test_round_views_rear import banked_candidates, packet_of, rear_round

# Re-exported so pytest resolves the fixture by name when imported into this
# module -- the fixture is defined once, in the module that owns rear_round.
__all__ = ["banked_candidates"]


def test_rear_prints_the_banked_entries(tmp_path, banked_candidates, capsys):
    root = rear_round(tmp_path)
    packet, _views = packet_of(root)

    code = round_views.main(["rear", str(root)])

    assert code == EXIT_OK
    answer = json.loads(capsys.readouterr().out)
    assert answer["view"] == "rear"
    assert answer["entries"] == packet["rear"]
    written = Path(answer["out"])
    assert written.is_file() and written.stat().st_size == answer["bytes"]


def test_rear_takes_no_set_argument():
    with pytest.raises(SystemExit) as exc:
        round_views.build_parser().parse_args(["rear", "round", "--set", "a"])
    assert exc.value.code == 2


def test_rear_refuses_a_round_with_no_rear_entries(tmp_path, capsys):
    root = bank_seat_round(tmp_path)
    write_manifest(root, program="room")
    packet_of(root)

    code = round_views.main(["rear", str(root)])

    assert code == EXIT_REFUSED
    document = json.loads(capsys.readouterr().out)
    assert document["reason"] == REASON_REFUSED


def test_index_names_the_rear_candidates_and_the_tool(tmp_path, banked_candidates):
    root = rear_round(tmp_path)
    packet, _views = packet_of(root)

    index = (root / INDEX_FILENAME).read_text()
    lines = index.splitlines()
    entry, = packet["rear"]
    assert any(line.startswith(f"rear {entry['set_id'][:12]}:") for line in lines)
    for candidate in entry["candidates"]:
        assert any(
            line.startswith(f"  {candidate['candidate_id'][:12]} {candidate['role']}:")
            for line in lines
        )
    tool_line = next(line for line in lines if "jasper-round-views rear" in line)
    assert tool_line == shlex.join(["jasper-round-views", "rear", str(root)])


def test_a_room_rounds_index_gets_no_rear_lines(tmp_path):
    root = bank_seat_round(tmp_path)
    write_manifest(root, program="room")
    packet_of(root)

    index = (root / INDEX_FILENAME).read_text()
    assert not any(line.startswith("rear ") for line in index.splitlines())
    assert "jasper-round-views rear" not in index
