# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ratchet: every production writer of a single-writer env file names itself.

AGENTS.md's Map section requires every single-writer ``/var/lib/jasper/*.env``
file to name its writer in a ``# Written by <owner>.`` header. ``write_env_file``,
``locked_update_env_file`` and ``locked_transform_env_file`` all accept an
``owner=`` kwarg for exactly this (see their docstrings in
``jasper/env_file.py`` / ``jasper/atomic_io.py``); ``owner`` stays optional
rather than required because ``locked_upsert_env_file`` — the text-preserving
sibling used by the audio-hardware and fan-in coupling reconcilers — has no
such parameter at all (it folds per-key edits onto raw text rather than
rendering a header via ``format_env_text``), and because one call
(``jasper/audio_hardware/reconcile.py`` writing ``BASE_ENV_PATH``, i.e.
``/etc/jasper/jasper.env``) targets a genuinely multi-writer file that must
NOT claim single ownership. This test is the AST-based ratchet in place of
that required-kwarg enforcement, in the style of
``test_atomic_io_conventions.py``: every OTHER call to the three header-aware
writers, in production code, must pass ``owner=``.

Deliberately scans ``jasper/`` only, not ``tests/``: test fixtures seed env
files that are not the wizard-owned single-writer kind this invariant is
about, and requiring ``owner=`` there would be churn with no reader.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_JASPER = _REPO / "jasper"

_OWNER_AWARE_WRITERS = {
    "write_env_file",
    "locked_update_env_file",
    "locked_transform_env_file",
}

# Every current call site already passes owner=. Add here ONLY with a
# documented reason (e.g. a second genuinely multi-writer file discovered
# later) — this is a ratchet, not a place to silence a real gap.
_ALLOWLIST: set[str] = set()


def _missing_owner_calls(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name not in _OWNER_AWARE_WRITERS:
            continue
        if any(kw.arg == "owner" for kw in node.keywords):
            continue
        lines.append(node.lineno)
    return lines


def test_every_owner_aware_env_writer_names_itself():
    offenders: dict[str, list[int]] = {}
    for path in sorted(_JASPER.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel in ("jasper/env_file.py", "jasper/atomic_io.py"):
            continue  # the implementations themselves, not callers
        missing = _missing_owner_calls(ast.parse(path.read_text()))
        if missing:
            offenders[rel] = missing

    new = {k: v for k, v in offenders.items() if k not in _ALLOWLIST}
    assert not new, (
        "Env-file writer call(s) missing owner= (AGENTS.md single-writer "
        "header invariant):\n  "
        + "\n  ".join(f"{k}:{v}" for k, v in sorted(new.items()))
    )

    stale = _ALLOWLIST - offenders.keys()
    assert not stale, (
        "Allowlisted module(s) now pass owner= everywhere — remove them so "
        "the ratchet tightens:\n  " + "\n  ".join(sorted(stale))
    )
