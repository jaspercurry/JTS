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
import socket
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest

from jasper.cli import _refusal, round_views
from tests.crossover_v2_banked_round import bank_measure_round, bank_verify_round

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


@pytest.mark.parametrize(("code", "status"), sorted(_refusal.STATUS_BY_CODE.items()))
def test_the_record_status_and_the_exit_code_always_agree(
    code: int, status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _refusal.failed(code, "a_slug", "a detail") == code
    out = capsys.readouterr()
    assert f'"status": "{status}"' in out.out
    assert out.err.startswith(f"{status} (a_slug): ")


def test_the_failing_codes_are_exactly_one_two_three() -> None:
    """A fourth failure word would need a fourth number, and there is none."""

    assert _refusal.EXIT_OK == 0
    assert sorted(_refusal.STATUS_BY_CODE) == [1, 2, 3]


#: A loopback port bound for the life of this module and never listened on:
#: connecting to it is REFUSED, and no other process can take it meanwhile. A
#: door there is one whose answer is LOST, which is the tool's other failure.
_HELD = socket.socket()
_HELD.bind(("127.0.0.1", 0))
UNANSWERED_URL = f"http://127.0.0.1:{_HELD.getsockname()[1]}"


def _audition_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The one roster tool with no refusable INPUT: what it declines is the
    speaker's own state, so the state is what this sets. The refusal lands
    before the writer lock, so nothing reaches CamillaDSP."""

    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: None
    )
    return ["start"]


#: One invocation per tool that PASSES argparse and reaches the tool, and that
#: the tool must decline: a round, bundle or spec that is not there, a program
#: nobody ships, a coordinate off the walk's grid, a door on a port nothing
#: listens on. Argparse's own usage errors are deliberately absent -- the
#: parser exits before the tool can publish anything. None of these touches
#: hardware or the network, and none reaches a measurement door.
_REFUSING_ARGV: dict[str, Callable[[Path, pytest.MonkeyPatch], list[str]]] = {
    "jasper.cli.basic_profile": lambda tmp, mp: [
        "review", "--hostname", "jts.local", "--base-url", UNANSWERED_URL,
    ],
    "jasper.cli.seat_level": lambda tmp, mp: [
        "--mic-serial", "no-such-serial", "--stimulus-wav", str(tmp / "absent.wav"),
    ],
    "jasper.cli.angle_capture": lambda tmp, mp: [
        "plan", "--program", "baseline", "--size", "no-such-size",
    ],
    "jasper.cli.measure": lambda tmp, mp: [
        "--kind", "baseline", "--specs", str(tmp / "absent-specs.json"),
    ],
    "jasper.cli.crossover_prescriber": lambda tmp, mp: [
        "packet", str(tmp / "absent-round"),
    ],
    "jasper.cli.round": lambda tmp, mp: [
        "bank", str(tmp / "absent-session"), "--campaign-root", str(tmp / "campaign"),
    ],
    "jasper.cli.round_views": lambda tmp, mp: ["entry", str(tmp / "absent-round")],
    # 1 us is off every fine grid the walk offers, so this refuses before the
    # measurement door opens on a speaker that HAS a crossover to measure.
    "jasper.cli.null_door": lambda tmp, mp: [
        "--bundle-dir", str(tmp), "--fc-hz", "1800", "--delays=1",
    ],
    "jasper.cli.audition": _audition_argv,
}


@pytest.mark.parametrize("module_name", SHARED_RULE)
def test_every_tuning_cli_publishes_the_shared_refusal_document(
    module_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refusal is an OUTPUT: one ``failed()`` document, on stdout, always.

    Driven through each tool's own ``main`` rather than asserted of
    ``_refusal`` alone, which is the only way to catch a tool that owns the
    shared codes and still prints its refusal somewhere else -- or nowhere.
    """

    module = importlib.import_module(module_name)

    code = module.main(_REFUSING_ARGV[module_name](tmp_path, monkeypatch))

    printed = capsys.readouterr()
    assert code in _refusal.STATUS_BY_CODE
    document = json.loads(printed.out)
    assert set(document) == {"status", "reason", "detail"}
    assert document["status"] == _refusal.STATUS_BY_CODE[code]
    assert printed.err.startswith(
        f"{document['status']} ({document['reason']}): "
    )


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


def _fixture_round(root: Path) -> _FixtureRound:
    measured = bank_measure_round(root, candidates=("cand-a", "cand-b"))
    bundle, = (measured / "bundle").iterdir()
    return _FixtureRound(
        measured=measured, verified=bank_verify_round(root), bundle=bundle,
    )


#: How each view is run against that round -- or, for a view this fixture
#: cannot feed, why not.
_VIEW_RUN: dict[str, str | Callable[[_FixtureRound], list[str]]] = {
    "entry": lambda r: ["entry", str(r.measured)],
    "frozen": _NO_CLOUD_GROUP,
    "per-seat": _NO_CLOUD_GROUP,
    "repeat": _NO_CLOUD_GROUP,
    "repeat-floor": _NO_CLOUD_GROUP,
    "candidates": lambda r: ["candidates", str(r.measured)],
    "agreement": _NO_CLOUD_GROUP,
    "co-metrics": _NO_CLOUD_GROUP,
    "directivity": _NO_CLOUD_GROUP,
    "cloud-binding": lambda r: ["cloud-binding", str(r.measured)],
    "forward-model": lambda r: [
        "forward-model", str(r.measured), "--measured-round", str(r.verified),
    ],
    "spec-sweep": _NO_CLOUD_GROUP,
    "gate-sweep": _NO_CAPTURES,
    "frequency": lambda r: ["frequency", str(r.measured)],
    "distortion": _NO_CAPTURES,
    "classify-features": _NO_CAPTURES,
    "close-reference": _NO_CAPTURES,
    "delay-landscape": lambda r: ["delay-landscape", str(r.bundle), "--fc-hz", "1800"],
    "delay-confirm": "the fixture banks no null_runs rows; jasper-null writes those",
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
