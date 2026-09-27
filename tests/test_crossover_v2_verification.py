# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import ast
from pathlib import Path


# --------------------------------------------------------------------------- #
# the engine seams have one caller
# --------------------------------------------------------------------------- #


#: Modules allowed to call through ``EngineSeams``. Exactly one: the session
#: the seams belong to. A burn-down list that must never grow — an entry here
#: would be a front end doing engine work.
_ENGINE_SEAM_CALLERS = frozenset({"jasper/active_speaker/crossover_v2/session.py"})


def _seam_reach_through_sites() -> list[str]:
    """Every ``<x>.seams.<y>`` attribute access under ``jasper/``."""
    root = Path(__file__).resolve().parents[1]
    found: list[str] = []
    for path in sorted((root / "jasper").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in _ENGINE_SEAM_CALLERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "seams"
            ):
                found.append(f"{relative}:{node.lineno}: .seams.{node.attr}")
    return found


def test_no_front_end_reaches_through_the_engine_seams():
    """Wave 2's enforcement pin, landing now that a front end exists.

    ``EngineSeams`` is public because construction and testing need it, and
    engine-INTERNAL because only ``TuningSession`` may call through it. The
    discipline had nothing to point at until the session was constructed in
    production; it does now.

    The failure this prevents is quiet: a host calling
    ``session.seams.records.bank(...)`` banks a record the session never counts
    in ``banked_record_ids`` — evidence on disk that the session denies taking.
    Nothing raises, and every other assertion stays green.

    A source-text pin under the same exception the import-direction guard above
    already records: an access that does not exist has no behaviour to observe,
    and the property is about the SET of accesses rather than any one call.
    """
    offenders = _seam_reach_through_sites()

    assert not offenders, (
        "a front end reaches through EngineSeams — only TuningSession may. "
        "Drive the four verbs instead:\n  " + "\n  ".join(offenders)
    )


def test_the_seam_reach_through_scan_detects_the_shape_it_guards():
    """Anti-vacuity: a guard that cannot see its subject reports silence.

    The construction under test IS the one the scan walks for, planted in a
    throwaway tree — so a scan narrowed to nothing fails here rather than
    letting the pin above pass forever on an empty sweep.
    """
    tree = ast.parse("session.seams.records.bank(record)\n")
    hits = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "seams"
    ]

    assert [node.attr for node in hits] == ["records"]
