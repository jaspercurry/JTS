# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tool catalog: one complete row per tool, each row a call its parser accepts."""

import argparse
import json
from pathlib import Path

import pytest

from jasper.active_speaker.answer_schemas import ANSWER_SCHEMAS
from jasper.active_speaker.measurement_programs import PURPOSES, available_presets, preset
from jasper.active_speaker.round_view_artifacts import CATALOG, PROG, READS, READS_LAPTOP
from jasper.cli import crossover_prescriber, round as round_cli, round_views

ROOT = Path(__file__).resolve().parents[1]
_PARSERS = {module.PROG: module.build_parser() for module in (round_views, crossover_prescriber, round_cli)}
#: A value each placeholder's parser accepts; any other placeholder takes "value".
_VALUES = {"<db>": "1", "<start>": "0", "<stop>": "1", "<change>": "candidate", "<program>": "speaker"}
_FIELDS = {"tool", "question", "needs", "reads", "programs", "argv", "schema", "artifact", "answer_fields"}


@pytest.mark.parametrize("command", CATALOG)
def test_every_row_is_complete_and_its_call_parses(command):
    row = CATALOG[command]
    assert row.question and "\n" not in row.question and row.needs and row.reads in READS
    assert bool(row.schema) == bool(row.answer_fields)
    prog, *words = command.split()
    argv = [*words, *(_VALUES.get(token, "value") if token.startswith("<") else token for token in row.argv)]
    if prog in _PARSERS:
        _PARSERS[prog].parse_args(argv)
    else:
        assert row.reads == READS_LAPTOP and (ROOT / words[0]).is_file()


def test_every_view_has_a_row_and_every_view_row_a_view():
    choices = next(action for action in _PARSERS[PROG]._actions if isinstance(action, argparse._SubParsersAction))
    assert set(choices.choices) - {"catalog"} == {command.split()[1] for command in CATALOG if command.startswith(PROG)}
    assert all(choice.help.startswith("[") for choice in choices._choices_actions if choice.dest != "catalog")


@pytest.mark.parametrize("program", PURPOSES)
def test_the_catalog_lists_every_tool_a_programs_rounds_can_use(program, capsys):
    """Its presets decide: a rear seat round is read by the room views too."""
    carried = {program, *(purpose for row in map(preset, available_presets()) if row.purpose == program
                          for purpose in row.purposes)}
    assert round_views.main(["catalog", "--program", program]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert (answer["view"], answer["schema"], answer["subject"], answer["parameters"]) == (
        "catalog", ANSWER_SCHEMAS["catalog"], {}, {"program": program})
    assert [tool["tool"] for tool in answer["tools"]] == [
        command for command, row in CATALOG.items() if not row.programs or carried & set(row.programs)]
    assert all(set(tool) == _FIELDS and tool["argv"][:len(tool["tool"].split())] == tool["tool"].split()
               for tool in answer["tools"])
