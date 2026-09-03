# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static lint-policy guards.

These tests do not replace Ruff. They pin the project-level lint contract
that lets Ruff's `BLE001` suppressions be load-bearing while the existing
suppression debt is paid down over time.
"""
from __future__ import annotations

import ast
import re
import tomllib
from io import StringIO
from pathlib import Path
from tokenize import COMMENT, generate_tokens

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("jasper", "tests", "scripts", "deploy")

# Ratchet ceilings for the tree's lint-suppression debt. Lowering either number
# is welcome; raising one means new suppression debt landed, and the PR that
# raises it owns the argument for why that suppression is the right call.
#
# Two traps, neither derivable from the assertions below:
#   - Never spell either marker literally in this file's prose. MAX_NOQA_MARKERS
#     is a SUBSTRING count over the scanned roots, and this file is one of them,
#     so a comment about the ratchet lands inside the number it describes. Spell
#     it tokenized (B-L-E-0-0-1), the way this sentence does.
#   - MAX_BLE001_MARKERS counts COMMENT TOKENS, not substrings. A bare grep over
#     the same tree answers a different, larger number; the two are not
#     interchangeable, which is why the assertion tokenizes.
#
# Which PR spent which slot, and the argument it made, is in `git log -p` and
# `git blame` on this file.
MAX_NOQA_MARKERS = 817
MAX_BLE001_MARKERS = 618

_BROAD_EXCEPT = re.compile(
    r"^\s*except (?:BaseException|Exception)(?: as [A-Za-z_][A-Za-z0-9_]*)?:"
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        files.extend(sorted(base.rglob("*.py")))
    return files


def test_ruff_ble_rule_is_enabled() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    selected = set(pyproject["tool"]["ruff"]["lint"]["select"])

    assert "BLE" in selected


def test_broad_exception_suppressions_are_explicit() -> None:
    missing: list[str] = []
    for path in _python_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BROAD_EXCEPT.match(line) and "# noqa: BLE001" not in line:
                missing.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")

    assert not missing, (
        "Broad Exception/BaseException handlers must either catch a narrower "
        "exception or carry an explicit `# noqa: BLE001` suppression marker:\n"
        + "\n".join(missing)
    )


def test_noqa_debt_does_not_grow() -> None:
    sources = [path.read_text(encoding="utf-8") for path in _python_files()]
    text = "\n".join(sources)
    ble_markers = sum(
        token.type == COMMENT and token.string.startswith("# noqa: BLE001")
        for source in sources
        for token in generate_tokens(StringIO(source).readline)
    )

    assert text.count("# noqa") <= MAX_NOQA_MARKERS
    assert ble_markers <= MAX_BLE001_MARKERS


# Un-ratcheted line ceilings for the commissioning program's largest files.
# Each is a round number ABOVE the file's size on the day the per-PR line
# ratchet was deleted, so a ceiling fires once on real growth instead of
# taxing one PR in five with a raise-and-justify paragraph.
#
# Do not raise one. The engine refactor only ever moves these files down;
# when every file here is under 5,000 lines, delete the rule.
MAX_LINES_BY_PATH = {
    "jasper/active_speaker/crossover_v2_flow.py": 15_000,
    "jasper/web/correction_crossover_v2.py": 10_000,
    "jasper/audio_measurement/program_analysis.py": 8_000,
    "jasper/active_speaker/crossover_envelope_v2.py": 5_000,
    "jasper/web/correction_crossover_v2_wired.py": 2_000,
    "jasper/audio_measurement/wired_capture.py": 1_000,
    "jasper/active_speaker/crossover_declaration.py": 1_000,
}


def _over_line_cap(path: Path, cap: int) -> str | None:
    """The complaint for one file over its ceiling, or ``None``."""

    count = len(path.read_text(encoding="utf-8").splitlines())
    if count <= cap:
        return None
    return f"{path.name}: {count} lines, ceiling {cap} (+{count - cap})"


def test_the_line_ceiling_reports_a_file_over_it(tmp_path) -> None:
    """The ceiling's own positive control.

    The marker ratchets above have none, and can afford it: they count over a
    tree that always has some markers, so a broken counter reads as zero and
    trivially passes a `<=`. This one compares per file, so a helper that
    returned a too-small count — a changed reader, a file it could not open —
    would report every file comfortably under its ceiling and read exactly like
    a codebase that had stopped growing.
    """

    planted = tmp_path / "_ceiling_probe.py"
    planted.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert _over_line_cap(planted, 3) is None

    # Asserted by MARKER, not by the whole formatted string: the message is
    # diagnostic prose, and a control that breaks when someone improves the
    # wording teaches the next person to loosen the control.
    complaint = _over_line_cap(planted, 2)
    assert complaint is not None
    assert "_ceiling_probe.py" in complaint
    assert "3" in complaint and "2" in complaint


def test_no_commissioning_file_passes_its_line_ceiling() -> None:
    over = [
        complaint
        for rel, cap in sorted(MAX_LINES_BY_PATH.items())
        if (complaint := _over_line_cap(REPO / rel, cap)) is not None
    ]

    assert not over, (
        "A commissioning file passed its one-shot ceiling. Cut a seam and "
        "move work out of it — the ceiling is not raised:\n" + "\n".join(over)
    )


def _unclosed_event_loops(source: str) -> list[str]:
    """Loops in `source` created by `new_event_loop()` that nothing closes.

    Parsed rather than pattern-matched. A text scan of this rule is a trap:
    comments and docstrings naming the anti-pattern read as violations, and
    Python 3.12 splits f-strings into sub-tokens so even a token filter leaks
    prose back in. The AST sees only code.
    """

    def _is_new_loop(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute):
            return func.attr == "new_event_loop"
        return isinstance(func, ast.Name) and func.id == "new_event_loop"

    bound: set[str] = set()
    closed: set[str] = set()
    unbound = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and _is_new_loop(node.value):
            bound.update(
                t.id for t in node.targets if isinstance(t, ast.Name)
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "close" and isinstance(node.func.value, ast.Name):
                closed.add(node.func.value.id)
            if _is_new_loop(node.func.value):
                unbound += 1

    problems = [f"`{name}` is created but never closed" for name in sorted(bound - closed)]
    if unbound:
        problems.append(
            f"{unbound} loop(s) never bound to a name "
            "(nothing can close them — use asyncio.run)"
        )
    return problems


def test_test_event_loops_are_closed_not_just_stopped() -> None:
    """A loop from `new_event_loop()` must be closed, not merely stopped.

    `loop.stop()` ends `run_forever` but releases nothing: the selector
    descriptor and the self-pipe pair stay open until the loop object happens
    to be garbage-collected. A function-scoped fixture that stops without
    closing therefore leaks 3 fds per test — invisible on a dev box (soft
    limit ~1e6), and on a CI runner (soft limit 1024) the casualty would not
    be the leaker but whichever unlucky test next tries to spawn a
    subprocess.

    Measured, not theorised: three fixtures held a monotonically climbing fd
    count until this rule landed. Whether that ever actually exhausted a CI
    runner is NOT established — the `errno=24` lines that made it look that
    way turned out to be injected by two intentional negative tests in
    `tests/test_wifi_guardian_script.py`, and the whole suite's fd high-water
    is ~43. Close loops because leaking them is wrong, not because of a
    specific incident.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if "new_event_loop" not in source:
            continue
        rel = path.relative_to(REPO)
        offenders += [f"{rel}: {problem}" for problem in _unclosed_event_loops(source)]

    assert not offenders, (
        "Event loops created in tests must be closed, not just stopped — "
        "stop() leaves the selector and self-pipe descriptors open until GC. "
        "Let the thread that owns the loop close it: "
        "`def _run(): try: loop.run_forever() finally: loop.close()`, the "
        "shape jasper/control/supervisor_runtime.py already uses. A close() "
        "in fixture teardown is skipped whenever teardown raises first:\n"
        + "\n".join(offenders)
    )


_LEAKY_FIXTURE = """
import asyncio


def loop_thread():
    loop = asyncio.new_event_loop()
    yield loop
    loop.stop()
"""

_CLOSED_FIXTURE = """
import asyncio


def loop_thread():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()
"""

_UNBOUND_LOOP = """
import asyncio

run = lambda coro: asyncio.new_event_loop().run_until_complete(coro)
"""


def test_loop_guard_detects_a_stopped_but_unclosed_loop() -> None:
    """The guard must fail on what it exists to catch. Four earlier versions
    of it were text-based and were fooled by comments, then docstrings, then
    f-string sub-tokens; a fifth passed against the unfixed tree. Pin the
    catching direction so a future simplification cannot go quietly vacuous.
    """
    assert _unclosed_event_loops(_LEAKY_FIXTURE) == [
        "`loop` is created but never closed"
    ]
    assert _unclosed_event_loops(_UNBOUND_LOOP) == [
        "1 loop(s) never bound to a name "
        "(nothing can close them — use asyncio.run)"
    ]


def test_loop_guard_accepts_a_closed_loop_and_ignores_prose() -> None:
    """And it must not cry wolf — including on prose that merely names the
    anti-pattern, which is why it walks the AST rather than the text."""
    assert _unclosed_event_loops(_CLOSED_FIXTURE) == []
    prose = (
        '"""Never write asyncio.new_event_loop().run_until_complete(x)."""\n'
        "# and never leave a new_event_loop() unclosed\n"
        'msg = f"{n} unbound new_event_loop().<call> is a leak"\n'
        "x = 1\n"
    )
    assert _unclosed_event_loops(prose) == []
