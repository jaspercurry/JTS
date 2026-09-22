# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AST guard: every blocking-shaped call site carries a deadline (R23,
#4416).

Scans every ``jasper/`` module for ``subprocess.run``/``check_output``/
``call`` (must carry a ``timeout=`` keyword), ``subprocess.Popen`` whose
return value is captured rather than fired-and-forgotten (its constructor
takes no ``timeout=`` at all, so the bound has to come from a nearby
``.wait(timeout=...)``/``.communicate(timeout=...)`` -- this walk cannot
verify that across statements), and ``asyncio.open_unix_connection``/
``open_connection`` (must sit inside ``asyncio.wait_for(...)`` or
``async with asyncio.timeout(...):``).

A call this walk cannot clear needs a trailing ``# unbounded: <short tag>``
comment on its own call statement, naming why. The marker lives at the call
site instead of a keyed-by-line-number allowlist, so an unrelated line
shift elsewhere in the file can't turn this test red -- git history keeps
the fuller rationale. A marker on a call that turns out to be bounded
(fixed, or the marker drifted) fails just as loud as a missing one.
"""
from __future__ import annotations

import ast
import io
import re
import textwrap
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOT = ROOT / "jasper"

# Functions that block the calling thread/coroutine and (run/check_output/
# call only) accept a `timeout=` keyword directly.
_SUBPROCESS_BLOCKING_FUNCS = frozenset({
    "subprocess.run", "subprocess.check_output", "subprocess.call",
})
_SUBPROCESS_POPEN_FUNC = "subprocess.Popen"
_ASYNCIO_CONNECT_FUNCS = frozenset({
    "asyncio.open_unix_connection", "asyncio.open_connection",
})
_ASYNCIO_TIMEOUT_FUNC = "asyncio.timeout"
_ASYNCIO_WAIT_FOR_FUNC = "asyncio.wait_for"

_MARKER_RE = re.compile(r"^#\s*unbounded:\s*(.+)$")


def _dotted_name(node: ast.expr) -> str | None:
    """``a.b.c`` for an Attribute/Name chain, else ``None``."""
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _markers(source: str) -> dict[int, str]:
    """``{lineno: tag}`` for every ``# unbounded: <tag>`` comment in
    ``source``, keyed by the physical line the comment sits on."""
    markers: dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        match = _MARKER_RE.match(tok.string.strip())
        if match:
            markers[tok.start[0]] = match.group(1).strip()
    return markers


class _BlockingCallVisitor(ast.NodeVisitor):
    """Finds every unbounded call this contract cares about in one module.

    ``_timeout_depth``/``_wait_for_depth`` track whether the call under
    visit is lexically inside an ``asyncio.timeout(...)`` with-block or an
    ``asyncio.wait_for(...)`` argument list -- both count as bounded for
    the asyncio connect functions.
    """

    def __init__(self) -> None:
        self.findings: list[tuple[int, str]] = []
        # Every call this contract tracks, bounded or not: (lineno,
        # end_lineno, dotted_name, is_unbounded) -- lets marker-checking
        # tell a live marker from a stale one without a second AST walk.
        self.calls: list[tuple[int, int, str, bool]] = []
        self._timeout_depth = 0
        self._wait_for_depth = 0
        self._fire_and_forget: set[int] = set()

    def visit_Expr(self, node: ast.Expr) -> None:
        # A bare expression statement discards its call's return value --
        # for Popen specifically, that means the caller never waits on it,
        # so the constructor call cannot block the caller.
        if (
            isinstance(node.value, ast.Call)
            and _dotted_name(node.value.func) == _SUBPROCESS_POPEN_FUNC
        ):
            self._fire_and_forget.add(id(node.value))
        self.generic_visit(node)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        entered = any(
            isinstance(item.context_expr, ast.Call)
            and _dotted_name(item.context_expr.func) == _ASYNCIO_TIMEOUT_FUNC
            for item in node.items
        )
        if entered:
            self._timeout_depth += 1
        self.generic_visit(node)
        if entered:
            self._timeout_depth -= 1

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    def _record(self, node: ast.Call, dotted: str, unbounded: bool) -> None:
        end = node.end_lineno or node.lineno
        self.calls.append((node.lineno, end, dotted, unbounded))
        if unbounded:
            self.findings.append((node.lineno, dotted))

    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted_name(node.func)
        if dotted in _SUBPROCESS_BLOCKING_FUNCS:
            self._record(
                node, dotted,
                not any(kw.arg == "timeout" for kw in node.keywords),
            )
        elif dotted == _SUBPROCESS_POPEN_FUNC:
            self._record(node, dotted, id(node) not in self._fire_and_forget)
        elif dotted in _ASYNCIO_CONNECT_FUNCS:
            self._record(
                node, dotted,
                self._timeout_depth <= 0 and self._wait_for_depth <= 0,
            )

        if dotted == _ASYNCIO_WAIT_FOR_FUNC:
            self._wait_for_depth += 1
            self.generic_visit(node)
            self._wait_for_depth -= 1
            return
        self.generic_visit(node)


def scan_source(source: str) -> list[tuple[int, str]]:
    """Every ``(lineno, dotted_func_name)`` unbounded call in ``source``."""
    visitor = _BlockingCallVisitor()
    visitor.visit(ast.parse(source))
    return visitor.findings


def scan_markers(
    source: str,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """``(missing, stale)``: unbounded calls with no marker, and marked
    calls that are actually bounded."""
    visitor = _BlockingCallVisitor()
    visitor.visit(ast.parse(source))
    markers = _markers(source)
    missing: list[tuple[int, str]] = []
    stale: list[tuple[int, str]] = []
    for lineno, end_lineno, dotted, unbounded in visitor.calls:
        marked = any(line in markers for line in range(lineno, end_lineno + 1))
        if unbounded and not marked:
            missing.append((lineno, dotted))
        elif not unbounded and marked:
            stale.append((lineno, dotted))
    return missing, stale


def scan_tree_markers(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """``scan_markers`` over every ``*.py`` under ``root``, keyed
    ``"relpath:lineno"``."""
    missing: dict[str, str] = {}
    stale: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        found_missing, found_stale = scan_markers(path.read_text(encoding="utf-8"))
        missing.update({f"{rel}:{ln}": dotted for ln, dotted in found_missing})
        stale.update({f"{rel}:{ln}": dotted for ln, dotted in found_stale})
    return missing, stale


def test_every_unbounded_call_is_marked_and_every_marker_is_live() -> None:
    """The guard itself: a new unbounded call fails this until it carries a
    ``# unbounded: ...`` marker, and a stale one (call since bounded) fails
    it too, so markers can't just accumulate."""
    missing, stale = scan_tree_markers(SCAN_ROOT)
    assert not missing, f"unbounded call(s) with no '# unbounded: ...' marker: {missing}"
    assert not stale, f"'# unbounded: ...' marker(s) on now-bounded call(s): {stale}"


def test_scan_flags_a_timeout_less_subprocess_run() -> None:
    """Proof the walk actually detects the fault it exists to catch."""
    findings = scan_source(textwrap.dedent("""
        import subprocess
        def f():
            subprocess.run(["true"], check=False)
    """))
    assert findings == [(4, "subprocess.run")]


def test_scan_flags_an_unwrapped_asyncio_connect() -> None:
    findings = scan_source(textwrap.dedent("""
        import asyncio
        async def f(path):
            return await asyncio.open_unix_connection(path)
    """))
    assert findings == [(4, "asyncio.open_unix_connection")]


def test_scan_flags_a_captured_popen() -> None:
    """A Popen whose handle is kept (not fired-and-forgotten) needs its
    bound demonstrated elsewhere -- the walk cannot verify that, so it
    always flags a captured Popen."""
    findings = scan_source(textwrap.dedent("""
        import subprocess
        def f():
            proc = subprocess.Popen(["true"])
            return proc
    """))
    assert findings == [(4, "subprocess.Popen")]


def test_scan_passes_bounded_calls() -> None:
    """No false positives: a timeout= run, a wait_for-wrapped connect, an
    asyncio.timeout-wrapped connect, and a fire-and-forget Popen all clear."""
    findings = scan_source(textwrap.dedent("""
        import asyncio
        import subprocess

        def f():
            subprocess.run(["true"], timeout=1.0)
            subprocess.Popen(["true"])

        async def g(host, port, path):
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.0,
            )
            async with asyncio.timeout(1.0):
                await asyncio.open_unix_connection(path)
    """))
    assert findings == []


def test_scan_markers_flags_an_unmarked_unbounded_call() -> None:
    missing, stale = scan_markers(textwrap.dedent("""
        import subprocess
        def f():
            subprocess.run(["true"], check=False)
    """))
    assert missing == [(4, "subprocess.run")]
    assert stale == []


def test_scan_markers_flags_a_stale_marker_on_a_now_bounded_call() -> None:
    """A leftover ``# unbounded: ...`` comment on a call that now carries
    ``timeout=`` must fail, not silently pass."""
    missing, stale = scan_markers(textwrap.dedent("""
        import subprocess
        def f():
            subprocess.run(["true"], timeout=1.0)  # unbounded: stale now
    """))
    assert missing == []
    assert stale == [(4, "subprocess.run")]
