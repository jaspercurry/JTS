# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Keep process-environment reads behind the startup configuration boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "jasper"


def _is_provider_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "JASPER_VOICE_PROVIDER"


def _is_process_environment(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr == "environ"
    )


class _ProviderReadVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.reads: list[tuple[str, int]] = []

    def _record(self, node: ast.AST) -> None:
        self.reads.append((".".join(self.scope), node.lineno))

    def _visit_scope(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_process_environment(node.value) and _is_provider_key(node.slice):
            self._record(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if not node.args:
            self.generic_visit(node)
            return
        function = node.func
        direct_os_read = isinstance(function, ast.Attribute) and (
            (
                isinstance(function.value, ast.Name)
                and function.value.id == "os"
                and function.attr == "getenv"
            )
            or (_is_process_environment(function.value) and function.attr == "get")
        )
        config_helper_read = isinstance(function, ast.Name) and function.id.startswith(
            "_env"
        )
        if (direct_os_read or config_helper_read) and _is_provider_key(node.args[0]):
            self._record(node)
        self.generic_visit(node)


def _direct_process_env_reads(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _ProviderReadVisitor()
    visitor.visit(tree)
    return visitor.reads


def test_only_startup_config_reads_provider_from_process_environment() -> None:
    readers: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(PKG.rglob("*.py")):
        reads = _direct_process_env_reads(path)
        if reads:
            readers[str(path.relative_to(ROOT))] = reads
    assert set(readers) == {"jasper/config.py"}
    config_reads = readers["jasper/config.py"]
    assert len(config_reads) == 1
    assert config_reads[0][0] == "Config.from_env"
