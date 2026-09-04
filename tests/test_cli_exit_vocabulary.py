# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the tuning CLIs speak ONE exit vocabulary, ``jasper/cli/_refusal.py``'s.

Every tool in the runbook's tool menu (``scripts/generate-tuning-tool-menu.py``'s
roster) takes ``EXIT_*`` from that module rather than numbering its own failures.
A tool that re-declares a code drifts silently: the same number came to mean
"refused" in one tool and "unreadable" in the next, which is what this pins shut.
Who is exempt is ``_refusal.OWN_EXIT_VOCABULARY``'s to say, not this file's.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

from jasper.cli import _refusal

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


def _declared_exit_names(module_name: str) -> set[str]:
    """The ``EXIT_*`` names this module assigns at module scope.

    Annotated assignments count too: ``EXIT_FOO: int = 4`` is the same drift.
    """

    path = CLI_DIR / f"{module_name.rsplit('.', 1)[-1]}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets = [
        target
        for node in tree.body
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
