# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tool catalog: one complete row per tool, each row a call its parser accepts,
each view's and prescriber verb's --help rendered from its rows, and with a round,
the one list of what to run next on it."""

import argparse
import json
import shlex
from pathlib import Path

import pytest

from jasper.active_speaker.answer_schemas import ANSWER_SCHEMAS
from jasper.active_speaker.crossover_v2.round_inputs import INDEX_FILENAME
from jasper.active_speaker.measurement_programs import PURPOSES, available_presets, preset
from jasper.active_speaker.round_catalog import catalog_command, round_calls
from jasper.active_speaker.round_view_artifacts import CATALOG, PROG, READS, READS_LAPTOP, bookkeeping_views
from jasper.cli import crossover_prescriber, round as round_cli, round_views
from tests.crossover_v2_banked_round import bank_seat_round
from tests.run_manifest_fixture import write_manifest
from tests.test_round_views_rear import packet_of

ROOT = Path(__file__).resolve().parents[1]
_PARSERS = {module.PROG: module.build_parser() for module in (round_views, crossover_prescriber, round_cli)}
#: The tools whose verbs' --help renders from their rows.
_VERBS = {prog: next(action for action in _PARSERS[prog]._actions if isinstance(action, argparse._SubParsersAction))
          for prog in (PROG, crossover_prescriber.PROG)}
_VIEWS = _VERBS[PROG]
#: A value each placeholder's parser accepts; any other placeholder takes "value".
_VALUES = {"<db>": "1", "<start>": "0", "<stop>": "1", "<change>": "candidate", "<program>": "room"}
_FIELDS = {"tool", "question", "needs", "reads", "programs", "argv", "schema", "artifact", "answer_fields"}
_CALL = {"argv", "set_id", "take_id", "needs", "out", "present", "bytes"}


@pytest.mark.parametrize("command", CATALOG)
def test_every_row_is_complete_and_its_call_parses_for_each_program_it_names(command):
    row = CATALOG[command]
    assert row.question and "\n" not in row.question and row.needs and row.reads in READS
    assert (row.avoid or not command.startswith(tuple(_VERBS))) and "\n" not in row.avoid
    assert bool(row.schema) == bool(row.answer_fields)
    prog, *words = command.split()
    for program in row.programs or PURPOSES:
        values = {**_VALUES, "<program>": program}
        argv = [*words, *(values.get(token, "value") if token.startswith("<") else token for token in row.argv)]
        if prog in _PARSERS:
            _PARSERS[prog].parse_args(argv)
        else:
            assert row.reads == READS_LAPTOP and (ROOT / words[0]).is_file()


def test_every_view_has_a_row_and_every_view_row_a_view():
    assert set(_VIEWS.choices) - {"catalog"} == {command.split()[1] for command in CATALOG if command.startswith(PROG)}
    assert all(choice.help.startswith("[") for choice in _VIEWS._choices_actions if choice.dest != "catalog")


@pytest.mark.parametrize("prog, verb", [(prog, verb) for prog, verbs in _VERBS.items() for verb in verbs.choices])
def test_every_verbs_help_has_a_description_and_examples_its_parser_accepts(prog, verb):
    parser = _VERBS[prog].choices[verb]
    examples = [line.split()[1:] for line in parser.format_help().splitlines() if line.startswith(f"  {prog} ")]
    assert parser.description and examples
    for argv in examples:
        _PARSERS[prog].parse_args([_VALUES.get(token, "value") if token.startswith("<") else token for token in argv])


@pytest.mark.parametrize("program", PURPOSES)
def test_the_catalog_lists_every_tool_a_programs_rounds_can_use(program, capsys):
    """The rows naming the program, and every view its rounds' bookkeeping publishes:
    a rear seat round is read by the room views too."""
    published = {f"{PROG} {name}" for row in map(preset, available_presets()) if row.purpose == program
                 for name, _, _ in bookkeeping_views(row.purposes)}
    own = {command for command, row in CATALOG.items() if not row.programs or program in row.programs}
    assert round_views.main(["catalog", "--program", program]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert (answer["view"], answer["schema"], answer["subject"], answer["parameters"]) == (
        "catalog", ANSWER_SCHEMAS["catalog"], {}, {"program": program})
    assert own | (published & CATALOG.keys()) <= {tool["tool"] for tool in answer["tools"]}
    assert all(set(tool) == _FIELDS and tool["argv"][:len(tool["tool"].split())] == tool["tool"].split()
               for tool in answer["tools"])


def _catalog(capsys, *argv: str) -> dict:
    assert round_views.main(["catalog", *argv]) == round_views.EXIT_OK
    return json.loads(capsys.readouterr().out)


def test_a_rounds_index_and_its_catalog_list_what_to_run_next_from_one_function(tmp_path, capsys):
    """The index names the catalog call, then each call the catalog fills from the round
    alone. A call that needs another input keeps it as a placeholder, a one-set round's
    view the bank publishes names the bank's file, the frequency call reads the bank's
    view and leaves it as it is, and a call runs as given and files what it names."""
    root = bank_seat_round(tmp_path / "rounds 'quoted' $(x)")
    write_manifest(root, program="room")
    packet_of(root)

    answer = _catalog(capsys, str(root))

    assert (answer["view"], answer["schema"], answer["subject"], answer["parameters"]) == (
        "catalog", ANSWER_SCHEMAS["catalog"], {"round_id": root.name}, {"program": None})
    assert all(set(tool) == _FIELDS | {"calls"} and all(set(call) == _CALL for call in tool["calls"])
               for tool in answer["tools"])
    calls = {tuple(call["argv"]): call for tool in answer["tools"] for call in tool["calls"]}
    index = (root / INDEX_FILENAME).read_text().splitlines()
    assert [shlex.split(line[3:-1]) for line in index[index.index("## Tools"):] if line.startswith("- `")] == [
        shlex.split(catalog_command(root)), *(list(argv) for argv, call in calls.items() if not call["needs"])]
    assert (calls[PROG, "repeat", str(root), "<other-round>"]["needs"], calls[PROG, "room", str(root)]["present"]) == (
        ["<other-round>"], True)

    view = Path(calls[PROG, "frequency", str(root)]["out"])
    banked = view.read_bytes()
    assert round_views.main([PROG, "frequency", str(root)][1:]) == round_views.EXIT_OK
    assert "out" not in json.loads(capsys.readouterr().out) and view.read_bytes() == banked
    grade = calls[PROG, "room-grade", str(root)]
    Path(grade["out"]).unlink()
    assert round_views.main(grade["argv"][1:]) == round_views.EXIT_OK
    capsys.readouterr()
    again = next(call for tool in _catalog(capsys, str(root))["tools"] for call in tool["calls"]
                 if call["argv"] == grade["argv"])
    assert (again["present"], again["bytes"]) == (True, Path(grade["out"]).stat().st_size)
    assert _catalog(capsys, str(root), "--program", "bass")["tools"] == []
    assert round_views.main(["catalog", str(root), "--set", "unknown"]) == round_views.EXIT_REFUSED


def _one_set(preset: str, role: str) -> dict:
    """A one-set round of ``preset`` whose set measured ``role``: one kept take in front and one behind."""
    return {"preset": preset, "sets": [{"set_id": "s", "capture_basis": {"role": role},
            "takes": [{"take_id": kind, "selected": True, "pose": {"kind": kind, "deg": 0, "elevation_deg": 0},
                       "curves": [{"role": role, "window": "gated" if role != "summed" else "ungated"}]}
                      for kind in ("bearing", "behind")]}]}


@pytest.mark.parametrize("preset,role", [("speaker/mark", "woofer"), ("speaker/mark", "summed"), ("room/seat", "summed")])
def test_a_one_set_rounds_calls_parse_name_their_set_and_file_apart(preset, role):
    """Only a view the bank publishes leaves out a one-set round's --set: speaker-fit
    requires it, and the round ladder reads other takes without it. speaker-fit reads
    only a set that measured one driver, and each take's call files its own artifact."""
    ready = [call for call in round_calls(Path("round"), _one_set(preset, role)) if not call["needs"]]
    for call in ready:
        _PARSERS[call["argv"][0]].parse_args(call["argv"][1:])
        row = CATALOG[call["tool"]]
        assert ("--set" in call["argv"]) == ("<set-id>" in row.argv and not row.bookkeeping)
    artifacts = [call["artifact"] for call in ready if call["take_id"] and call["artifact"]]
    assert artifacts and len(set(artifacts)) == len(artifacts)
    assert {call["take_id"] for call in ready if call["tool"] == f"{PROG} speaker-fit"} == (
        {"bearing", "behind"} if preset == "speaker/mark" and role != "summed" else set())
