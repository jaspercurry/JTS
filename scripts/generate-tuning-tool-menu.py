#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Render the tuning runbook's generated tables from their owning data.

ADR-0204: per-tool detail lives in each
CLI's own ``--help``; this table is only the index, one row per tool, so
drift between the runbook and a tool's real prog/description/exit-code
surface is structurally impossible -- the table is a *rendering* of the
CLIs, never a second description of them (the counted-in-one-place pattern,
ADR-0181). ``TUNING_TOOL_MODULES`` below is the roster: exactly the
``[project.scripts]`` entries this runbook's tool menu names, each with its
own ``build_parser()`` and a module-level ``AUTHORITY_TIER`` constant this
script reads rather than re-derives. ``jasper-doctor`` and non-CLI surfaces
are deliberately absent because they have no safe argparse metadata to
render. The fault table is a rendering of ``REASON_REGISTRY`` and its
household-facing copy.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py          # write
    PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py --check  # verify; exit 1 on drift
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from jasper.active_speaker.crossover_v2.refusal_copy import REASON_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "tuning-operator-runbook.md"

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED TOOL MENU "
    "(scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->"
)
END_MARKER = "<!-- END GENERATED TOOL MENU -->"
FAULT_BEGIN_MARKER = (
    "<!-- BEGIN GENERATED FAULT TABLE "
    "(scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->"
)
FAULT_END_MARKER = "<!-- END GENERATED FAULT TABLE -->"

# The tuning tools this table covers: the [project.scripts] entries from
# pyproject.toml that docs/tuning-operator-runbook.md's tool menu names, in
# the happy path's own order. Widening this list is a deliberate edit, not
# something the generator infers -- see the module docstring for who is
# excluded and why.
TUNING_TOOL_MODULES: tuple[str, ...] = (
    "jasper.cli.basic_profile",
    "jasper.cli.mic_calibration",
    "jasper.cli.seat_level",
    "jasper.cli.angle_capture",
    "jasper.cli.measure",
    "jasper.cli.crossover_prescriber",
    "jasper.cli.round",
    "jasper.cli.round_views",
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


def _cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def render_fault_table() -> str:
    header = "| Code | What happened | What to do next | Screen |\n|---|---|---|---|"
    rows = []
    for code, spec in sorted(REASON_REGISTRY.items()):
        happened = spec.retry_copy.message if spec.retry_copy else spec.message
        if spec.next_action is not None:
            action = spec.next_action["label"]
        elif spec.retry_copy is not None:
            action = spec.retry_copy.retry_action
        else:
            action = ""
        rows.append(
            f"| `{_cell(code)}` | {_cell(happened)} | {_cell(action)} | "
            f"`{_cell(spec.template)}` |"
        )
    body = "\n".join(rows)
    return f"{FAULT_BEGIN_MARKER}\n{header}\n{body}\n{FAULT_END_MARKER}"


def _spliced(text: str, begin: str, end_marker: str, generated: str) -> str:
    """``text`` with the region between the markers replaced by ``generated``.

    Raises ``ValueError`` (uncaught, by design) if either marker is missing
    or out of order -- a generator that silently no-ops on a moved/deleted
    marker would let the runbook's committed table drift unnoticed, which is
    the exact failure this generator exists to close.
    """
    start = text.index(begin)
    end = text.index(end_marker, start) + len(end_marker)
    return text[:start] + generated + text[end:]


def spliced(text: str, generated: str) -> str:
    return _spliced(text, BEGIN_MARKER, END_MARKER, generated)


def render_document(text: str) -> str:
    updated = spliced(text, render_table())
    return _spliced(
        updated,
        FAULT_BEGIN_MARKER,
        FAULT_END_MARKER,
        render_fault_table(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true",
        help="verify the committed runbook matches the regenerated table; "
             "write nothing, exit 1 on drift",
    )
    args = parser.parse_args(argv)

    current = RUNBOOK.read_text(encoding="utf-8")
    updated = render_document(current)

    if args.check:
        if updated != current:
            print(
                f"error: {RUNBOOK} generated content is stale -- re-run "
                "scripts/generate-tuning-tool-menu.py without --check",
                file=sys.stderr,
            )
            return 1
        return 0

    RUNBOOK.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
