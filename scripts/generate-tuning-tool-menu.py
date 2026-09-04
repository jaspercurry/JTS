#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Render the tuning runbook's tool-menu table from each CLI's own argparse
metadata, and splice it into docs/tuning-operator-runbook.md between the
generated-content markers.

ADR-0204 / tuning-master-plan.md ticket 6.4: per-tool detail lives in each
CLI's own ``--help``; this table is only the index, one row per tool, so
drift between the runbook and a tool's real prog/description/exit-code
surface is structurally impossible -- the table is a *rendering* of the
CLIs, never a second description of them (the counted-in-one-place pattern,
ADR-0181). ``TUNING_TOOL_MODULES`` below is the roster: exactly the
``[project.scripts]`` entries this runbook's tool menu names, each with its
own ``build_parser()`` and a module-level ``AUTHORITY_TIER`` constant this
script reads rather than re-derives. ``jasper-doctor`` and the non-CLI
surfaces (the four prescription doors, republish/decline, the two
``scripts/`` shell helpers, ``GET :8780/state``) are deliberately absent:
none has a ``build_parser()`` this script can safely import and call without
side effects (``jasper-doctor``'s parser is built inline in ``main()`` and
running that touches the live system; the others are not CLIs at all, so
there is no argparse metadata to render) -- they stay hand-written in the
runbook's own "Other surfaces" table right after the generated one.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py          # write
    PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py --check  # verify; exit 1 on drift
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "tuning-operator-runbook.md"

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED TOOL MENU "
    "(scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->"
)
END_MARKER = "<!-- END GENERATED TOOL MENU -->"

# The tuning tools this table covers: the [project.scripts] entries from
# pyproject.toml that docs/tuning-operator-runbook.md's tool menu names, in
# the happy path's own order. Widening this list is a deliberate edit, not
# something the generator infers -- see the module docstring for who is
# excluded and why.
TUNING_TOOL_MODULES: tuple[str, ...] = (
    "jasper.cli.basic_profile",
    "jasper.cli.seat_level",
    "jasper.cli.angle_capture",
    "jasper.cli.arm_walk",
    "jasper.cli.measure",
    "jasper.cli.crossover_prescriber",
    "jasper.cli.round",
    "jasper.cli.round_views",
    "jasper.cli.project_ring",
    "jasper.cli.classify_features",
    "jasper.cli.delay_sweep",
    "jasper.cli.close_reference",
    "jasper.cli.null_door",
    "jasper.cli.audition",
    "jasper.cli.declare_geometry",
)


def _subcommand_names(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    """Subcommand names, in the order ``add_parser`` added them.

    No public argparse API names this; ``format_usage`` walks the same
    private ``_subparsers`` action to build the ``{a,b,c}`` usage group this
    reads instead of re-parsing.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return tuple(action.choices)
    return ()


def _tool_row(module_name: str) -> str:
    module = importlib.import_module(module_name)
    parser = module.build_parser()
    subcommands = _subcommand_names(parser)
    tool = parser.prog + (" " + "\\|".join(subcommands) if subcommands else "")
    description = " ".join((parser.description or "").split())
    where = Path(module.__file__).resolve().relative_to(ROOT)
    return f"| `{tool}` | {description} | {module.AUTHORITY_TIER} | `{where}` |"


def render_table() -> str:
    header = "| Tool | Does | Authority | Where |\n|---|---|---|---|"
    rows = "\n".join(_tool_row(name) for name in TUNING_TOOL_MODULES)
    return f"{BEGIN_MARKER}\n{header}\n{rows}\n{END_MARKER}"


def spliced(text: str, generated: str) -> str:
    """``text`` with the region between the markers replaced by ``generated``.

    Raises ``ValueError`` (uncaught, by design) if either marker is missing
    or out of order -- a generator that silently no-ops on a moved/deleted
    marker would let the runbook's committed table drift unnoticed, which is
    the exact failure this generator exists to close.
    """
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER, start) + len(END_MARKER)
    return text[:start] + generated + text[end:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true",
        help="verify the committed runbook matches the regenerated table; "
             "write nothing, exit 1 on drift",
    )
    args = parser.parse_args(argv)

    current = RUNBOOK.read_text(encoding="utf-8")
    updated = spliced(current, render_table())

    if args.check:
        if updated != current:
            print(
                f"error: {RUNBOOK} tool menu is stale -- re-run "
                "scripts/generate-tuning-tool-menu.py without --check",
                file=sys.stderr,
            )
            return 1
        return 0

    RUNBOOK.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
