# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the tuning CLIs speak ONE exit vocabulary, ``jasper/cli/_refusal.py``'s.

Every tool in the runbook's tool menu (``scripts/generate-tuning-tool-menu.py``'s
roster) takes ``EXIT_*`` from that module rather than numbering its own failures.
A tool that re-declares a code drifts silently: the same number came to mean
"refused" in one tool and "unreadable" in the next, which is what this pins shut.
Who is exempt is ``_refusal.OWN_EXIT_VOCABULARY``'s to say, not this file's.

The vocabulary is also what a tool PRINTS, and stdout is where it prints it:
every roster tool driven to a refusal publishes ``failed()``'s document there
and one sentence on stderr, and a success publishes the tool's answer -- one
document, bounded, naming the artifact it wrote rather than inlining it. Both
halves are asserted by calling each tool's own ``main``, because a shape only
holds where the tools actually reach it.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest

from jasper.active_speaker.wizard_client import WizardClient
from jasper.active_speaker.crossover_v2 import prescription_document
from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for
from jasper.cli import _refusal, round_views
from tests.crossover_v2_banked_round import (
    bank_measure_round,
    bank_seat_round,
    bank_verify_round,
)
from tests.room_median_fixture import write_room_median
from tests.run_manifest_fixture import write_manifest
from tests.test_round_views_directivity import BASELINE, _take as directivity_take
from tests.test_round_views_repeat import _mark_take as mark_take

CLI_DIR = Path(_refusal.__file__).resolve().parent

_MENU_SCRIPT = CLI_DIR.parents[1] / "scripts" / "generate-tuning-tool-menu.py"
_spec = importlib.util.spec_from_file_location("generate_tuning_tool_menu", _MENU_SCRIPT)
assert _spec is not None and _spec.loader is not None
_menu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_menu)

SHARED_RULE = tuple(
    name
    for name in _menu.TUNING_TOOL_MODULES
    if name not in _refusal.OWN_EXIT_VOCABULARY
)


def _sources(module_name: str) -> list[Path]:
    """This tool's own source: one module, or every module of a package."""

    leaf = CLI_DIR / module_name.rsplit(".", 1)[-1]
    return sorted(leaf.glob("*.py")) if leaf.is_dir() else [leaf.with_suffix(".py")]


def _declared_exit_names(module_name: str) -> set[str]:
    """The ``EXIT_*`` names this module assigns at module scope.

    Annotated assignments count too: ``EXIT_FOO: int = 4`` is the same drift.
    """

    targets = [
        target
        for path in _sources(module_name)
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
        for target in (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
    ]
    return {
        target.id
        for target in targets
        if isinstance(target, ast.Name) and target.id.startswith("EXIT_")
    }


@pytest.mark.parametrize("module_name", SHARED_RULE)
def test_no_tuning_cli_numbers_its_own_exits(module_name: str) -> None:
    assert _declared_exit_names(module_name) == set()


@pytest.mark.parametrize("module_name", SHARED_RULE)
def test_every_tuning_cli_exit_name_is_the_shared_constant(module_name: str) -> None:
    """The names a tool exposes are ``_refusal``'s, with its values.

    Paired with the AST test above, which is what makes this more than an
    equality check: a module cannot satisfy both by re-typing the numbers.
    """

    module = importlib.import_module(module_name)
    names = {name for name in vars(module) if name.startswith("EXIT_")}
    assert names, f"{module_name} names no exit code"
    for name in names:
        assert getattr(module, name) is getattr(_refusal, name)


@pytest.mark.parametrize("module_name", sorted(_refusal.OWN_EXIT_VOCABULARY))
def test_the_exempt_modules_are_real_and_in_the_menu(module_name: str) -> None:
    """An exemption for a tool that left the menu is an exemption to delete."""

    assert module_name in _menu.TUNING_TOOL_MODULES


@pytest.mark.parametrize("fields", [
    {}, {"code": "measurement_candidate_speaker_mismatch"},
    {"next_action": {"id": "apply_matching_room_layer"}},
    {"code": "measurement_candidate_speaker_mismatch",
     "next_action": {"id": "apply_matching_room_layer"}},
])
@pytest.mark.parametrize(("code", "status"), sorted(_refusal.STATUS_BY_CODE.items()))
def test_the_record_status_and_the_exit_code_always_agree(code, status, fields, capsys):
    assert _refusal.failed(code, "a_slug", {}, **fields) == code
    assert json.loads(capsys.readouterr().out) == {
        "status": status, "reason": "a_slug", "detail": {}, **fields,
    }


def test_the_failing_codes_are_exactly_one_two_three() -> None:
    """A fourth failure word would need a fourth number, and there is none."""

    assert _refusal.EXIT_OK == 0
    assert sorted(_refusal.STATUS_BY_CODE) == [1, 2, 3]


def _basic_profile_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(WizardClient, "open", lambda *a, **kw: (0, "unavailable"))
    return ["review", "--hostname", "jts.local"]


def _audition_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The one roster tool with no refusable INPUT: what it declines is the
    speaker's own state, so the state is what this sets. The refusal lands
    before the writer lock, so nothing reaches CamillaDSP."""

    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: None
    )
    return ["start"]


def _mic_calibration_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The household record is the input here, so its absence is the refusal:
    the door declines to show a mic nothing has registered."""

    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "absent.json")
    )
    return ["show"]


#: One invocation per tool that PASSES argparse and reaches the tool, and that
#: the tool must decline: a round, bundle or spec that is not there, a program
#: nobody ships, a coordinate off the walk's grid, an unavailable door. Argparse's own usage errors are deliberately absent -- the
#: parser exits before the tool can publish anything. None of these touches
#: hardware or the network, and none reaches a measurement door.
_REFUSING_ARGV: dict[str, Callable[[Path, pytest.MonkeyPatch], list[str]]] = {
    "jasper.cli.basic_profile": _basic_profile_argv,
    "jasper.cli.mic_calibration": _mic_calibration_argv,
    "jasper.cli.seat_level": lambda tmp, mp: [
        "--mic-serial", "no-such-serial",
    ],
    "jasper.cli.angle_capture": lambda tmp, mp: [
        "serve", "--attest-rig-clear", "--hostname", "jts.local", "--settle-s", "-1",
    ],
    "jasper.cli.crossover_prescriber": lambda tmp, mp: [
        "contract", "--round", str(tmp / "absent-round"),
    ],
    "jasper.cli.round": lambda tmp, mp: [
        "run", "--poses", "not-a-layout",
    ],
    "jasper.cli.round_views": lambda tmp, mp: ["entry", str(tmp / "absent-round")],
    "jasper.cli.audition": _audition_argv,
}


@pytest.mark.parametrize("module_name", SHARED_RULE)
def test_every_tuning_cli_publishes_the_shared_refusal_document(
    module_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every CLI emits status, reason and detail, with optional code and next_action."""

    module = importlib.import_module(module_name)

    code = module.main(_REFUSING_ARGV[module_name](tmp_path, monkeypatch))

    printed = capsys.readouterr()
    assert code in _refusal.STATUS_BY_CODE
    document = json.loads(printed.out)
    required = {"status", "reason", "detail"}
    assert required <= document.keys() <= required | {"code", "next_action"}
    assert document["status"] == _refusal.STATUS_BY_CODE[code]
    assert printed.err.startswith(
        f"{document['status']} ({document['reason']}): "
    )


@pytest.mark.parametrize("module_name,verb", [
    ("jasper.cli.crossover_prescriber", "judge"),
    ("jasper.cli.crossover_prescriber", "compose"),
    ("jasper.cli.round", "reset"),
])
def test_document_failures_use_the_shared_contract(module_name, verb, tmp_path, monkeypatch, capsys):
    error = prescription_document.PrescriptionDocumentRefused(
        "bass_fit_inputs_missing", "bass", "missing takes", evidence={"round_id": "round-1"})
    def refuse(*args, **kwargs):
        raise error
    monkeypatch.setattr(prescription_document, "saved_base", refuse)
    path = tmp_path / "document.json"
    path.write_text(json.dumps({"kind": "jts_prescription", "schema": 1, "base": "saved",
                                "sections": {}, "rationale": "Test a refusal."}))
    module = importlib.import_module(module_name)
    if verb != "reset":
        monkeypatch.setattr(module, "saved_base", refuse)
    assert module.main([verb, *([] if verb == "reset" else [str(path)])]) == _refusal.EXIT_REFUSED
    printed = capsys.readouterr()
    assert json.loads(printed.out) == {
        "status": "refused", "reason": error.code, "code": error.code,
        "detail": {"section": error.section, "error": error.error, "evidence": error.evidence},
        "next_action": refusal_copy_for(error.code)[1],
    }
    assert printed.err


#: The ceiling on a numeric array an answer may carry: a curve or a grid
#: belongs in the artifact the answer names, never in the answer.
MAX_ANSWER_ARRAY = 16

_NO_CLOUD_GROUP = "the fixture banks no cloud group: no positions, no graded spec"
_NO_CAPTURES = "the fixture banks no WAVs, so no capture ring and no summed takes"


class _FixtureRound(NamedTuple):
    """The two rounds ``tests/crossover_v2_banked_round`` banks -- stage 1's
    solos, ladder and entry baseline, stage 2's VERIFY sum -- and the bundle
    inside the first, which the bundle-taking verbs read instead of the tree."""

    measured: Path
    verified: Path
    bundle: Path
    seat: Path


def _fixture_round(root: Path) -> _FixtureRound:
    measured = bank_measure_round(root, candidates=("cand-a", "cand-b"))
    bundle, = (measured / "bundle").iterdir()
    return _FixtureRound(
        measured=measured, verified=bank_verify_round(root), bundle=bundle,
        seat=bank_seat_round(root),
    )


def _room_grade_argv(round_: _FixtureRound) -> list[str]:
    write_room_median(round_.measured)
    return ["room-grade", str(round_.measured)]


def _directivity_argv(round_: _FixtureRound) -> list[str]:
    takes = [directivity_take(index, pose, level) for index, (pose, level) in enumerate(BASELINE)]
    write_manifest(round_.measured, groups=[{"set_id": "woofer", "capture_basis": {"role": "woofer"}, "takes": takes}])
    return ["directivity", str(round_.measured), "--set", "woofer"]


def _repeat_argv(round_: _FixtureRound) -> list[str]:
    for root in (round_.measured, round_.verified):
        write_manifest(root, groups=[{"set_id": root.name, "capture_basis": {"role": "woofer"},
                                      "takes": [mark_take(f"{root.name}-{i}", 0.5 * i) for i in range(2)]}])
    return ["repeat", str(round_.measured), str(round_.verified)]


#: How each view is run against that round -- or, for a view this fixture
#: cannot feed, why not.
_VIEW_RUN: dict[str, str | Callable[[_FixtureRound], list[str]]] = {
    "entry": lambda r: ["entry", str(r.measured)],
    "frozen": _NO_CLOUD_GROUP,
    "per-seat": _NO_CLOUD_GROUP,
    "repeat": _repeat_argv,
    "repeat-floor": _NO_CLOUD_GROUP,
    "candidates": lambda r: ["candidates", str(r.measured)],
    "directivity": _directivity_argv,
    "cloud-binding": lambda r: ["cloud-binding", str(r.measured)],
    "speaker-fit": "answer-only fit inputs are covered in test_round_views_speaker_fit",
    "sweep": _NO_CAPTURES,
    "frequency": lambda r: ["frequency", str(r.measured)],
    "distortion": _NO_CAPTURES,
    "bass": _NO_CAPTURES,
    "bass-compare": _NO_CAPTURES,
    "bass-fit-table": _NO_CAPTURES,
    "rear": "a banked rear batch is covered in test_round_views_rear_cli",
    "dsp-replay": _NO_CAPTURES,
    "dsp-levels": _NO_CAPTURES,
    "classify-features": _NO_CAPTURES,
    "findings": lambda r: ["findings", str(r.measured)],
    "room-grade": _room_grade_argv,
    "close-reference": _NO_CAPTURES,
    "room": lambda r: ["room", str(r.seat)],
    "delay-landscape": lambda r: ["delay-landscape", str(r.bundle), "--fc-hz", "1800"],
    "inventory": lambda r: ["inventory", str(r.measured)],
}


def _numeric_arrays(node: Any) -> Iterator[list[Any]]:
    """Every array carrying numbers in a document, at any depth.

    ANY number makes an array one, not every: a curve with a null at an
    excluded bin is the same curve, and a rule keyed on "all of them" would
    let exactly the longest ones through.
    """

    if isinstance(node, Mapping):
        for value in node.values():
            yield from _numeric_arrays(value)
    elif isinstance(node, list):
        if any(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in node
        ):
            yield node
        for item in node:
            yield from _numeric_arrays(item)


@pytest.mark.parametrize(
    "view", _menu._subcommand_names(round_views.build_parser())
)
def test_a_view_that_succeeds_prints_one_bounded_answer(
    view: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A success is an ANSWER: one document, no failure word in it, and the
    artifact named rather than poured onto the operator's terminal."""

    argv = _VIEW_RUN[view]
    if isinstance(argv, str):
        pytest.skip(argv)
    # A view of a LIVE session bundle lands beside the CALLER, so the caller
    # stands in the temporary directory.
    monkeypatch.chdir(tmp_path)

    code = round_views.main(argv(_fixture_round(tmp_path)))

    printed = capsys.readouterr()
    assert code == _refusal.EXIT_OK
    answer = json.loads(printed.out)
    assert "status" not in answer
    assert max((len(a) for a in _numeric_arrays(answer)), default=0) <= MAX_ANSWER_ARRAY
    written = Path(answer["out"])
    assert written.is_file()
    assert written.stat().st_size == answer["bytes"]
