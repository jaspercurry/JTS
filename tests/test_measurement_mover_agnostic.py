# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""MS-17's structural pin: the measurement engine does not know who moved the mic.

MS-17 (ADR-0228) states the invariant and names this test as the thing that
makes it operable rather than aspirational: the engine
below the front-end seam imports nothing from the arm tooling and nothing from
the web front end, so a third mover — phone-guided, or whatever comes next — is
added with zero engine edits.

Same family as ``tests/test_correction_boundary_ssot.py``'s
``test_package_boundary_holds``: an AST walk, not a
grep, so a deferred import inside a function body is caught exactly like a
top-level one.

``ENGINE_ROOTS`` is today's engine, not its final shape. The plan's waves move
the analysis layer and the session graph into the engine; each wave that adds a
package adds it here, in the same PR. A root that stops existing fails the
liveness check below rather than silently narrowing the scan.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The measurement engine as it stands. Grows as the engine lands (plan §2 MS-17).
ENGINE_ROOTS: tuple[str, ...] = (
    "jasper/active_speaker/crossover_v2",
    "jasper/audio_measurement",
)

# The front ends. `experiments` is the usb-turntable arm driver's tree;
# `arm_walk` and `angle_capture*` are the arm ladder's Python modules, which
# live under BOTH `jasper/active_speaker/` and `jasper/cli/` — so the match is
# on the module's own name at any position in the dotted path, never on a
# single package prefix.
#
# `angle_capture` is matched by prefix because the tooling is two modules today
# (`angle_capture` and `angle_capture_spool`) and a third would otherwise walk
# straight through the guard.
ARM_MODULE_NAMES: tuple[str, ...] = ("arm_walk",)
ARM_MODULE_PREFIXES: tuple[str, ...] = ("angle_capture",)
FORBIDDEN_HEADS: tuple[tuple[str, ...], ...] = (("experiments",), ("jasper", "web"))

# Liveness: the names above must still name real modules. A rename that emptied
# this vocabulary would leave a green test guarding nothing.
ARM_MODULES_THAT_MUST_EXIST: tuple[str, ...] = (
    "jasper/active_speaker/arm_walk.py",
    "jasper/active_speaker/angle_capture.py",
    "jasper/cli/arm_walk.py",
    "jasper/cli/angle_capture.py",
    "jasper/web/__init__.py",
)

# KNOWN BOUND — imports only. The arm driver itself is reached as a SUBPROCESS
# tool path (`jasper/active_speaker/arm_walk.py`'s `DEFAULT_TOOL_PATH` points at
# `/opt/jasper/experiments/usb-turntable/jts_turntable.py`), so an engine module
# that hard-coded that path would carry an arm dependency this walk cannot see.
# Stated rather than plugged: the seam MS-17 protects is the import graph, and a
# path literal appearing inside the engine is a review finding, not a new axis.


def _imported_names(path: Path) -> list[tuple[int, str]]:
    """Every module name this file imports, absolute and relative alike.

    Relative imports are resolved against the file's own package so
    ``from ..arm_walk import x`` is compared as ``jasper.active_speaker.arm_walk``
    and cannot hide behind its dots.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(REPO_ROOT).parts[:-1])
    names: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = [node.module or ""]
                # `from pkg import mod` may name a submodule, not an attribute.
                names.extend(
                    (node.lineno, f"{node.module}.{alias.name}")
                    for alias in node.names
                    if node.module
                )
            else:
                anchor = package[: len(package) - node.level + 1]
                base = [".".join(anchor + ([node.module] if node.module else []))]
                names.extend(
                    (node.lineno, ".".join(anchor + [alias.name]))
                    for alias in node.names
                )
            names.extend((node.lineno, item) for item in base)
    return names


def _is_front_end(name: str) -> bool:
    parts = name.split(".")
    if any(tuple(parts[: len(head)]) == head for head in FORBIDDEN_HEADS):
        return True
    return any(
        part in ARM_MODULE_NAMES or part.startswith(ARM_MODULE_PREFIXES)
        for part in parts
    )


def test_the_forbidden_vocabulary_still_names_real_modules():
    """Anti-vacuity: a renamed front end must fail here, not go unguarded."""
    missing = [
        rel for rel in ARM_MODULES_THAT_MUST_EXIST if not (REPO_ROOT / rel).exists()
    ]
    assert not missing, (
        "the mover-agnosticism guard names modules that no longer exist "
        f"{missing} — repoint ARM_MODULE_NAMES/FORBIDDEN_HEADS, do not delete "
        "the guard"
    )


@pytest.mark.parametrize("root", ENGINE_ROOTS)
def test_the_engine_imports_no_mover_and_no_front_end(root: str):
    """MS-17: no analysis, gate, or record semantic may branch on mover identity.

    An import is the first way that branch gets written, and it is the one a
    reviewer misses. If this fails, the engine grew a dependency on WHO placed
    the mic: move the caller above the seam and pass the answer in as
    ``measure(position=...)``.
    """
    engine = REPO_ROOT / root
    assert engine.is_dir(), f"{root} is not a directory — repoint ENGINE_ROOTS"

    modules = sorted(engine.rglob("*.py"))
    assert modules, f"{root} contains no modules — the scan would be vacuous"

    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: imports {name}"
        for path in modules
        for lineno, name in _imported_names(path)
        if _is_front_end(name)
    ]
    assert not offenders, (
        "the measurement engine must import neither the arm tooling nor the web "
        "front end — both movers call the same measure(position=...), and a "
        "third mover is meant to need zero engine edits (plan §2 MS-17):\n"
        + "\n".join(offenders)
    )
