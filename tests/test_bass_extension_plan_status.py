# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bass_extension_unshipped_surfaces_do_not_exist() -> None:
    """Waves 1-3 are merged; the commissioning backend and runtime
    scheduler are not. Pin the gap so a future edit cannot silently
    claim they shipped without also updating this assertion."""
    for unshipped_surface in (
        "jasper/web/bassext_backend.py",
        "jasper/bass_extension/scheduler.py",
        "jasper/bass_extension/runtime.py",
    ):
        assert not (ROOT / unshipped_surface).exists(), unshipped_surface


def test_wave3_transactions_have_no_production_callers() -> None:
    owner = ROOT / "jasper" / "bass_extension" / "__init__.py"
    entry_points = {
        "apply_bass_extension",
        "bypass_bass_extension",
        "recover_pending_bass_extension_apply",
    }
    for path in (ROOT / "jasper").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        allowed_owner_uses: set[int] = set()
        if path == owner:
            bypass = next(
                node
                for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "bypass_bass_extension"
            )
            delegation_calls = [
                node
                for node in ast.walk(bypass)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "apply_bass_extension"
            ]
            assert len(delegation_calls) == 1
            allowed_owner_uses.add(id(delegation_calls[0].func))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert entry_points.isdisjoint(alias.name for alias in node.names), path
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in entry_points
            ):
                assert id(node) in allowed_owner_uses, path
            elif isinstance(node, ast.Attribute) and node.attr in entry_points:
                assert False, path
