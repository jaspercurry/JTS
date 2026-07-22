# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: every adapter-emitted ``FitRefusal`` code is registered vocabulary.

The enclosure adapters build ``FitRefusal(<code>, <detail>)`` for a fit refusal,
while ``BassExtensionRefusal`` (``jasper/bass_extension/profile.py``) is the
canonical refusal vocabulary. Those are two sources of truth for the same codes.
This static test links them so a typo'd or newly-added adapter refusal code that
is not a registered member fails CI instead of drifting apart from the enum.

It is deliberately tolerant of how the code is written — a bare string literal
(today's style) OR a ``BassExtensionRefusal.NAME`` / ``.value`` reference (the
stronger single-source style, should the adapters adopt it) — but strict that
the code must be *statically resolvable*: a non-literal, non-enum code (a bare
variable, an f-string, a computed value) fails, because such a code cannot be
checked here and is exactly where an unregistered string would hide.
"""
from __future__ import annotations

import ast
from pathlib import Path

from jasper.bass_extension.profile import BassExtensionRefusal

ADAPTERS_DIR = (
    Path(__file__).resolve().parents[1] / "jasper" / "bass_extension" / "adapters"
)


def _fit_refusal_code_nodes(path: Path) -> list[tuple[int, ast.AST | None]]:
    """Return ``(lineno, code_node)`` for every ``FitRefusal(...)`` call.

    Matches both ``FitRefusal(...)`` and ``<module>.FitRefusal(...)``, and reads
    the code from the first positional arg or a ``refusal=`` keyword. ``None``
    means the call supplied no discernible code arg (itself a failure)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[tuple[int, ast.AST | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != "FitRefusal":
            continue
        code_node: ast.AST | None = node.args[0] if node.args else None
        if code_node is None:
            code_node = next(
                (kw.value for kw in node.keywords if kw.arg == "refusal"), None
            )
        calls.append((node.lineno, code_node))
    return calls


def _resolve_code(node: ast.AST | None) -> str | None:
    """Resolve a code node to the refusal string it denotes, or ``None`` when it
    is neither a string literal nor a ``BassExtensionRefusal`` member reference."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # `BassExtensionRefusal.NAME` or `BassExtensionRefusal.NAME.value`
    attr = node
    if isinstance(attr, ast.Attribute) and attr.attr == "value":
        attr = attr.value
    if (
        isinstance(attr, ast.Attribute)
        and isinstance(attr.value, ast.Name)
        and attr.value.id == "BassExtensionRefusal"
    ):
        member = BassExtensionRefusal.__members__.get(attr.attr)
        # An unknown member name resolves to a sentinel so the caller's
        # membership check fails with a clear message rather than silently
        # skipping it.
        return member.value if member is not None else f"<unknown:{attr.attr}>"
    return None


def test_adapter_fit_refusal_codes_are_registered_vocabulary() -> None:
    valid = {member.value for member in BassExtensionRefusal}
    emitted: set[str] = set()
    for path in sorted(ADAPTERS_DIR.glob("*.py")):
        for lineno, code_node in _fit_refusal_code_nodes(path):
            resolved = _resolve_code(code_node)
            assert resolved is not None, (
                f"{path.name}:{lineno} FitRefusal code must be a static string "
                "literal or a BassExtensionRefusal member so it can be checked"
            )
            assert resolved in valid, (
                f"{path.name}:{lineno} FitRefusal code {resolved!r} is not a "
                "BassExtensionRefusal member"
            )
            emitted.add(resolved)
    # Guard against a broken scan silently matching nothing.
    assert emitted, "no adapter FitRefusal codes found — the AST scan is broken"
