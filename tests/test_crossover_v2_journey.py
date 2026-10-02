# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291: the crossover_v2 package's architecture guards — what a domain module
may import, and that the package's import graph stays acyclic.
"""

from __future__ import annotations


import ast
from graphlib import CycleError, TopologicalSorter
from pathlib import Path


# architecture — dependency direction

#: The two things a module in this package may not import.
FORBIDDEN_IMPORT_ROOTS = ("jasper.web", "crossover_v2_flow")


def _forbidden_imports(module: Path) -> list[str]:
    """Every real import in ``module`` naming a forbidden root.

    Parsed rather than grepped. A line-regex reads the module's own PROSE — the
    docstring sentence that states this very rule begins "import and nothing
    from ...crossover_v2_flow" and matched, so the grep shape reported the
    package's promise as its violation. ``ast`` sees import STATEMENTS only, at
    any nesting depth, which is also what makes a lazy in-function import
    (the shape a domain module would actually smuggle a host dependency
    through) visible to this guard.
    """

    tree = ast.parse(module.read_text(), filename=str(module))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # ``level`` > 0 is a relative import, whose module is a suffix:
            # ``from .crossover_v2_flow import X`` inside the package would
            # read as ``crossover_v2_flow``.
            names = [node.module or ""]
        else:
            continue
        found += [
            name for name in names
            if any(root in name for root in FORBIDDEN_IMPORT_ROOTS)
        ]
    return found


def test_the_import_guard_sees_a_planted_violation(tmp_path):
    """The guard's own positive control. Without it, an ``ast`` walk that
    matched nothing — a changed node type, a typo'd root — would report every
    module clean and read exactly like compliance.

    Planted in ``tmp_path`` rather than beside this file: the probes were
    collectable ``tests/*.py`` modules for the length of the write, and a run
    interrupted between the write and the ``unlink`` left them in the source
    tree (#2291 Phase 5 ledger). The guard reads a path, so it neither knows
    nor cares which directory.
    """

    planted = tmp_path / "_journey_guard_probe.py"
    planted.write_text(
        "def f():\n    from jasper.web import correction_crossover_v2\n"
    )
    assert _forbidden_imports(planted) == ["jasper.web"]

    planted_relative = tmp_path / "_journey_guard_probe2.py"
    planted_relative.write_text("from ..crossover_v2_flow import PHASE_CHECK\n")
    assert _forbidden_imports(planted_relative) == ["crossover_v2_flow"]


def test_no_domain_module_imports_the_host_or_the_legacy_flow():
    """#2291's dependency direction, asserted over the WHOLE package.

    ``test_crossover_v2_verification.py`` pins the same rule for one file. This
    walks the package instead, because the per-file shape cannot see a module
    that does not exist yet — journey.py was exactly such a module, and Phase 5
    adds more. Over-covering is free; the narrow guard is left where it is
    rather than deleted, since a suite that stops asserting its own module's
    purity is a worse trade than one redundant read.
    """

    package = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2"
    )
    modules = sorted(package.glob("*.py"))
    assert len(modules) >= 6, f"expected the package's modules, saw {modules}"
    assert package / "journey.py" in modules

    offenders = {
        module.name: bad
        for module in modules
        if (bad := _forbidden_imports(module))
    }
    assert offenders == {}


def test_no_test_module_imports_the_conductor_test_file():
    """#2291 5c-i's own result, held: the conductor test files have no importers.

    It had eighteen. They reached it for twenty-five fixture symbols — including
    all three Phase-0 characterization pins — which is what made a file of
    conductor-specific tests undeletable while the conductor is being dissolved.
    The fixtures now live in ``tests/crossover_v2_fixtures.py``; this asserts
    nobody re-creates the blocker, when a new importer would be an easy and
    invisible thing to add; the guard matches the whole
    ``test_crossover_v2_conductor*`` family by prefix.

    Prose mentions are fine and deliberately not matched — several modules cite
    the file(s) in a docstring to say where a behaviour is pinned. Only a real
    ``import`` counts.

    **Every ``*.py`` under ``tests/``, not just ``test_*.py``.** The highest-
    probability way to re-create the blocker is an import from
    ``crossover_v2_fixtures.py`` itself — eighteen modules import that, so one
    line there restores the blocker for all eighteen at once — and a
    ``test_*`` glob cannot see it. Helper modules and ``conftest.py`` are in
    scope for the same reason.

    Both import spellings count: the absolute ``tests.test_crossover_v2_conductor*``
    and the relative ``from .test_crossover_v2_conductor* import …``. The
    relative form has no precedent in this suite, which is exactly why a guard
    keyed only to the absolute one would be the easy thing to slip past.
    """

    tests_dir = Path(__file__).resolve().parent
    modules = sorted(tests_dir.glob("*.py"))
    assert len(modules) >= 50, f"expected the test suite, saw {len(modules)}"
    assert tests_dir / "crossover_v2_fixtures.py" in modules

    absolute_prefix = "tests.test_crossover_v2_conductor"
    relative_prefix = "test_crossover_v2_conductor"

    def names_a_conductor_file(node: ast.AST) -> bool:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            prefix = relative_prefix if node.level else absolute_prefix
            return module.startswith(prefix)
        if isinstance(node, ast.Import):
            return any(alias.name.startswith(absolute_prefix) for alias in node.names)
        return False

    offenders: dict[str, list[int]] = {}
    for module in modules:
        if module.name.startswith("test_crossover_v2_conductor"):
            continue
        text = module.read_text()
        # Coverage-preserving fast path: an import that names a conductor file
        # has to spell the prefix, in either spelling. Parsing all ~825
        # modules unconditionally cost this one guard several seconds; a
        # dynamically-built import would escape the AST check below with or
        # without this line.
        if relative_prefix not in text:
            continue
        tree = ast.parse(text, filename=str(module))
        lines = [
            node.lineno for node in ast.walk(tree)
            if names_a_conductor_file(node)
        ]
        if lines:
            offenders[module.name] = lines

    assert offenders == {}


# architecture — the package's own shape

#: How a module inside the package spells itself from the outside.
PACKAGE_DOTTED = "jasper.active_speaker.crossover_v2"


def _package_suffix(dotted_name: str, package: str) -> list[str]:
    """``["contracts"]`` for ``<package>.contracts.X``, ``[]`` for anything else."""

    prefix = f"{package}."
    if not dotted_name.startswith(prefix):
        return []
    return [dotted_name[len(prefix):].split(".")[0]]


def _intra_package_edges(package: Path, dotted: str) -> dict[str, set[str]]:
    """``{module: the sibling modules it imports}``, by bare module name.

    Both spellings the package actually uses are read: the relative
    ``from .contracts import X`` / ``from . import priors``, and the absolute
    ``from jasper.active_speaker.crossover_v2.contracts import X`` that two
    lazy in-function imports use today. A guard blind to the second would miss
    the exact shape a cycle arrives in, since deferring an import into a
    function body is how a developer works around one.

    ``__init__.py`` is deliberately not a node. Importing any submodule
    executes the package ``__init__``, and this one re-exports most of the
    package — so a graph containing it is cyclic by construction and would say
    nothing about whether the MODULES depend on each other in one direction.

    """

    modules = {path.stem for path in package.glob("*.py")} - {"__init__"}
    edges: dict[str, set[str]] = {}
    for path in sorted(package.glob("*.py")):
        if path.stem == "__init__":
            continue
        found: set[str] = set()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    named = (
                        [node.module.split(".")[0]] if node.module
                        else [alias.name for alias in node.names]
                    )
                elif node.level == 0 and node.module:
                    named = _package_suffix(node.module, dotted)
                else:  # ``from ..flat_spec import X`` — outside the package
                    named = []
            elif isinstance(node, ast.Import):
                named = [
                    name
                    for alias in node.names
                    for name in _package_suffix(alias.name, dotted)
                ]
            else:
                continue
            found |= {n for n in named if n in modules and n != path.stem}
        edges[path.stem] = found
    return edges


def _import_cycle(edges: dict[str, set[str]]) -> tuple[str, ...] | None:
    try:
        TopologicalSorter(edges).prepare()
    except CycleError as exc:
        return tuple(exc.args[1])
    return None


def test_the_cycle_guard_sees_a_planted_cycle(tmp_path):
    """The acyclicity guard's own positive control.

    Two ways this guard could pass while asserting nothing, and the plant
    catches both: an edge walk that matches no import shape returns an empty
    graph, which is trivially acyclic and reads exactly like a healthy
    package; and a cycle detector wired to the wrong end of the graph never
    raises. The planted package uses ONE spelling per direction — relative one
    way, absolute the other — so a walker that understands only one of them
    finds a single edge and no cycle.
    """

    package = tmp_path / "tmpkg"
    package.mkdir()
    (package / "__init__.py").write_text("from .a import A\n")
    (package / "a.py").write_text("from .b import B\n")
    (package / "b.py").write_text("from tmpkg.a import A\n")

    edges = _intra_package_edges(package, "tmpkg")
    assert edges == {"a": {"b"}, "b": {"a"}}, "the __init__ is not a node"
    assert _import_cycle(edges) is not None


def test_the_package_import_graph_stays_acyclic():
    """#2662's G1: the DAG the package happens to be becomes the DAG it is.

    ``test_no_domain_module_imports_the_host_or_the_legacy_flow`` above forbids
    two imports by name. It says nothing about the package's INTERNAL shape,
    so nothing stopped ``contracts`` — which eight nodes of this graph import
    (nine files do; the package ``__init__`` is not a node) and which
    imports none of them — from importing ``coordinator`` tomorrow. The
    layering was an accident of how the extraction happened to land; this
    makes it a contract, at the cost of one walk.

    The edge floor is not decoration. An assertion that a graph has no cycle
    is satisfied by a graph with no edges, so a walker broken by a Python
    grammar change would report perfect health.
    """

    package = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2"
    )
    edges = _intra_package_edges(package, PACKAGE_DOTTED)

    assert len(edges) >= 15, f"expected the package's modules, saw {len(edges)}"
    assert sum(len(deps) for deps in edges.values()) >= 20

    cycle = _import_cycle(edges)
    assert cycle is None, f"crossover_v2 import cycle: {' -> '.join(cycle or ())}"

