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
verify that across statements, so any captured ``Popen`` needs an allowlist
entry saying where its bound lives), and ``asyncio.open_unix_connection``/
``open_connection`` (must sit inside ``asyncio.wait_for(...)`` or
``async with asyncio.timeout(...):``).

A site this walk cannot clear needs an entry in ALLOWLIST naming why. Remove
an entry once its call site gets a real deadline; add one only for a call
this session found and left open -- never to silence a fresh one without
looking at it.
"""
from __future__ import annotations

import ast
import textwrap
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

# path (relative to ROOT, POSIX separators) : lineno -> why this exact call
# site is not bounded today. Every entry is a call this round's AST walk
# actually found unbounded at HEAD -- re-verify before adding or removing one.
ALLOWLIST: dict[str, str] = {
    "jasper/audio_hardware/reconcile.py:1210": (
        "renders asound.conf via a sourced bash lib with no timeout=; a hang "
        "here is bounded only by the unit's own TimeoutStartSec=50s "
        "(jasper-audio-hardware-reconcile.service). Adding a bare timeout= "
        "would raise TimeoutExpired uncaught past main() (see the OSError "
        "catch three lines below this call for the shape a real fix needs) "
        "-- a design-judgment fix, not a one-line addition."
    ),
    "jasper/audio_hardware/reconcile.py:1244": (
        "same gap as line 1210 (render_asound_conf, no timeout=); the "
        "adjacent OSError catch (rc=127) shows the shape a bounded version "
        "needs, but does not itself bound a hang."
    ),
    "jasper/audio_measurement/correction_lane.py:43": (
        "popen_correction_play returns the Popen to a sync/thread caller, "
        "which owns the wait/timeout (see exec_correction_play just below "
        "for the bounded async analog); Popen's own constructor takes no "
        "timeout= keyword."
    ),
    "jasper/cli/aec_commission.py:644": (
        "wait_reconciler_idle polls `systemctl is-active` inside its own "
        "30s wall-clock deadline, but the individual subprocess.run call is "
        "itself unbounded -- a wedged systemd hangs past that deadline. "
        "Interactive commissioning CLI (an operator is at the terminal)."
    ),
    "jasper/cli/aec_commission.py:781": (
        "recorder = subprocess.Popen(...) is bounded by "
        "recorder.wait(timeout=5) below and the finally block's "
        "terminate()+wait(timeout=2); the constructor call itself takes no "
        "timeout= keyword."
    ),
    "jasper/cli/aec_init.py:899": (
        "interactive commissioning tool; `amixer sset` against a live chip "
        "has no bound today. Operator present at the terminal."
    ),
    "jasper/cli/aec_init.py:904": (
        "interactive commissioning tool; `amixer sget` readback has no "
        "bound today. Operator present at the terminal."
    ),
    "jasper/cli/wake_enroll.py:246": (
        "systemctl(action, unit) restarts jasper-voice (Type=notify) "
        "synchronously; a safe bound has to exceed the unit's own "
        "TimeoutStartSec, not an arbitrary short literal that would "
        "false-fail a legitimate slow start. Interactive commissioning CLI."
    ),
    "jasper/platform/uds.py:48": (
        "_connect's retry loop already bounds itself on retry_budget_sec "
        "wall-clock (default 1.2s); a Unix-domain connect() blocks only on "
        "kernel accept-queue backpressure, not network RTT, so wrapping the "
        "individual attempt adds no protection the loop's own deadline "
        "doesn't already provide. Flagged by R6's review as a candidate "
        "for this allowlist (#4416)."
    ),
}


def _dotted_name(node: ast.expr) -> str | None:
    """``a.b.c`` for an Attribute/Name chain, else ``None``."""
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


class _BlockingCallVisitor(ast.NodeVisitor):
    """Finds every unbounded call this contract cares about in one module.

    ``_timeout_depth``/``_wait_for_depth`` track whether the call under
    visit is lexically inside an ``asyncio.timeout(...)`` with-block or an
    ``asyncio.wait_for(...)`` argument list -- both count as bounded for
    the asyncio connect functions.
    """

    def __init__(self) -> None:
        self.findings: list[tuple[int, str]] = []
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

    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted_name(node.func)
        if dotted in _SUBPROCESS_BLOCKING_FUNCS:
            if not any(kw.arg == "timeout" for kw in node.keywords):
                self.findings.append((node.lineno, dotted))
        elif dotted == _SUBPROCESS_POPEN_FUNC:
            if id(node) not in self._fire_and_forget:
                self.findings.append((node.lineno, dotted))
        elif dotted in _ASYNCIO_CONNECT_FUNCS:
            if self._timeout_depth <= 0 and self._wait_for_depth <= 0:
                self.findings.append((node.lineno, dotted))

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


def scan_tree(root: Path) -> dict[str, str]:
    """``{"relpath:lineno": dotted_func_name}`` for every unbounded call
    under ``root``, keyed relative to this repo's ROOT with POSIX
    separators (matching ALLOWLIST's keys)."""
    findings: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for lineno, dotted in scan_source(path.read_text(encoding="utf-8")):
            findings[f"{rel}:{lineno}"] = dotted
    return findings


def test_every_blocking_call_site_has_a_deadline_or_is_allowlisted() -> None:
    """The guard itself: a new unbounded call added anywhere under jasper/
    fails this until it either carries a real deadline or gets an honest
    ALLOWLIST entry naming why not -- and a stale entry (the call got fixed,
    or moved) fails it too, so the allowlist can't just grow."""
    found = scan_tree(SCAN_ROOT)
    assert set(found) == set(ALLOWLIST), (
        f"missing from ALLOWLIST (newly unbounded): {set(found) - set(ALLOWLIST)}; "
        f"stale ALLOWLIST entries (now bounded or gone): {set(ALLOWLIST) - set(found)}"
    )


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
