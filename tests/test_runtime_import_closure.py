# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tuning truth layer imports nothing from the product runtime.

Structural, not behavioural: the walk reads ``ast`` import nodes, so it sees
the edge a convenience re-export adds before anyone runs the code. Three
questions per module — what its own file imports at any depth, whether every
relative import still resolves, and what its module-scope import closure
executes.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The runtime packages the truth layer may not reach.
FORBIDDEN = (
    "jasper.web",
    "jasper.control",
    "jasper.voice_daemon",
    "jasper.mux",
    "jasper.camilla",
)

#: The active-speaker truth layer (ADR-0228 entries 1 and 8). ``startup_load`` is deliberately
#: absent: lane A's load transaction stops and starts units through
#: ``jasper.control.restart_broker``, which is a real runtime dependency.
TRUTH_LAYER = (
    "jasper.active_speaker.baseline_profile",
    "jasper.active_speaker.camilla_yaml",
    "jasper.active_speaker.driver_safety",
    "jasper.active_speaker.graph_safety",
    "jasper.active_speaker.linearization_envelope",
    "jasper.active_speaker.linearization_fit",
    "jasper.active_speaker.measurement",
    "jasper.active_speaker.path_safety",
    "jasper.active_speaker.profile",
    "jasper.active_speaker.runtime_contract",
    "jasper.active_speaker.staging",
    "jasper.active_speaker.state_paths",
)


def _module_path(dotted: str) -> Path | None:
    base = REPO_ROOT / Path(*dotted.split("."))
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    leaf = base.with_suffix(".py")
    return leaf if leaf.exists() else None


def _dotted(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _package_exports(init: Path) -> set[str]:
    """Module-scope names an ``__init__.py`` binds.

    Tells ``from pkg import name`` (an attribute) apart from
    ``from pkg import submodule`` (a module that has to exist on disk).
    """

    names: set[str] = set()
    for node in ast.walk(ast.parse(init.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _guards_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _import_nodes(
    body: list[ast.stmt],
    *,
    deferred: bool,
) -> Iterator[ast.Import | ast.ImportFrom]:
    """Import statements in ``body``; ``deferred`` also enters function bodies.

    A class body executes with the module, so it is always entered. The
    ``if TYPE_CHECKING:`` arm never executes, so it is always skipped — its
    ``else:`` arm is not.
    """

    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if deferred:
                yield from _import_nodes(node.body, deferred=True)
        elif isinstance(node, ast.If) and _guards_type_checking(node.test):
            yield from _import_nodes(node.orelse, deferred=deferred)
        else:
            for field in ("body", "orelse", "finalbody"):
                yield from _import_nodes(getattr(node, field, []), deferred=deferred)
            for handler in getattr(node, "handlers", []):
                yield from _import_nodes(handler.body, deferred=deferred)
            for case in getattr(node, "cases", []):
                yield from _import_nodes(case.body, deferred=deferred)


def _imports(path: Path, *, deferred: bool) -> set[str]:
    """Every ``jasper.*`` name ``path`` imports."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _dotted(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    found: set[str] = set()
    for node in _import_nodes(tree.body, deferred=deferred):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
            continue
        if node.level:
            base = package[: len(package) - (node.level - 1)]
            module = ".".join([*base, *([node.module] if node.module else [])])
        else:
            module = node.module or ""
        found.add(module)
        found.update(f"{module}.{alias.name}" for alias in node.names)
    return {name for name in found if name.split(".")[0] == "jasper"}


def _closure(start: str) -> set[str]:
    """Every ``jasper`` module executed by ``import <start>``."""

    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        path = _module_path(current)
        if path is None:
            # A ``from x import y`` name rather than a module.
            parent = current.rpartition(".")[0]
            if parent and parent not in seen:
                stack.append(parent)
            continue
        dotted = _dotted(path)
        if dotted in seen:
            continue
        seen.add(dotted)
        parts = dotted.split(".")
        # Importing a submodule executes every ancestor package body.
        stack.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        stack.extend(_imports(path, deferred=False))
    return seen


def _offenders(modules: set[str]) -> list[str]:
    return sorted(
        module
        for module in modules
        if any(module == bad or module.startswith(f"{bad}.") for bad in FORBIDDEN)
    )


TRUTH_LAYER_MODULES = TRUTH_LAYER + tuple(
    sorted(
        _dotted(path)
        for path in (REPO_ROOT / "jasper" / "audio_measurement").rglob("*.py")
        if _dotted(path) != "jasper.audio_measurement"
    )
)


def test_the_walk_sees_a_real_import_graph():
    """A walker that returned nothing would satisfy every assertion below."""

    assert len(TRUTH_LAYER_MODULES) >= 40
    closure = _closure("jasper.active_speaker.runtime_contract")
    assert len(closure) >= 20
    assert "jasper.active_speaker.profile" in closure
    # A runtime module still reaches what the truth layer may not.
    assert _offenders(_closure("jasper.active_speaker.startup_load"))


@pytest.mark.parametrize(
    "deferred", [False, True], ids=["module-scope", "deferred-too"]
)
def test_the_walk_reads_both_import_depths(deferred):
    """The deferred pass is the one that sees a function-body import."""

    runtime_contract = REPO_ROOT / "jasper/active_speaker/runtime_contract.py"
    found = _imports(runtime_contract, deferred=deferred)
    assert ("jasper.bass_extension" in found) is deferred


@pytest.mark.parametrize("module", TRUTH_LAYER_MODULES)
def test_the_truth_layer_never_imports_the_runtime(module):
    path = _module_path(module)
    assert path is not None, f"{module} does not exist"
    assert _offenders(_imports(path, deferred=True)) == []
    assert _offenders(_closure(module)) == []


@pytest.mark.parametrize(
    "path",
    sorted((REPO_ROOT / "jasper" / "active_speaker").rglob("*.py")),
    ids=lambda path: str(path.relative_to(REPO_ROOT / "jasper" / "active_speaker")),
)
def test_active_speaker_jasper_imports_resolve(path):
    """A def moved between modules leaves no dangling ``jasper`` import.

    Relative and absolute alike: a relocation re-levels the first and leaves the
    second spelled at the old home. The suite stubs these callers, so only the
    walk sees it — at module scope and, the half a caller grep misses, inside a
    function body. Resolution is ``_module_path``, not ``find_spec``: a spec
    lookup executes the target's parent package, so an absent optional
    dependency would surface here as a dangling import.
    """

    package = _dotted(path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    unresolved = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in _import_nodes(tree.body, deferred=True):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        else:
            module = (
                resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module or ""
            )
            targets = [module]
            init = _module_path(module)
            if init is not None and init.name == "__init__.py":
                # ``from pkg import x``: x is a submodule unless the package
                # body binds the name itself.
                exported = _package_exports(init)
                targets += [
                    f"{module}.{alias.name}"
                    for alias in node.names
                    if alias.name not in exported
                ]
        unresolved.extend(
            target
            for target in targets
            if target.split(".")[0] == "jasper" and _module_path(target) is None
        )
    assert unresolved == []
