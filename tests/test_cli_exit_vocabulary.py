# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the tuning CLIs speak ONE exit vocabulary, ``jasper/cli/_refusal.py``'s.

Every tool in the runbook's tool menu (``scripts/generate-tuning-tool-menu.py``'s
roster) takes ``EXIT_*`` from that module rather than numbering its own failures.
A tool that re-declares a code drifts silently: the same number came to mean
"refused" in one tool and "unreadable" in the next, which is what this pins shut.
Two doors are deliberately outside and named here, once.
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

#: By module stem: the exit names that module may own, and why. Everything
#: else in ``jasper/cli`` imports them (``_refusal.py``'s docstring agrees).
OWN_VOCABULARY = {
    # A human-only sudo `set`/`show` config door: `show` before anything was
    # declared is not an unreadable input.
    "declare_geometry": {"EXIT_NOT_FOUND"},
}

#: The one tool whose codes are a family of its own: a long-running mover
#: service whose stall codes and signal exits live in
#: ``jasper/active_speaker/arm_walk.py``'s ``EXIT_NAMES``.
OWN_FAMILY = ("jasper.cli.arm_walk",)


def _declared_exit_names(path: Path) -> set[str]:
    """The ``EXIT_*`` names this module assigns at module scope.

    Annotated assignments count too: ``EXIT_FOO: int = 4`` is the same drift.
    """

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


@pytest.mark.parametrize(
    "path",
    [p for p in sorted(CLI_DIR.glob("*.py")) if p.name != "_refusal.py"],
    ids=lambda p: p.stem,
)
def test_no_cli_numbers_its_own_exits(path: Path) -> None:
    assert _declared_exit_names(path) <= OWN_VOCABULARY.get(path.stem, set())


@pytest.mark.parametrize("module_name", _menu.TUNING_TOOL_MODULES)
def test_every_tuning_cli_exit_name_is_the_shared_constant(module_name: str) -> None:
    """The names a tool exposes are ``_refusal``'s, with its values.

    Paired with the AST test above, which is what makes this more than an
    equality check: a module cannot satisfy both by re-typing the numbers.
    """

    if module_name in OWN_FAMILY:
        pytest.skip("its own stall-code family, named in _refusal.py's docstring")
    module = importlib.import_module(module_name)
    own = OWN_VOCABULARY.get(module_name.rsplit(".", 1)[-1], set())
    names = {name for name in vars(module) if name.startswith("EXIT_")} - own
    assert names, f"{module_name} names no exit code"
    for name in names:
        assert getattr(module, name) is getattr(_refusal, name)


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
