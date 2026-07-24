# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the sealed-only runtime-arming gate stays consistent across its
five independent expressions.

``BASS_EXTENSION_RUNTIME_ADAPTER_IDS`` (``jasper/bass_extension/__init__.py``)
is the single source of truth for which enclosure-adapter id(s) are allowed
to arm the bass-extension runtime block (subsonic-protection requirement,
natural-target emission, etc.). Four other call sites independently
re-implement the same gate as a literal ``"sealed_v1"`` comparison instead of
importing the frozenset:

  * ``jasper/active_speaker/camilla_yaml.py::_bass_extension_emission``
  * ``jasper/active_speaker/baseline_profile.py::_bass_extension_graph_summary``
  * ``jasper/active_speaker/runtime_contract.py::_evaluated_profile_summary``
  * ``jasper/bass_extension/__init__.py::apply_bass_extension`` (the
    subsonic-protection refusal check)

This is a deliberate, tracked trade-off, not an oversight. Three of the
four sites are hot files under active, concurrent ownership by another
workstream; the fourth (``apply_bass_extension``) lives in this same module
and could reference the frozenset with no import at all, but unifying it is
deferred to the same single future change as the other three, not carved
out early. The unification is intended to happen once a second adapter is
armed and a plain literal comparison genuinely can't express the gate
anymore — not before. Until then, this test is the drift tripwire: it
AST-parses each enforcement site, extracts the adapter-id string
literal(s) it compares against, and asserts they equal
``BASS_EXTENSION_RUNTIME_ADAPTER_IDS``.

Scope, stated honestly: this test pins the literal *value set* compared at
each site, not the gate's polarity — a ``!=`` accidentally flipped to
``==`` at any one site still passes, because that is a behavioral-
correctness question for each site's own callers, not a cross-site
consistency question this AST scan can answer. Renaming a site's local
``adapter_id`` variable makes this test conservatively fail (a reported
vanished comparison) rather than silently pass — update
``_references_adapter_id`` for the new name.

**When this test fails:** do not patch just the one site that drifted.
Update the frozenset AND all four enforcement sites together, deliberately,
in the same change — pinning them to each other only has value if a failure
here means "reconcile all five," never "silence this one assertion."
"""
from __future__ import annotations

import ast
from pathlib import Path

from jasper.bass_extension import BASS_EXTENSION_RUNTIME_ADAPTER_IDS

ROOT = Path(__file__).resolve().parents[1]

# (file, function) pairs for the four independently re-implemented copies of
# the sealed_v1-only gate. Update this alongside any legitimate rename/move —
# that's the intended "loud" failure path (see _find_function below), not a
# reason to relax the test.
_SITES: tuple[tuple[Path, str], ...] = (
    (
        ROOT / "jasper" / "active_speaker" / "camilla_yaml.py",
        "_bass_extension_emission",
    ),
    (
        ROOT / "jasper" / "active_speaker" / "baseline_profile.py",
        "_bass_extension_graph_summary",
    ),
    (
        ROOT / "jasper" / "active_speaker" / "runtime_contract.py",
        "_evaluated_profile_summary",
    ),
    (
        ROOT / "jasper" / "bass_extension" / "__init__.py",
        "apply_bass_extension",
    ),
)

# ast.Compare operators whose semantics are "membership/equality against a
# literal" — the shapes a sealed_v1 gate could plausibly be spelled with.
_GATE_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def _find_function(
    tree: ast.AST, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the unique function named ``name`` anywhere in ``tree``.

    ``None`` on zero OR more-than-one match — an ambiguous match is just as
    unsafe to silently pick from as a missing one."""
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    return matches[0] if len(matches) == 1 else None


def _references_adapter_id(node: ast.AST) -> bool:
    """True if ``node`` names the profile's adapter id in any spelling this
    repo currently uses: a bare ``adapter_id`` variable, a
    ``[...]["adapter_id"]`` dict-key access, or an ``.adapter_id`` attribute."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "adapter_id":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "adapter_id":
            return True
        if isinstance(sub, ast.Constant) and sub.value == "adapter_id":
            return True
    return False


def _string_constants(node: ast.AST) -> set[str]:
    """Every string-constant literal reachable from ``node``. Walking (not a
    direct isinstance check) means a container literal like
    ``in {"sealed_v1", "other_v1"}`` is covered, not just a bare string."""
    return {
        sub.value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    }


def _adapter_id_literals(func: ast.AST) -> set[str]:
    """Collect every string literal compared against the adapter id anywhere
    in ``func``.

    Walks every ``Compare`` node (not just a top-level ``if``), so this is
    robust to the comparison being nested or reordered (literal-first or
    literal-last), or nested a level deeper than today's
    ``if adapter_id != "sealed_v1":`` shape. Only handles simple two-operand
    comparisons (plain ``a == b`` and container ``a in {...}``/``a not in
    {...}``) — a chained comparison like ``a == b == c`` is not a shape this
    codebase produces for this gate, so it's skipped rather than guessed at.
    """
    literals: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        op = node.ops[0]
        if not isinstance(op, _GATE_OPS):
            continue
        left, right = node.left, node.comparators[0]
        left_refs = _references_adapter_id(left)
        right_refs = _references_adapter_id(right)
        # Exactly one side must reference adapter_id — that side is the
        # "subject", the literal(s) on the other side are the gate value.
        if left_refs and not right_refs:
            literals |= _string_constants(right)
        elif right_refs and not left_refs:
            literals |= _string_constants(left)
    return literals


def test_bass_extension_runtime_gate_matches_single_source_of_truth() -> None:
    expected = set(BASS_EXTENSION_RUNTIME_ADAPTER_IDS)
    assert expected, (
        "BASS_EXTENSION_RUNTIME_ADAPTER_IDS (jasper/bass_extension/__init__.py) "
        "is empty — the single source of truth itself looks broken; fix that "
        "before trusting this test's comparisons"
    )

    for path, func_name in _SITES:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        func = _find_function(tree, func_name)
        assert func is not None, (
            f"{rel}: no unique function named {func_name!r} found. This "
            "enforcement site was renamed, moved, split, or removed — its "
            "sealed_v1-only gate may have vanished silently. Find where the "
            "gate logic now lives and update _SITES in this test to match "
            "(a vanished site is exactly what this test exists to catch, so "
            "do not skip it — either the gate moved and _SITES needs "
            "updating, or the gate is genuinely gone and that's a bug)."
        )

        found = _adapter_id_literals(func)
        assert found, (
            f"{rel}:{func_name} — no `adapter_id`-vs-string-literal "
            "comparison found by the AST scan. Either the gate's comparison "
            "shape changed to something this test's extraction logic "
            "doesn't recognize (update _adapter_id_literals), or the gate "
            "itself was silently dropped from this function (a bug — "
            "restore it). Either way, do not let this assertion go missing."
        )

        assert found == expected, (
            f"{rel}:{func_name} enforces adapter id(s) {sorted(found)}, but "
            "BASS_EXTENSION_RUNTIME_ADAPTER_IDS in jasper/bass_extension/"
            f"__init__.py is {sorted(expected)}. These five expressions of "
            "the same sealed-only gate have drifted apart. Reconcile them "
            "together, deliberately (see this test module's docstring for "
            "why the duplication exists and when to unify onto the "
            "frozenset directly) — do not patch only the site that changed."
        )
