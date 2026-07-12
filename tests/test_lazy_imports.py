# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guards that the memory-diet lazy-imports stay lazy.

Each test runs in its own Python subprocess so module-cache state
doesn't leak between cases (sys.modules is process-global). On a
Pi 5, the savings these guards protect are:

- openwakeword stub → sklearn doesn't load (~67 MB resident)
- gemini_session lazy → google.genai doesn't load unless provider=gemini (~49 MB)
- openai_session lazy → openai SDK doesn't load unless provider=openai (~11 MB)

A regression in any of these would silently re-inflate jasper-voice's
RSS by tens of MB. CI catches the import-graph change, not the bytes,
but the import-graph IS the cost on Python.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_DECLARED_LEAF_DEPENDENCIES = {"httpx", "rapidfuzz", "sounddevice"}


def _run_probe(probe: str) -> dict[str, bool]:
    """Run `probe` in a fresh subprocess; parse `key=true|false` lines."""
    out = subprocess.check_output(
        [sys.executable, "-c", probe], stderr=subprocess.STDOUT, text=True,
    )
    result: dict[str, bool] = {}
    for line in out.splitlines():
        if "=" in line and line.split("=", 1)[1].strip() in {"true", "false"}:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip() == "true"
    return result


def test_wake_does_not_load_sklearn() -> None:
    """The openwakeword stub in jasper.wake should keep sklearn out
    of sys.modules. sklearn is ~67 MB resident; we never train custom
    verifier models, so it's pure dead weight."""
    probe = (
        "import sys\n"
        "import jasper.wake  # noqa: F401\n"
        "loaded = any(m == 'sklearn' or m.startswith('sklearn.') for m in sys.modules)\n"
        "print(f'sklearn_loaded={str(loaded).lower()}')\n"
    )
    result = _run_probe(probe)
    assert result.get("sklearn_loaded") is False, (
        "sklearn was loaded into sys.modules after importing jasper.wake. "
        "The custom_verifier_model stub at the top of jasper/wake.py was "
        "either removed or stopped working. ~67 MB regression."
    )


def test_voice_daemon_import_does_not_load_genai() -> None:
    """Importing jasper.voice_daemon must not eagerly load google.genai.
    The Gemini adapter is now lazy-imported inside _make_connection so
    non-Gemini users don't pay the ~49 MB cost."""
    probe = (
        "import sys\n"
        "import jasper.voice_daemon  # noqa: F401\n"
        "loaded = 'google.genai' in sys.modules\n"
        "print(f'genai_loaded={str(loaded).lower()}')\n"
    )
    result = _run_probe(probe)
    assert result.get("genai_loaded") is False, (
        "google.genai was loaded into sys.modules just by importing "
        "jasper.voice_daemon. The Gemini adapter must stay lazy in "
        "_make_connection so non-Gemini users avoid the cost."
    )


def test_voice_daemon_import_does_not_load_openai() -> None:
    """openai SDK should also stay out at module-import time. The
    openai_session adapter's class definition is module-top, but the
    SDK import is already inside _resolve_connect_call. Belt-and-
    suspenders: with voice_daemon's adapter imports now lazy, the
    openai_session module itself shouldn't load either unless the
    active provider is openai or grok."""
    probe = (
        "import sys\n"
        "import jasper.voice_daemon  # noqa: F401\n"
        "loaded = 'openai' in sys.modules\n"
        "print(f'openai_loaded={str(loaded).lower()}')\n"
    )
    result = _run_probe(probe)
    assert result.get("openai_loaded") is False, (
        "openai was loaded into sys.modules just by importing "
        "jasper.voice_daemon. Voice adapter imports should be lazy "
        "(inside _make_connection branches)."
    )


def test_voice_daemon_import_does_not_require_declared_leaf_dependencies() -> None:
    """Pure daemon helpers stay importable without touching leaf packages.

    These dependencies are installed on a full speaker, but importing them is
    deliberately deferred until their owning network/audio path is used. Test
    modules therefore do not need process-global stand-ins for them.
    """
    probe = (
        "import sys\n"
        "for name in ('httpx', 'rapidfuzz', 'sounddevice'):\n"
        "    sys.modules[name] = None\n"
        "import jasper.voice_daemon  # noqa: F401\n"
        "print('voice_daemon_imported=true')\n"
    )
    result = _run_probe(probe)
    assert result.get("voice_daemon_imported") is True


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "modules"
    )


def _sys_modules_subscript(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and _is_sys_modules(node.value)
    )


def _subscript_key(node: ast.AST) -> str | None:
    if not _sys_modules_subscript(node):
        return None
    assert isinstance(node, ast.Subscript)
    key = node.slice.value if isinstance(node.slice, ast.Constant) else None
    return key if isinstance(key, str) else None


def _sys_modules_mutation_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and _is_sys_modules(node.func.value)
        and node.func.attr in {"__setitem__", "setdefault", "update"}
    )


def _function_mutates_sys_modules(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            if any(_sys_modules_subscript(target) for target in targets):
                return True
        if isinstance(child, ast.Delete) and any(
            _sys_modules_subscript(target) for target in child.targets
        ):
            return True
        if isinstance(child, ast.Call) and _sys_modules_mutation_call(child):
            return True
    return False


def _dependency_arg(node: ast.Call) -> str | None:
    candidates = [*node.args, *(kw.value for kw in node.keywords)]
    for candidate in candidates:
        if (
            isinstance(candidate, ast.Constant)
            and isinstance(candidate.value, str)
            and candidate.value in _DECLARED_LEAF_DEPENDENCIES
        ):
            return candidate.value
    return None


def _update_dependency(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Dict):
        for key in node.args[0].keys:
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in _DECLARED_LEAF_DEPENDENCIES
            ):
                return key.value
    for keyword in node.keywords:
        if keyword.arg in _DECLARED_LEAF_DEPENDENCIES:
            return keyword.arg
    return None


class _ImportTimeDependencyStubVisitor(ast.NodeVisitor):
    """Find supported dependency stubs on Python import-definition surfaces.

    This is intentionally a syntactic ratchet, not general execution analysis.
    It covers direct assignments plus common ``setdefault``/``update``/
    ``__setitem__`` calls in module and class bodies, and same-tree/shared
    helper calls from decorators, defaults, keyword defaults, and annotations.
    Runtime function/method/lambda bodies remain valid scopes for hardware
    fakes and are not inspected.
    """

    def __init__(self, mutating_helpers: set[str]) -> None:
        self._mutating_helpers = mutating_helpers
        self.lines: list[int] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_surface(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_surface(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)
        for statement in node.body:
            self.visit(statement)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _visit_function_surface(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_param in getattr(node, "type_params", ()):
            self.visit(type_param)

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(
            _subscript_key(target) in _DECLARED_LEAF_DEPENDENCIES
            for target in node.targets
        ):
            self.lines.append(node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if _subscript_key(node.target) in _DECLARED_LEAF_DEPENDENCIES:
            self.lines.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        helper_stub = (
            isinstance(node.func, ast.Name)
            and node.func.id in self._mutating_helpers
            and _dependency_arg(node) is not None
        )
        direct_stub = False
        if _sys_modules_mutation_call(node):
            assert isinstance(node.func, ast.Attribute)
            if node.func.attr == "update":
                direct_stub = _update_dependency(node) is not None
            else:
                direct_stub = _dependency_arg(node) is not None
        if helper_stub or direct_stub:
            self.lines.append(node.lineno)
        self.generic_visit(node)


def _mutating_helper_names(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _function_mutates_sys_modules(node)
    }


def _import_time_dependency_stub_lines(
    tree: ast.Module, *, mutating_helpers: set[str] | None = None,
) -> list[int]:
    helpers = mutating_helpers or _mutating_helper_names(tree)
    visitor = _ImportTimeDependencyStubVisitor(helpers)
    visitor.visit(tree)
    return visitor.lines


def test_dependency_stub_ratchet_detects_import_time_writes_only() -> None:
    tree = ast.parse(
        """
import sys
sys.modules["httpx"] = object()

def install(name):
    sys.modules[name] = object()

install("sounddevice")

def scoped_hardware_fake():
    sys.modules["rapidfuzz"] = object()
"""
    )
    assert _import_time_dependency_stub_lines(tree) == [3, 8]


def test_dependency_stub_ratchet_checks_class_and_definition_surfaces() -> None:
    tree = ast.parse(
        """
import sys

def install(name):
    sys.modules.setdefault(name, object())

class ImportTimeClassBody:
    sys.modules.update({"httpx": object()})
    sys.modules.update(rapidfuzz=object())

def import_time_default(value=install("sounddevice")):
    return value

def scoped_hardware_fake():
    sys.modules.__setitem__("rapidfuzz", object())
"""
    )
    assert _import_time_dependency_stub_lines(tree) == [8, 9, 11]


def _dependency_stub_violations(root: Path) -> list[str]:
    parsed = [
        (
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        for path in sorted((root / "tests").rglob("*.py"))
    ]
    shared_helpers: set[str] = set()
    for _, tree in parsed:
        shared_helpers.update(_mutating_helper_names(tree))

    return [
        f"{path.relative_to(root)}:{line}"
        for path, tree in parsed
        for line in _import_time_dependency_stub_lines(
            tree, mutating_helpers=shared_helpers,
        )
    ]


def test_dependency_stub_ratchet_scans_nested_shared_helpers(tmp_path: Path) -> None:
    helper = tmp_path / "tests" / "support" / "dependency_stubs.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "import sys\n"
        "def install_dependency_stub(name):\n"
        "    sys.modules.__setitem__(name, object())\n",
        encoding="utf-8",
    )
    conftest = tmp_path / "tests" / "nested" / "conftest.py"
    conftest.parent.mkdir(parents=True)
    conftest.write_text(
        "from tests.support.dependency_stubs import install_dependency_stub\n"
        "install_dependency_stub('rapidfuzz')\n",
        encoding="utf-8",
    )

    assert _dependency_stub_violations(tmp_path) == [
        "tests/nested/conftest.py:2",
    ]


def test_tests_do_not_stub_declared_dependencies_at_import_time() -> None:
    """Import-time fakes poison later tests through process-global caching.

    Scoped ``sys.modules`` fakes remain valid (the doctor tests use them to
    exercise hardware errors). Only import-time writes/calls are rejected.
    """
    violations = _dependency_stub_violations(ROOT)

    assert not violations, (
        "tests must not install httpx/rapidfuzz/sounddevice fakes in "
        f"sys.modules at import time: {', '.join(violations)}"
    )
