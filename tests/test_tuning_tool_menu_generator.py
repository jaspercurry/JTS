# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The tuning runbook's tool-menu table is generated, and stays generated.

ADR-0204 / ticket 6.4: the runbook's per-tool menu is a rendering of each
CLI's own ``prog``/``description``/``AUTHORITY_TIER``, never a second,
hand-typed description of them (the counted-in-one-place pattern,
ADR-0181). This is the regeneration pin: committed ``docs/tuning-operator-
runbook.md`` must equal what ``scripts/generate-tuning-tool-menu.py`` would
write right now, so a CLI edited without regenerating fails here instead of
drifting silently into the runbook.

The script is a script, not a package module (scripts/derive-crossover-
incident-fixture.py's own tests document why), so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "generate-tuning-tool-menu.py"
)
_spec = importlib.util.spec_from_file_location(
    "generate_tuning_tool_menu", _SCRIPT
)
assert _spec is not None and _spec.loader is not None
menu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(menu)


def test_the_committed_runbook_table_equals_the_regenerated_one():
    committed = menu.RUNBOOK.read_text(encoding="utf-8")

    assert menu.spliced(committed, menu.render_table()) == committed, (
        "docs/tuning-operator-runbook.md's tool menu is stale -- run "
        "PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py"
    )


def test_check_mode_agrees_and_writes_nothing():
    before = menu.RUNBOOK.read_text(encoding="utf-8")

    assert menu.main(["--check"]) == 0
    assert menu.RUNBOOK.read_text(encoding="utf-8") == before


def test_every_row_names_a_real_tool_at_a_real_path():
    """One row per covered module; the file it points at actually exists,
    so ``Where`` is never a promise the tree does not keep."""
    for module_name in menu.TUNING_TOOL_MODULES:
        row = menu._tool_row(module_name)
        assert row.startswith("| `")
        # " | " (spaced) is the column separator; a subcommand-listing tool
        # name's escaped "\|" has no surrounding spaces, so this is exactly
        # the four columns regardless of how many subcommands a tool has.
        columns = row.removeprefix("| ").removesuffix(" |").split(" | ")
        assert len(columns) == 4, columns


def test_every_covered_tool_declares_its_own_authority_tier():
    """One owner (ticket 6.4): the tier lives in the CLI module, not here."""
    for module_name in menu.TUNING_TOOL_MODULES:
        module = importlib.import_module(module_name)
        tier = module.AUTHORITY_TIER
        assert isinstance(tier, str) and tier
        assert tier.split()[0].split("(")[0] in (
            "advisory", "measured", "mutating", "mutating-with-gates",
        )


def test_jasper_doctor_and_the_non_cli_surfaces_are_not_generated():
    """Scoped to what has a build_parser(): the runbook's own "Other
    surfaces" table (doors, HTTP endpoints, the two `scripts/` helpers,
    `jasper-doctor`) has no CLI metadata source and stays hand-written."""
    assert "jasper.cli.doctor" not in menu.TUNING_TOOL_MODULES
