# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: every adapter-emitted ``FitRefusal`` code is registered vocabulary.

The enclosure adapters build ``FitRefusal(<code>, <detail>)`` with a bare string
literal for the code, while ``BassExtensionRefusal``
(``jasper/bass_extension/profile.py``) is the canonical refusal vocabulary.
Those are two sources of truth for the same codes. This static test links them
so a typo'd or newly-added adapter refusal string that is not a registered
member fails CI instead of drifting silently apart from the enum.

Static (AST) on purpose: it does not run the adapters, so it catches an
unregistered code on any refusal path, not only the ones a fixture happens to
trigger.
"""
from __future__ import annotations

import ast
from pathlib import Path

from jasper.bass_extension.profile import BassExtensionRefusal

ADAPTERS_DIR = (
    Path(__file__).resolve().parents[1] / "jasper" / "bass_extension" / "adapters"
)


def _fit_refusal_codes(path: Path) -> list[tuple[int, object]]:
    """Return ``(lineno, first_arg)`` for every ``FitRefusal(...)`` call.

    ``first_arg`` is the literal string when the code is a constant, otherwise
    the raw AST node (so the test can flag a non-literal code)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    codes: list[tuple[int, object]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FitRefusal"
            and node.args
        ):
            first = node.args[0]
            value = first.value if isinstance(first, ast.Constant) else first
            codes.append((node.lineno, value))
    return codes


def test_adapter_fit_refusal_codes_are_registered_vocabulary() -> None:
    valid = {member.value for member in BassExtensionRefusal}
    emitted: set[str] = set()
    for path in sorted(ADAPTERS_DIR.glob("*.py")):
        for lineno, code in _fit_refusal_codes(path):
            assert isinstance(code, str), (
                f"{path.name}:{lineno} FitRefusal code must be a string literal"
            )
            assert code in valid, (
                f"{path.name}:{lineno} FitRefusal({code!r}) is not a "
                "BassExtensionRefusal member"
            )
            emitted.add(code)
    # Guard against a broken scan silently matching nothing.
    assert emitted, "no adapter FitRefusal codes found — the AST scan is broken"
