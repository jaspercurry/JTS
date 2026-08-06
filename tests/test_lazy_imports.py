# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guards that the memory-diet lazy-imports stay lazy.

Each test runs in its own Python subprocess so module-cache state
doesn't leak between cases (sys.modules is process-global). On a
Pi 5, the savings these guards protect are:

- openWakeWord guard → sklearn doesn't load (~78 MiB resident; measured
  2026-08-06 on jts3.local and jts.local, see jasper/openwakeword_guard.py)
- gemini_session lazy → google.genai doesn't load unless provider=gemini (~49 MB)
- openai_session lazy → openai SDK doesn't load unless provider=openai (~11 MB)

A regression in any of these would silently re-inflate jasper-voice's
RSS by tens of MB. CI catches the import-graph change, not the bytes,
but the import-graph IS the cost on Python.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
_DECLARED_LEAF_DEPENDENCIES = {"httpx", "rapidfuzz", "sounddevice"}

_GUARD_FUNCTION = "ensure_openwakeword_import_safe"

# Where openWakeWord import sites must be guarded.
#
# `tests/` is excluded because test modules install *fake* openwakeword
# packages in sys.modules and embed `import openwakeword` inside probe
# source strings — neither is a real import of the real package.
#
# `experiments/` is excluded because experiments/aec3-v2-deep-tune-spike
# documents its prereqs as a standalone laptop venv holding "pybind11,
# numpy, openwakeword, onnxruntime" (see that directory's README) — it is
# frozen bench archaeology that does not have `jasper` importable, so
# adding the guard call there would be a new hard dependency for no
# runtime benefit. Nothing in it runs on a speaker.
_OPENWAKEWORD_SCAN_ROOTS = ("jasper", "scripts")

_OPENWAKEWORD_SKIP_REASON = (
    "openwakeword and scikit-learn are not installed in this environment; "
    "the static guard in test_every_openwakeword_import_site_is_guarded "
    "still runs. CI installs both (uv --group openwakeword-onnx)."
)


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


def _module_installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


_needs_openwakeword = pytest.mark.skipif(
    not (_module_installed("openwakeword") and _module_installed("sklearn")),
    reason=_OPENWAKEWORD_SKIP_REASON,
)

_SKLEARN_PROBE_TAIL = (
    "loaded = any(m == 'sklearn' or m.startswith('sklearn.') for m in sys.modules)\n"
    "print(f'sklearn_loaded={str(loaded).lower()}')\n"
)


def _imports_openwakeword(node: ast.AST) -> bool:
    """True when `node` imports the real openwakeword package."""
    if isinstance(node, ast.Import):
        return any(
            alias.name == "openwakeword" or alias.name.startswith("openwakeword.")
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        if node.level:  # a relative import can never reach openwakeword
            return False
        module = node.module or ""
        return module == "openwakeword" or module.startswith("openwakeword.")
    return False


def _guard_call_lines(body: list[ast.stmt]) -> list[int]:
    """Lines of bare `ensure_openwakeword_import_safe()` statements in `body`.

    Only *direct* statements of the body count. A call nested inside an
    `if`/`try`/`for` is not accepted: it is not unconditionally reached, so
    it does not prove the guard ran.
    """
    lines: list[int] = []
    for stmt in body:
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            continue
        func = stmt.value.func
        if isinstance(func, ast.Name):
            name: str | None = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            name = None
        if name == _GUARD_FUNCTION:
            lines.append(stmt.lineno)
    return lines


def _collect_openwakeword_sites(
    body: list[ast.stmt], out: list[tuple[int, bool]],
) -> None:
    """Attribute every openwakeword import in `body` to this guard scope.

    A guard scope is a module body or a function body. Nested functions
    start their own scope; a guard call in an enclosing scope does not
    count for them. That is deliberately stricter than runtime necessity
    (a module-level call really would run first) so the codebase keeps one
    uniform, greppable convention: the guard call sits immediately before
    the import it protects, in the same function.

    Like the dependency-stub ratchet below, this is a syntactic check, not
    execution analysis.
    """
    guard_lines = _guard_call_lines(body)
    first_guard = min(guard_lines) if guard_lines else None

    def walk(nodes: object) -> None:
        for node in nodes:  # type: ignore[union-attr]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _collect_openwakeword_sites(node.body, out)
                continue
            if isinstance(node, ast.Lambda):
                continue
            if _imports_openwakeword(node):
                guarded = first_guard is not None and first_guard < node.lineno
                out.append((node.lineno, guarded))
            walk(ast.iter_child_nodes(node))

    walk(body)


def _openwakeword_import_sites(root: Path) -> list[tuple[str, int, bool]]:
    """Every openwakeword import under the scanned roots, with guard status."""
    sites: list[tuple[str, int, bool]] = []
    for scan_root in _OPENWAKEWORD_SCAN_ROOTS:
        for path in sorted((root / scan_root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            found: list[tuple[int, bool]] = []
            _collect_openwakeword_sites(tree.body, found)
            sites.extend(
                (str(path.relative_to(root)), line, guarded)
                for line, guarded in found
            )
    return sorted(sites)


def test_openwakeword_scanner_flags_unguarded_sites(tmp_path: Path) -> None:
    """The scanner must actually distinguish guarded from unguarded."""
    pkg = tmp_path / "jasper"
    pkg.mkdir()
    (pkg / "guarded.py").write_text(
        "from .openwakeword_guard import ensure_openwakeword_import_safe\n"
        "\n"
        "def build():\n"
        "    ensure_openwakeword_import_safe()\n"
        "    try:\n"
        "        from openwakeword.model import Model\n"
        "    except ImportError:\n"
        "        return None\n"
        "    return Model\n",
        encoding="utf-8",
    )
    (pkg / "unguarded.py").write_text(
        "def build():\n"
        "    import openwakeword\n"
        "    return openwakeword\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()

    assert _openwakeword_import_sites(tmp_path) == [
        ("jasper/guarded.py", 6, True),
        ("jasper/unguarded.py", 2, False),
    ]


def test_openwakeword_scanner_rejects_wrong_scope_and_conditional_guards(
    tmp_path: Path,
) -> None:
    """A guard that isn't unconditionally before the import doesn't count."""
    pkg = tmp_path / "jasper"
    pkg.mkdir()
    (pkg / "cases.py").write_text(
        "from .openwakeword_guard import ensure_openwakeword_import_safe\n"
        "\n"
        "ensure_openwakeword_import_safe()\n"
        "\n"
        "def outer_scope_only():\n"
        "    import openwakeword\n"          # module-level guard, other scope
        "    return openwakeword\n"
        "\n"
        "def conditional(flag):\n"
        "    if flag:\n"
        "        ensure_openwakeword_import_safe()\n"
        "    import openwakeword\n"          # guard not unconditionally reached
        "    return openwakeword\n"
        "\n"
        "def too_late():\n"
        "    import openwakeword\n"          # guard runs after the import
        "    ensure_openwakeword_import_safe()\n"
        "    return openwakeword\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()

    assert _openwakeword_import_sites(tmp_path) == [
        ("jasper/cases.py", 6, False),
        ("jasper/cases.py", 12, False),
        ("jasper/cases.py", 16, False),
    ]


def test_every_openwakeword_import_site_is_guarded() -> None:
    """No openWakeWord import may run without the sklearn guard first.

    openWakeWord's __init__ imports custom_verifier_model, which imports
    scikit-learn: measured +80,080 to +80,592 KiB RSS on two Pi 5 speakers
    (2026-08-06). jasper-voice, jasper-doctor, and the offline training
    tools are separate processes, so each openWakeWord entry point has to
    install the guard itself — relying on "some other module imported
    jasper.wake first" is how jasper-doctor and a standalone jasper.vad
    import silently paid the full cost.

    Fix a failure by calling `ensure_openwakeword_import_safe()` (from
    jasper/openwakeword_guard.py) as the statement before the import.
    """
    sites = _openwakeword_import_sites(ROOT)

    assert sites, (
        "found no openwakeword imports at all under "
        f"{_OPENWAKEWORD_SCAN_ROOTS} — the scanner stopped working, so this "
        "guard would pass vacuously"
    )

    unguarded = [f"{path}:{line}" for path, line, guarded in sites if not guarded]
    assert not unguarded, (
        "openwakeword is imported without jasper.openwakeword_guard."
        f"{_GUARD_FUNCTION}() running first at: {', '.join(unguarded)}. "
        "That pulls scikit-learn into the process (~78 MiB resident on a "
        "Pi 5). Call the guard as the statement immediately before the "
        "import."
    )


def test_importing_wake_has_no_openwakeword_side_effect() -> None:
    """jasper.wake must not touch sys.modules just by being imported.

    The guard used to be a module-top `sys.modules` write in jasper/wake.py,
    which meant jasper.vad was protected only because jasper-voice happened
    to import jasper.wake first. The guard is now an explicit call at each
    import site, so importing jasper.wake should install nothing.
    """
    probe = (
        "import sys\n"
        "import jasper.wake  # noqa: F401\n"
        "touched = any(m.split('.')[0] == 'openwakeword' for m in sys.modules)\n"
        "print(f'openwakeword_touched={str(touched).lower()}')\n"
    )
    result = _run_probe(probe)
    assert result.get("openwakeword_touched") is False, (
        "importing jasper.wake wrote an openwakeword entry into sys.modules. "
        "The guard belongs at each import site, not at a module top — a "
        "module-top side effect reads as dead code and silently protects "
        "unrelated modules by import order."
    )


@_needs_openwakeword
def test_vad_alone_does_not_load_sklearn() -> None:
    """A process that imports ONLY jasper.vad must still dodge sklearn.

    Before the shared guard, this probe loaded 169 sklearn modules
    (+144,928 KiB RSS on jts3.local, 2026-08-06): jasper/vad.py imported
    openwakeword with no guard of its own and free-rode on jasper-voice
    importing jasper.wake first. scripts/probe-wake-gate.py constructs
    SpeechVAD without that import order.
    """
    probe = (
        "import sys\n"
        "import jasper.vad\n"
        "try:\n"
        "    jasper.vad.SpeechVAD()\n"
        "except BaseException:\n"
        "    pass  # missing Silero assets are fine; the import already ran\n"
        "assert 'openwakeword' in sys.modules, 'probe never reached openwakeword'\n"
        + _SKLEARN_PROBE_TAIL
    )
    result = _run_probe(probe)
    assert result.get("sklearn_loaded") is False, (
        "constructing jasper.vad.SpeechVAD pulled scikit-learn into the "
        "process. jasper/vad.py must call ensure_openwakeword_import_safe() "
        "before its openwakeword import."
    )


@_needs_openwakeword
def test_doctor_wake_check_does_not_load_sklearn() -> None:
    """jasper-doctor is its own process and never imports jasper.wake.

    Measured before the shared guard: running check_openwakeword_model
    loaded 169 sklearn modules (+138,352 KiB RSS on jts3.local,
    2026-08-06). The doctor runs on every install.
    """
    probe = (
        "import sys\n"
        "from types import SimpleNamespace\n"
        "from jasper.cli.doctor import check_openwakeword_model\n"
        "try:\n"
        "    check_openwakeword_model(SimpleNamespace(wake_model='hey_jarvis'))\n"
        "except BaseException:\n"
        "    pass  # unstaged assets are fine; the import already ran\n"
        "assert 'openwakeword' in sys.modules, 'probe never reached openwakeword'\n"
        + _SKLEARN_PROBE_TAIL
    )
    result = _run_probe(probe)
    assert result.get("sklearn_loaded") is False, (
        "jasper-doctor's openWakeWord check pulled scikit-learn into the "
        "process. jasper/cli/doctor/wake.py must call "
        "ensure_openwakeword_import_safe() before its openwakeword import."
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
