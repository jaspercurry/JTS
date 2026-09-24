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

from jasper.active_speaker.measurement_programs import BASE_CANDIDATE
from jasper.active_speaker.crossover_v2.round_inputs import INDEX_FILENAME, PACKET_FILENAME
from jasper.audio_measurement.evidence_reasons import REASON_NO_COMPARISON, REASON_NO_REPEATS
from jasper.cli import round_views
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED
from jasper.cli.round_views.rear import REFUSE_NO_REAR
from tests.crossover_v2_banked_round import bank_seat_round
from tests.run_manifest_fixture import write_manifest
from tests.test_round_views_rear import (
    _MUTED, _VARIANT, banked_candidates, packet_of, pair_round, rear_round,
)

# Re-exported so pytest resolves the fixture by name when imported into this
# module -- the fixture is defined once, in the module that owns rear_round.
__all__ = ["banked_candidates"]


@pytest.mark.parametrize("reason", [None, "no_repeats", "insufficient_positions",
                                   "insufficient_agreement_seats", "unknown_analysis_reason"])
def test_rear_prints_the_banked_entries(tmp_path, banked_candidates, capsys, reason):
    root = rear_round(tmp_path)
    packet, _views = packet_of(root)
    if reason is not None:
        packet["rear"][0]["comparison"]["repeat_spread"]["reason"] = reason
        (root / PACKET_FILENAME).write_text(json.dumps(packet))

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
    assert document["reason"] == REFUSE_NO_REAR


#: One round per rendering branch of ``rear_lines``: the default fixture
#: scores every candidate at every position; dropping to one repeat leaves
#: ``repeat_spread`` with no comparable difference; starving one candidate of
#: any position the reference also measured leaves it with no pooled regression
#: to report at all; and a PAIR round has one played candidate, so it renders
#: the batch line with no candidate lines under it at all.
_INDEX_CASES = (
    pytest.param(lambda tmp_path: rear_round(tmp_path), "repeat_spread {",
                 id="every_candidate_scored"),
    pytest.param(lambda tmp_path: rear_round(tmp_path, candidates=(BASE_CANDIDATE, _VARIANT), repeats=1),
                 f"repeat_spread {REASON_NO_REPEATS}", id="no_repeats"),
    pytest.param(lambda tmp_path: rear_round(tmp_path, missing={_MUTED: (-20, 20), _VARIANT: (0,)}),
                 "worst_regression=unavailable", id="a_candidate_with_no_comparable_position"),
    pytest.param(lambda tmp_path: pair_round(tmp_path),
                 f"repeat_spread {REASON_NO_COMPARISON}", id="a_pair_round"),
)


@pytest.mark.parametrize("build_round, expected_fragment", _INDEX_CASES)
def test_index_names_the_rear_candidates_and_the_tool(
    tmp_path, banked_candidates, build_round, expected_fragment,
):
    root = build_round(tmp_path)
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
    assert any(expected_fragment in line for line in lines)


def test_a_room_rounds_index_gets_no_rear_lines(tmp_path):
    root = bank_seat_round(tmp_path)
    write_manifest(root, program="room")
    packet_of(root)

    index = (root / INDEX_FILENAME).read_text()
    assert not any(line.startswith("rear ") for line in index.splitlines())
    assert "jasper-round-views rear" not in index


def test_a_pair_rounds_index_names_the_null_and_the_front_rear_comparisons(tmp_path, banked_candidates):
    root = pair_round(tmp_path, behind_gap_ms=0.5, sidecar_curves=False)
    packet_of(root)
    index = (root / INDEX_FILENAME).read_text()
    commands = [shlex.split(line.strip("`- ")) for line in index.splitlines() if "jasper-round-views compare" in line]

    assert "behind the speaker" in index
    roles = sorted(tuple(command[command.index(flag) + 1] for flag in ("--a-role", "--b-role")) for command in commands)
    # Front against rear woofer at each pose kind, and front against behind for the sum.
    assert roles == [("summed", "summed"), ("woofer", "woofer:rear"), ("woofer", "woofer:rear")]
