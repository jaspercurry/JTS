# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ``ProgramAnalysis`` field census, re-run from source on every CI pass.

The cutover's analyze registry defines an analysis unit as *a maximal set of
``ProgramAnalysis`` fields that are present or absent together*, and a field is
eligible for a unit only when it is **produced** — bound from a statement that
reads the capture samples. A field that copies an input parameter
(**passthrough**), that re-reads another field's value (**projection**), or that
is a predicate over inputs alone (**input predicate**) belongs to no unit.

That taxonomy is the counting method behind the plan's analysis claim, so it is
pinned mechanically here rather than restated in prose: the classification below
comes from a fresh ``ast.parse`` of the package, never from a hand-kept list.

Adding a ``ProgramAnalysis`` field, or a fifth construction site, turns this red
on purpose — both are changes that re-open the unit table, and the table is what
the plan's count is made of.

**Removal condition:** this pin dies with the registry table it feeds. Once the
unit table owns the field-to-unit map directly, the map is the census and this
file is the second writer of it.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "jasper" / "audio_measurement" / "program_analysis"

PASSTHROUGH = "passthrough"
PROJECTION = "projection"
INPUT_PREDICATE = "input_predicate"
PRODUCED = "produced"

# The enclosing functions of the ProgramAnalysis construction sites. Named
# rather than numbered by line, so the pin survives edits above them.
CONSTRUCTION_SITE_OWNERS = frozenset(
    {
        "analyze_program_capture",  # the shared `replace`
        "_analyze_check",
        "_analyze_measure",
        "_analyze_verify",
    }
)


def _module() -> ast.Module:
    """The package read as one tree: the split is by phase, the census is not."""
    body: list[ast.stmt] = []
    for path in sorted(SOURCE.glob("*.py")):
        body.extend(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body)
    return ast.Module(body=body, type_ignores=[])


def _declared_fields(tree: ast.Module) -> tuple[str, ...]:
    """The annotated attributes of the ``ProgramAnalysis`` dataclass, in order."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ProgramAnalysis":
            return tuple(
                statement.target.id
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
            )
    raise AssertionError("ProgramAnalysis is not a top-level class in this module")


def _root_name(node: ast.expr) -> str | None:
    """The base name of an attribute chain: ``a.b.c`` -> ``a``. Else ``None``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _parameters(function: ast.FunctionDef) -> frozenset[str]:
    arguments = function.args
    names = [
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    ]
    names += [extra.arg for extra in (arguments.vararg, arguments.kwarg) if extra]
    return frozenset(names)


def _reads_inputs_only(node: ast.expr, parameters: frozenset[str]) -> bool:
    """True for an expression built solely from parameters and constants.

    Deliberately excludes calls: a call may read the capture, which is what
    makes a field produced.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            return False
        if isinstance(child, ast.Name) and child.id not in parameters:
            return False
    return True


def _classify(value: ast.expr, parameters: frozenset[str]) -> str:
    if isinstance(value, ast.Name):
        return PASSTHROUGH if value.id in parameters else PRODUCED
    if isinstance(value, ast.Attribute):
        root = _root_name(value)
        if root is None:
            return PRODUCED
        return PASSTHROUGH if root in parameters else PROJECTION
    if isinstance(value, (ast.Compare, ast.BoolOp, ast.UnaryOp)) and _reads_inputs_only(
        value, parameters
    ):
        return INPUT_PREDICATE
    return PRODUCED


def _construction_sites(
    tree: ast.Module, fields: frozenset[str]
) -> tuple[tuple[str, ast.Call], ...]:
    """Every ``ProgramAnalysis(...)`` build and every all-fields ``replace(...)``.

    Returned as ``(enclosing function name, call node)`` pairs.
    """
    sites: list[tuple[str, ast.Call]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        # Innermost owner wins: a nested def's calls belong to the nested def.
        nested = {
            inner
            for outer in ast.walk(function)
            if isinstance(outer, ast.FunctionDef) and outer is not function
            for inner in ast.walk(outer)
        }
        for node in ast.walk(function):
            if node in nested or not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            builds = node.func.id == "ProgramAnalysis"
            rebuilds = (
                node.func.id == "replace" and bool(keywords) and keywords <= fields
            )
            if builds or rebuilds:
                sites.append((function.name, node))
    return tuple(sites)


def census() -> dict[str, str]:
    """Field name -> class, from a fresh parse. Raises if a field disagrees."""
    tree = _module()
    fields = frozenset(_declared_fields(tree))
    classified: dict[str, str] = {}
    for owner, call in _construction_sites(tree, fields):
        owning_function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == owner
        )
        parameters = _parameters(owning_function)
        for keyword in call.keywords:
            assert keyword.arg is not None, f"**kwargs into ProgramAnalysis in {owner}"
            found = _classify(keyword.value, parameters)
            previous = classified.setdefault(keyword.arg, found)
            assert previous == found, (
                f"{keyword.arg} is written as {previous} at one site and {found} at "
                f"another ({owner}:{keyword.lineno}) — a field with two classes has "
                "no single answer to 'is it produced', so the unit table cannot "
                "place it"
            )
    return classified


def test_every_program_analysis_field_is_written_at_a_construction_site():
    """No field reaches a consumer holding only its class default.

    A field that no construction site writes is invisible to the census, so it
    would silently belong to no unit — the registry would run every gate and
    still never produce it.
    """
    declared = set(_declared_fields(_module()))
    written = set(census())
    assert not declared - written, (
        "ProgramAnalysis fields with no construction-site write: "
        f"{sorted(declared - written)}"
    )
    assert not written - declared, (
        "construction sites write names that are not ProgramAnalysis fields: "
        f"{sorted(written - declared)}"
    )


def test_the_construction_sites_are_the_four_the_census_was_taken_over():
    """A fifth site re-opens the census, so it may not arrive unnoticed."""
    owners = {owner for owner, _ in _construction_sites(_module(), frozenset(_declared_fields(_module())))}
    assert owners == CONSTRUCTION_SITE_OWNERS


def test_the_field_census_reproduces_the_committed_classification():
    """The counting method behind the plan's analysis claim, re-derived.

    The three non-produced classes are pinned by NAME because each is a stated
    exception; ``produced`` is pinned by count, because re-listing it here would
    make this file a second writer of the dataclass.
    """
    classified = census()
    by_class: dict[str, set[str]] = {}
    for field, found in classified.items():
        by_class.setdefault(found, set()).add(field)

    assert by_class[PASSTHROUGH] == {
        "phase", "program_id", "mic_tier", "mic_calibrated",
    }
    assert by_class[PROJECTION] == {"glitch_detected"}
    assert by_class[INPUT_PREDICATE] == {"configured_path_composed"}
    assert len(by_class[PRODUCED]) == 22
    assert len(classified) == 28
