# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# architecture — dependency direction and purity
# --------------------------------------------------------------------------


#: The strangler destination, module by module. ``verification.py`` was this
#: pin's original subject; the wave-1 engine modules join it because the
#: direction law binds the whole package and they are its newest members. The
#: two assertions below survive that repointing verbatim.
#:
#: A module that lands in the engine adds its name here, in the same PR. This
#: is a SOURCE-TEXT pin, which the charter otherwise forbids: it reads the
#: module's own import lines rather than its behaviour. It is kept because it
#: is the test-side guard on the zero-upward-imports invariant, and because an
#: import that does not exist has no behaviour to observe.
_STRANGLER_DESTINATION_MODULES = (
    "verification.py",
    "session.py",
    "session_seams.py",
    "playback_transaction.py",
    "measure_spec.py",
)


@pytest.mark.parametrize("module", _STRANGLER_DESTINATION_MODULES)
def test_the_evaluator_imports_no_web_host_and_no_legacy_flow(module: str):
    """#2291's dependency direction: domain modules do not import the host,
    and the strangler destination does not import the monolith it replaces."""

    source = (
        Path(__file__).resolve().parents[1]
        / "jasper"
        / "active_speaker"
        / "crossover_v2"
        / module
    ).read_text()
    imports = [
        line
        for line in source.splitlines()
        if re.match(r"\s*(import|from)\s", line) and "#" not in line.split()[0]
    ]
    joined = "\n".join(imports)
    assert "jasper.web" not in joined
    assert "crossover_v2_flow" not in joined


def test_the_direction_pin_still_names_modules_that_exist():
    """Anti-vacuity. A renamed engine module must fail here rather than quietly
    narrowing the scan to the four that still resolve."""
    package = (
        Path(__file__).resolve().parents[1]
        / "jasper" / "active_speaker" / "crossover_v2"
    )
    missing = [
        name for name in _STRANGLER_DESTINATION_MODULES
        if not (package / name).is_file()
    ]
    assert not missing, missing


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
