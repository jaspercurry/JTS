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
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

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


def test_the_committed_runbook_tool_menu_equals_the_regenerated_one():
    committed = menu.RUNBOOK.read_text(encoding="utf-8")

    assert menu.spliced(committed, menu.render_table()) == committed, (
        "docs/tuning-operator-runbook.md's generated tool menu is stale -- run "
        "PYTHONPATH=. .venv/bin/python scripts/generate-tuning-tool-menu.py"
    )


def test_the_committed_playbook_bounds_equal_the_regenerated_ones():
    committed = menu.PLAYBOOK.read_text(encoding="utf-8")
    assert menu.render_playbook(committed) == committed


@pytest.mark.parametrize("document", ["RUNBOOK", "PLAYBOOK"])
def test_check_mode_detects_drift_in_either_document_without_writing(
    document, tmp_path, monkeypatch,
):
    for name in ("RUNBOOK", "PLAYBOOK"):
        original = getattr(menu, name)
        local = tmp_path / original.name
        local.write_bytes(original.read_bytes())
        monkeypatch.setattr(menu, name, local)
    before = {path: path.read_bytes() for path in (menu.RUNBOOK, menu.PLAYBOOK)}
    assert menu.main(["--check"]) == 0
    assert {path: path.read_bytes() for path in before} == before
    path = getattr(menu, document)
    marker = menu.BEGIN_MARKER if document == "RUNBOOK" else menu.BOUNDS_BEGIN
    path.write_text(path.read_text().replace(marker, marker + "\nstale"))
    stale = path.read_bytes()
    assert menu.main(["--check"]) == 1
    assert path.read_bytes() == stale
    assert menu.main([]) == 0
    assert {path: path.read_bytes() for path in before} == before


def test_every_rendered_bound_traces_to_its_python_owner():
    sources = {}
    for line in menu.render_bounds().splitlines():
        if " = " in line:
            name, source = line.split(" = ")
            module, _, path = source.partition(":")
            value = importlib.import_module(module)
            for key in path.split(".") if path else ():
                value = getattr(value, key.removesuffix("()"))
                if key.endswith("()"):
                    value = value()
            sources[name] = value
        elif line.startswith("| ") and not line.startswith("| Name |"):
            name, rendered, unit, source = line.strip("| ").split(" | ")
            owner, *keys = source.split(".")
            value = sources[owner]
            for key in keys:
                value = value[key] if isinstance(value, Mapping) else getattr(value, key)
            assert name and unit
            if callable(value):
                assert rendered == f"{value.__name__}(fc_hz)"
            else:
                expected = json.loads(json.dumps(value, ensure_ascii=False).replace("—", "--"))
                assert json.loads(rendered.replace("\\|", "|")) == expected


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
    """Only tools with safe argparse metadata belong in the tool table."""
    assert "jasper.cli.doctor" not in menu.TUNING_TOOL_MODULES
