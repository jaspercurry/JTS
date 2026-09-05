# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guards that the memory-diet lazy-imports stay lazy.

Each test runs in its own Python subprocess so module-cache state
doesn't leak between cases (sys.modules is process-global). On a
Pi 5, the savings these guards protect are:

- openWakeWord guard → sklearn doesn't load (the measured RSS table lives in
  jasper/openwakeword_guard.py, which owns that figure; don't restate it here)
- gemini_session lazy → google.genai doesn't load unless provider=gemini (~49 MB)
- openai_session lazy → openai SDK doesn't load unless provider=openai (~11 MB)
- resident daemons lazy → dbus_next and the oneshot
  jasper.multiroom.reconcile stay out of jasper-control, jasper-usbmic and
  jasper-aec-bridge (31-49 fewer modules each; -0.8 to -2.6 MB on x86_64 —
  jasper-control and the grouping supervisor drop reconcile.py's whole
  transitive graph, not just dbus_next, so they save the most)
- doctor lazy → PortAudio (via sounddevice) doesn't load in jasper-doctor,
  which opens no audio device
- jasper.active_speaker's module __getattr__ → jasper-voice reaches
  volume_latch without the commissioning stack behind its siblings
  (95 fewer modules; -7 MB on x86_64)

A regression in any of these would silently re-inflate jasper-voice's
RSS by tens of MB. CI catches the import-graph change, not the bytes,
but the import-graph IS the cost on Python.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
_DECLARED_LEAF_DEPENDENCIES = {"httpx", "rapidfuzz", "sounddevice"}

_GUARD_FUNCTION = "ensure_openwakeword_import_safe"

# The scan covers the whole tree and subtracts named exclusions, rather than
# listing roots to include. An include-list is fail-open: a new top-level
# directory that imports openWakeWord is silently unscanned, which is the same
# "nobody noticed" shape this guard exists to prevent.
#
# Each exclusion must still match a real openWakeWord import, checked by
# test_openwakeword_scan_exclusions_are_all_live — a stale exclusion that has
# quietly become a blanket hole fails instead of lingering.
_OPENWAKEWORD_EXCLUDED_DIRS: dict[str, str] = {}
# `tests/` is deliberately NOT excluded. Test modules mention openwakeword only
# as fake sys.modules entries and inside probe source strings, neither of which
# is an import statement — so policing them costs nothing and a test that ever
# does import the real package should be guarded like anything else.

# Directories that hold no first-party Python. Any dot-prefixed component is
# skipped, which matters more here than it looks: `.claude/worktrees/` holds a
# full repo copy per agent session (measured 100,995 of 108,509 candidate files
# on this machine), so walking them would be slow *and* would fail this repo's
# guard on another session's in-progress code. `.venv/` matters for the same
# reason in CI — site-packages ships openWakeWord's own unguarded imports.
def _is_skipped_dir(name: str) -> bool:
    return name.startswith(".") or name in {
        "node_modules", "__pycache__", "site-packages", "build", "dist",
    }

_OPENWAKEWORD_SKIP_REASON = (
    "openwakeword and scikit-learn are not installed in this environment; "
    "the static guard in test_every_openwakeword_import_site_is_guarded "
    "still runs. CI installs both — scikit-learn from the openwakeword-onnx "
    "dependency group, openwakeword itself from the separate `uv pip install "
    "--no-deps openwakeword==0.6.0` step — so these run there."
)


def _run_probe(probe: str) -> dict[str, bool]:
    """Run `probe` in a fresh subprocess; parse `key=true|false` lines.

    cwd and PYTHONPATH are pinned to this checkout on purpose. `python -c`
    puts the *caller's* cwd on `sys.path`, so an unpinned probe resolves
    `jasper` from wherever pytest happened to be invoked — or, failing that,
    from site-packages, which on a machine with several checkouts is a
    different tree. These probes are the only runtime evidence the guard
    works, so a green describing someone else's tree is worse than a failure.
    """
    out = subprocess.check_output(
        [sys.executable, "-c", probe],
        stderr=subprocess.STDOUT,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
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


def _names_openwakeword(module: str) -> bool:
    return module == "openwakeword" or module.startswith("openwakeword.")


def _names_openwakeword_submodule(module: str) -> bool:
    """A *dotted* openwakeword name, i.e. one `find_spec` cannot resolve lazily."""
    return module.startswith("openwakeword.")


def _imports_openwakeword(node: ast.AST) -> bool:
    """True when `node` imports the real openwakeword package.

    Covers the statement forms (`import openwakeword`, `import openwakeword.x
    as y`, `from openwakeword.model import Model`) and the dynamic forms with
    a literal module name (`importlib.import_module("openwakeword")`,
    `__import__("openwakeword")`). A dynamic import built from a computed
    string is not detectable here and none exists in-tree.

    `find_spec` is deliberately split. `find_spec("openwakeword")` locates a
    *top-level* name and executes nothing, so it stays legal — jasper/web/
    wake_setup.py relies on that to keep openWakeWord out of the page-render
    path. `find_spec("openwakeword.model")` is a different operation: to find
    a submodule the import system must import the parent package first (a
    documented `importlib.util.find_spec` behaviour, measured here), which
    pulls scikit-learn in exactly the way this guard exists to prevent. Only
    the dotted form counts as an import.
    """
    if isinstance(node, ast.Import):
        return any(_names_openwakeword(alias.name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.level:  # a relative import can never reach openwakeword
            return False
        return _names_openwakeword(node.module or "")
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            name: str | None = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            name = None
        matches: Callable[[str], bool]
        if name in {"import_module", "__import__"}:
            matches = _names_openwakeword
        elif name == "find_spec":
            matches = _names_openwakeword_submodule
        else:
            return False
        return any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and matches(arg.value)
            for arg in node.args
        )
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

    def walk(nodes: Iterable[ast.AST]) -> None:
        for node in nodes:
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


def _scanned_python_files(root: Path, *, excluded: bool = False) -> list[Path]:
    """First-party .py files, either the policed set or the excluded set."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place rather than filtering afterwards: rglob would still
        # descend into every agent worktree (~100k files) before discarding it.
        dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]
        here = Path(dirpath)
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            rel = (here / filename).relative_to(root).parts
            if (rel[0] in _OPENWAKEWORD_EXCLUDED_DIRS) is excluded:
                out.append(here / filename)
    return sorted(out)


def _sites_in(root: Path, paths: list[Path]) -> list[tuple[str, int, bool]]:
    sites: list[tuple[str, int, bool]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        # Substring prefilter before the parse. Sound, not a heuristic: every
        # form _imports_openwakeword recognises spells the package out as a
        # literal — a dotted/plain module name in an Import/ImportFrom, or a
        # string constant handed to import_module/__import__/find_spec — so a
        # file without the substring cannot contain a site. Verified by
        # differential run, not just argued: identical output, 8 sites.
        #
        # It matters because ast.parse dominates this scan and only 31 of 1,435
        # files mention openwakeword at all, so ~98% of the parsing was wasted.
        # Measured ~30x here; absolute seconds are deliberately not quoted,
        # because on a shared dev box the same unfiltered scan measured 5.3s
        # and 16.4s an hour apart. The ratio and the file counts are the stable
        # facts. This also stops being a fixed cost: the tree only grows, and
        # CI runs this lane on three Python versions.
        if "openwakeword" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        found: list[tuple[int, bool]] = []
        _collect_openwakeword_sites(tree.body, found)
        sites.extend(
            (str(path.relative_to(root)), line, guarded) for line, guarded in found
        )
    return sorted(sites)


def _openwakeword_import_sites(root: Path) -> list[tuple[str, int, bool]]:
    """Every openwakeword import outside the named exclusions, with guard status."""
    return _sites_in(root, _scanned_python_files(root))


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

    assert _openwakeword_import_sites(tmp_path) == [
        ("jasper/guarded.py", 6, True),
        ("jasper/unguarded.py", 2, False),
    ]


def test_openwakeword_scanner_reaches_new_dirs_and_dynamic_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: a brand-new top-level directory is policed, not ignored.

    Also covers the two dynamic forms with a literal module name, and that
    an excluded directory (a synthetic fixture name, independent of
    whatever real entries _OPENWAKEWORD_EXCLUDED_DIRS currently holds) is
    still skipped. An include-list of scan roots would have missed every
    line below.
    """
    monkeypatch.setattr(
        sys.modules[__name__],
        "_OPENWAKEWORD_EXCLUDED_DIRS",
        {"excluded_fixture_dir": "test-only exclusion, not a real one"},
    )
    newdir = tmp_path / "brand_new_service"
    newdir.mkdir()
    (newdir / "runtime.py").write_text(
        "import importlib\n"
        "\n"
        "def a():\n"
        "    return importlib.import_module('openwakeword')\n"
        "\n"
        "def b():\n"
        "    return __import__('openwakeword.model')\n"
        "\n"
        "def c():\n"
        "    import openwakeword.model as m\n"
        "    return m\n"
        "\n"
        "def d():\n"
        # Resolving a submodule imports the parent package, so this IS an
        # import and must be flagged.
        "    return importlib.util.find_spec('openwakeword.model')\n",
        encoding="utf-8",
    )
    excluded = tmp_path / "excluded_fixture_dir"
    excluded.mkdir()
    (excluded / "spike.py").write_text("import openwakeword\n", encoding="utf-8")

    assert _openwakeword_import_sites(tmp_path) == [
        ("brand_new_service/runtime.py", 4, False),
        ("brand_new_service/runtime.py", 7, False),
        ("brand_new_service/runtime.py", 10, False),
        ("brand_new_service/runtime.py", 14, False),
    ]


def test_openwakeword_scanner_leaves_top_level_find_spec_legal(tmp_path: Path) -> None:
    """`find_spec("openwakeword")` executes nothing, so it must not be flagged.

    This is the other half of the `find_spec` boundary and it is load-bearing,
    not symmetry for its own sake: `jasper/web/wake_setup.py` locates bundled
    model assets this way specifically to keep openWakeWord out of the
    socket-activated `jasper-web` render path. If this form were policed, the
    honest fix would be to import openWakeWord there — the opposite of what
    this guard is for.
    """
    pkg = tmp_path / "jasper"
    pkg.mkdir()
    (pkg / "assets.py").write_text(
        "import importlib.util\n"
        "\n"
        "def asset_dir():\n"
        "    spec = importlib.util.find_spec('openwakeword')\n"
        "    return None if spec is None else spec.origin\n",
        encoding="utf-8",
    )

    assert _openwakeword_import_sites(tmp_path) == []


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


def test_openwakeword_scan_exclusions_are_all_live() -> None:
    """Every named exclusion must still be excusing a real import.

    An exclusion that stops matching anything has become a blanket hole in the
    scan: it keeps a whole top-level directory unpoliced while reading like a
    narrow, justified carve-out. Fail so it gets deleted or re-argued.
    """
    excluded_sites = _sites_in(ROOT, _scanned_python_files(ROOT, excluded=True))
    covered = {Path(path).parts[0] for path, _, _ in excluded_sites}

    stale = sorted(set(_OPENWAKEWORD_EXCLUDED_DIRS) - covered)
    assert not stale, (
        f"these openwakeword scan exclusions no longer match any import: {stale}. "
        "Delete the entry so the directory is policed again, or say in the "
        "comment what it is still excusing."
    )


def test_openwakeword_scan_skip_list_hides_no_tracked_file() -> None:
    """The directory skip-list must not hide first-party code from the scan.

    `_is_skipped_dir` drops every dot-directory, and that part is not
    negotiable: `.venv/`'s site-packages ships openWakeWord's own unguarded
    imports, and `.claude/worktrees/` holds a full repo copy per agent session,
    so scanning them would fail this guard on a sibling session's in-progress
    code. But it is a blanket rule, and the fail-closed argument the exclusion
    map above makes is only as strong as the claim that nothing first-party
    sits behind it — the one hole that map cannot check, because the skipped
    directories are *supposed* to contain openwakeword imports.

    Tracked files are exactly the first-party set, so ask git instead of
    assuming. Measured when this landed: 0 of 1,438 tracked .py files.
    """
    # -z, not plain output: git *quotes* a path containing a space or a
    # non-ASCII byte, and splitting that on whitespace would silently yield
    # fragments that match no skipped directory — a file hidden from the scan
    # would then read as not-hidden. NUL-separated output is unambiguous, so
    # this check cannot fail open on an awkward filename.
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "*.py"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        ).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable, so the tracked-file set is unknowable here")

    hidden = sorted(
        path
        for path in tracked
        if path and any(_is_skipped_dir(part) for part in Path(path).parts[:-1])
    )
    # Truncated: a genuine regression is a handful of files, but widening
    # _is_skipped_dir by one wrong name hides hundreds, and an assertion
    # message that dumps them all is unreadable exactly when it matters.
    shown = ", ".join(hidden[:10])
    more = f" (+{len(hidden) - 10} more)" if len(hidden) > 10 else ""
    assert not hidden, (
        f"{len(hidden)} tracked Python file(s) sit in a directory the "
        f"openwakeword scan skips, so they are policed by nothing: {shown}"
        f"{more}. Either move them out, or narrow _is_skipped_dir and add a "
        "named entry to _OPENWAKEWORD_EXCLUDED_DIRS so the exclusion is "
        "argued and checked."
    )


def test_every_openwakeword_import_site_is_guarded() -> None:
    """No openWakeWord import may run without the sklearn guard first.

    openWakeWord's __init__ imports custom_verifier_model, which imports
    scikit-learn; jasper/openwakeword_guard.py owns the measured RSS table.
    jasper-voice, jasper-doctor, and the offline training tools are separate
    processes, so each openWakeWord entry point has to install the guard
    itself — relying on "some other module imported jasper.wake first" is how
    jasper-doctor and a standalone jasper.vad import silently paid the full
    cost.

    Fix a failure by calling `ensure_openwakeword_import_safe()` (from
    jasper/openwakeword_guard.py) as the statement before the import.
    """
    sites = _openwakeword_import_sites(ROOT)

    assert sites, (
        "found no openwakeword imports anywhere in the tree — the scanner "
        "stopped working, so this guard would pass vacuously"
    )

    unguarded = [f"{path}:{line}" for path, line, guarded in sites if not guarded]
    assert not unguarded, (
        "openwakeword is imported without jasper.openwakeword_guard."
        f"{_GUARD_FUNCTION}() running first at: {', '.join(unguarded)}. "
        "That pulls scikit-learn into the process; jasper/openwakeword_guard.py "
        "has the measured cost. Call the guard as the statement immediately "
        "before the import."
    )


def test_run_probe_runs_against_the_checkout_under_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every probe below must import THIS tree, whatever pytest's cwd is.

    `python -c` puts the caller's cwd first on `sys.path`, so an unpinned
    probe imports whichever `jasper` is nearest — the decoy here stands in for
    that. It is not hypothetical: this repo's shared venv resolves `jasper` to
    a different checkout, and the guard's own history is a strict-editable
    finder silently serving the wrong module to a probe.
    """
    decoy = tmp_path / "jasper"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("DECOY = True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = _run_probe(
        "import jasper\n"
        'print(f\'decoy_imported={str(hasattr(jasper, "DECOY")).lower()}\')\n'
    )
    assert result.get("decoy_imported") is False, (
        "_run_probe imported a `jasper` from the caller's cwd instead of the "
        "tree under test, so every probe in this file would report on the "
        "wrong checkout. Pin cwd and PYTHONPATH to ROOT."
    )


def test_importing_wake_has_no_openwakeword_side_effect() -> None:
    """jasper.wake must not touch sys.modules just by being imported.

    The guard is an explicit call at each import site, NOT a module-top
    `sys.modules` write in jasper/wake.py — that shape would protect jasper.vad
    only when jasper-voice happened to import jasper.wake first. So importing
    jasper.wake should install nothing.
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
        "from jasper.cli.doctor.wake import check_openwakeword_model\n"
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


def test_doctor_import_does_not_load_portaudio() -> None:
    """jasper-doctor runs on every install and opens no audio device.

    Importing `sounddevice` loads the PortAudio shared library, so the two
    doctor checks that query devices import it inside their own bodies. Every
    module in the doctor's import graph has to keep that bargain: one
    top-level `import sounddevice` anywhere in it costs the load on every run
    and makes the doctor unimportable on a host without the library.

    `registered_checks()`, not a bare package import: the check modules are
    imported on demand now (ADR-0233 rule 5's `--core` subset), and the full
    scope is what a default `jasper-doctor` run loads.
    """
    probe = (
        "import sys\n"
        "from jasper.cli.doctor import registered_checks\n"
        "registered_checks()\n"
        "assert 'jasper.cli.aec_bridge_config' in sys.modules, (\n"
        "    'probe never reached aec_bridge_config, the module whose "
        "lazy import this pins')\n"
        "print('sounddevice_loaded=' + "
        "str('sounddevice' in sys.modules).lower())\n"
    )
    result = _run_probe(probe)
    assert result.get("sounddevice_loaded") is False, (
        "importing jasper.cli.doctor pulled sounddevice into sys.modules. "
        "Keep the import inside the function that opens or queries a device."
    )


def test_doctor_core_scope_imports_only_its_own_modules() -> None:
    """A `--core` run must not import a check module outside CORE_MODULES —
    the modules it gets no row from are what the subset exists to skip. A
    fresh interpreter, because this one has already imported them."""
    probe = (
        "import sys\n"
        "from jasper.cli.doctor._registry import (\n"
        "    CORE_MODULES, MODULE_ROSTER, registered_checks)\n"
        "registered_checks(core_only=True)\n"
        "extra = {m for m in MODULE_ROSTER\n"
        "         if f'jasper.cli.doctor.{m}' in sys.modules} - set(CORE_MODULES)\n"
        "for name in sorted(extra):\n"
        "    print(f'leaked_{name}=true')\n"
        "print('core_scope_leaked=' + str(bool(extra)).lower())\n"
    )
    result = _run_probe(probe)
    leaked = sorted(k[len("leaked_"):] for k in result if k.startswith("leaked_"))
    assert result.get("core_scope_leaked") is False, (
        f"a --core run imported check modules outside CORE_MODULES: {leaked}. "
        "Something in a core module's import graph reaches them, so the "
        "subset pays for the modules it skips."
    )


@pytest.mark.parametrize(
    ("sys_modules_name", "result_key"),
    [
        pytest.param(
            "google.genai", "genai_loaded",
            id="voice_daemon_import_does_not_load_genai",
        ),
        pytest.param(
            "openai", "openai_loaded",
            id="voice_daemon_import_does_not_load_openai",
        ),
        pytest.param(
            "scipy", "scipy_loaded",
            id="voice_daemon_import_does_not_load_scipy",
        ),
    ],
)
def test_voice_daemon_import_does_not_load_heavy_optional_dependency(
    sys_modules_name: str, result_key: str,
) -> None:
    """Importing jasper.voice_daemon must not eagerly load a heavy
    optional dependency. Provider adapter SDKs (google.genai, openai)
    stay lazy inside _make_connection / _resolve_connect_call so a user
    on a different provider doesn't pay that import cost. scipy stays
    out entirely: it costs ~58 MB RSS, which a 415 MB streambox cannot
    spare, and jasper-voice's MemoryHigh is sized on its absence
    (issue #3697)."""
    probe = (
        "import sys\n"
        "import jasper.voice_daemon  # noqa: F401\n"
        f"loaded = {sys_modules_name!r} in sys.modules\n"
        f"print('{result_key}=' + str(loaded).lower())\n"
    )
    result = _run_probe(probe)
    assert result.get(result_key) is False, (
        f"{sys_modules_name} was loaded into sys.modules just by importing "
        "jasper.voice_daemon. Heavy optional dependencies must stay lazy, "
        "inside the path that actually needs them."
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


@pytest.mark.parametrize(
    ("module_to_import", "modules_that_must_stay_out"),
    [
        pytest.param(
            "jasper.control.server",
            (
                "dbus_next",
                "jasper.active_speaker.baseline_profile",
                "jasper.active_speaker.design_draft",
                "jasper.multiroom.reconcile",
                "numpy",
                "scipy",
                "sounddevice",
            ),
            id="jasper-control",
        ),
        pytest.param(
            "jasper.control.grouping_supervisor",
            ("dbus_next", "jasper.multiroom.reconcile"),
            id="grouping-supervisor",
        ),
        pytest.param("jasper.cli.usb_mic", ("dbus_next",), id="jasper-usbmic"),
        pytest.param(
            "jasper.cli.aec_bridge", ("dbus_next", "scipy", "sounddevice"),
            id="jasper-aec-bridge",
        ),
        pytest.param(
            "jasper.voice_daemon",
            (
                "yaml",
                "jasper.audio_measurement",
                "jasper.active_speaker.baseline_profile",
            ),
            id="jasper-voice",
        ),
    ],
)
def test_resident_daemon_import_leaves_oneshot_subsystems_out(
    module_to_import: str, modules_that_must_stay_out: tuple[str, ...],
) -> None:
    """A resident daemon must not pay import cost for a oneshot subsystem.

    These daemons name ``dbus_next`` only inside ``except`` tuples, and
    ``jasper.multiroom.reconcile`` only behind a bonded-box branch, so both
    belong behind function-local imports. The measurement stack
    (``numpy``/``scipy``/``sounddevice``) belongs to the oneshot commissioners;
    jasper-control reaches their persisted state through stdlib-only record
    modules instead. ``setup_status`` answers a streambox or passive box from
    the topology alone, so the baseline/design candidate stack stays behind
    its active-speaker branch. jasper-voice touches ``jasper.active_speaker`` only for
    the ``volume_latch`` leaf, which its package ``__getattr__`` keeps
    separable from the commissioning submodules. ``scipy`` is the same
    bargain at a much larger price (``jasper.dsp_numpy`` owns that figure):
    the AEC bridge's steady-state resampling and high-pass are
    ``jasper.dsp_numpy``. ``sounddevice`` leaves the bridge's import graph for
    the reason it leaves the doctor's: loading PortAudio is the capture
    threads' cost, paid where they open a device. The smallest supported box
    is a 415 MB Pi Zero 2 W (issue #3697).
    """
    probe = (
        "import sys\n"
        f"import {module_to_import}  # noqa: F401\n"
        f"for name in {tuple(modules_that_must_stay_out)!r}:\n"
        "    hit = any(m == name or m.startswith(name + '.')"
        " for m in sys.modules)\n"
        "    print(name + '=' + str(hit).lower())\n"
    )
    result = _run_probe(probe)
    leaked = [n for n in modules_that_must_stay_out if result.get(n) is not False]
    assert not leaked, (
        f"importing {module_to_import} pulled {', '.join(leaked)} into "
        "sys.modules; a resident daemon pays that RSS for the life of the "
        "process. Keep the import inside the function that needs it."
    )


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
